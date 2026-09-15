#!/bin/sh
set -eu

board_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
efi_dir="$BINARIES_DIR/efi-part/EFI/BOOT"
rootfs="$BINARIES_DIR/rootfs.ext4"
root_hash_file="$BINARIES_DIR/rootfs.roothash"
hash_offset_file="$BINARIES_DIR/rootfs.hash-offset"

data_size=$(stat -Lc %s "$rootfs")
if [ $((data_size % 4096)) -ne 0 ]; then
    echo "rootfs size is not aligned to a 4096-byte verity block" >&2
    exit 1
fi
data_blocks=$((data_size / 4096))
hash_offset=$((data_size + 4096))
truncate -s "$hash_offset" "$rootfs"
rm -f "$root_hash_file"
veritysetup --format=1 --hash=sha256 \
    --data-block-size=4096 --hash-block-size=4096 \
    --data-blocks="$data_blocks" --hash-offset="$hash_offset" \
    --root-hash-file="$root_hash_file" format "$rootfs" "$rootfs"
printf '%s\n' "$hash_offset" > "$hash_offset_file"

root_hash=$(cat "$root_hash_file")

install -d "$efi_dir"
install -m 0644 "$BINARIES_DIR/bzImage" "$efi_dir/bzImage"
install -m 0644 "$BINARIES_DIR/rootfs.cpio.gz" "$efi_dir/recovery.cpio.gz"
install -m 0644 "$board_dir/grub.cfg" "$efi_dir/grub.cfg"

"$HOST_DIR/bin/grub-editenv" "$efi_dir/grubenv" create
"$HOST_DIR/bin/grub-editenv" "$efi_dir/grubenv" set \
    "ORDER=A B R" A_OK=1 B_OK=0 R_OK=1 A_TRY=0 B_TRY=0 R_TRY=0 \
    "A_HASH=$root_hash" "B_HASH=$root_hash" \
    "A_HASH_OFFSET=$hash_offset" "B_HASH_OFFSET=$hash_offset"

rm -f "$BINARIES_DIR/data.ext4"
"$HOST_DIR/sbin/mkfs.ext4" -q -F -L ota-data \
    "$BINARIES_DIR/data.ext4" 128M

support/scripts/genimage.sh -c "$board_dir/genimage.cfg"
