"""The neighbour run's second verdict: did one account exhaust the connection pool?"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

_MODULE_PATH = Path(__file__).resolve().parents[5] / "bin" / "perf" / "report_activity.py"


def _load() -> Any:
    """Import the script by path, as `test_perf_budget.py` does."""
    spec = importlib.util.spec_from_file_location("report_activity", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["report_activity"] = module
    spec.loader.exec_module(module)
    return module


report_activity = _load()

_HEADER = "iso_time,usename,application_name,state,backends,max_connections"


def _csv(tmp_path: Path, *rows: str) -> Path:
    """Write a sampler CSV holding *rows*."""
    path = tmp_path / "pg_activity.csv"
    path.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def _calm(tmp_path: Path) -> Path:
    return _csv(tmp_path, "2026-09-11T00:00:00Z,ul_web,urbanlens-web,active,4,100")


def _pressured(tmp_path: Path) -> Path:
    return _csv(tmp_path, "2026-09-11T00:00:00Z,ul_web,urbanlens-web,idle,85,100")


# -- The pool verdict ------------------------------------------------------


def test_a_pressured_pool_fails_the_run(tmp_path: Path) -> None:
    assert report_activity.main([str(_pressured(tmp_path)), "--fail-on-pressure"]) != 0


def test_a_calm_pool_passes(tmp_path: Path) -> None:
    assert report_activity.main([str(_calm(tmp_path)), "--fail-on-pressure"]) == 0


def test_without_the_flag_it_still_only_reports(tmp_path: Path) -> None:
    """The informational use predates the verdict and keeps working."""
    assert report_activity.main([str(_pressured(tmp_path))]) == 0


def test_a_missing_csv_fails_rather_than_passing_quietly(tmp_path: Path) -> None:
    """A sampler that never ran must not be read as a pool that was fine."""
    assert report_activity.main([str(tmp_path / "absent.csv"), "--fail-on-pressure"]) != 0


def test_a_csv_with_no_samples_fails_too(tmp_path: Path) -> None:
    """The sampler ran, the database answered nothing - still not evidence."""
    empty = tmp_path / "pg_activity.csv"
    empty.write_text(_HEADER + "\n", encoding="utf-8")
    assert report_activity.main([str(empty), "--fail-on-pressure"]) != 0


# -- The 53300 grep --------------------------------------------------------


def test_a_connection_refusal_in_the_database_log_fails_the_run(tmp_path: Path) -> None:
    """`53300` is the pool refusing a client - the outage's own error code."""
    log = tmp_path / "db.log"
    log.write_text("2026-09-11 00:00:00 UTC [1] FATAL:  sorry, too many clients already\n", encoding="utf-8")
    assert report_activity.main([str(_calm(tmp_path)), "--fail-on-pressure", "--db-log", str(log)]) != 0


def test_a_clean_database_log_passes(tmp_path: Path) -> None:
    log = tmp_path / "db.log"
    log.write_text("2026-09-11 00:00:00 UTC [1] LOG:  checkpoint starting\n", encoding="utf-8")
    assert report_activity.main([str(_calm(tmp_path)), "--fail-on-pressure", "--db-log", str(log)]) == 0


def test_the_sqlstate_is_matched_as_well_as_the_message(tmp_path: Path) -> None:
    """Postgres logs the code without the English text under some settings."""
    log = tmp_path / "db.log"
    log.write_text("2026-09-11 00:00:00 UTC [1] FATAL:  53300\n", encoding="utf-8")
    assert report_activity.main([str(_calm(tmp_path)), "--fail-on-pressure", "--db-log", str(log)]) != 0


def test_a_missing_database_log_fails_when_one_was_asked_for(tmp_path: Path) -> None:
    """Asking for the grep and not getting it is not the same as a clean log."""
    assert (
        report_activity.main([str(_calm(tmp_path)), "--fail-on-pressure", "--db-log", str(tmp_path / "absent.log")])
        != 0
    )


# -- Guards on the instrument itself ---------------------------------------


def test_the_calm_fixture_really_is_calm(tmp_path: Path) -> None:
    """Without this every assertion above could be passing for the wrong reason."""
    summary = report_activity.summarise(report_activity.load(_calm(tmp_path)))
    assert not summary.under_pressure
    assert summary.peak == 4


def test_the_pressured_fixture_really_is_pressured(tmp_path: Path) -> None:
    summary = report_activity.summarise(report_activity.load(_pressured(tmp_path)))
    assert summary.under_pressure
    assert summary.peak_fraction >= report_activity.PRESSURE_FRACTION
