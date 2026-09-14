"""Fixtures for the contract suite."""

from __future__ import annotations

import os

import pytest
from schema_source import (
    API_KEY_ENV,
    ContractConfigurationError,
    live_base_url,
    manifest_api_key,
)

#: Role the in-process account is provisioned under. Distinct from the
#: Playwright suite's roles so a shared database cannot have one suite's run
#: reusing an account the other left behind.
CONTRACT_ROLE = "contract"


def _assert_on_a_test_database() -> None:
    """Refuse to run against anything but a test database.

    Every request this suite generates goes through the real ORM, and ``UL_CONTRACT_METHODS=all`` makes some of
    them writes.

    Raises:
        ContractConfigurationError: If the active connection does not look like a database pytest-django created."""
    from django.db import connection

    name = connection.settings_dict["NAME"]
    configured = os.environ.get("UL_TEST_DB_NAME", "").strip()

    if configured:
        if name != configured:
            raise ContractConfigurationError(
                f"Connected to {name!r}, but UL_TEST_DB_NAME asks for {configured!r}. Refusing to run."
            )
        return
    # Django's default when TEST.NAME is unset.
    if not str(name).startswith("test_"):
        raise ContractConfigurationError(
            f"Connected to {name!r}, which is not a test database. Set UL_TEST_DB_NAME to a unique value and re-run.",
        )


@pytest.fixture(autouse=True)
def _keep_the_test_connection_open():
    """Stop the WSGI request cycle from closing the test's database connection.

    Django's own test client disconnects ``close_old_connections`` from ``request_started`` before invoking the
    handler, and reconnects it after."""
    if live_base_url():
        yield
        return

    from django.core.signals import request_finished, request_started
    from django.db import close_old_connections

    request_started.disconnect(close_old_connections)
    request_finished.disconnect(close_old_connections)
    try:
        yield
    finally:
        request_started.connect(close_old_connections)
        request_finished.connect(close_old_connections)


@pytest.fixture
def contract_headers() -> dict[str, str]:
    """Credentials every generated request is sent with.

    In-process, an account is provisioned through the same service the Playwright suite's management command
    uses, so both suites authenticate as the same *kind* of user rather than two separately-invented ones.

    Returns:
        A header mapping carrying the bearer token.

    Raises:
        ContractConfigurationError: In live mode, when no key was supplied."""
    if live_base_url():
        key = os.environ.get(API_KEY_ENV, "").strip() or manifest_api_key()
        if not key:
            raise ContractConfigurationError(
                f"Live mode needs an API key. Set {API_KEY_ENV}, or point UL_E2E_ACCOUNTS_FILE at the manifest written by `manage.py provision_integration_env --out ...`.",
            )
        return {"Authorization": f"Bearer {key}"}

    # The `django_db` marker added during collection is what built and pointed
    # at the test database; this only confirms it landed.
    _assert_on_a_test_database()

    from urbanlens.dashboard.services.integration_testing.accounts import generate_password, provision_account

    account, _created = provision_account(role=CONTRACT_ROLE, password=generate_password())
    if not account.api_key:
        raise ContractConfigurationError("The provisioned contract account has no API key.")
    return {"Authorization": f"Bearer {account.api_key}"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark this directory's tests, and give the ones that call the app a database.

    ``django_db`` is applied here rather than written on the test because whether a database is wanted at all
    depends on the mode, which is a runtime decision."""
    in_process = not live_base_url()
    for item in items:
        item.add_marker(pytest.mark.contract)
        if in_process and "contract_headers" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.django_db)
