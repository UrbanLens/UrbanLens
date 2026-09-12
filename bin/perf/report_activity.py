#!/usr/bin/env python3
"""Summarise a `pg_activity_sampler.sh` CSV into the four things worth reading."""

from __future__ import annotations

import argparse
import collections
import csv
from dataclasses import dataclass, field
from pathlib import Path
import sys

#: Fraction of ``max_connections`` above which the pool is reported as pressured.
#: The same figure the readiness endpoint uses, so the two agree about what "close to full" means.
PRESSURE_FRACTION = 0.8

#: What Postgres says when the pool refuses a client. Both spellings, because
#: `log_error_verbosity` decides whether the English text appears beside the code.
REFUSAL_MARKERS = ("53300", "too many clients already")


@dataclass(frozen=True)
class ActivitySummary:
    """What one sampling run says about the connection pool."""

    peak: int = 0
    peak_at: str | None = None
    max_connections: int = 0
    peak_breakdown: dict[str, int] = field(default_factory=dict)
    idle_at_peak: int = 0
    pressured_seconds: int = 0

    @property
    def idle_share_at_peak(self) -> float:
        """Fraction of the peak's backends that were idle rather than working."""
        return self.idle_at_peak / self.peak if self.peak else 0.0

    @property
    def peak_fraction(self) -> float:
        """How full the pool got, as a fraction of ``max_connections``."""
        return self.peak / self.max_connections if self.max_connections else 0.0

    @property
    def under_pressure(self) -> bool:
        """Whether the pool ever came close enough to full to be worth explaining."""
        return self.pressured_seconds > 0 or self.peak_fraction >= PRESSURE_FRACTION


def load(path: Path) -> list[dict[str, str]]:
    """Read the sampler CSV.

    Args:
        path: The CSV written by ``pg_activity_sampler.sh``.

    Returns:
        Its rows, or an empty list when the file holds only a header."""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def summarise(rows: list[dict[str, str]]) -> ActivitySummary:
    """Reduce the samples to a peak, a breakdown, and a pressure verdict.

    Args:
        rows: Parsed CSV rows.

    Returns:
        The summary."""
    per_second: dict[str, int] = collections.defaultdict(int)
    idle_per_second: dict[str, int] = collections.defaultdict(int)
    breakdown: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    max_connections = 0

    for row in rows:
        stamp = row["iso_time"]
        backends = int(row["backends"])
        per_second[stamp] += backends
        if row["state"].startswith("idle"):
            idle_per_second[stamp] += backends
        breakdown[stamp][f"{row['usename']}/{row['application_name']}"] += backends
        max_connections = max(max_connections, int(row["max_connections"]))

    if not per_second:
        return ActivitySummary()

    peak_at = max(per_second, key=lambda stamp: per_second[stamp])
    threshold = max_connections * PRESSURE_FRACTION
    return ActivitySummary(
        peak=per_second[peak_at],
        peak_at=peak_at,
        max_connections=max_connections,
        peak_breakdown=dict(breakdown[peak_at].most_common()),
        idle_at_peak=idle_per_second[peak_at],
        pressured_seconds=sum(1 for total in per_second.values() if total >= threshold),
    )


def render(summary: ActivitySummary) -> str:
    """Format the summary for a terminal.

    Args:
        summary: What :func:`summarise` produced.

    Returns:
        The text to print."""
    if not summary.peak:
        return "pg_stat_activity: no samples (the sampler ran but the database answered nothing)."

    lines = [
        "",
        f"pg_stat_activity: peaked at {summary.peak}/{summary.max_connections} backends ({summary.peak_fraction:.0%}) at {summary.peak_at}",
    ]
    if summary.under_pressure:
        lines.append(f"  {summary.pressured_seconds}s spent at or above {PRESSURE_FRACTION:.0%} of max_connections.")
        if summary.idle_share_at_peak >= 0.5:
            lines.append(
                f"  {summary.idle_share_at_peak:.0%} of the peak's backends were IDLE - connections held open by processes that were not using them. That is P104's shape, not a busy database.",
            )
    else:
        lines.append(f"  {summary.idle_share_at_peak:.0%} idle at the peak; the pool was never close to full.")
    lines.append("  at the peak, by role/application:")
    lines.extend(f"    {count:4d}  {name}" for name, count in list(summary.peak_breakdown.items())[:8])
    lines.append("")
    return "\n".join(lines)


def count_refusals(path: Path) -> int:
    """How many times the database refused a connection, per its log.

    Args:
        path: A file holding the database container's log.

    Returns:
        Matching lines."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return sum(1 for line in text.splitlines() if any(marker in line for marker in REFUSAL_MARKERS))


def failures(summary: ActivitySummary, refusals: int) -> list[str]:
    """Why this run should fail, if it should.

    Args:
        summary: What :func:`summarise` produced.
        refusals: Connection refusals counted in the database log.

    Returns:
        One line per reason."""
    reasons = []
    if not summary.peak:
        reasons.append("the sampler produced no samples, so this run is not evidence that the pool held")
    elif summary.under_pressure:
        reasons.append(
            f"the pool reached {summary.peak}/{summary.max_connections} backends ({summary.peak_fraction:.0%}) and spent {summary.pressured_seconds}s at or above {PRESSURE_FRACTION:.0%} of max_connections",
        )
    if refusals:
        reasons.append(f"the database refused {refusals} connection(s) - one account's work reached another's")
    return reasons


def main(argv: list[str] | None = None) -> int:
    """Print a summary of one sampler CSV.

    Args:
        argv: Command-line arguments, for testing.

    Returns:
        Process exit status.

    Raises:
        SystemExit: The arguments did not parse."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="CSV written by pg_activity_sampler.sh.")
    parser.add_argument(
        "--fail-on-pressure",
        action="store_true",
        help="Exit non-zero when the pool was pressured, refused a client, or was never sampled.",
    )
    parser.add_argument("--db-log", type=Path, help="Database log to grep for connection refusals.")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"no sampler CSV at {args.csv}", file=sys.stderr)
        if args.fail_on_pressure:
            print("refusing to call that a pass: an absent sampler is a broken harness, not a healthy pool.", file=sys.stderr)
            return 1
        return 0

    summary = summarise(load(args.csv))
    print(render(summary))

    refusals = 0
    if args.db_log is not None:
        if not args.db_log.exists():
            print(f"no database log at {args.db_log}", file=sys.stderr)
            if args.fail_on_pressure:
                return 1
        else:
            refusals = count_refusals(args.db_log)

    if not args.fail_on_pressure:
        return 0

    reasons = failures(summary, refusals)
    for reason in reasons:
        print(f"FAIL - {reason}", file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
