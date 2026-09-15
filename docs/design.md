# Design

## Why x86_64 first

The host can use KVM for x86_64 guests. An ARM64 guest on an x86_64 host uses
TCG, which is useful for architecture testing but makes the build-test loop much
slower. None of the OTA invariants depends on RK3576, SPI, UFS, or an ARM CPU.

The first implementation therefore uses QEMU `q35`, OVMF, GRUB, and virtio.
ARM64 `virt` + U-Boot is a later port after the tests describe the desired
behaviour.

## Disk and boot state

| GPT partition | Content | Updated by normal OTA |
|---|---|---|
| 1 | FAT EFI, GRUB, `grubenv`, recovery kernel/initramfs | No |
| 2 | read-only ext4 rootfs A + dm-verity tree, including its kernel | When B is active |
| 3 | read-only ext4 rootfs B + dm-verity tree, including its kernel | When A is active |
| 4 | ext4 persistent data | No |

GRUB defaults to recovery. It selects a normal slot only when `<slot>_OK=1` and
`<slot>_TRY=0`, then records that the slot has been tried before booting it. A
late userspace health check clears `TRY` through `rauc status mark-good`. A crash
or forced reboot before that point makes GRUB skip the attempted slot next time.

This is intentionally visible and inspectable rather than clever: students can
mount the EFI filesystem, run `grub-editenv ... list`, and correlate every bit
with the serial log.

`/run`, `/tmp`, and `/mnt` are tmpfs mounts. This is an OTA invariant too:
volatile network, bundle-mount, and daemon state must not survive an abrupt
QEMU power cut and poison the next boot.

## Trust boundaries

The development CA signs every RAUC bundle. The target contains only its
certificate. The private key remains on the host and is ignored by Git.

dm-verity now checks each normal rootfs against a per-slot root hash and mounts
it read-only. This still is not a verified boot chain: the EFI partition,
`grubenv`, kernel command line, GRUB, and slot kernels are not authenticated.
UEFI Secure Boot and root-hash authentication are the next independent
exercise. Key rotation and rollback-index protection remain later work; the
current lab intentionally accepts an older correctly signed bundle.

## Fault-injection matrix

| Failure | Injection point | Expected invariant |
|---|---|---|
| Wrong signature | Before installation | No slot or boot state changes |
| Truncated bundle | Download/install | Active slot remains bootable |
| Wrong `compatible` | Manifest validation | No slot or boot state changes |
| QEMU power cut | While inactive slot is written | Active slot remains selected |
| Bad userspace | Before `mark-good` | One failed try, then old slot boots |
| Both slots bad | GRUB selection | RAM-only recovery boots |
| Recovery tool in a normal slot | Before recovery action | Command refuses to run |
| Inspect failed A/B slots | RAM recovery | Slot byte hashes remain unchanged |
| Damaged data partition | Recovery shell | A/B partitions remain inspectable |
| Damaged active verity tree | Offline `qemu-io` write | dm-verity restarts; intact peer boots |

The automated test also writes a marker to the data partition and checks it
after OTA, hard power loss, rollback, recovery, recovery-driven reinstall, and
dm-verity fallback.

QMP will be used to stop or reset the VM at deterministic log markers, while
the serial console provides assertions such as `OTA_LAB_BOOT_OK` and
`OTA_LAB_RECOVERY_READY`.
