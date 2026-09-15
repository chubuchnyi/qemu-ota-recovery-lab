#!/usr/bin/env python3
"""Run the OTA, integrity, power-cut, rollback, and recovery scenarios."""

from __future__ import annotations

import http.server
import json
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_DIR / "artifacts"
BASE_IMAGE = ARTIFACTS_DIR / "images" / "v1" / "disk.img"
GOOD_BUNDLE = ARTIFACTS_DIR / "update-v2.raucb"
BROKEN_BUNDLE = ARTIFACTS_DIR / "update-vbad.raucb"
TAMPERED_BUNDLE = ARTIFACTS_DIR / "update-tampered.raucb"
INCOMPATIBLE_BUNDLE = ARTIFACTS_DIR / "update-incompatible.raucb"
SERIAL_LOG = ARTIFACTS_DIR / "e2e-serial.log"
RESULTS_FILE = ARTIFACTS_DIR / "e2e-results.json"
RUN_QEMU = PROJECT_DIR / "scripts" / "run-qemu.sh"
CREATE_BUNDLE = PROJECT_DIR / "scripts" / "create-bundle.sh"
BUILD_OUTPUT = PROJECT_DIR / "output" / "buildroot"
HASH_OFFSET_FILE = BUILD_OUTPUT / "images" / "rootfs.hash-offset"
HOST_RAUC = PROJECT_DIR / "output" / "buildroot" / "host" / "bin" / "rauc"
KEYRING = PROJECT_DIR / "keys" / "dev" / "ca.cert.pem"
EXPECTED_COMPATIBLE = "qemu-ota-recovery-lab-x86_64"
WRONG_COMPATIBLE = "qemu-ota-recovery-lab-not-this-machine"
TRUNCATED_PATH = "/update-truncated.raucb"


class LabError(RuntimeError):
    """An end-to-end assertion failed."""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ARTIFACTS_DIR), **kwargs)

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != TRUNCATED_PATH:
            super().do_GET()
            return

        # Advertise the complete bundle but deliberately close after 1 MiB.
        # libcurl must report a partial transfer and RAUC must leave grubenv
        # unchanged because it has not validated an installable bundle yet.
        size = GOOD_BUNDLE.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with GOOD_BUNDLE.open("rb") as source:
            self.wfile.write(source.read(1024 * 1024))
            self.wfile.flush()
        self.close_connection = True


class SerialVM:
    def __init__(self, state_dir: Path, log_path: Path, revision: str) -> None:
        self.state_dir = state_dir
        self.qmp_socket = state_dir / "qmp.sock"
        self.log_file = log_path.open("wb")
        self.process: subprocess.Popen[bytes] | None = None
        self.buffer = bytearray()
        self.cursor = 0
        self.command_number = 0
        self.log_file.write(f"OTA_LAB_TEST_COMMIT={revision}\n".encode("ascii"))
        self.log_file.flush()

    def start(self, *, fresh: bool) -> None:
        args = [str(RUN_QEMU)]
        if fresh:
            args.append("--fresh")
        args.append(str(BASE_IMAGE))
        env = os.environ.copy()
        env["OTA_LAB_STATE_DIR"] = str(self.state_dir)
        self.log_file.write(b"\n===== QEMU START =====\n")
        self.log_file.flush()
        self.buffer = bytearray()
        self.cursor = 0
        self.process = subprocess.Popen(
            args,
            cwd=PROJECT_DIR,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )

    def _tail(self) -> str:
        return bytes(self.buffer[-8000:]).decode("utf-8", errors="replace")

    def wait_for(
        self, pattern: bytes | re.Pattern[bytes], timeout: float
    ) -> re.Match[bytes]:
        if isinstance(pattern, bytes):
            regex = re.compile(re.escape(pattern))
        else:
            regex = pattern
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            match = regex.search(self.buffer, self.cursor)
            if match:
                self.cursor = match.end()
                return match

            if self.process is None or self.process.stdout is None:
                raise LabError("QEMU is not running")

            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select(
                [self.process.stdout], [], [], min(0.5, remaining)
            )
            if not readable:
                if self.process.poll() is not None:
                    raise LabError(
                        f"QEMU exited with {self.process.returncode} while waiting for "
                        f"{regex.pattern!r}\n{self._tail()}"
                    )
                continue

            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                continue
            self.buffer.extend(chunk)
            self.log_file.write(chunk)
            self.log_file.flush()

        raise LabError(f"timeout waiting for {regex.pattern!r}\n{self._tail()}")

    def send(self, command: str) -> None:
        if self.process is None or self.process.stdin is None:
            raise LabError("QEMU is not running")
        self.process.stdin.write(command.encode("utf-8"))
        self.process.stdin.flush()

    def login_after(self, marker: str, timeout: float = 60.0) -> None:
        self.wait_for(marker.encode("utf-8"), timeout)
        self.wait_for(b"ota-lab login:", 30.0)
        self.send("root\n")
        self.wait_for(b"# ", 10.0)

    def command(self, command: str, expected: int = 0, timeout: float = 60.0) -> None:
        self.command_number += 1
        marker = f"__OTA_LAB_COMMAND_{self.command_number}_RC="
        self.send(
            f"{command}\n"
            "lab_command_rc=$?\n"
            f"printf '{marker}%s__\\n' \"$lab_command_rc\"\n"
        )
        match = self.wait_for(
            re.compile(re.escape(marker.encode("ascii")) + rb"([0-9]+)__"),
            timeout,
        )
        actual = int(match.group(1))
        if actual != expected:
            raise LabError(
                f"guest command returned {actual}, expected {expected}: {command}\n"
                f"{self._tail()}"
            )

    def qmp_quit(self) -> None:
        deadline = time.monotonic() + 10.0
        while not self.qmp_socket.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.qmp_socket.exists():
            raise LabError(f"QMP socket did not appear: {self.qmp_socket}")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(str(self.qmp_socket))
            stream = client.makefile("rwb", buffering=0)
            greeting = stream.readline()
            if b'"QMP"' not in greeting:
                raise LabError(f"unexpected QMP greeting: {greeting!r}")
            stream.write(b'{"execute":"qmp_capabilities"}\r\n')
            while b'"return"' not in stream.readline():
                pass
            stream.write(b'{"execute":"quit"}\r\n')

        if self.process is not None:
            self.process.wait(timeout=10.0)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.send("\x01x")
                self.process.wait(timeout=10.0)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                self.process.terminate()
                self.process.wait(timeout=10.0)
        self.log_file.close()


def require_artifacts() -> None:
    required = [
        BASE_IMAGE,
        GOOD_BUNDLE,
        BROKEN_BUNDLE,
        CREATE_BUNDLE,
        BUILD_OUTPUT / "images" / "rootfs.ext4",
        HASH_OFFSET_FILE,
        HOST_RAUC,
        KEYRING,
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise LabError("missing build artifacts:\n  " + "\n  ".join(missing))
    if shutil.which("qemu-io") is None:
        raise LabError("qemu-io is required for the offline disk-corruption test")


def gpt_partition_offset(image: Path, partition_number: int) -> int:
    """Return a GPT partition's byte offset from a raw disk image."""
    sector_size = 512
    with image.open("rb") as disk:
        disk.seek(sector_size)
        header = disk.read(92)
        if len(header) != 92 or header[:8] != b"EFI PART":
            raise LabError(f"invalid GPT header in {image}")

        entries_lba = struct.unpack_from("<Q", header, 72)[0]
        entry_count = struct.unpack_from("<I", header, 80)[0]
        entry_size = struct.unpack_from("<I", header, 84)[0]
        if not 1 <= partition_number <= entry_count or entry_size < 128:
            raise LabError(f"invalid GPT partition number: {partition_number}")

        entry_offset = (
            entries_lba * sector_size + (partition_number - 1) * entry_size
        )
        disk.seek(entry_offset)
        entry = disk.read(entry_size)
        if len(entry) != entry_size or entry[:16] == bytes(16):
            raise LabError(f"missing GPT partition {partition_number} in {image}")
        first_lba = struct.unpack_from("<Q", entry, 32)[0]
        if first_lba == 0:
            raise LabError(f"invalid first LBA for GPT partition {partition_number}")
        return first_lba * sector_size


def corrupt_overlay(vm: SerialVM, slot: str) -> int:
    """Overwrite one hash-tree block inside a stopped slot's qcow2 overlay."""
    partition_number = {"A": 2, "B": 3}[slot]
    partition_offset = gpt_partition_offset(BASE_IMAGE, partition_number)
    try:
        hash_offset = int(HASH_OFFSET_FILE.read_text(encoding="ascii").strip())
    except ValueError as error:
        raise LabError(f"invalid verity hash offset in {HASH_OFFSET_FILE}") from error
    # Keep ext4 intact so GRUB can load the slot's kernel.  veritysetup writes
    # its 4 KiB superblock at hash_offset and the Merkle tree immediately after
    # it; changing the first tree block is detected on the first rootfs reads.
    corruption_offset = partition_offset + hash_offset + 4096
    overlay = vm.state_dir / "disk.qcow2"
    command = [
        "qemu-io",
        "-f",
        "qcow2",
        "-c",
        f"write -P 0xa5 {corruption_offset} 4096",
        str(overlay),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    vm.log_file.write(
        (
            "\n===== HOST FAULT INJECTION =====\n"
            f"OTA_LAB_TEST_CORRUPTION slot={slot} region=verity-hash-tree "
            f"offset={corruption_offset} length=4096 pattern=0xa5\n"
        ).encode("ascii")
    )
    vm.log_file.write(completed.stdout)
    vm.log_file.flush()
    if completed.returncode != 0:
        raise LabError(
            "qemu-io corruption failed:\n"
            + completed.stdout.decode("utf-8", errors="replace")
        )
    return corruption_offset


def rauc_info(bundle: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(HOST_RAUC), "info", "--keyring", str(KEYRING), str(bundle)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def prepare_fault_bundles() -> None:
    shutil.copyfile(GOOD_BUNDLE, TAMPERED_BUNDLE)
    with TAMPERED_BUNDLE.open("r+b") as bundle:
        bundle.seek(-32, os.SEEK_END)
        original = bundle.read(1)
        if not original:
            raise LabError("could not read bundle byte for corruption")
        bundle.seek(-1, os.SEEK_CUR)
        bundle.write(bytes([original[0] ^ 0x80]))

    created = subprocess.run(
        [
            str(CREATE_BUNDLE),
            str(BUILD_OUTPUT),
            "v-incompatible",
            str(INCOMPATIBLE_BUNDLE),
            WRONG_COMPATIBLE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if created.returncode != 0:
        raise LabError(
            "could not create incompatible bundle:\n"
            + created.stdout.decode("utf-8", errors="replace")
        )

    good = rauc_info(GOOD_BUNDLE)
    tampered = rauc_info(TAMPERED_BUNDLE)
    incompatible = rauc_info(INCOMPATIBLE_BUNDLE)
    if (
        good.returncode != 0
        or EXPECTED_COMPATIBLE.encode() not in good.stdout
        or tampered.returncode == 0
    ):
        raise LabError("host-side RAUC signature preflight did not behave as expected")
    if (
        incompatible.returncode != 0
        or WRONG_COMPATIBLE.encode() not in incompatible.stdout
    ):
        raise LabError("could not create a valid bundle for a different compatible")


def project_revision() -> str:
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_DIR, text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=PROJECT_DIR, text=True
    ).strip()
    return f"{revision}-dirty" if dirty else revision


def assert_pristine_boot_state(vm: SerialVM) -> None:
    for expected in ("ORDER=A B R", "A_OK=1", "B_OK=0", "A_TRY=0", "B_TRY=0"):
        vm.command(
            "grub-editenv /boot/EFI/BOOT/grubenv list | "
            f"grep -qx '{expected}'"
        )


def serve_artifacts() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_port


def run() -> None:
    require_artifacts()
    revision = project_revision()
    prepare_fault_bundles()
    server, port = serve_artifacts()
    state_dir = Path(tempfile.mkdtemp(prefix="qemu-ota-lab-"))
    vm = SerialVM(state_dir, SERIAL_LOG, revision)
    url = f"http://10.0.2.2:{port}"

    try:
        print(f"implementation commit: {revision}", flush=True)
        print("[1/11] boot pristine v1 in slot A through dm-verity", flush=True)
        vm.start(fresh=True)
        vm.login_after("OTA_LAB_BOOT_OK slot=A version=v1")
        vm.command("test \"$(cat /etc/ota-version)\" = v1")
        vm.command("veritysetup status ota-root >/dev/null")
        vm.command(
            "test \"$(awk '$2 == \"/\" {print $1}' /proc/mounts)\" = "
            "/dev/mapper/ota-root"
        )
        vm.command(
            "awk '$2 == \"/\" {print $4}' /proc/mounts | "
            "tr , '\\n' | grep -qx ro"
        )
        vm.command("touch /etc/ota-lab-write-test", expected=1)
        vm.command("rauc status >/dev/null")
        vm.command("echo survives-all-reboots > /data/e2e-marker")

        print("[2/11] refuse recovery tooling from a normal slot", flush=True)
        vm.command("ota-recovery status", expected=2)

        print("[3/11] reject a bundle with a damaged signature", flush=True)
        vm.command(f"rauc install {url}/{TAMPERED_BUNDLE.name}", expected=1)
        assert_pristine_boot_state(vm)

        print("[4/11] survive a truncated HTTP response", flush=True)
        vm.command(f"rauc install {url}{TRUNCATED_PATH}", expected=1)
        assert_pristine_boot_state(vm)

        print("[5/11] reject a signed bundle for another machine", flush=True)
        vm.command(f"rauc install {url}/{INCOMPATIBLE_BUNDLE.name}", expected=1)
        assert_pristine_boot_state(vm)

        print("[6/11] install signed v2 into B and boot it", flush=True)
        vm.command(f"rauc install {url}/{GOOD_BUNDLE.name}", timeout=90.0)
        vm.send("reboot\n")
        vm.login_after("OTA_LAB_BOOT_OK slot=B version=v2")
        vm.command("test \"$(cat /etc/ota-version)\" = v2")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")

        print("[7/11] cut QEMU power while writing inactive slot A", flush=True)
        vm.send(f"rauc install {url}/{BROKEN_BUNDLE.name}\n")
        vm.wait_for(b"Copying image to rootfs.0", 90.0)
        vm.qmp_quit()
        vm.start(fresh=False)
        vm.login_after("OTA_LAB_BOOT_OK slot=B version=v2")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")

        print("[8/11] install bad userspace and verify automatic rollback", flush=True)
        vm.command(f"rauc install {url}/{BROKEN_BUNDLE.name}", timeout=90.0)
        vm.send("reboot\n")
        vm.wait_for(b"OTA_LAB_HEALTH_FAILED slot=A version=vbad", 60.0)
        vm.login_after("OTA_LAB_BOOT_OK slot=B version=v2", timeout=90.0)
        vm.command("test \"$(cat /etc/ota-version)\" = v2")

        print("[9/11] inspect failed slots from RAM-only recovery", flush=True)
        vm.command("rauc status mark-bad booted")
        vm.command("rauc status mark-bad other")
        vm.send("reboot\n")
        vm.login_after("OTA_LAB_RECOVERY_READY version=v1")
        vm.command("test \"$(awk '$2 == \"/\" {print $3}' /proc/mounts)\" = rootfs")
        vm.command("grep -q ' /dev/pts ' /proc/mounts")
        vm.command("grep -q ' /boot ' /proc/mounts && grep -q ' /data ' /proc/mounts")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")
        vm.command("rauc status >/dev/null")
        vm.command(
            "sha256sum /dev/vda2 /dev/vda3 > /tmp/recovery-slot-hashes"
        )
        vm.command(
            "ota-recovery status > /tmp/ota-recovery-status && "
            "cat /tmp/ota-recovery-status && "
            "grep -q '^OTA_RECOVERY_CONTEXT mode=ram root_type=rootfs$' "
            "/tmp/ota-recovery-status && "
            "grep -q '^OTA_RECOVERY_SLOT name=A device=/dev/vda2 version=vbad$' "
            "/tmp/ota-recovery-status && "
            "grep -q '^OTA_RECOVERY_SLOT name=B device=/dev/vda3 version=v2$' "
            "/tmp/ota-recovery-status && "
            "grep -q '^OTA_RECOVERY_DATA device=/dev/vda4 mounted=yes$' "
            "/tmp/ota-recovery-status"
        )
        vm.command(
            "sha256sum /dev/vda2 /dev/vda3 | "
            "cmp - /tmp/recovery-slot-hashes"
        )
        vm.command(
            f"ota-recovery install {url}/{GOOD_BUNDLE.name}", expected=2
        )

        print("[10/11] reinstall through guarded recovery tooling", flush=True)
        vm.command(
            f"ota-recovery install {url}/{GOOD_BUNDLE.name} --confirm",
            timeout=120.0,
        )
        vm.send("reboot\n")
        restored = vm.wait_for(
            re.compile(rb"OTA_LAB_BOOT_OK slot=([AB]) version=v2"), 90.0
        )
        vm.wait_for(b"ota-lab login:", 30.0)
        vm.send("root\n")
        vm.wait_for(b"# ", 10.0)
        vm.command("test \"$(cat /etc/ota-version)\" = v2")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")
        restored_slot = restored.group(1).decode("ascii")
        print(f"      recovery restored slot {restored_slot}", flush=True)

        print("[11/11] corrupt an active slot and fall back via dm-verity", flush=True)
        corrupted_slot = "B" if restored_slot == "A" else "A"
        vm.command(f"rauc install {url}/{GOOD_BUNDLE.name}", timeout=90.0)
        vm.send("reboot\n")
        vm.login_after(
            f"OTA_LAB_BOOT_OK slot={corrupted_slot} version=v2", timeout=90.0
        )
        vm.command("veritysetup status ota-root >/dev/null")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")
        vm.qmp_quit()
        corruption_offset = corrupt_overlay(vm, corrupted_slot)
        vm.start(fresh=False)
        vm.wait_for(
            re.compile(rb"device-mapper: verity:.*corrupt", re.IGNORECASE),
            60.0,
        )
        vm.login_after(
            f"OTA_LAB_BOOT_OK slot={restored_slot} version=v2", timeout=90.0
        )
        vm.command("veritysetup status ota-root >/dev/null")
        vm.command("test \"$(cat /etc/ota-version)\" = v2")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")
        print(
            f"      corrupted slot {corrupted_slot} at byte {corruption_offset}; "
            f"fell back to {restored_slot}",
            flush=True,
        )
    finally:
        vm.stop()
        server.shutdown()
        server.server_close()
        shutil.rmtree(state_dir, ignore_errors=True)

    result = {
        "commit": revision,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "result": "PASS",
        "restored_slot": restored_slot,
        "corrupted_slot": corrupted_slot,
        "corruption_offset": corruption_offset,
        "fallback_slot": restored_slot,
        "scenarios": [
            "pristine-slot-a-boot",
            "recovery-command-refused-outside-recovery",
            "tampered-signature-rejected",
            "truncated-http-rejected",
            "incompatible-bundle-rejected",
            "signed-update-to-slot-b",
            "qmp-power-cut-during-slot-write",
            "failed-health-check-rollback",
            "ram-only-recovery-inspection",
            "guarded-reinstall-from-recovery",
            "dm-verity-corruption-fallback",
        ],
    }
    RESULTS_FILE.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: all OTA/recovery invariants held; serial log: {SERIAL_LOG}", flush=True)
    print(f"machine-readable result: {RESULTS_FILE}", flush=True)


if __name__ == "__main__":
    try:
        run()
    except (LabError, OSError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
