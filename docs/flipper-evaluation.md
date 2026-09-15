# Flipper installer/recovery evaluation

Both Flipper projects can be exercised off-device, but neither is an ideal
foundation for a generic OTA/recovery course.

## Mapping the old phases to this lab

| Original phase | What is useful for the learning goal | Status here |
|---|---|---|
| 3: OTA | Signed updates, inactive-slot writes, boot attempts, confirmation, rollback | Implemented and covered by the end-to-end test |
| 4: GUI/Flatpak | Update progress UI and application updates | Deliberately deferred; it does not change the OTA safety state machine |
| 5: hardening | Recovery and CI fault tests | Implemented for QEMU; verified boot and production PKI remain later lessons |

The original OSTree/profile work remains useful as a separate application-layer
exercise. This lab starts one layer lower with whole-system RAUC A/B updates so
that power loss, boot selection, and recovery are observable as block-device
operations. OSTree can be added later without changing those invariants.

## `flipperos-installer`

Upstream: [flipperdevices/flipperos-installer](https://github.com/flipperdevices/flipperos-installer)

Useful without QEMU already:

- `cargo run --example dry_run --no-default-features` drives the real controller
  and installation plan without writing a device.
- `cargo run --example gui_screenshot --features gui` renders the real 256x144
  Slint UI into PNG files.
- Unit tests cover bundles, staging, layout, menu logic, installation guards,
  and much of UFS provisioning.

In QEMU, its TUI can run on the serial console. Its GUI can render through a
256x144 virtio-gpu (`xres=256,yres=144`), but standard virtio keyboard events
need an adapter to the Flipper button mapping.

QEMU 8.2 has `ufs` and `ufs-lu` devices, so UFS block I/O and read-only Query
descriptors can be tested. It does not implement Configuration Descriptor
writes or writable `bBootLunEn`; therefore the destructive UFS provisioning and
atomic Boot-LU switch cannot be validated without extending QEMU. A virtual SD
card or a lab-only virtio target is sufficient for the rest of the installation
pipeline. QEMU's own [UFS qtests](https://gitlab.com/qemu-project/qemu/-/blob/master/tests/qtest/ufs-test.c)
also call out the missing Write Descriptor support.

## `flipperos-recovery`

Upstream: [flipperdevices/flipperos-recovery](https://github.com/flipperdevices/flipperos-recovery)

The Buildroot initramfs can be booted directly by QEMU after adding a small
QEMU kernel-config fragment (serial, generic PCI, virtio block/network, and
optionally virtio GPU). Storage repair, networking, SSH, filesystem tools, and
RAM-only operation are all meaningful tests.

The RK3576 DTB, Rockchip BootROM, Falcon path, MCU, USB gadget controller, and
physical UFS behaviour are not modeled by QEMU `virt`. Those are hardware
integration tests, not recovery-policy tests.

## Decision

Keep the Flipper repositories as later case studies. Build the learning path on
a generic x86_64/KVM lab first, because it provides fast deterministic reboots
and lets every OTA/recovery invariant be automated. Once the state machine is
well tested, porting it to ARM64/U-Boot becomes a controlled exercise rather
than debugging the board and the update design simultaneously.
