"""Scratch plugin: report the test after which committed ApiCallLog / ApiRateLimit rows appear."""

import os

import pytest

_state = {"conn": None, "last": None}
_OUT = os.environ.get("LEAKDET_DIR", "/tmp")


def _counts():
    from django.db import connection
    import psycopg

    from urbanlens.dashboard.models.api_call_log.model import ApiCallLog

    if _state["conn"] is None:
        params = connection.get_connection_params()
        if not str(params.get("dbname", "")).startswith("test"):
            return None
        _state["conn"] = psycopg.connect(**params, autocommit=True)
    with _state["conn"].cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM "{ApiCallLog._meta.db_table}"')
        return cursor.fetchone()[0]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    yield
    try:
        n = _counts()
    except Exception as exc:  # noqa: BLE001
        _state["conn"] = None
        with open(os.path.join(_OUT, f"errors_{os.getenv('PYTEST_XDIST_WORKER', 'main')}.txt"), "a") as fh:
            fh.write(f"{item.nodeid} {exc!r}\n")
        return
    if n is None:
        with open(os.path.join(_OUT, f"errors_{os.getenv('PYTEST_XDIST_WORKER', 'main')}.txt"), "a") as fh:
            fh.write(f"{item.nodeid} none\n")
        return
    if _state["last"] is not None and n != _state["last"]:
        with open(os.path.join(_OUT, f"leaks_{os.getenv('PYTEST_XDIST_WORKER', 'main')}.txt"), "a") as fh:
            fh.write(f"{item.nodeid} {_state['last']}->{n}\n")
    _state["last"] = n
