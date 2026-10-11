"""Gate, parametrize and report the live-locations suite.

No ``live_check`` test runs unless ``UL_LIVE_LOCATIONS=1`` and a dedicated REData is named by
``UL_LIVE_REDATA_API_URL``/``UL_LIVE_REDATA_API_KEY``. CI sets ``UL_REDATA_API_URL`` to a
placeholder, so the ordinary settings are never read as "REData is available". A ``live_source``
test asks a public source directly, so it needs only ``UL_LIVE_LOCATIONS=1``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Any

import pytest

# The suite's modules share `live_sites` by plain import. pytest runs with
# `--import-mode=importlib`, which puts nothing on the path, so this directory
# adds itself before anything imports from it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_sites import InconclusiveError, LiveRedata, Site, load_sites, select_sites, settled  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

_RESULTS: dict[str, dict[str, dict[str, Any]]] = {}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("live-locations")
    group.addoption(
        "--live-sites",
        default=os.environ.get("UL_LIVE_SITES", "primary"),
        help="primary (default), all, or comma-separated keys from kirkbrides.toml",
    )
    group.addoption(
        "--live-report",
        default=os.environ.get("UL_LIVE_REPORT", ""),
        help="Write a JSON report of every check and REData call here",
    )


def _enabled() -> bool:
    return os.environ.get("UL_LIVE_LOCATIONS", "").strip() == "1"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "site" not in metafunc.fixturenames:
        return
    sites = select_sites(load_sites(), metafunc.config.getoption("--live-sites"))
    metafunc.parametrize("site", sites, ids=[site.key for site in sites], scope="session")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    reason = ""
    if not _enabled():
        reason = "live-locations suite: set UL_LIVE_LOCATIONS=1 (see docs/LOCATION_DATA_TESTS.md)"
    elif LiveRedata.from_env() is None:
        reason = "live-locations suite: set UL_LIVE_REDATA_API_URL and UL_LIVE_REDATA_API_KEY"
    for item in (item for item in items if item.get_closest_marker("live_source")):
        item.add_marker(pytest.mark.live_locations)
        if not _enabled():
            item.add_marker(
                pytest.mark.skip(
                    reason="live-locations suite: set UL_LIVE_LOCATIONS=1 (see docs/LOCATION_DATA_TESTS.md)"
                )
            )
    # Only the checks themselves: the hook sees every item in the session, and the suite's own unit tests need no REData.
    for item in (item for item in items if item.get_closest_marker("live_check")):
        item.add_marker(pytest.mark.live_locations)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))
            continue
        site = _site(item)
        check = _check_name(item)
        if site is not None and check in site.known_issues:
            item.add_marker(pytest.mark.xfail(reason=f"{site.key} {check}: {site.known_issues[check]}", strict=True))


@pytest.fixture(scope="session")
def redata() -> Iterator[LiveRedata]:
    client = LiveRedata.from_env()
    if client is None:
        pytest.skip("UL_LIVE_REDATA_API_URL/UL_LIVE_REDATA_API_KEY are not set")
    if not client.reachable():
        pytest.fail(
            f"REData at {client.base_url} did not answer capabilities/ - check the URL, key and UL_LIVE_REDATA_HOST"
        )
    yield client
    path = os.environ.get("UL_LIVE_REPORT", "")
    if path:
        Path(path).write_text(json.dumps({"checks": settled(_RESULTS), "calls": client.log}, indent=1, default=str))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    site = _site(item)
    if site is not None and (report.when == "call" or report.outcome != "passed"):
        inconclusive = call.excinfo is not None and call.excinfo.errisinstance(InconclusiveError)
        _RESULTS.setdefault(site.key, {})[_check_name(item) or item.name] = {
            "outcome": "inconclusive" if inconclusive else report.outcome,
            "detail": str(call.excinfo.value)[:300] if call.excinfo else _notes(report),
        }
    return report


def _notes(report: pytest.TestReport) -> str:
    """What a passing check recorded about how it passed (``record_property``)."""
    return "; ".join(f"{name}: {value}" for name, value in report.user_properties)[:300]


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record each subtest of a check under its own name, which the check's own report does not carry."""
    context = getattr(report, "context", None)
    match = re.search(r"\[([^\]]+)\]$", report.nodeid)
    if context is None or match is None or report.when != "call" or match.group(1) not in _RESULTS:
        return
    detail = str(report.longrepr)[-300:] if report.failed else ""
    if report.skipped and isinstance(report.longrepr, tuple):
        detail = str(report.longrepr[2])[:300]
    outcome = "known issue" if report.skipped else report.outcome
    _RESULTS[match.group(1)][f"pipeline: {getattr(context, 'msg', '')}"] = {"outcome": outcome, "detail": detail}


def _site(item: pytest.Item) -> Site | None:
    callspec = getattr(item, "callspec", None)
    site = callspec.params.get("site") if callspec is not None else None
    return site if isinstance(site, Site) else None


def _check_name(item: pytest.Item) -> str:
    marker = item.get_closest_marker("live_check")
    return str(marker.args[0]) if marker and marker.args else ""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live_locations: checks real REData answers for known places (tests/live_locations)"
    )
    config.addinivalue_line(
        "markers", "live_check(name): the kirkbrides.toml known_issues key and report name of a live-locations check"
    )
    config.addinivalue_line(
        "markers", "live_source: asks a public source directly, without REData (tests/live_locations)"
    )
    if path := config.getoption("--live-report", default=""):
        os.environ["UL_LIVE_REPORT"] = path
