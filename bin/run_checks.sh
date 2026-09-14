#!/usr/bin/env bash
# Whole-tree invariant checks (manual-only; each also runs as its own CI step).
#
# Usage:  bin/run_checks.sh            (or: bun run check)
set -uo pipefail
cd "$(dirname "$0")/.."

# `pre-commit` via the project venv when present.
if [ -x .venv/bin/pre-commit ]; then
    PRE_COMMIT=(.venv/bin/pre-commit)
elif command -v pre-commit >/dev/null 2>&1; then
    PRE_COMMIT=(pre-commit)
elif command -v uv >/dev/null 2>&1; then
    PRE_COMMIT=(uv run pre-commit)
else
    echo "pre-commit not found: run 'uv sync' first." >&2
    exit 1
fi

HOOKS=(
    imports-tracked
    outage-not-cached
    versioned-writes
    signal-reachable
    concealed-writes
    pin-not-published-to-wiki
    migration-graph
    static-url-literals
    bem-modifiers
    image-file-reads
    doc-line-refs
    docs-refs
    docs-index
    ruff-format-check
)

failed=()
for hook in "${HOOKS[@]}"; do
    if ! "${PRE_COMMIT[@]}" run --hook-stage manual --all-files "$hook"; then
        failed+=("$hook")
    fi
done

if [ ${#failed[@]} -gt 0 ]; then
    printf '\nFAILED: %s\n' "${failed[*]}"
    exit 1
fi
printf '\nAll whole-tree checks passed.\n'
