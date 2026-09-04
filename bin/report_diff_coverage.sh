#!/usr/bin/env bash
#
# Coverage of the lines this branch changed, rather than of the whole tree.
#
# A whole-project percentage barely moves on any one branch, so it cannot answer
# the question review actually asks: is the code being added covered? diff-cover
# intersects a coverage report with `git diff`, and reports only on lines the
# branch touched.
#
# Usage:
#   bin/report_diff_coverage.sh                       # measure, then report
#   bin/report_diff_coverage.sh --reuse               # use an existing coverage.xml
#   bin/report_diff_coverage.sh --branch release/x    # compare against something else
#   bin/report_diff_coverage.sh --fail-under 80       # exit non-zero below a threshold
#   bin/report_diff_coverage.sh -- src/urbanlens/dashboard/tests/hypothesis/test_foo.py
#
# Measuring means running the suite under coverage, which is slow - the full run
# is on the order of an hour and a half. Two ways to keep that usable: pass the
# tests that cover your change after `--`, or use `--reuse` against a
# coverage.xml an earlier run already produced. A partial report is honest here
# in a way it would not be for whole-tree coverage, because the lines it reports
# on are the ones you just wrote.
set -euo pipefail

BRANCH="${UL_DIFF_COVER_BRANCH:-origin/main}"
REUSE=0
FAIL_UNDER=""
REPORT_DIR="reports/diff-coverage"
pytest_args=()

while [ $# -gt 0 ]; do
    case "$1" in
        --branch) BRANCH="${2:-}"; shift 2 ;;
        --branch=*) BRANCH="${1#*=}"; shift ;;
        --fail-under) FAIL_UNDER="${2:-}"; shift 2 ;;
        --fail-under=*) FAIL_UNDER="${1#*=}"; shift ;;
        --reuse) REUSE=1; shift ;;
        --) shift; pytest_args+=("$@"); break ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) pytest_args+=("$1"); shift ;;
    esac
done

if ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
    echo "error: '$BRANCH' is not a ref this checkout knows. Fetch it, or pass --branch." >&2
    exit 2
fi

if [ "$REUSE" -eq 0 ]; then
    echo "==> measuring coverage${pytest_args[*]:+ over ${pytest_args[*]}}"
    # A unique database for the same reason bin/run_tests.sh uses one.
    UL_TEST_DB_NAME="${UL_TEST_DB_NAME:-dc_$(date +%s)_$$}" \
        python -m coverage run -m pytest "${pytest_args[@]}"
    python -m coverage xml -o coverage.xml
elif [ ! -f coverage.xml ]; then
    echo "error: --reuse needs a coverage.xml, and there is none. Run without --reuse first." >&2
    exit 2
fi

mkdir -p "$REPORT_DIR"
args=(coverage.xml --compare-branch "$BRANCH" --html-report "$REPORT_DIR/index.html" --markdown-report "$REPORT_DIR/report.md")
[ -n "$FAIL_UNDER" ] && args+=(--fail-under "$FAIL_UNDER")

echo "==> diffing coverage against $BRANCH"
# The console script when it is on PATH, the module when only the venv has it.
if command -v diff-cover >/dev/null 2>&1; then
    diff-cover "${args[@]}"
else
    python -m diff_cover.diff_cover_tool "${args[@]}"
fi
echo "==> wrote $REPORT_DIR/index.html"
