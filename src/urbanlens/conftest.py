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

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


def _disable_hypothesis_example_patching() -> None:
    """Stop Hypothesis trying to codemod an ``@example`` suggestion on failure.

    When a ``@given`` test fails, Hypothesis' pytest plugin offers a patch
    adding an ``@example(...)`` for the falsifying input. That is a convenience.
    Building it runs a ``libcst`` codemod, and in a full-suite run this
    environment raises ``AttributeError: __provides__`` inside
    ``libcst.matchers._gather_constructed_visit_funcs`` - which happens inside
    ``pytest_runtest_makereport``, so it does not merely lose the suggestion: it
    raises ``INTERNALERROR`` while *building the failure report*, aborting the
    whole run and destroying the identity of the test that failed. The
    thirteenth consolidation lost a real failure that way (1 failed, 9,074
    passed, no test name anywhere in the output).

    It does not reproduce on a single module - a failing ``@given`` test reports
    perfectly in isolation - so it needs state a long run accumulates, which is
    exactly when losing the report costs most.

    The plugin already guards this import with ``except ImportError: return``,
    so making the import fail is its own supported degradation path rather than
    a monkeypatch of its internals. The trade is explicit: no auto-suggested
    ``@example`` decorator, in exchange for always being able to see *which*
    property test failed.
    """
    # None is CPython's own "block this import" sentinel - `import x` raises
    # ImportError when sys.modules[x] is None - but typeshed types the mapping
    # as dict[str, ModuleType], which the sentinel predates. Narrowly suppressed
    # with the code actually raised: the previous comment named [assignment],
    # which silenced nothing and hid the [arg-type] underneath it.
    sys.modules.setdefault("hypothesis.extra._patching", None)  # type: ignore[arg-type]


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


_disable_hypothesis_example_patching()
_configure_hypothesis()


@pytest.fixture(scope="session", autouse=True)
def isolated_media_root() -> Iterator[None]:
    """Give the suite its own throwaway ``MEDIA_ROOT``, deleted when it ends.

    ``base.py`` points ``MEDIA_ROOT`` at ``src/urbanlens/media`` - the same
    directory a local dev server writes real uploads into. Nothing overrode it
    for tests, so every fixture that saved a file wrote there and left it
    behind: ``TestCase`` rolls the database back between tests, but it does not
    roll back the filesystem. On this checkout that had accumulated ~14,500
    files (55 MB) of test debris mixed in with dev data, and it was not
    harmless - a polluted media root makes
    ``test_pin_privacy_from_a_wiki.PrivatePhotoBytesTests`` fail intermittently.

    Same reasoning as ``CACHES`` and ``CELERY_BROKER_URL`` in
    ``settings/test.py``: shared external state that tests must not inherit or
    hand on. A fresh directory per session also means one run cannot be
    influenced by a previous one's files, which is what makes the flake above
    reproducible-or-not depending on how many times you had run the suite.

    Session-scoped rather than per-test: some tests write a file in
    ``setUpTestData``/class setup and read it back later, and a per-test root
    would pull the file out from under them.
    """
    from django.test import override_settings

    with (
        tempfile.TemporaryDirectory(prefix="urbanlens-test-media-") as media_root,
        override_settings(MEDIA_ROOT=media_root),
    ):
        yield


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
