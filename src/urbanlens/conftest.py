"""Pytest configuration for the UrbanLens test suite."""

from __future__ import annotations

from collections.abc import Iterator
import os

import pytest

from urbanlens.core.testing_network import LocalhostOnlyNetwork


@pytest.fixture(scope="session", autouse=True)
def block_external_network() -> Iterator[None]:
    """Deny accidental internet access in tests while allowing localhost."""
    if os.getenv("UL_ALLOW_TEST_INTERNET", "False").lower() in {"true", "1", "yes"}:
        yield
        return

    guard = LocalhostOnlyNetwork().start()
    try:
        yield
    finally:
        guard.stop()
