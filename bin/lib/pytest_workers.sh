# shellcheck shell=bash
#
# How many pytest-xdist workers a run gets when its caller did not say.
#
# Sourced by bin/host_pytest.sh and bin/run_tests.sh. A directory, or the whole suite, runs on 4 workers with
# CI's `--dist loadscope --no-loadscope-reorder`. Files and test ids run in one process: each worker collects
# on its own, which costs more than a few files take to run.
#
# UL_TEST_WORKERS=N sets the count for directory runs, and 0 keeps them in one process. Any -n, --numprocesses,
# -p no:xdist, --pdb, --trace or --collect-only among the arguments wins over both.
#
# Callers set `set -euo pipefail` themselves; this file deliberately does not.

# Sets the array UL_XDIST_ARGS to the arguments to put before the caller's own.
#   $1      the repository root, which relative paths in the arguments are resolved against
#   $2...   the caller's pytest arguments
ul_default_xdist_args() {
    local root="$1"
    shift
    # shellcheck disable=SC2034  # read by the caller
    UL_XDIST_ARGS=()
    local workers="${UL_TEST_WORKERS:-4}"
    local arg prev="" target files=0 dirs=0
    for arg in "$@"; do
        case "$arg" in
            -n | -n[0-9]* | -nauto | -nlogical | --numprocesses | --numprocesses=* | --pdb | --trace | -pno:xdist | --co | --collect-only)
                return 0
                ;;
        esac
        if [ "$prev" = "-p" ] && [ "$arg" = "no:xdist" ]; then
            return 0
        fi
        case "$prev" in
            # Options whose next argument is a value, not a selection.
            -k | -m | -p | -o | -c | -W | --deselect | --ignore | --ignore-glob | --rootdir | --confcutdir) ;;
            *)
                if [[ "$arg" != -* ]]; then
                    target="${arg%%::*}"
                    [[ "$target" = /* ]] || target="$root/$target"
                    if [ -d "$target" ]; then
                        dirs=$((dirs + 1))
                    elif [ -e "$target" ]; then
                        files=$((files + 1))
                    fi
                fi
                ;;
        esac
        prev="$arg"
    done
    if [ "$workers" -gt 0 ] && { [ "$dirs" -gt 0 ] || [ "$files" -eq 0 ]; }; then
        # shellcheck disable=SC2034  # read by the caller
        UL_XDIST_ARGS=(-n "$workers" --dist loadscope --no-loadscope-reorder)
    fi
}
