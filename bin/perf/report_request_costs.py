#!/usr/bin/env python3
"""Rank views by what a request to them costs, from the app's slow-request log lines.

Run the app with ``UL_SLOW_REQUEST_MS=1`` so every request is logged, drive it, then feed this its logs. Ranked by
total CPU, because under concurrency that is what one endpoint takes from every other.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
import sys

_LINE = re.compile(
    r"slow request view=(?P<view>\S+) method=(?P<method>\S+) status=(?P<status>\S+) wall_ms=(?P<wall>[\d.]+) "
    r"cpu_ms=(?P<cpu>[\d.]+) sql_ms=(?P<sql>[\d.]+) sql_n=(?P<sql_n>\d+) sql_rows=(?P<rows>\d+) user=(?P<user>\S*) "
    r"bytes=(?P<bytes>\S+) path=(?P<path>\S*)",
)


@dataclass
class ViewCost:
    """Every logged request to one view and method."""

    view: str
    method: str
    wall: list[float] = field(default_factory=list)
    cpu: list[float] = field(default_factory=list)
    sql: list[float] = field(default_factory=list)
    queries: list[int] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)
    errors: int = 0

    @property
    def count(self) -> int:
        return len(self.wall)

    @property
    def total_cpu(self) -> float:
        return sum(self.cpu)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; 0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def parse(lines: list[str]) -> dict[tuple[str, str], ViewCost]:
    """Group slow-request lines by view and method.

    Args:
        lines: Log lines; anything that is not a slow-request line is skipped.

    Returns:
        One `ViewCost` per view and method.
    """
    costs: dict[tuple[str, str], ViewCost] = {}
    for line in lines:
        match = _LINE.search(line)
        if not match:
            continue
        key = (match.group("view"), match.group("method"))
        cost = costs.get(key) or costs.setdefault(key, ViewCost(view=key[0], method=key[1]))
        cost.wall.append(float(match.group("wall")))
        cost.cpu.append(float(match.group("cpu")))
        cost.sql.append(float(match.group("sql")))
        cost.queries.append(int(match.group("sql_n")))
        cost.rows.append(int(match.group("rows")))
        size = match.group("bytes")
        if size.isdigit():
            cost.sizes.append(int(size))
        if match.group("status").isdigit() and int(match.group("status")) >= 500:
            cost.errors += 1
    return costs


def render(costs: dict[tuple[str, str], ViewCost], limit: int) -> str:
    """The ranking, as Markdown.

    Args:
        costs: From `parse`.
        limit: Rows to show.

    Returns:
        Markdown text.
    """
    total_cpu = sum(cost.total_cpu for cost in costs.values()) or 1.0
    lines = [
        "| view | method | count | cpu share | p50 wall ms | p95 wall ms | mean cpu ms | mean sql ms | mean queries | max queries | mean rows | mean KB | 5xx |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    ranked = sorted(costs.values(), key=lambda cost: cost.total_cpu, reverse=True)[:limit]
    for cost in ranked:
        mean = lambda values: sum(values) / len(values) if values else 0.0  # noqa: E731
        lines.append(
            f"| {cost.view} | {cost.method} | {cost.count} | {cost.total_cpu / total_cpu * 100:.1f}% | {percentile(cost.wall, 0.5):.0f} | "
            f"{percentile(cost.wall, 0.95):.0f} | {mean(cost.cpu):.1f} | {mean(cost.sql):.1f} | {mean(cost.queries):.1f} | {max(cost.queries, default=0)} | "
            f"{mean(cost.rows):.0f} | {mean(cost.sizes) / 1024:.1f} | {cost.errors} |",
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Print the ranking."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="The app's logs, e.g. `docker logs ul_perf_app > app.log 2>&1`.")
    parser.add_argument("--limit", type=int, default=40, help="Rows to show (default: 40).")
    args = parser.parse_args(argv)
    costs = parse(args.log.read_text(encoding="utf-8", errors="replace").splitlines())
    if not costs:
        sys.stderr.write("No slow-request lines found. Was UL_SLOW_REQUEST_MS set low enough to log every request?\n")
        return 1
    sys.stdout.write(render(costs, args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
