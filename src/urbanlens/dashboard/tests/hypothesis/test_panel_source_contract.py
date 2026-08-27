"""The panel contract: what a plugin author must declare, enforced.

Panels are the codebase's main extension seam, and the base class lets an author
omit most of what a panel needs. ``section_id`` and ``title`` default to empty
strings and ``cache_source`` is only meaningful by convention, so the three most
likely mistakes all fail *quietly at render* - a section with no DOM id for HTMX
to swap against, a panel with no heading, or a cache-backed panel that looks up
the empty key forever and sits in its pending state.

The important test here is the first one: every panel this repo actually ships
must be well-formed. It turns a silent runtime absence into a loud CI failure, and
it is the check that keeps working as panels are added.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.pins.external_data import (
    InfoPanelSource,
    PanelSource,
    panel_source_problems,
    panel_sources,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class ShippedPanelsAreWellFormedTests(SimpleTestCase):
    def test_every_registered_panel_declares_what_it_needs(self) -> None:
        broken = {key: panel_source_problems(source) for key, source in panel_sources().items() if panel_source_problems(source)}

        self.assertEqual(broken, {}, f"registered panels are misconfigured: {broken}")

    def test_the_registry_is_not_empty(self) -> None:
        """Guards the check above from passing because nothing was registered."""
        self.assertGreater(len(panel_sources()), 5)


class _MinimalCachePanel(InfoPanelSource):
    key = "contract_probe"
    cache_source = "contract_probe"
    section_id = "contract-probe-section"
    icon = "science"
    title = "Contract Probe"

    def fetch(self, pin: Pin) -> None:  # pragma: no cover - not exercised
        return None

    def render_context(self, pin: Pin, data: dict) -> dict | None:  # pragma: no cover
        return None


class PanelSourceProblemsTests(SimpleTestCase):
    """Each omission the base class permits is reported, by name."""

    def test_a_fully_declared_panel_has_no_problems(self) -> None:
        self.assertEqual(panel_source_problems(_MinimalCachePanel()), [])

    def test_a_missing_title_is_reported(self) -> None:
        class NoTitle(_MinimalCachePanel):
            title = ""

        self.assertIn("title", " ".join(panel_source_problems(NoTitle())))

    def test_a_missing_section_id_is_reported(self) -> None:
        class NoSection(_MinimalCachePanel):
            section_id = ""

        self.assertIn("section_id", " ".join(panel_source_problems(NoSection())))

    def test_a_cache_backed_panel_without_a_cache_source_is_reported(self) -> None:
        """The quietest of the three: the fetch writes one key and the read looks
        for another, so the panel polls forever and never renders."""

        class NoCacheSource(_MinimalCachePanel):
            cache_source = ""

        self.assertIn("cache_source", " ".join(panel_source_problems(NoCacheSource())))

    def test_a_gallery_media_provider_is_exempt_from_the_presentation_attributes(self) -> None:
        """Media providers render as tabs inside the combined gallery, which supplies
        the surrounding markup - they have no section of their own to identify or head.
        Requiring these of them flagged nine correct shipped panels on the first pass."""
        from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

        class GalleryProbe(GalleryMediaSource):
            key = "gallery_probe"
            cache_source = "gallery_probe"

            def fetch(self, pin: Pin) -> None:  # pragma: no cover
                return None

            def render_context(self, pin: Pin, data: dict) -> dict | None:  # pragma: no cover
                return None

            def media_items(self, data: dict) -> list:  # pragma: no cover
                return []

        self.assertEqual(panel_source_problems(GalleryProbe()), [])

    def test_a_gallery_provider_still_needs_a_cache_source(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

        class NoCache(GalleryMediaSource):
            key = "gallery_probe_2"
            cache_source = ""

            def fetch(self, pin: Pin) -> None:  # pragma: no cover
                return None

            def render_context(self, pin: Pin, data: dict) -> dict | None:  # pragma: no cover
                return None

            def media_items(self, data: dict) -> list:  # pragma: no cover
                return []

        self.assertIn("cache_source", " ".join(panel_source_problems(NoCache())))

    def test_a_source_that_renders_nothing_is_exempt_too(self) -> None:
        """BoundaryPanelSource is the shipped example: it renders no section at all,
        fetching data the map and the external API consume. Demanding presentation
        attributes of it flagged a correct core panel on the second pass."""

        class DataOnly(PanelSource):
            key = "data_only_probe"

            def is_ready(self, pin: Pin) -> bool:  # pragma: no cover
                return True

            def fetch(self, pin: Pin) -> None:  # pragma: no cover
                return None

        self.assertEqual(panel_source_problems(DataOnly()), [])

    def test_a_non_cache_panel_is_not_asked_for_a_cache_source(self) -> None:
        """Bespoke panels keep their data elsewhere - requiring it would be wrong."""

        class Bespoke(PanelSource):
            key = "bespoke_probe"
            section_id = "bespoke-probe"
            title = "Bespoke Probe"

            def is_ready(self, pin: Pin) -> bool:  # pragma: no cover
                return True

            def fetch(self, pin: Pin) -> None:  # pragma: no cover
                return None

        self.assertEqual(panel_source_problems(Bespoke()), [])
