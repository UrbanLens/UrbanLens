"""The capacity report attributes every number to the hold it happened in, or the table blames the wrong level."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

_MODULE_PATH = Path(__file__).resolve().parents[5] / "bin" / "perf" / "report_capacity.py"


def _load() -> Any:
    """Import the script by path, as `test_perf_budget.py` does."""
    spec = importlib.util.spec_from_file_location("report_capacity", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["report_capacity"] = module
    spec.loader.exec_module(module)
    return module


report_capacity = _load()

STARTED_MS = 1_800_000_000_000

STAGES_DOCUMENT = {
    "started_at_ms": STARTED_MS,
    "stages": [
        {"name": "ramp_100", "kind": "ramp", "users": 100, "start": 0, "seconds": 60},
        {"name": "u100", "kind": "hold", "users": 100, "start": 60, "seconds": 60},
        {"name": "ramp_200", "kind": "ramp", "users": 200, "start": 120, "seconds": 60},
        {"name": "u200", "kind": "hold", "users": 200, "start": 180, "seconds": 60},
        {"name": "drain", "kind": "drain", "users": 0, "start": 240, "seconds": 30},
    ],
    "budgets": {"page": 1000, "fragment": 500, "bulk": None},
    "endpoints": {"map_view": "page", "map_search": "fragment", "map_document": "bulk"},
}


def _stages(tmp_path: Path, document: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "stages.json"
    path.write_text(json.dumps(document or STAGES_DOCUMENT), encoding="utf-8")
    return path


def _trend(p95: float, count: int = 10) -> dict[str, Any]:
    return {"values": {"count": count, "med": p95 / 2, "p(95)": p95, "p(99)": p95 * 1.2, "max": p95 * 2}}


def test_a_metric_key_splits_into_name_and_tags() -> None:
    name, tags = report_capacity.parse_metric_key("http_req_duration{endpoint:map_view,stage:u100}")

    assert name == "http_req_duration"
    assert tags == {"endpoint": "map_view", "stage": "u100"}
    assert report_capacity.parse_metric_key("page_views") == ("page_views", {})


def test_stages_are_placed_on_the_wall_clock(tmp_path: Path) -> None:
    stages, _ = report_capacity.load_stages(_stages(tmp_path))

    assert report_capacity.stage_for(STARTED_MS + 60_000, stages).name == "u100"
    assert report_capacity.stage_for(STARTED_MS + 119_999, stages).name == "u100"
    assert report_capacity.stage_for(STARTED_MS + 120_000, stages).name == "ramp_200"
    assert report_capacity.stage_for(STARTED_MS - 1, stages) is None


def test_a_run_that_never_started_is_refused(tmp_path: Path) -> None:
    document = dict(STAGES_DOCUMENT, started_at_ms=None)

    try:
        report_capacity.load_stages(_stages(tmp_path, document))
    except ValueError:
        return
    raise AssertionError("a stages document without a start time was accepted")


def test_only_asserted_classes_are_judged_against_a_budget() -> None:
    summary = {
        "metrics": {
            "http_req_duration{endpoint:map_view,stage:u100}": _trend(1500),
            "http_req_duration{endpoint:map_search,stage:u100}": _trend(100),
            "http_req_duration{endpoint:map_document,stage:u100}": _trend(90_000),
            "http_req_duration{endpoint:map_view,stage:u200}": _trend(0, count=0),
        },
    }

    rows = {(row["stage"], row["endpoint"]): row for row in report_capacity.endpoint_rows(summary, STAGES_DOCUMENT)}

    assert rows["u100", "map_view"]["over"] is True
    assert rows["u100", "map_search"]["over"] is False
    assert rows["u100", "map_document"]["over"] is False
    assert ("u200", "map_view") not in rows, "an endpoint with no requests in a hold is not a row"


def test_container_cores_come_from_counter_deltas_inside_holds(tmp_path: Path) -> None:
    csv_path = tmp_path / "containers.csv"
    header = "epoch_ms,container,usage_usec,throttled_usec,nr_throttled,memory_bytes,cpu_max"
    rows = [
        # ramp: ignored
        f"{STARTED_MS + 10_000},ul_perf_app,0,0,0,100,200000/100000",
        f"{STARTED_MS + 62_000},ul_perf_app,1000000,0,0,100,200000/100000",
        # two seconds in the hold at 1.5 cores, a quarter of it throttled
        f"{STARTED_MS + 64_000},ul_perf_app,4000000,500000,3,1048576000,200000/100000",
        # counter reset: a recreated container, dropped
        f"{STARTED_MS + 66_000},ul_perf_app,10,0,0,100,200000/100000",
    ]
    csv_path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    stages, _ = report_capacity.load_stages(_stages(tmp_path))

    result = report_capacity.container_rows(csv_path, stages)

    assert len(result) == 1
    row = result[0]
    assert row["stage"] == "u100"
    assert round(row["mean_cores"], 2) == 1.5
    assert round(row["throttled_share"], 2) == 0.25
    assert round(row["peak_memory_mb"]) == 1000


def test_proxy_errors_are_counted_per_hold_and_404_is_not_an_error(tmp_path: Path) -> None:
    def line(offset_seconds: int, status: int, rt: str = "0.100") -> str:
        from datetime import UTC, datetime

        when = datetime.fromtimestamp((STARTED_MS / 1000) + offset_seconds, tz=UTC).strftime("%d/%b/%Y:%H:%M:%S +0000")
        return (
            f'10.0.0.1 - - [{when}] "GET /dashboard/map/ HTTP/1.1" {status} 512 "-" "k6" "10.0.0.1" '
            f"rt={rt} urt={rt} ust={status} uct=0.001 up=172.18.0.5:8000"
        )

    log = tmp_path / "nginx.log"
    log.write_text(
        "\n".join(
            [line(70, 200), line(71, 502, "2.000"), line(72, 404), line(190, 504, "120.000"), "not a request line"]
        )
        + "\n",
        encoding="utf-8",
    )
    stages, _ = report_capacity.load_stages(_stages(tmp_path))

    rows = {row["stage"]: row for row in report_capacity.nginx_rows(log, stages)}

    assert rows["u100"]["requests"] == 3
    assert rows["u100"]["errors"] == {"502": 1}
    assert rows["u200"]["errors"] == {"504": 1}


def test_the_report_marks_an_endpoint_over_budget(tmp_path: Path) -> None:
    summary = {
        "metrics": {
            "http_req_duration{endpoint:map_view,stage:u100}": _trend(1500),
            "http_req_failed{stage:u100}": {"values": {"rate": 0.01}},
            "page_views{journey:map,stage:u100}": {"values": {"count": 42}},
        },
    }

    text = report_capacity.render(summary, STAGES_DOCUMENT, [], [])

    assert "**OVER**" in text
    assert "| u100 | 100 | 1.00% | - | 42 |" in text


def test_a_rate_with_no_samples_is_no_data_rather_than_zero() -> None:
    """k6 reports an empty sub-metric as rate 0 with its threshold passed."""
    summary = {
        "metrics": {
            "http_req_failed{stage:u100}": {"values": {"rate": 0.0, "passes": 0, "fails": 120}},
            "ws_handshake_ok{stage:u100}": {"values": {"rate": 0.0, "passes": 0, "fails": 0}},
        },
    }

    text = report_capacity.render(summary, STAGES_DOCUMENT, [], [])

    assert "| u100 | 100 | 0.00% | - |" in text
