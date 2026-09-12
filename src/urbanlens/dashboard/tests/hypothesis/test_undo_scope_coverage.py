"""Every undo handler must declare read *and* write scopes for its domain."""

from __future__ import annotations

import importlib
import pkgutil

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.external_api.views_undo import (
    _DOMAIN_READ_SCOPES_BY_MODEL_LABEL,
    _DOMAIN_WRITE_SCOPES_BY_MODEL_LABEL,
)
from urbanlens.dashboard.services.undo import handlers as handlers_package


def _registered_model_labels() -> set[str]:
    """Every ``MODEL_LABEL`` declared by a shipped undo handler."""
    labels: set[str] = set()
    for module in pkgutil.iter_modules(handlers_package.__path__):
        imported = importlib.import_module(f"{handlers_package.__name__}.{module.name}")
        label = getattr(imported, "MODEL_LABEL", None)
        if label:
            labels.add(label)
    return labels


class UndoScopeCoverageTests(SimpleTestCase):
    def test_the_scan_finds_the_handlers(self) -> None:
        """Guards the checks below from passing on an empty registry."""
        self.assertGreaterEqual(len(_registered_model_labels()), 5)

    def test_every_handler_declares_a_write_scope(self) -> None:
        """The dangerous direction: a gap here drops to undo:write alone."""
        missing = sorted(_registered_model_labels() - set(_DOMAIN_WRITE_SCOPES_BY_MODEL_LABEL))

        self.assertEqual(missing, [], "these undo types can be restored without their domain write scope")

    def test_every_handler_declares_a_read_scope(self) -> None:
        missing = sorted(_registered_model_labels() - set(_DOMAIN_READ_SCOPES_BY_MODEL_LABEL))

        self.assertEqual(missing, [], "these undo types never appear in the API's undo history")

    def test_neither_map_names_a_handler_that_does_not_exist(self) -> None:
        labels = _registered_model_labels()
        stale = sorted((set(_DOMAIN_READ_SCOPES_BY_MODEL_LABEL) | set(_DOMAIN_WRITE_SCOPES_BY_MODEL_LABEL)) - labels)

        self.assertEqual(stale, [], "these scope entries refer to undo handlers that no longer exist")

    def test_the_two_maps_cover_the_same_labels(self) -> None:
        """A label readable but not writable (or vice versa) is a half-wired domain."""
        self.assertEqual(sorted(_DOMAIN_READ_SCOPES_BY_MODEL_LABEL), sorted(_DOMAIN_WRITE_SCOPES_BY_MODEL_LABEL))
