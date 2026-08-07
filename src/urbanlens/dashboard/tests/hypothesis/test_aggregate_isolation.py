"""One failing contributor must not take down an aggregate it belongs to.

Three user-facing aggregates fan out to independently-registered contributors:
the Memories feed (covered in ``test_memories_source_isolation``), the Journal,
and the pin detail page's panel readiness map. Each is an advertised
extensibility seam - "add one function/plugin and nothing else changes" - which
is exactly what makes unguarded fan-out expensive: a bug in one contributor takes
out every other contributor's output along with the page.

The panel one matters most. ``panel_readiness`` builds the pin detail page's tab
strip, and panels are the plugin surface, so a single plugin raising in
``is_ready()`` returned a 500 for the app's busiest page rather than affecting its
own tab.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.services.memories import journal
from urbanlens.dashboard.services.pins.external_data import PanelSource, panel_readiness


class JournalSourceIsolationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.pin = baker.make(Pin, profile=self.profile, location=location, name="Powerhouse")
        baker.make(Review, profile=self.profile, pin=self.pin, rating=4)

    def test_the_review_shows_up_normally(self) -> None:
        """Baseline - without this the isolation test proves nothing."""
        self.assertIn("review", {entry.kind for entry in journal.get_journal_entries(self.profile)})

    def test_one_broken_source_does_not_lose_the_others(self) -> None:
        def boom(profile):
            raise ValueError("corrupt comment row")
            yield  # pragma: no cover - generator marker

        with mock.patch.dict(journal.JOURNAL_SOURCES, {"comments": boom}):
            entries = journal.get_journal_entries(self.profile)

        self.assertIn("review", {entry.kind for entry in entries}, "a healthy source must still contribute")

    def test_every_source_failing_yields_an_empty_journal_rather_than_an_error(self) -> None:
        def boom(profile):
            raise RuntimeError("boom")
            yield  # pragma: no cover - generator marker

        with mock.patch.dict(journal.JOURNAL_SOURCES, dict.fromkeys(journal.JOURNAL_SOURCES, boom)):
            self.assertEqual(journal.get_journal_entries(self.profile), [])


class _HealthyPanel(PanelSource):
    """A bespoke panel - i.e. not cache- or slide-backed, so panel_readiness calls
    its is_ready() directly. Cache-backed panels (InfoPanelSource and friends) are
    resolved by one bulk query instead and never invoke is_ready per source."""

    key = "healthy_panel"
    section_id = "healthy-section"
    icon = "check"
    title = "Healthy"

    def is_ready(self, pin: Pin) -> bool:
        return True

    def fetch(self, pin: Pin) -> None:  # pragma: no cover - never called here
        return None


class _BrokenPanel(_HealthyPanel):
    key = "broken_panel"
    section_id = "broken-section"
    title = "Broken"

    def is_ready(self, pin: Pin) -> bool:
        raise RuntimeError("this plugin's readiness check is broken")


class PanelReadinessIsolationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        location = Location.objects.create(latitude=41.0, longitude=-75.0)
        self.pin = baker.make(Pin, profile=self.profile, location=location)

    def test_a_panel_raising_in_is_ready_does_not_break_the_others(self) -> None:
        readiness = panel_readiness(self.pin, [_HealthyPanel(), _BrokenPanel()])

        self.assertTrue(readiness["healthy_panel"], "a working panel must still report ready")
        self.assertFalse(readiness["broken_panel"], "a panel that cannot answer is treated as not ready")

    def test_every_declared_panel_appears_in_the_map(self) -> None:
        """The tab strip iterates this map - a missing key would drop a tab silently."""
        readiness = panel_readiness(self.pin, [_HealthyPanel(), _BrokenPanel()])

        self.assertEqual(set(readiness), {"healthy_panel", "broken_panel"})
