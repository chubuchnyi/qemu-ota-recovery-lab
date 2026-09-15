#!/bin/sh
set -eu

board_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
efi_dir="$BINARIES_DIR/efi-part/EFI/BOOT"

install -d "$efi_dir"
install -m 0644 "$BINARIES_DIR/bzImage" "$efi_dir/bzImage"
install -m 0644 "$BINARIES_DIR/rootfs.cpio.gz" "$efi_dir/recovery.cpio.gz"
install -m 0644 "$board_dir/grub.cfg" "$efi_dir/grub.cfg"

"$HOST_DIR/bin/grub-editenv" "$efi_dir/grubenv" create
"$HOST_DIR/bin/grub-editenv" "$efi_dir/grubenv" set \
    "ORDER=A B R" A_OK=1 B_OK=0 R_OK=1 A_TRY=0 B_TRY=0 R_TRY=0

rm -f "$BINARIES_DIR/data.ext4"
"$HOST_DIR/sbin/mkfs.ext4" -q -F -L ota-data \
    "$BINARIES_DIR/data.ext4" 128M

support/scripts/genimage.sh -c "$board_dir/genimage.cfg"
