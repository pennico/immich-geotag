#!/usr/bin/env bash
# One dispatcher tick. flock -n skips the run if the previous one is still going.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dispatcher/logs
exec flock -n "dispatcher/.lock" python3 dispatcher/dispatch.py "$@"
