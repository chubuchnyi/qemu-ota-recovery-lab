#!/usr/bin/env python3
"""Run the OTA, power-cut, rollback, and recovery scenarios over QEMU serial."""

from __future__ import annotations

import http.server
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_DIR / "artifacts"
BASE_IMAGE = ARTIFACTS_DIR / "images" / "v1" / "disk.img"
GOOD_BUNDLE = ARTIFACTS_DIR / "update-v2.raucb"
BROKEN_BUNDLE = ARTIFACTS_DIR / "update-vbad.raucb"
TAMPERED_BUNDLE = ARTIFACTS_DIR / "update-tampered.raucb"
SERIAL_LOG = ARTIFACTS_DIR / "e2e-serial.log"
RUN_QEMU = PROJECT_DIR / "scripts" / "run-qemu.sh"
HOST_RAUC = PROJECT_DIR / "output" / "buildroot" / "host" / "bin" / "rauc"
KEYRING = PROJECT_DIR / "keys" / "dev" / "ca.cert.pem"


class LabError(RuntimeError):
    """An end-to-end assertion failed."""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ARTIFACTS_DIR), **kwargs)

    def log_message(self, _format: str, *args: object) -> None:
        return


class SerialVM:
    def __init__(self, state_dir: Path, log_path: Path) -> None:
        self.state_dir = state_dir
        self.qmp_socket = state_dir / "qmp.sock"
        self.log_file = log_path.open("wb")
        self.process: subprocess.Popen[bytes] | None = None
        self.buffer = bytearray()
        self.cursor = 0
        self.command_number = 0

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

    def wait_for(self, pattern: bytes | re.Pattern[bytes], timeout: float) -> re.Match[bytes]:
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
    required = [BASE_IMAGE, GOOD_BUNDLE, BROKEN_BUNDLE, HOST_RAUC, KEYRING]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise LabError("missing build artifacts:\n  " + "\n  ".join(missing))


def create_tampered_bundle() -> None:
    shutil.copyfile(GOOD_BUNDLE, TAMPERED_BUNDLE)
    with TAMPERED_BUNDLE.open("r+b") as bundle:
        bundle.seek(-32, os.SEEK_END)
        original = bundle.read(1)
        if not original:
            raise LabError("could not read bundle byte for corruption")
        bundle.seek(-1, os.SEEK_CUR)
        bundle.write(bytes([original[0] ^ 0x80]))

    good = subprocess.run(
        [str(HOST_RAUC), "info", "--keyring", str(KEYRING), str(GOOD_BUNDLE)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    bad = subprocess.run(
        [str(HOST_RAUC), "info", "--keyring", str(KEYRING), str(TAMPERED_BUNDLE)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if good.returncode != 0 or bad.returncode == 0:
        raise LabError("host-side RAUC signature preflight did not behave as expected")


def serve_artifacts() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_port


def run() -> None:
    require_artifacts()
    create_tampered_bundle()
    server, port = serve_artifacts()
    state_dir = Path(tempfile.mkdtemp(prefix="qemu-ota-lab-"))
    vm = SerialVM(state_dir, SERIAL_LOG)
    url = f"http://10.0.2.2:{port}"

    try:
        print("[1/7] boot pristine v1 in slot A")
        vm.start(fresh=True)
        vm.login_after("OTA_LAB_BOOT_OK slot=A version=v1")
        vm.command("test \"$(cat /etc/ota-version)\" = v1")
        vm.command("rauc status >/dev/null")
        vm.command("echo survives-all-reboots > /data/e2e-marker")

        print("[2/7] reject a bundle with a damaged signature")
        vm.command(f"rauc install {url}/{TAMPERED_BUNDLE.name}", expected=1)
        vm.command(
            "grub-editenv /boot/EFI/BOOT/grubenv list | "
            "grep -qx 'ORDER=A B R'"
        )
        vm.command(
            "grub-editenv /boot/EFI/BOOT/grubenv list | "
            "grep -qx 'B_OK=0'"
        )

        print("[3/7] install signed v2 into B and boot it")
        vm.command(f"rauc install {url}/{GOOD_BUNDLE.name}", timeout=90.0)
        vm.send("reboot\n")
        vm.login_after("OTA_LAB_BOOT_OK slot=B version=v2")
        vm.command("test \"$(cat /etc/ota-version)\" = v2")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")

        print("[4/7] cut QEMU power while writing inactive slot A")
        vm.send(f"rauc install {url}/{BROKEN_BUNDLE.name}\n")
        vm.wait_for(b"Copying image to rootfs.0", 90.0)
        vm.qmp_quit()
        vm.start(fresh=False)
        vm.login_after("OTA_LAB_BOOT_OK slot=B version=v2")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")

        print("[5/7] install bad userspace and verify automatic rollback")
        vm.command(f"rauc install {url}/{BROKEN_BUNDLE.name}", timeout=90.0)
        vm.send("reboot\n")
        vm.wait_for(b"OTA_LAB_HEALTH_FAILED slot=A version=vbad", 60.0)
        vm.login_after("OTA_LAB_BOOT_OK slot=B version=v2", timeout=90.0)
        vm.command("test \"$(cat /etc/ota-version)\" = v2")

        print("[6/7] mark A and B bad and boot RAM-only recovery")
        vm.command("rauc status mark-bad booted")
        vm.command("rauc status mark-bad other")
        vm.send("reboot\n")
        vm.login_after("OTA_LAB_RECOVERY_READY version=v1")
        vm.command("test \"$(awk '$2 == \"/\" {print $3}' /proc/mounts)\" = rootfs")
        vm.command("grep -q ' /dev/pts ' /proc/mounts")
        vm.command("grep -q ' /boot ' /proc/mounts && grep -q ' /data ' /proc/mounts")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")
        vm.command("rauc status >/dev/null")

        print("[7/7] reinstall a signed system from recovery and boot it")
        vm.command(f"rauc install {url}/{GOOD_BUNDLE.name}", timeout=90.0)
        vm.send("reboot\n")
        restored = vm.wait_for(
            re.compile(rb"OTA_LAB_BOOT_OK slot=([AB]) version=v2"), 90.0
        )
        vm.wait_for(b"ota-lab login:", 30.0)
        vm.send("root\n")
        vm.wait_for(b"# ", 10.0)
        vm.command("test \"$(cat /etc/ota-version)\" = v2")
        vm.command("test \"$(cat /data/e2e-marker)\" = survives-all-reboots")
        print(f"      recovery restored slot {restored.group(1).decode('ascii')}")
    finally:
        vm.stop()
        server.shutdown()
        server.server_close()
        shutil.rmtree(state_dir, ignore_errors=True)

    print(f"PASS: all OTA/recovery invariants held; serial log: {SERIAL_LOG}")


if __name__ == "__main__":
    try:
        run()
    except (LabError, OSError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
