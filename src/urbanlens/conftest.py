"""Pytest configuration for the UrbanLens test suite."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

import pytest

from urbanlens.core.testing_network import (
    ExternalNetworkGuardVerificationError,
    LocalhostOnlyNetwork,
    verify_external_network_blocked,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


def _configure_hypothesis() -> None:
    """Point Hypothesis' example database somewhere the test user can write.

    Hypothesis defaults to ``.hypothesis/examples`` beside the current working
    directory and replays previously-failing examples first on every later
    run. In the test container that directory is owned by root (``docker exec``
    defaults to root) while tests run as ``appuser``, so the store was
    read-only in practice: a once-failing example was replayed forever and
    newly discovered ones were never recorded. The visible symptom is a test
    that fails in one large run and passes in isolation with no code change -
    see PROBLEMS.md, "test_only_submitted_fields_ever_move".

    The directory is deliberately stable rather than per-run: the store is only
    useful across runs, and ``DirectoryBasedExampleDatabase`` is file-per-entry
    and safe for concurrent readers/writers. Set ``UL_HYPOTHESIS_EXAMPLE_DIR``
    to relocate it, or to an empty value to run without a store at all.
    """
    from hypothesis import settings as hypothesis_settings
    from hypothesis.database import DirectoryBasedExampleDatabase, ExampleDatabase, InMemoryExampleDatabase

    database: ExampleDatabase
    configured = os.getenv("UL_HYPOTHESIS_EXAMPLE_DIR")
    if configured is not None and not configured.strip():
        database = InMemoryExampleDatabase()
    else:
        directory = Path(configured or Path(tempfile.gettempdir()) / "urbanlens-hypothesis-examples")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # An unwritable inherited directory is the exact failure this
            # exists to avoid, so prove writability rather than assume it.
            probe = directory / ".write-probe"
            probe.touch()
            probe.unlink()
            database = DirectoryBasedExampleDatabase(str(directory))
        except OSError:
            logger.warning("Hypothesis example directory %s is not writable; running without a stored example database", directory)
            database = InMemoryExampleDatabase()

    hypothesis_settings.register_profile("urbanlens", database=database)
    hypothesis_settings.load_profile("urbanlens")


_configure_hypothesis()


@pytest.fixture(scope="session", autouse=True)
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
