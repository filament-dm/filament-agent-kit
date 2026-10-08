#!/usr/bin/env bash
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
export FILAMENT_TEST_BASH=${FILAMENT_TEST_BASH:-$(command -v bash)}
exec python3 "$HERE/suite.py" "$@"
