#!/usr/bin/env python3
"""Benchmark raw pytest throughput on this machine against a fixed test sample.

Building the test database and running tests against it are different costs
that do not blend into one number - see pyproject.toml's ``[tool.mutmut]``
comment ("about three minutes against 3.5 seconds"). This script measures both
separately, on a deterministic, stride-sampled slice of
``src/urbanlens/dashboard/tests/hypothesis/`` (stride rather than a plain
alphabetical prefix, since files cluster by feature name - `test_billing_*`,
`test_boundary_*` - and a prefix slice would just benchmark one feature area):

1. a cold run against a freshly created, uniquely-named test database
   (``--reuse-db --create-db``, kept afterward instead of torn down)
2. a warm run of the same sample against that same database (``--reuse-db``)

``cold_seconds - warm_seconds`` approximates one-time database setup cost;
``tests_total / warm_seconds`` approximates steady-state throughput. Compare
either number across machines - this script does not try to average them into
a single "score".

Usage::

    python bin/benchmark_test_speed.py [--sample-size N] [--label NAME] [--keep-db]

Run it with whichever interpreter already has this project's dependencies
installed: the project venv directly on a local checkout, or the container's
venv via ``docker exec``/``docker compose run`` on a Docker-based environment.
Prints one JSON object to stdout summarizing the result; progress goes to
stderr so stdout stays parseable.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import time
import uuid

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "src" / "urbanlens" / "dashboard" / "tests" / "hypothesis"

_DROP_DB_SNIPPET = """
import sys
import django
django.setup()
from django.db import connection
name = sys.argv[1]
params = connection.get_connection_params()
params.pop("database", None)
params["dbname"] = "postgres"
maintenance = connection.Database.connect(**params)
try:
    maintenance.autocommit = True
    with maintenance.cursor() as cursor:
        cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", [name])
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
finally:
    maintenance.close()
"""

_SUMMARY_KEYS = ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "deselected")


def _sample_files(sample_size: int) -> list[Path]:
    """Return a stride-sampled, deterministic slice of the hypothesis test files."""
    files = sorted(SAMPLE_DIR.glob("test_*.py"))
    if sample_size >= len(files):
        return files
    stride = len(files) / sample_size
    return [files[int(i * stride)] for i in range(sample_size)]


def _slug(label: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", label).lower()


def _run_pytest(python: str, files: list[Path], db_name: str, extra_args: list[str]) -> tuple[float, subprocess.CompletedProcess[str]]:
    env = dict(os.environ)
    env["UL_TEST_DB_NAME"] = db_name
    # Always eager, on both machines: locally there is no reachable broker (see
    # local-non-docker-pytest-needs-celery-eager), and forcing it everywhere
    # keeps the two machines doing the same actual work instead of one
    # enqueuing-and-returning while the other runs the task inline.
    env["UL_CELERY_TASK_ALWAYS_EAGER"] = "True"
    args = [python, "-m", "pytest", *extra_args, *[str(f) for f in files]]
    start = time.monotonic()
    result = subprocess.run(args, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
    elapsed = time.monotonic() - start
    return elapsed, result


def _parse_summary(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in _SUMMARY_KEYS:
        match = re.search(rf"(\d+) {key}", output)
        if match:
            counts[key] = int(match.group(1))
    return counts


def _drop_database(python: str, db_name: str) -> None:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "urbanlens.UrbanLens.settings.test"
    try:
        subprocess.run(
            [python, "-c", _DROP_DB_SNIPPET, db_name],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except Exception as exc:
        print(f"warning: could not drop benchmark database {db_name!r}: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample-size", type=int, default=30, help="number of test files to sample (default: 30)")
    parser.add_argument("--label", default=socket.gethostname(), help="machine label for the JSON result (default: hostname)")
    parser.add_argument("--keep-db", action="store_true", help="skip dropping the benchmark database afterward")
    parser.add_argument("--out", type=Path, default=None, help="also append the JSON result as a line to this file")
    args = parser.parse_args()

    python = sys.executable
    all_files = sorted(SAMPLE_DIR.glob("test_*.py"))
    files = _sample_files(args.sample_size)
    if not files:
        print(f"error: no test files found under {SAMPLE_DIR}", file=sys.stderr)
        return 1

    db_name = f"bench_{_slug(args.label)}_{uuid.uuid4().hex[:8]}"
    common_args = ["-q", "-p", "no:randomly", "--no-header"]

    print(f"==> sampled {len(files)} of {len(all_files)} files, db={db_name}", file=sys.stderr)

    print("==> cold run (fresh database)", file=sys.stderr)
    cold_elapsed, cold_result = _run_pytest(python, files, db_name, [*common_args, "--reuse-db", "--create-db"])
    cold_counts = _parse_summary(cold_result.stdout + cold_result.stderr)
    print(f"    {cold_elapsed:.2f}s, {cold_counts}", file=sys.stderr)

    print("==> warm run (reused database)", file=sys.stderr)
    warm_elapsed, warm_result = _run_pytest(python, files, db_name, [*common_args, "--reuse-db"])
    warm_counts = _parse_summary(warm_result.stdout + warm_result.stderr)
    print(f"    {warm_elapsed:.2f}s, {warm_counts}", file=sys.stderr)

    if not args.keep_db:
        _drop_database(python, db_name)

    tests_total = sum(warm_counts.get(k, 0) for k in ("passed", "failed", "error", "xfailed", "xpassed"))
    result = {
        "label": args.label,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "python_version": sys.version.split()[0],
        "sample_files": len(files),
        "total_files": len(all_files),
        "cold_seconds": round(cold_elapsed, 2),
        "cold_counts": cold_counts,
        "warm_seconds": round(warm_elapsed, 2),
        "warm_counts": warm_counts,
        "tests_total": tests_total,
        "tests_per_second": round(tests_total / warm_elapsed, 2) if warm_elapsed and tests_total else None,
        "estimated_db_build_seconds": round(cold_elapsed - warm_elapsed, 2),
        "cold_exit_code": cold_result.returncode,
        "warm_exit_code": warm_result.returncode,
    }

    print(json.dumps(result, indent=2))
    if args.out:
        with args.out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result) + "\n")

    if warm_result.returncode not in (0, 1):  # 1 = some tests failed/errored, still a valid timing run
        print("warning: warm run exited abnormally (not just test failures) - timing may be unreliable", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
