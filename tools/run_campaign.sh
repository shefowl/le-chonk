#!/usr/bin/env bash
# Limit the entire harness + model CLI process tree, not the game.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec systemd-run --user --scope --quiet \
  -p MemoryHigh=5G -p MemoryMax=6G -p MemorySwapMax=0 -p OOMPolicy=kill \
  python3 -u -m harness.run "$@"
