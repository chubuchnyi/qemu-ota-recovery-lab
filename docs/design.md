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
| 2 | ext4 rootfs A, including its kernel | When B is active |
| 3 | ext4 rootfs B, including its kernel | When A is active |
| 4 | ext4 persistent data | No |

GRUB defaults to recovery. It selects a normal slot only when `<slot>_OK=1` and
`<slot>_TRY=0`, then records that the slot has been tried before booting it. A
late userspace health check clears `TRY` through `rauc status mark-good`. A crash
or forced reboot before that point makes GRUB skip the attempted slot next time.

This is intentionally visible and inspectable rather than clever: students can
mount the EFI filesystem, run `grub-editenv ... list`, and correlate every bit
with the serial log.

`/run` is a tmpfs. This is an OTA invariant too: volatile network and daemon
state must not survive an abrupt QEMU power cut and poison the next boot.

## Trust boundaries

The development CA signs every RAUC bundle. The target contains only its
certificate. The private key remains on the host and is ignored by Git.

The initial milestone authenticates OTA payloads but does not claim a verified
boot chain. UEFI Secure Boot, signed GRUB/kernel artifacts, dm-verity rootfs,
key rotation, and rollback-index protection are later independent exercises.
In particular, the current lab will accept an older correctly signed bundle;
that is intentional until the anti-rollback lesson adds a trusted monotonic
version source.

## Fault-injection matrix

| Failure | Injection point | Expected invariant |
|---|---|---|
| Wrong signature | Before installation | No slot or boot state changes |
| Truncated bundle | Download/install | Active slot remains bootable |
| Wrong `compatible` | Manifest validation | No slot or boot state changes |
| QEMU power cut | While inactive slot is written | Active slot remains selected |
| Bad userspace | Before `mark-good` | One failed try, then old slot boots |
| Both slots bad | GRUB selection | RAM-only recovery boots |
| Damaged data partition | Recovery shell | A/B partitions remain inspectable |

The automated test also writes a marker to the data partition and checks it
after OTA, hard power loss, rollback, recovery, and recovery-driven reinstall.

QMP will be used to stop or reset the VM at deterministic log markers, while
the serial console provides assertions such as `OTA_LAB_BOOT_OK` and
`OTA_LAB_RECOVERY_READY`.
