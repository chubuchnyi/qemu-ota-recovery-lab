#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: $0 BUILD_OUTPUT VERSION OUTPUT.raucb" >&2
    exit 2
fi

build_output=$1
version=$2
bundle_output=$3
project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
host_rauc="$build_output/host/bin/rauc"
rootfs="$build_output/images/rootfs.ext4"
key="$project_dir/keys/dev/ca.key.pem"
cert="$project_dir/keys/dev/ca.cert.pem"

# RAUC shells out to host-side helpers such as mksquashfs and veritysetup.
# Buildroot installs them next to the host RAUC binary, not into the base
# container's global PATH.
PATH="$build_output/host/bin:$build_output/host/sbin:$PATH"
export PATH

for path in "$host_rauc" "$rootfs" "$key" "$cert"; do
    if [ ! -e "$path" ]; then
        echo "missing required file: $path" >&2
        exit 1
    fi
done

work=$(mktemp -d)
bundle_tmp="${bundle_output}.tmp.$$"
trap 'rm -rf "$work"; rm -f "$bundle_tmp"' EXIT INT TERM
cp "$rootfs" "$work/rootfs.ext4"

cat > "$work/manifest.raucm" <<EOF
[update]
compatible=qemu-ota-recovery-lab-x86_64
version=$version
description=QEMU OTA lab update $version

[bundle]
format=verity

[image.rootfs]
filename=rootfs.ext4
EOF

mkdir -p "$(dirname -- "$bundle_output")"
rm -f "$bundle_tmp"
"$host_rauc" --cert "$cert" --key "$key" \
    bundle "$work" "$bundle_tmp"
mv -f "$bundle_tmp" "$bundle_output"
echo "Created $bundle_output"
