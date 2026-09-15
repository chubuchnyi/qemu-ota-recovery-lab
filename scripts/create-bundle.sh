#!/bin/sh
set -eu

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
    echo "usage: $0 BUILD_OUTPUT VERSION OUTPUT.raucb [COMPATIBLE]" >&2
    exit 2
fi

build_output=$1
version=$2
bundle_output=$3
compatible=${4:-qemu-ota-recovery-lab-x86_64}
project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
host_rauc="$build_output/host/bin/rauc"
rootfs="$build_output/images/rootfs.ext4"
root_hash_file="$build_output/images/rootfs.roothash"
hash_offset_file="$build_output/images/rootfs.hash-offset"
key="$project_dir/keys/dev/ca.key.pem"
cert="$project_dir/keys/dev/ca.cert.pem"

# RAUC shells out to host-side helpers such as mksquashfs and veritysetup.
# Buildroot installs them next to the host RAUC binary, not into the base
# container's global PATH.
PATH="$build_output/host/bin:$build_output/host/sbin:$PATH"
export PATH

for path in "$host_rauc" "$rootfs" "$root_hash_file" "$hash_offset_file" \
    "$key" "$cert"; do
    if [ ! -e "$path" ]; then
        echo "missing required file: $path" >&2
        exit 1
    fi
done

work=$(mktemp -d)
bundle_tmp="${bundle_output}.tmp.$$"
trap 'rm -rf "$work"; rm -f "$bundle_tmp"' EXIT INT TERM
cp "$rootfs" "$work/rootfs.ext4"
root_hash=$(cat "$root_hash_file")
hash_offset=$(cat "$hash_offset_file")

case "$root_hash" in
    ''|*[!0-9a-fA-F]*)
        echo "invalid dm-verity root hash: $root_hash" >&2
        exit 1
        ;;
esac
if [ "${#root_hash}" -ne 64 ]; then
    echo "invalid dm-verity root hash length" >&2
    exit 1
fi
case "$hash_offset" in
    ''|*[!0-9]*)
        echo "invalid dm-verity hash offset: $hash_offset" >&2
        exit 1
        ;;
esac

cat > "$work/hook.sh" <<EOF
#!/bin/sh
set -eu

case "\${1:-}" in
    slot-post-install)
        case "\${RAUC_SLOT_BOOTNAME:-}" in
            A|B) ;;
            *)
                echo "verity hook: invalid bootname '\${RAUC_SLOT_BOOTNAME:-}'" >&2
                exit 1
                ;;
        esac
        grub-editenv /boot/EFI/BOOT/grubenv set \
            "\${RAUC_SLOT_BOOTNAME}_HASH=$root_hash" \
            "\${RAUC_SLOT_BOOTNAME}_HASH_OFFSET=$hash_offset"
        sync
        echo "OTA_LAB_VERITY_HASH_UPDATED slot=\${RAUC_SLOT_BOOTNAME} hash_offset=$hash_offset"
        ;;
    *)
        exit 1
        ;;
esac
EOF
chmod 0755 "$work/hook.sh"

cat > "$work/manifest.raucm" <<EOF
[update]
compatible=$compatible
version=$version
description=QEMU OTA lab update $version

[bundle]
format=verity

[hooks]
filename=hook.sh

[image.rootfs]
filename=rootfs.ext4
hooks=post-install
EOF

mkdir -p "$(dirname -- "$bundle_output")"
rm -f "$bundle_tmp"
"$host_rauc" --cert "$cert" --key "$key" \
    bundle "$work" "$bundle_tmp"
mv -f "$bundle_tmp" "$bundle_output"
echo "Created $bundle_output"
