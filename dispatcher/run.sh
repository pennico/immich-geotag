#!/usr/bin/env bash
# One dispatcher tick. Overlap protection (non-blocking flock) is inside
# the Python entry point, so this works on macOS and Linux alike.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dispatcher/logs
exec python3 dispatcher/dispatch.py "$@"
