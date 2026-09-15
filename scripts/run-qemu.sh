#!/bin/bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
fresh=0
if [[ ${1:-} == --fresh ]]; then
    fresh=1
    shift
fi

base_image=${1:-"$project_dir/artifacts/images/v1/disk.img"}
state_dir=${OTA_LAB_STATE_DIR:-"$project_dir/run"}
disk_overlay="$state_dir/disk.qcow2"
vars_image="$state_dir/OVMF_VARS.fd"

if [[ ! -f $base_image ]]; then
    echo "base image not found: $base_image" >&2
    echo "run: make build VERSION=v1" >&2
    exit 1
fi

mkdir -p "$state_dir"
if (( fresh )); then
    rm -f "$disk_overlay" "$vars_image"
fi

if [[ ! -f $disk_overlay ]]; then
    qemu-img create -q -f qcow2 -F raw -b "$(realpath "$base_image")" "$disk_overlay"
fi

ovmf_code=${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}
ovmf_vars_template=${OVMF_VARS_TEMPLATE:-/usr/share/OVMF/OVMF_VARS_4M.fd}
if [[ ! -f $ovmf_code || ! -f $ovmf_vars_template ]]; then
    echo "OVMF firmware not found; install the ovmf package or set OVMF_CODE and OVMF_VARS_TEMPLATE" >&2
    exit 1
fi
if [[ ! -f $vars_image ]]; then
    cp "$ovmf_vars_template" "$vars_image"
fi

accel=(-machine "q35,accel=tcg" -cpu max)
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    accel=(-machine "q35,accel=kvm" -cpu host)
fi

echo "Persistent VM disk: $disk_overlay"
echo "Guest OTA URL: http://10.0.2.2:8000/update-v2.raucb"
echo "Exit QEMU with Ctrl-a x"

exec qemu-system-x86_64 \
    "${accel[@]}" \
    -m 1024 \
    -smp 2 \
    -display none \
    -serial mon:stdio \
    -drive if=pflash,format=raw,readonly=on,file="$ovmf_code" \
    -drive if=pflash,format=raw,file="$vars_image" \
    -drive if=virtio,format=qcow2,file="$disk_overlay" \
    -device virtio-rng-pci \
    -netdev user,id=net0,hostfwd=tcp::2222-:22 \
    -device virtio-net-pci,netdev=net0 \
    -qmp unix:"$state_dir/qmp.sock",server=on,wait=off
