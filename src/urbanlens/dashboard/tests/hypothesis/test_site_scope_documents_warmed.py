"""A pin that becomes a site gets its site-scope documents fetched without anyone opening Sources.

The campus's first CRIS fetch ran a few seconds before the building sweep made it a site, cached the
one-building answer, and nothing re-fetched until Sources was opened again."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.pins import source_documents

_SOURCES = "urbanlens.dashboard.services.pins.source_documents.document_panel_sources"
_CACHED = "urbanlens.dashboard.services.pins.source_documents._cached_payload"
_SCHEDULE = "urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch"


def _source(key: str, *, ready: bool, gate: bool = True) -> mock.Mock:
    source = mock.Mock(key=key)
    source.documents_ready.return_value = ready
    source.gate.return_value = gate
    return source


class WarmSiteScopeDocumentsTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin = mock.Mock(location=mock.Mock())

    def _warm(self, sources: list, cached: dict | None = None) -> mock.Mock:
        with (
            mock.patch(_SOURCES, return_value=sources),
            mock.patch(_CACHED, return_value=cached),
            mock.patch(_SCHEDULE) as schedule,
        ):
            source_documents.warm_site_scope_documents(self.pin)
        return schedule

    def test_a_source_not_ready_at_site_scope_is_fetched(self) -> None:
        schedule = self._warm([_source("cris_building", ready=False)], cached={"site_scope": False})

        schedule.assert_called_once_with("cris_building", self.pin)

    def test_a_source_already_ready_is_left_alone(self) -> None:
        schedule = self._warm([_source("cris_building", ready=True)], cached={"site_scope": True})

        schedule.assert_not_called()

    def test_a_source_that_cannot_fetch_for_this_pin_is_left_alone(self) -> None:
        schedule = self._warm([_source("cris_building", ready=False, gate=False)])

        schedule.assert_not_called()
