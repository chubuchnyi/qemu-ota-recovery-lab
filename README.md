# QEMU OTA/recovery lab

A small, reproducible lab for learning how an embedded Linux system installs a
signed OTA update, chooses an A/B slot, rolls back a failed boot, and enters a
RAM-only recovery environment. It intentionally uses generic x86_64 QEMU/KVM
instead of emulating a specific board.

## What the first milestone models

- GPT disk with a persistent EFI/GRUB partition, rootfs A, rootfs B, and data.
- The same kernel and userland boot either from an ext4 A/B slot or as a
  recovery initramfs entirely from RAM.
- RAUC installs X.509-signed `verity` bundles only into the inactive rootfs.
- GRUB keeps `ORDER` plus `OK`/`TRY` state for A, B, and recovery in
  `grubenv`.
- A late health check marks a successful boot good.
- A deliberately broken image reboots without marking itself good, so GRUB
  falls back to the previous slot. If neither normal slot is usable, GRUB boots
  recovery.
- RAM recovery provides a guarded `ota-recovery` command for non-writing slot
  inspection and signed reinstall.
- QEMU writes only to a qcow2 overlay; the generated base image stays pristine.

## Quick start

Requirements on the host: Docker, `qemu-system-x86_64`, `qemu-img`, OVMF, and
KVM access. On Ubuntu these are provided by `docker.io`, `qemu-system-x86`,
`qemu-utils`, and `ovmf`.

```console
make build VERSION=v1
make fresh-run VERSION=v1
```

The serial console is also the QEMU monitor. Exit with `Ctrl-a x`. The VM disk
under `run/` persists across restarts.

Build a second version and its signed bundle:

```console
make bundle VERSION=v2
make serve
```

Buildroot uses one incremental workspace under `output/buildroot`; immutable
boot-disk snapshots are published under `artifacts/images/<version>/`. The
first build compiles the toolchain, while later lesson versions reuse it.

Then, in another terminal, boot the VM and install from the guest:

```console
rauc status
rauc install http://10.0.2.2:8000/update-v2.raucb
reboot
```

For the rollback exercise:

```console
make build-broken VERSION=vbad
make serve
```

Install `update-vbad.raucb`. The new slot emits `OTA_LAB_HEALTH_FAILED`, reboots,
and should be skipped by GRUB on the following boot.

The generated private key is deliberately excluded from Git. It is a disposable
development key, not a production PKI.

Run the complete automated scenario after the first toolchain build:

```console
make test-e2e
```

It rejects a tampered signature, a truncated HTTP response, and a correctly
signed bundle for another machine. It then performs `v1 -> v2`, kills QEMU
through QMP while the inactive slot is being written, verifies the old slot
still boots, checks rollback from bad userspace, forces RAM-only recovery, and
reinstalls a signed system from recovery. The full serial transcript and a
machine-readable result are saved under `artifacts/`.

## Project stages

1. Boot A and the recovery initramfs.
2. Install a valid signed A-to-B update and mark B good.
3. Exercise invalid signatures, interrupted writes, and automatic rollback.
4. Run automated serial/QMP tests with deterministic power-cut injection.
5. Add dm-verity rootfs and UEFI Secure Boot as separate lessons.
6. Optionally port the same state machine to ARM64 `virt` + U-Boot.

See [docs/design.md](docs/design.md) for the design.

## Lessons

1. [OTA fault injection: validation, power loss, rollback, and recovery](docs/lessons/01-ota-fault-injection.md)
2. [A guarded recovery workflow](docs/lessons/02-guarded-recovery.md)

## Upstream references

- [RAUC full-system QEMU/GRUB A/B example](https://rauc.readthedocs.io/en/latest/examples.html)
- [RAUC bootloader and boot-confirmation integration](https://rauc.readthedocs.io/en/latest/integration.html)
- [Buildroot](https://buildroot.org/) for the target, host tools, and disk image
- [QEMU system emulation documentation](https://www.qemu.org/docs/master/system/)
