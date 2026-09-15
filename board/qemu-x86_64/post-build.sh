#!/bin/sh
set -eu

target_dir=$1
project_dir=${BR2_EXTERNAL_OTA_LAB_PATH:?BR2_EXTERNAL_OTA_LAB_PATH is required}
version=${OTA_LAB_VERSION:-v1}
broken=${OTA_LAB_BROKEN:-0}

install -d "$target_dir/etc/rauc" "$target_dir/boot" "$target_dir/data"
install -m 0644 "$project_dir/keys/dev/ca.cert.pem" \
    "$target_dir/etc/rauc/ca.cert.pem"
printf '%s\n' "$version" > "$target_dir/etc/ota-version"

if [ "$broken" = "1" ]; then
    : > "$target_dir/etc/ota-break-boot"
else
    rm -f "$target_dir/etc/ota-break-boot"
fi
