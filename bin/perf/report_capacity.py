#!/usr/bin/env python3
"""Turn a capacity run's k6 summary, container samples and proxy log into one table per hold."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

_METRIC_KEY = re.compile(r"^(?P<metric>[a-z_]+)\{(?P<tags>[^}]*)\}$")

_NGINX_LINE = re.compile(
    r"\[(?P<time>[^\]]+)\] \"(?P<method>[A-Z]+) (?P<path>\S+)[^\"]*\" (?P<status>\d{3}) .*?"
    r"rt=(?P<rt>[\d.]+) urt=(?P<urt>\S+) ust=(?P<ust>\S+) uct=(?P<uct>\S+)",
)


@dataclass(frozen=True, slots=True)
class Stage:
    """One ramp, hold or drain, placed on the wall clock."""

    name: str
    kind: str
    users: int
    start_ms: int
    end_ms: int

    def covers(self, epoch_ms: float) -> bool:
        """Whether *epoch_ms* falls inside this stage."""
        return self.start_ms <= epoch_ms < self.end_ms


def parse_metric_key(key: str) -> tuple[str, dict[str, str]]:
    """``http_req_duration{endpoint:map_view,stage:u100}`` as its name and tags.

    Args:
        key: A k6 summary metric key.

    Returns:
        The metric name, and its tags; no tags for a plain metric.
    """
    match = _METRIC_KEY.match(key)
    if not match:
        return key, {}
    tags = dict(part.split(":", 1) for part in match.group("tags").split(",") if ":" in part)
    return match.group("metric"), tags


def load_stages(path: Path) -> tuple[list[Stage], dict[str, Any]]:
    """The run's stages on the wall clock, and the budgets and endpoint classes it was judged under.

    Args:
        path: The ``stages.json`` the scenario writes.

    Returns:
        The stages, and the whole document.

    Raises:
        ValueError: The run recorded no start time, so nothing sampled can be placed in a stage.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    started = document.get("started_at_ms")
    if started is None:
        raise ValueError(f"{path} records no start time; setup() did not finish.")
    stages = [Stage(name=stage["name"], kind=stage["kind"], users=stage["users"], start_ms=started + stage["start"] * 1000, end_ms=started + (stage["start"] + stage["seconds"]) * 1000) for stage in document["stages"]]
    return stages, document


def stage_for(epoch_ms: float, stages: list[Stage]) -> Stage | None:
    """The stage covering *epoch_ms*, if any."""
    return next((stage for stage in stages if stage.covers(epoch_ms)), None)


def endpoint_rows(summary: dict[str, Any], stages_document: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per endpoint per hold, with its budget verdict.

    Args:
        summary: The k6 ``handleSummary`` document.
        stages_document: The ``stages.json`` document.

    Returns:
        Rows in hold order, then endpoint order.
    """
    budgets = stages_document.get("budgets", {})
    classes = stages_document.get("endpoints", {})
    order = {stage["name"]: index for index, stage in enumerate(stages_document["stages"])}
    rows = []
    for key, metric in summary.get("metrics", {}).items():
        name, tags = parse_metric_key(key)
        if name != "http_req_duration" or "endpoint" not in tags or "stage" not in tags:
            continue
        values = metric.get("values", {})
        if not values.get("count"):
            continue
        budget_class = classes.get(tags["endpoint"], "")
        budget = budgets.get(budget_class)
        p95 = values.get("p(95)", 0.0)
        rows.append(
            {
                "stage": tags["stage"],
                "endpoint": tags["endpoint"],
                "class": budget_class,
                "count": int(values["count"]),
                "p50": values.get("med", 0.0),
                "p95": p95,
                "p99": values.get("p(99)", 0.0),
                "max": values.get("max", 0.0),
                "budget": budget,
                "over": bool(budget) and p95 >= budget,
            },
        )
    rows.sort(key=lambda row: (order.get(row["stage"], math.inf), row["class"], -row["p95"]))
    return rows


def container_rows(path: Path, stages: list[Stage]) -> list[dict[str, Any]]:
    """Mean and peak cores, throttled share and peak memory, per container per hold.

    Consecutive samples of one container become one interval, counted only when it starts and ends inside the same
    hold: one that spans a ramp would average the ramp's load into the hold's. A counter that goes backwards is a
    recreated container and its interval is dropped.

    Args:
        path: The ``container_sampler.sh`` CSV.
        stages: The run's stages.

    Returns:
        Rows in hold order, busiest container first.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        samples = list(csv.DictReader(handle))
    previous: dict[str, dict[str, str]] = {}
    totals: dict[tuple[str, str], dict[str, float]] = {}
    for sample in samples:
        name = sample["container"]
        before = previous.get(name)
        previous[name] = sample
        if before is None:
            continue
        wall_usec = (int(sample["epoch_ms"]) - int(before["epoch_ms"])) * 1000
        used = int(sample["usage_usec"]) - int(before["usage_usec"])
        throttled = int(sample["throttled_usec"]) - int(before["throttled_usec"])
        if wall_usec <= 0 or used < 0 or throttled < 0:
            continue
        stage = stage_for(int(sample["epoch_ms"]), stages)
        if stage is None or stage.kind != "hold" or not stage.covers(int(before["epoch_ms"])):
            continue
        entry = totals.setdefault((stage.name, name), {"used": 0.0, "wall": 0.0, "peak": 0.0, "throttled": 0.0, "memory": 0.0})
        entry["used"] += used
        entry["wall"] += wall_usec
        entry["throttled"] += throttled
        entry["peak"] = max(entry["peak"], used / wall_usec)
        entry["memory"] = max(entry["memory"], int(sample["memory_bytes"]))
    order = {stage.name: index for index, stage in enumerate(stages)}
    rows = [
        {
            "stage": stage_name,
            "container": container,
            "mean_cores": entry["used"] / entry["wall"],
            "peak_cores": entry["peak"],
            "throttled_share": entry["throttled"] / entry["wall"],
            "peak_memory_mb": entry["memory"] / 1_048_576,
        }
        for (stage_name, container), entry in totals.items()
    ]
    rows.sort(key=lambda row: (order.get(row["stage"], math.inf), -row["mean_cores"]))
    return rows


def nginx_rows(path: Path, stages: list[Stage]) -> list[dict[str, Any]]:
    """What the proxy saw per hold: requests, errors by status, and its own p95.

    Args:
        path: The proxy's ``docker logs`` for the run.
        stages: The run's stages.

    Returns:
        One row per hold that saw a logged request. Successful header polls are not logged by design, so counts
        here are lower than k6's.
    """
    buckets: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _NGINX_LINE.search(line)
            if not match:
                continue
            try:
                when = datetime.strptime(match.group("time"), "%d/%b/%Y:%H:%M:%S %z").timestamp() * 1000
            except ValueError:
                continue
            stage = stage_for(when, stages)
            if stage is None or stage.kind != "hold":
                continue
            bucket = buckets.setdefault(stage.name, {"stage": stage.name, "requests": 0, "errors": {}, "times": []})
            bucket["requests"] += 1
            status = match.group("status")
            if status[0] in "45" and status != "404":
                bucket["errors"][status] = bucket["errors"].get(status, 0) + 1
            bucket["times"].append(float(match.group("rt")))
    order = {stage.name: index for index, stage in enumerate(stages)}
    rows = []
    for bucket in sorted(buckets.values(), key=lambda item: order.get(item["stage"], math.inf)):
        times = sorted(bucket.pop("times"))
        bucket["p95_seconds"] = times[min(len(times) - 1, math.ceil(0.95 * len(times)) - 1)] if times else 0.0
        rows.append(bucket)
    return rows


def render(summary: dict[str, Any], stages_document: dict[str, Any], containers: list[dict[str, Any]], proxy: list[dict[str, Any]]) -> str:
    """The report, as Markdown.

    Args:
        summary: The k6 summary.
        stages_document: The ``stages.json`` document.
        containers: From `container_rows`.
        proxy: From `nginx_rows`.

    Returns:
        Markdown text.
    """
    lines = ["# Capacity run", ""]
    metrics = summary.get("metrics", {})
    lines += ["| hold | users | requests failed | ws handshakes ok | page views |", "|---|---:|---:|---:|---:|"]
    for stage in stages_document["stages"]:
        if stage["kind"] != "hold":
            continue
        failed = metrics.get(f"http_req_failed{{stage:{stage['name']}}}", {}).get("values", {}).get("rate")
        socket = metrics.get(f"ws_handshake_ok{{stage:{stage['name']}}}", {}).get("values", {}).get("rate")
        views = sum(int(metric.get("values", {}).get("count", 0)) for key, metric in metrics.items() if parse_metric_key(key)[0] == "page_views" and parse_metric_key(key)[1].get("stage") == stage["name"])
        lines.append(f"| {stage['name']} | {stage['users']} | {_percent(failed)} | {_percent(socket)} | {views or '-'} |")

    lines += ["", "## Endpoints", "", "| hold | endpoint | class | count | p50 ms | p95 ms | p99 ms | max ms | budget |", "|---|---|---|---:|---:|---:|---:|---:|---|"]
    for row in endpoint_rows(summary, stages_document):
        verdict = "-" if not row["budget"] else ("**OVER**" if row["over"] else f"< {row['budget']}")
        lines.append(
            f"| {row['stage']} | {row['endpoint']} | {row['class']} | {row['count']} | {row['p50']:.0f} | {row['p95']:.0f} | {row['p99']:.0f} | {row['max']:.0f} | {verdict} |",
        )

    if containers:
        lines += ["", "## Containers", "", "| hold | container | mean cores | peak cores | throttled | peak memory MB |", "|---|---|---:|---:|---:|---:|"]
        lines += [f"| {row['stage']} | {row['container']} | {row['mean_cores']:.2f} | {row['peak_cores']:.2f} | {_percent(row['throttled_share'])} | {row['peak_memory_mb']:.0f} |" for row in containers if row["mean_cores"] >= 0.01]

    if proxy:
        lines += ["", "## Proxy", "", "| hold | logged requests | errors | p95 s |", "|---|---:|---|---:|"]
        for row in proxy:
            errors = ", ".join(f"{status}: {count}" for status, count in sorted(row["errors"].items())) or "none"
            lines.append(f"| {row['stage']} | {row['requests']} | {errors} | {row['p95_seconds']:.3f} |")
    lines.append("")
    return "\n".join(lines)


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def main(argv: list[str] | None = None) -> int:
    """Print the report, and write it where asked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True, help="k6 handleSummary JSON.")
    parser.add_argument("--stages", type=Path, required=True, help="stages.json from the scenario.")
    parser.add_argument("--containers", type=Path, help="container_sampler.sh CSV.")
    parser.add_argument("--nginx-log", type=Path, help="The proxy's docker logs for the run.")
    parser.add_argument("--out", type=Path, help="Also write the Markdown here.")
    args = parser.parse_args(argv)

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    stages, document = load_stages(args.stages)
    containers = container_rows(args.containers, stages) if args.containers else []
    proxy = nginx_rows(args.nginx_log, stages) if args.nginx_log else []
    report = render(summary, document, containers, proxy)
    sys.stdout.write(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
