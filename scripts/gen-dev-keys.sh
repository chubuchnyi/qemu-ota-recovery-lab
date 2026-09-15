#!/bin/sh
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
key_dir="$project_dir/keys/dev"
key="$key_dir/ca.key.pem"
cert="$key_dir/ca.cert.pem"

if [ -s "$key" ] && [ -s "$cert" ]; then
    exit 0
fi

mkdir -p "$key_dir"
openssl req -x509 -newkey rsa:3072 -sha256 -nodes \
    -keyout "$key" \
    -out "$cert" \
    -days 3650 \
    -subj "/O=QEMU OTA Lab/CN=development signing key"
chmod 0600 "$key"
echo "Generated development-only RAUC key pair in $key_dir"
