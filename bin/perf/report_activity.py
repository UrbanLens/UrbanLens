#!/usr/bin/env python3
"""Summarise a `pg_activity_sampler.sh` CSV into the four things worth reading.

The CSV is a row per (role, application_name, state) per second, which is the
right thing to keep and the wrong thing to read. This reduces it to the shape
P104 had: how close the pool came to full, which tier was holding it, how much
of that was idle rather than working, and when the peak happened - so it can be
lined up against the k6 phase table printed beside it.

The idle share is the part worth naming separately, but only once the pool is
actually under pressure. A pool full of *working* backends is a busy database; a
pool full of *idle* ones is connections held open by processes that are not
using them, which is a different fault with a different fix, and it is the one
the outage actually had. Half of four backends idle is just a quiet afternoon.
"""

from __future__ import annotations

import argparse
import collections
import csv
from dataclasses import dataclass, field
from pathlib import Path
import sys

#: Fraction of ``max_connections`` above which the pool is reported as pressured.
#: The same figure the readiness endpoint uses, so the two agree about what
#: "close to full" means.
#:
#: Probably too lax, on one run's evidence: X15 peaked at 75/100 with 74 of them
#: from a single tier, and this reported "never close to full". Left aligned with
#: the readiness endpoint rather than tuned here, because two different answers
#: to "is the pool in trouble" is worse than one imperfect answer - but the
#: composition matters as much as the total, and neither this nor the endpoint
#: looks at it yet.
PRESSURE_FRACTION = 0.8


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
        Its rows, or an empty list when the file holds only a header.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def summarise(rows: list[dict[str, str]]) -> ActivitySummary:
    """Reduce the samples to a peak, a breakdown, and a pressure verdict.

    Args:
        rows: Parsed CSV rows.

    Returns:
        The summary. An empty one when the CSV held no samples.
    """
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
        The text to print.
    """
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


def main(argv: list[str] | None = None) -> int:
    """Print a summary of one sampler CSV.

    Args:
        argv: Command-line arguments, for testing.

    Returns:
        Process exit status. Zero even when the pool was pressured: this
        reports, and k6 decides.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="CSV written by pg_activity_sampler.sh.")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"no sampler CSV at {args.csv}", file=sys.stderr)
        return 0

    print(render(summarise(load(args.csv))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
