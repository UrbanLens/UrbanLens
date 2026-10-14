#!/usr/bin/env bash
#
# Run the build step each package plugin ships as its build.sh, after its requirements are installed.
#
# Usage: bin/run_plugin_build_steps.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

shopt -s nullglob
for step in src/urbanlens/plugins/*/build.sh; do
    bash "$step"
done
