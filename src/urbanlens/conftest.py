"""Pytest configuration for the UrbanLens test suite."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from urbanlens.core.testing_network import (
    ExternalNetworkGuardVerificationError,
    LocalhostOnlyNetwork,
    verify_external_network_blocked,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="session", autouse=True)  # noqa: RUF076 - We explicitly want global and implicit autouse.
def block_external_network() -> Iterator[None]:
    """Deny accidental internet access in tests while allowing localhost."""
    if os.getenv("UL_ALLOW_TEST_INTERNET", "False").lower() in {"true", "1", "yes"}:
        yield
        return

    guard = LocalhostOnlyNetwork().start()
    try:
        try:
            verify_external_network_blocked()
        except ExternalNetworkGuardVerificationError as exc:
            guard.stop()
            pytest.exit(str(exc), returncode=1)
        yield
    finally:
        guard.stop()
