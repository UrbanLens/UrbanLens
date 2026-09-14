"""Every journal source must declare the scopes the API needs to serve it."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.external_api.views import MemoriesJournalView
from urbanlens.dashboard.services.memories.journal import JOURNAL_SOURCES


class JournalSourceScopeCoverageTests(SimpleTestCase):
    def test_the_scan_finds_both_sides(self) -> None:
        """Guards the checks below from passing on an empty comparison."""
        self.assertGreaterEqual(len(JOURNAL_SOURCES), 3)
        self.assertGreaterEqual(len(MemoriesJournalView.JOURNAL_SOURCE_SCOPES), 3)

    def test_every_source_declares_its_scopes(self) -> None:
        """Otherwise the source is silently unreachable through the API."""
        unmapped = sorted(set(JOURNAL_SOURCES) - set(MemoriesJournalView.JOURNAL_SOURCE_SCOPES))

        self.assertEqual(unmapped, [], "these journal sources can never be served by the external API")

    def test_no_scope_entry_names_a_source_that_no_longer_exists(self) -> None:
        stale = sorted(set(MemoriesJournalView.JOURNAL_SOURCE_SCOPES) - set(JOURNAL_SOURCES))

        self.assertEqual(stale, [], "these scope entries refer to journal sources that were removed")

    def test_no_source_declares_an_empty_scope_set(self) -> None:
        """An empty set is omitted rather than granted, so it reads as 'mapped'
        while behaving as 'never served'."""
        empty = sorted(key for key, scopes in MemoriesJournalView.JOURNAL_SOURCE_SCOPES.items() if not scopes)

        self.assertEqual(empty, [])

    def test_every_source_requires_the_endpoints_own_base_scope(self) -> None:
        """``filter_sources_by_grants`` deliberately does not assume the view's
        own ``required_scopes`` were satisfied, so each entry must list them."""
        base = MemoriesJournalView.required_scopes_by_method["GET"]
        missing = sorted(
            key for key, scopes in MemoriesJournalView.JOURNAL_SOURCE_SCOPES.items() if not base <= set(scopes)
        )

        self.assertEqual(missing, [], "these sections could be granted without the endpoint's base scope")
