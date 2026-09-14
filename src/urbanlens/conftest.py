"""Pytest configuration for the UrbanLens test suite."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import tempfile
from typing import TYPE_CHECKING

import pytest

from urbanlens.core.testing_network import (
    ExternalNetworkGuardVerificationError,
    LocalhostOnlyNetwork,
    verify_external_network_blocked,
)
from urbanlens.core.tests.ai_guard import patched_ai_gateway

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


def _disable_hypothesis_example_patching() -> None:
    """Stop Hypothesis trying to codemod an ``@example`` suggestion on failure.

    Building the suggestion runs a ``libcst`` codemod that raises
    ``INTERNALERROR`` inside ``pytest_runtest_makereport`` on long runs,
    aborting the whole run and hiding which test failed. The plugin already
    guards this import with ``except ImportError: return``, so blocking the
    import is its supported degradation path. The trade is explicit: no
    auto-suggested ``@example``, in exchange for always seeing which test
    failed.
    """
    # None is CPython's own "block this import" sentinel - `import x` raises
    # ImportError when sys.modules[x] is None - but typeshed types the mapping
    # as dict[str, ModuleType], which the sentinel predates.
    sys.modules.setdefault("hypothesis.extra._patching", None)  # type: ignore[arg-type]


def _configure_hypothesis() -> None:
    """Point Hypothesis' example database somewhere the test user can write.

    Hypothesis defaults to ``.hypothesis/examples`` beside the cwd, which in
    the test container is root-owned while tests run as ``appuser`` - so past
    failures replay forever and new ones are never recorded. The directory is
    deliberately stable rather than per-run, and safe for concurrent
    readers/writers. Set ``UL_HYPOTHESIS_EXAMPLE_DIR`` to relocate it, or to
    an empty value to run without a store at all.
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


_disable_hypothesis_example_patching()
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


@pytest.fixture(scope="session", autouse=True)
def block_ai_gateway() -> Iterator[None]:
    """Stop any test reaching a real LLM provider.

    Mirrors ``TestRunner.setup_test_environment``'s patching, which pytest
    never runs since pytest-django ignores ``TEST_RUNNER``.
    """
    with patched_ai_gateway():
        yield
