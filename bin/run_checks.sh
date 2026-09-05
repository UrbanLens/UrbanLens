#!/usr/bin/env bash
# The whole-tree invariant checks, which are manual-only in .pre-commit-config.yaml.
#
# Each re-reads every tracked file regardless of what changed, so together they
# added ~27s to every commit for no benefit: a whole-repo invariant is equally
# true before and after any single edit. They run here instead, and in CI on
# every push (.github/workflows/ci.yml runs each one as its own step).
#
# `pre-commit run` accepts one hook id at a time, hence the loop.
#
# Usage:  bin/run_checks.sh            (or: bun run check)
set -uo pipefail
cd "$(dirname "$0")/.."

# `pre-commit` lives in the project venv, which is not necessarily activated -
# `git commit` runs its hooks through an absolute path, so nothing else in the
# repo has needed it to be on PATH.
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
