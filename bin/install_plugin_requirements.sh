#!/usr/bin/env bash
#
# Install the Python packages each package plugin lists in its requirements.txt into the project environment.
#
# Core's dependencies come from pyproject.toml and uv.lock; a plugin's own come from here, so an install without
# the plugin never carries them. Run after `uv sync`, which removes packages the lock file does not name.
#
# Usage: bin/install_plugin_requirements.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

shopt -s nullglob
requirements=(src/urbanlens/plugins/*/requirements.txt)
if [ ${#requirements[@]} -eq 0 ]; then
    exit 0
fi

args=()
for file in "${requirements[@]}"; do
    args+=(-r "$file")
done
uv pip install --python "${UV_PROJECT_ENVIRONMENT:-.venv}/bin/python" "${args[@]}"
