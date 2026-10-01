"""The request-cost ranking decides which endpoints get optimised first, so it must rank by what they take from others."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

_MODULE_PATH = Path(__file__).resolve().parents[5] / "bin" / "perf" / "report_request_costs.py"


def _load() -> Any:
    """Import the script by path, as `test_perf_budget.py` does."""
    spec = importlib.util.spec_from_file_location("report_request_costs", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["report_request_costs"] = module
    spec.loader.exec_module(module)
    return module


report_request_costs = _load()


def _line(view: str, *, cpu: int, wall: int = 100, status: int = 200, size: str = "2048", method: str = "GET") -> str:
    return (
        f"2026-09-15 16:00:00,000 WARNING urbanlens.dashboard.middleware [middleware:305] slow request view={view} "
        f"method={method} status={status} wall_ms={wall} cpu_ms={cpu} sql_ms=12 sql_n=7 sql_rows=40 user=3 "
        f"bytes={size} path=/dashboard/x/"
    )


def test_requests_group_by_view_and_method() -> None:
    costs = report_request_costs.parse(
        [_line("map.view", cpu=10), _line("map.view", cpu=30), _line("map.search", cpu=5, method="POST"), "noise"],
    )

    assert costs["map.view", "GET"].count == 2
    assert costs["map.view", "GET"].total_cpu == 40
    assert costs["map.search", "POST"].count == 1


def test_the_ranking_is_by_total_cpu_not_by_the_slowest_request() -> None:
    """A cheap poll sent a thousand times takes more from everyone than one slow page."""
    lines = [_line("page.slow", cpu=900, wall=2000)] + [_line("poll.cheap", cpu=2, wall=5)] * 1000

    text = report_request_costs.render(report_request_costs.parse(lines), limit=10)

    rows = [line for line in text.splitlines() if line.startswith(("| poll", "| page"))]
    assert rows[0].startswith("| poll.cheap ")


def test_a_streamed_response_has_no_size_and_is_not_counted_as_zero() -> None:
    costs = report_request_costs.parse(
        [_line("map.document", cpu=50, size="-"), _line("map.document", cpu=50, size="4096")]
    )

    assert costs["map.document", "GET"].sizes == [4096]


def test_server_errors_are_counted() -> None:
    costs = report_request_costs.parse(
        [_line("x", cpu=1, status=500), _line("x", cpu=1, status=404), _line("x", cpu=1)]
    )

    assert costs["x", "GET"].errors == 1


def test_percentile_is_nearest_rank() -> None:
    values = [float(value) for value in range(1, 101)]

    assert report_request_costs.percentile(values, 0.95) == 95.0
    assert report_request_costs.percentile(values, 0.5) == 50.0
    assert report_request_costs.percentile([], 0.95) == 0.0
