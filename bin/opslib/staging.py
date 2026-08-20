"""The whole "deploy staging and prove it works" process, as one callable run.

Existing pieces answered part of this: ``deploy.sh`` pulls and restarts,
``clone_prod_to_staging.sh`` copies the data, and ``deploy_webhook.py`` fires
the first on a push. What none of them answered is the question a caller
actually has - *is staging now up, carrying prod's data, running the new code,
and passing its tests?* - so verifying it meant a human reading logs.

This runs the sequence and reports per-step status, so a caller (or an agent)
can act on one JSON answer and only go digging when something fails.

Ordering is deliberate. Code is pulled *before* the data clone so a restore
runs against the migrations it was dumped for, and the health wait sits between
the restart and every assertion, because a check that races the container it is
checking reports on the previous deployment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Any
import urllib.error
import urllib.request

from .steps import new_run

if TYPE_CHECKING:
    from .steps import Run

#: Tables compared before and after the clone. Not an exhaustive list - the
#: point is to notice a restore that silently produced an empty or partial
#: database, which shows up in the big tables first.
#:
#: ``dashboard_user_pins``, not ``dashboard_pins``: ``Pin.Meta.db_table`` names
#: the former and the latter exists nowhere. A name that matches no table counts
#: -1, which the comparison below then skipped, so the single most important
#: table in this list was never actually compared while the report said five
#: tables matched.
_VERIFIED_TABLES = ("dashboard_user_pins", "dashboard_locations", "auth_user", "dashboard_profiles", "dashboard_images")

#: Pages that must answer before staging is called up. Deliberately includes an
#: authenticated-only path: a 302 to login proves routing and middleware work,
#: where a 200 on the landing page alone can be served by a half-booted app.
_SMOKE_PATHS = (("/", (200,)), ("/dashboard/map/", (200, 302)), ("/dashboard/site-admin/", (200, 302, 403)))


def _db_container(env_file: Path) -> str:
    """The Postgres container name a checkout's ``.env`` produces.

    Mirrors docker-compose.yml's own
    ``urbanlens_${UL_CONTAINER_NAME:-${UL_ENVIRONMENT:-production}}_db``. Derived
    in one place because getting it wrong is silent: a name that matches no
    running container makes every table count -1, and the data-preservation
    check below reads that as nothing to complain about. Both sides of that
    comparison used ``UL_ENVIRONMENT`` alone, so any deployment setting
    ``UL_CONTAINER_NAME`` - which is the reason the variable exists - passed the
    check vacuously.

    Args:
        env_file: The checkout's ``.env``.

    Returns:
        The container name.
    """
    environment = _read_env_var(env_file, "UL_ENVIRONMENT", "production")
    return f"urbanlens_{_read_env_var(env_file, 'UL_CONTAINER_NAME', environment)}_db"


def _read_env_var(env_file: Path, key: str, default: str = "") -> str:
    """Read one KEY=value from a .env file without sourcing it.

    Args:
        env_file: Path to the .env file.
        key: Variable name.
        default: Returned when absent.

    Returns:
        The value, quotes stripped.
    """
    if not env_file.is_file():
        return default
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return default


def _table_counts(container: str, db_user: str, db_name: str, tables: tuple[str, ...]) -> dict[str, int]:
    """Row counts for ``tables`` in one Postgres container.

    A missing table reports -1 rather than raising: the comparison is meant to
    survive a schema that has moved on, and "this table does not exist on one
    side" is itself worth seeing in the report.

    Args:
        container: Docker container name running Postgres.
        db_user: Database user.
        db_name: Database name.
        tables: Table names to count.

    Returns:
        Mapping of table name to row count (-1 when unavailable).
    """
    import re
    import subprocess

    counts: dict[str, int] = {}
    for table in tables:
        # psql has no bind parameters for an identifier, so the table name is
        # interpolated - which is only safe because nothing but a plain
        # identifier is allowed through. The list is a module constant today;
        # this keeps that from becoming load-bearing if a caller ever passes
        # something else.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            counts[table] = -1
            continue
        try:
            completed = subprocess.run(

                # cannot see: `table` is already proven to match
                # [A-Za-z_][A-Za-z0-9_]* by this point.
                ["docker", "exec", container, "psql", "-U", db_user, "-d", db_name, "-tAc", f"SELECT count(*) FROM {table}"],  # noqa: S608
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            counts[table] = int(completed.stdout.strip()) if completed.returncode == 0 and completed.stdout.strip().isdigit() else -1
        except (OSError, ValueError, subprocess.TimeoutExpired):
            counts[table] = -1
    return counts


def _http_status(url: str, timeout: int = 10) -> int:
    """GET a URL and return its status, following no redirects.

    Args:
        url: Absolute URL.
        timeout: Seconds.

    Returns:
        The HTTP status code.

    Raises:
        urllib.error.URLError: The host could not be reached.
    """

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def run_staging_pipeline(
    *,
    checkout: Path,
    branch: str,
    prod_dir: Path | None,
    run_dir: Path,
    skip_data: bool = False,
    skip_tests: bool = False,
    allow_dirty: bool = False,
    run_id: str = "",
    test_selection: str = "not slow",
) -> dict[str, Any]:
    """Deploy staging from ``branch`` and verify the result.

    Args:
        checkout: The staging checkout to deploy.
        branch: Git ref to deploy.
        prod_dir: Production checkout to copy data from; None skips the clone.
        run_dir: Where to write the run's log and JSON record.
        skip_data: Skip the prod data clone even when ``prod_dir`` is given.
        skip_tests: Skip the in-container integration tests.
        allow_dirty: Deploy even when the checkout has uncommitted changes,
            which ``git reset --hard`` will discard.
        test_selection: ``-k`` expression for the integration test step.
        run_id: Use this run id rather than generating one, so a caller that
            has already returned an id to somebody stays consistent with it.

    Returns:
        The run record: overall status plus one entry per step.
    """
    run = new_run("staging-deploy", run_dir, run_id=run_id)
    run.context = {"checkout": str(checkout), "branch": branch, "prod_dir": str(prod_dir) if prod_dir else None}

    env_file = checkout / ".env"
    environment = _read_env_var(env_file, "UL_ENVIRONMENT", "unknown")
    site_url = _read_env_var(env_file, "UL_SITE_URL", "").rstrip("/")
    app_port = _read_env_var(env_file, "UL_APP_PORT", "")
    container_name = _read_env_var(env_file, "UL_CONTAINER_NAME", environment)

    # Refuse to run against production. This tool restarts containers and can
    # overwrite a database; the one thing it must never do is do that to prod
    # because an argument pointed at the wrong checkout.
    if environment == "production":
        run.record("preflight", "failed", f"{checkout} is a production checkout - refusing")
        return run.finish()
    # `git reset --hard` below discards anything uncommitted. On a deploy
    # target that is the intent; pointed at a working checkout by mistake it
    # destroys someone's in-progress work, and the argument that does that is a
    # single wrong --checkout.
    import subprocess

    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=checkout, capture_output=True, text=True, check=False).stdout.strip()
    if dirty and not allow_dirty:
        run.record("preflight", "failed", f"{checkout} has uncommitted changes; refusing to reset --hard over them (pass allow_dirty to override)")
        return run.finish()

    run.record("preflight", "ok", f"environment={environment} container={container_name}")

    run.run_step("pull", ["git", "fetch", "origin", branch], cwd=checkout)
    run.run_step("checkout", ["git", "reset", "--hard", f"origin/{branch}"], cwd=checkout)
    head = run.run_step("revision", ["git", "rev-parse", "--short", "HEAD"], cwd=checkout)
    run.context["deployed_sha"] = head.output_tail.strip()

    prod_counts: dict[str, int] = {}
    if prod_dir and not skip_data:
        prod_env = prod_dir / ".env"
        prod_counts = _table_counts(
            _db_container(prod_env),
            _read_env_var(prod_env, "UL_DB_USER", "postgres"),
            _read_env_var(prod_env, "UL_DB_NAME", "postgres"),
            _VERIFIED_TABLES,
        )
        run.context["prod_counts"] = prod_counts
        run.run_step(
            "clone-prod-data",
            ["bash", str(checkout / "bin" / "clone_prod_to_staging.sh"), "--yes", "--prod-dir", str(prod_dir)],
            cwd=checkout,
            timeout=3600,
        )
        # The clone script leaves its dump behind on some paths (a known
        # defect); a production dump sitting in a checkout is worth removing
        # whether or not the clone succeeded.
        removed = [str(path) for path in checkout.glob("prod_to_staging_*.dump")]
        for path in removed:
            with_suppress_missing(Path(path))
        run.record("remove-dump", "ok", f"removed {len(removed)} production dump(s) from the checkout")
    else:
        run.record("clone-prod-data", "skipped", "no prod dir given" if not prod_dir else "--skip-data")

    run.run_step("compose-up", ["docker", "compose", "up", "--build", "-d"], cwd=checkout, timeout=3600)

    app_container = f"urbanlens_{container_name}_app"
    base_url = site_url or (f"http://127.0.0.1:{app_port}" if app_port else "")
    if run.steps[-1].status == "failed":
        # Same reasoning as the dev pipeline: a health wait after a failed
        # start burns the timeout to reach a conclusion already known.
        run.record("wait-healthy", "skipped", "compose-up failed; not waiting on a stack that did not start")
        # See the note in devenv: compose's own warnings crowd out the app's
        # traceback, so the container is asked directly.
        run.run_step("capture-app-log", ["docker", "logs", "--tail", "60", app_container], cwd=checkout, timeout=180, check=False)
    elif base_url:
        run.run_check("wait-healthy", lambda: _healthy(base_url), timeout=600, interval=5)
    else:
        run.record("wait-healthy", "failed", "neither UL_SITE_URL nor UL_APP_PORT is set in .env")

    run.run_step(
        "migrations-applied",
        ["docker", "exec", app_container, "/app/.venv/bin/python", "src/urbanlens/manage.py", "migrate", "--check"],
        cwd=checkout,
        timeout=600,
    )

    if prod_counts:
        staging_counts = _table_counts(
            _db_container(env_file),
            _read_env_var(env_file, "UL_DB_USER", "postgres"),
            _read_env_var(env_file, "UL_DB_NAME", "postgres"),
            _VERIFIED_TABLES,
        )
        run.context["staging_counts"] = staging_counts
        # A -1 on either side means the count could not be taken - a missing
        # table, or a container name that matched nothing. That is a check that
        # did not run, and reporting it as a pass is the failure mode this whole
        # step exists to catch, so it is a shortfall in its own right.
        uncountable = [f"{table}: prod={prod_counts[table]} staging={staging_counts.get(table, -1)} (not counted)" for table in _VERIFIED_TABLES if prod_counts.get(table, -1) < 0 or staging_counts.get(table, -1) < 0]
        compared = [table for table in _VERIFIED_TABLES if prod_counts.get(table, -1) >= 0 and staging_counts.get(table, -1) >= 0]
        shortfalls = [f"{table}: prod={prod_counts[table]} staging={staging_counts[table]}" for table in compared if prod_counts[table] > 0 and staging_counts[table] < prod_counts[table]]
        problems = uncountable + shortfalls
        # `compared`, not `len(prod_counts)`: the old summary counted the tables
        # it had *asked* about, so a run that compared none of them still said
        # five matched.
        run.record("data-preserved", "failed" if problems else "ok", "; ".join(problems) or f"{len(compared)} of {len(_VERIFIED_TABLES)} tables match or exceed prod")
    else:
        run.record("data-preserved", "skipped", "no clone performed")

    if base_url:
        failures = []
        for path, allowed in _SMOKE_PATHS:
            try:
                status = _http_status(f"{base_url}{path}")
            except OSError as exc:
                failures.append(f"{path}: {exc}")
                continue
            if status not in allowed:
                failures.append(f"{path}: {status}")
        run.record("smoke-pages", "failed" if failures else "ok", "; ".join(failures) or f"{len(_SMOKE_PATHS)} paths answered")

    if skip_tests:
        run.record("integration-tests", "skipped", "--skip-tests")
    else:
        run.run_step(
            "integration-tests",
            ["docker", "exec", "-e", "UL_TEST_DB_NAME=staging_verify", app_container, "/app/.venv/bin/python", "-m", "pytest", "src/urbanlens", "-q", "-k", test_selection, "--create-db"],
            cwd=checkout,
            timeout=5400,
        )

    return run.finish()


def with_suppress_missing(path: Path) -> None:
    """Delete a file, ignoring one that is already gone."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def _healthy(base_url: str) -> tuple[bool, str]:
    """Whether the site answers a request.

    Args:
        base_url: Scheme and host of the deployment.

    Returns:
        ``(passed, detail)`` for :meth:`Run.run_check`.
    """
    status = _http_status(f"{base_url}/", timeout=10)
    return (status < 500, f"GET / -> {status}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments, or None for ``sys.argv``.

    Returns:
        Process exit code: 0 when every step passed.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Deploy staging and verify it works.")
    parser.add_argument("--checkout", default=os.getenv("UL_STAGING_DIR", "."), help="Staging checkout to deploy.")
    parser.add_argument("--branch", default=os.getenv("UL_DEPLOY_WEBHOOK_BRANCH", "main"), help="Git ref to deploy.")
    parser.add_argument("--prod-dir", default=os.getenv("UL_PROD_DIR"), help="Production checkout to copy data from.")
    parser.add_argument("--run-dir", default=os.getenv("UL_OPS_RUN_DIR", ""), help="Where run logs and records are written. Default: <checkout>/.ops-runs")
    parser.add_argument("--skip-data", action="store_true", help="Do not copy production data.")
    parser.add_argument("--skip-tests", action="store_true", help="Do not run the integration tests.")
    parser.add_argument("--allow-dirty", action="store_true", help="Deploy over uncommitted changes in the checkout (they are discarded).")
    parser.add_argument("--tests", default="not slow", help="pytest -k selection for the integration step.")
    args = parser.parse_args(argv)

    checkout = Path(args.checkout).resolve()
    # Inside the checkout rather than a shared temp directory: these records
    # name production tables and row counts, and a predictable path under a
    # world-writable /tmp is the wrong home for that.
    run_dir = Path(args.run_dir).resolve() if args.run_dir else checkout / ".ops-runs"

    record = run_staging_pipeline(
        checkout=checkout,
        branch=args.branch,
        prod_dir=Path(args.prod_dir).resolve() if args.prod_dir else None,
        run_dir=run_dir,
        skip_data=args.skip_data,
        skip_tests=args.skip_tests,
        allow_dirty=args.allow_dirty,
        test_selection=args.tests,
    )
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
