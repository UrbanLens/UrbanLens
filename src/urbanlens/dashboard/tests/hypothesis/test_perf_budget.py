"""`bin/perf/derive_budget.py` decides whether every phase of a load run passes.

It is the one number the whole neighbour suite is judged against, and it is
computed from a measurement rather than written down - which is the right
design and also means a mistake in it is invisible: a budget that came out too
generous makes a failing run pass, and nothing else in the suite would notice.

The formula's three terms each cover a case the others get wrong, so each is
tested at a size where it is the term that decides.

Written as plain functions rather than in the `*Tests` classes used elsewhere
here: those are collected because they subclass `TestCase`, and this needs
`tmp_path` and `capsys`, which pytest does not inject into unittest cases. A
`*Tests` class that subclasses nothing is silently not collected at all - this
file collected zero tests in its first draft for exactly that reason.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[5] / "bin" / "perf" / "derive_budget.py"


def _load() -> Any:
    """Import the script by path.

    It lives in ``bin/`` and is a command rather than a package member, so there
    is no import path to it. Loading it here rather than shelling out keeps the
    assertions on the functions instead of on stdout parsing.
    """
    spec = importlib.util.spec_from_file_location("derive_budget", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["derive_budget"] = module
    spec.loader.exec_module(module)
    return module


derive_budget = _load()


def summary(p95: float | None, count: int = 300, metric: str | None = None) -> dict[str, Any]:
    """A k6 summary document carrying one trend."""
    name = metric or "http_req_duration{scenario:neighbour,phase:idle}"
    values: dict[str, Any] = {"count": count}
    if p95 is not None:
        values["p(95)"] = p95
    return {"metrics": {name: {"values": values}}}


# -- TheFormulaTests -------------------------------------------------------
"""Each term, at a size where it is the one that decides."""


def test_the_additive_floor_wins_when_the_baseline_is_tiny() -> None:
    """3 x 4ms is a 12ms budget, which ordinary jitter fails."""
    assert derive_budget.derive(4.0) == 254


def test_the_multiple_wins_in_the_middle() -> None:
    assert derive_budget.derive(200.0) == 600


def test_the_ceiling_wins_when_the_baseline_is_large() -> None:
    assert derive_budget.derive(900.0) == int(derive_budget.CEILING_MS)


def test_the_budget_is_never_below_the_baseline() -> None:
    """A budget under the baseline would fail every phase including the quiet ones."""
    for p95 in (1.0, 50.0, 300.0, 749.0):
        assert derive_budget.derive(p95) > p95


def test_the_budget_rises_with_the_baseline() -> None:
    previous = 0
    for p95 in (1.0, 10.0, 100.0, 300.0, 500.0):
        current = derive_budget.derive(p95)
        assert current >= previous
        previous = current


# -- TheCeilingWarningTests ------------------------------------------------
"""Saying when the verdict is about the host rather than about the actor."""


def test_it_is_quiet_while_the_slack_decides() -> None:
    assert not derive_budget.ceiling_dominates(100.0)


def test_it_speaks_up_once_the_ceiling_decides() -> None:
    assert derive_budget.ceiling_dominates(400.0)


def test_the_boundary_is_where_the_multiple_crosses_the_ceiling() -> None:
    edge = derive_budget.CEILING_MS / derive_budget.SLACK_FACTOR
    assert not derive_budget.ceiling_dominates(edge - 1)
    assert derive_budget.ceiling_dominates(edge + 1)


# -- ReadingTheSummaryTests ------------------------------------------------
"""The parsing, including the case that must not be papered over."""


def test_it_reads_the_phase_tagged_trend() -> None:
    assert derive_budget.baseline_p95(summary(243.4)) == pytest.approx(243.4)


def test_it_falls_back_to_the_untagged_trend() -> None:
    """Only when the tagged one is missing - a renamed baseline phase, say."""
    assert derive_budget.baseline_p95(summary(180.0, metric="http_req_duration")) == pytest.approx(180.0)


def test_a_baseline_that_measured_nothing_is_refused() -> None:
    """The dangerous case. A budget derived from no requests has nothing behind it,
    and every phase of the run that follows would be judged against it."""
    with pytest.raises(SystemExit):
        derive_budget.baseline_p95(summary(50.0, count=0))


def test_a_summary_with_no_trend_at_all_is_refused() -> None:
    with pytest.raises(SystemExit):
        derive_budget.baseline_p95({"metrics": {}})


def test_the_command_prints_one_integer(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(summary(243.4)), encoding="utf-8")

    assert derive_budget.main([str(path)]) == 0

    assert capsys.readouterr().out.strip() == str(derive_budget.derive(243.4))
