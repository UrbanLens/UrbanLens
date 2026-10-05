"""Gate, parametrize and report the live-locations suite.

No ``live_check`` test runs unless ``UL_LIVE_LOCATIONS=1`` and a dedicated REData is named by
``UL_LIVE_REDATA_API_URL``/``UL_LIVE_REDATA_API_KEY``. CI sets ``UL_REDATA_API_URL`` to a
placeholder, so the ordinary settings are never read as "REData is available".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from live_sites import InconclusiveError, LiveRedata, Site, load_sites, select_sites
import pytest

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
        Path(path).write_text(json.dumps({"checks": _RESULTS, "calls": client.log}, indent=1, default=str))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    site = _site(item)
    if site is not None and (report.when == "call" or (report.when == "setup" and report.outcome != "passed")):
        inconclusive = call.excinfo is not None and call.excinfo.errisinstance(InconclusiveError)
        _RESULTS.setdefault(site.key, {})[_check_name(item) or item.name] = {
            "outcome": "inconclusive" if inconclusive else report.outcome,
            "detail": str(call.excinfo.value)[:300] if call.excinfo else "",
        }
    return report


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
    if path := config.getoption("--live-report", default=""):
        os.environ["UL_LIVE_REPORT"] = path
