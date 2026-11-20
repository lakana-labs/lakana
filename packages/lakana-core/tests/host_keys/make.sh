#!/usr/bin/env bash
# Regenerate the host-key fixtures of test_known_host_keys.py with ssh-keygen. Only the public
# halves are kept; the private keys live in a temporary directory removed on exit.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
cd "$work"
key() { ssh-keygen -q -N '' -C '' "$@" </dev/null; }
key -t ed25519 -f host_ed25519
key -t rsa -b 3072 -f host_rsa
for bits in 256 384 521; do key -t ecdsa -b "$bits" -f "host_ecdsa$bits"; done
key -t ed25519 -f ca_ed25519
key -t ecdsa -b 256 -f ca_ecdsa
key -t rsa -b 3072 -f ca_rsa
sign() { ssh-keygen -q -h -n git.example.com "$@"; }
sign -s ca_ed25519 -I rsa-by-ed25519 host_rsa.pub
sign -s ca_ecdsa -I ed25519-by-ecdsa host_ed25519.pub
sign -s ca_rsa -t rsa-sha2-512 -I ecdsa256-by-rsa-sha2-512 host_ecdsa256.pub
sign -s ca_rsa -t rsa-sha2-256 -I ecdsa384-by-rsa-sha2-256 host_ecdsa384.pub
sign -s ca_rsa -t ssh-rsa -I ecdsa521-by-ssh-rsa host_ecdsa521.pub
cp ./*.pub "$here/"
cd "$here"
for file in *.pub; do printf '%s ' "$file"; ssh-keygen -l -f "$file"; done > ssh-keygen-l.txt
