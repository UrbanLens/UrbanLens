#!/usr/bin/env python3
"""Turn a baseline k6 summary into the p95 ceiling the measured pass is judged against."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SLACK_FACTOR = 3.0

#: Added to the baseline when the multiple would be smaller. Below roughly this
#: much, a latency change is not something a person can perceive.
SLACK_FLOOR_MS = 250.0

#: The budget never exceeds this however slow the baseline was. A second is
#: about where a page stops feeling like it responded.
CEILING_MS = 1000.0

#: Where k6 puts the neighbour's trend for the baseline phase. The phase name is
#: `baselinePhase()` in `tests/perf/k6/lib/schedule.js`; renaming it there
#: without changing it here falls back to the metric below, which is why the
#: fallback says so out loud rather than quietly producing a slightly different
#: number.
_METRIC = "http_req_duration{scenario:neighbour,phase:idle}"

#: Used when the phase-tagged metric is missing. Wider than intended - it
#: includes `setup`'s sign-ins, which are a second of PBKDF2 each - so a budget
#: derived from it is not wrong so much as not the thing that was asked for.
_FALLBACK_METRIC = "http_req_duration"


def baseline_p95(summary: dict) -> float:
    """The neighbour's p95 in the baseline pass, in milliseconds.

    Args:
        summary: A parsed k6 ``handleSummary`` document.

    Returns:
        The p95 in milliseconds.

    Raises:
        SystemExit: The summary has no usable trend, which means the baseline pass issued no requests - deriving a budget from it would produce a number..."""
    metrics = summary.get("metrics", {})
    for name in (_METRIC, _FALLBACK_METRIC):
        values = metrics.get(name, {}).get("values", {})
        p95 = values.get("p(95)")
        count = values.get("count", 0)
        if p95 is not None and count:
            if name == _FALLBACK_METRIC:
                print(
                    f"warning: the baseline summary has no {_METRIC!r}, so this budget comes from every request in the run including setup's sign-ins. Check that schedule.js's baseline phase is still called 'idle'.",
                    file=sys.stderr,
                )
            return float(p95)
    raise SystemExit("The baseline summary has no request timings in it. The baseline pass measured nothing, so there is no budget to derive.")


def derive(p95: float) -> int:
    """The budget for the measured pass, in whole milliseconds.

    Args:
        p95: The baseline p95 in milliseconds.

    Returns:
        The ceiling every asserted phase's p95 must stay below."""
    return int(min(CEILING_MS, max(p95 * SLACK_FACTOR, p95 + SLACK_FLOOR_MS)))


def ceiling_dominates(p95: float) -> bool:
    """Whether the absolute ceiling, not the slack, is what set the budget.

    Worth saying out loud rather than leaving in the arithmetic.

    Args:
        p95: The baseline p95 in milliseconds.

    Returns:
        True when the ceiling is doing the work."""
    return max(p95 * SLACK_FACTOR, p95 + SLACK_FLOOR_MS) > CEILING_MS


def main(argv: list[str] | None = None) -> int:
    """Read a baseline summary and print the derived budget.

    Args:
        argv: Command-line arguments, for testing.

    Returns:
        Process exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path, help="Path to the baseline pass's JSON summary.")
    parser.add_argument("--explain", action="store_true", help="Also write the reasoning to stderr.")
    args = parser.parse_args(argv)

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    p95 = baseline_p95(summary)
    budget = derive(p95)

    if args.explain:
        print(f"baseline p95 {p95:.0f}ms -> budget {budget}ms (x{SLACK_FACTOR} = {p95 * SLACK_FACTOR:.0f}, +{SLACK_FLOOR_MS:.0f} = {p95 + SLACK_FLOOR_MS:.0f}, ceiling {CEILING_MS:.0f})", file=sys.stderr)

    if ceiling_dominates(p95):
        print(
            f"warning: the baseline p95 is already {p95:.0f}ms, so the {CEILING_MS:.0f}ms ceiling set this budget rather than the slack. "
            "A failure in this run may say more about the host than about the account under test - check the baseline before believing the verdict.",
            file=sys.stderr,
        )

    print(budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
