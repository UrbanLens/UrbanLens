"""Tests for services.ai.tools.dismissals - recent_dismissals and reopen_explainer, through registry.execute()."""

from __future__ import annotations

from datetime import UTC, datetime

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.services.ai.dismissals import DismissalEntry
from urbanlens.dashboard.services.ai.tools.registry import REGISTRY, ToolContext, available_tools, execute


def _plain_profile():
    """A profile with SiteFeature.AI granted - see test_ai_tools_registry.py's own docstring for why."""
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return _make_profile()


def _context(profile, dismissals: tuple[DismissalEntry, ...] = ()) -> ToolContext:
    return ToolContext(profile=profile, now=datetime.now(tz=UTC), dismissals=dismissals)


class RecentDismissalsToolTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_is_registered_and_available(self) -> None:
        self.assertIn("recent_dismissals", REGISTRY)
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertIn("recent_dismissals", names)

    def test_lists_what_the_client_sent_this_turn(self) -> None:
        entries = (
            DismissalEntry(id="x", kind="explainer", heading="Labels", body="Tag your pins.", page="/organize/"),
        )
        result = execute("recent_dismissals", {}, _context(self.profile, entries))
        self.assertNotIn("error", result.data)
        self.assertEqual(len(result.data["dismissals"]), 1)
        self.assertEqual(result.data["dismissals"][0]["id"], "x")

    def test_empty_when_the_client_sent_nothing(self) -> None:
        result = execute("recent_dismissals", {}, _context(self.profile))
        self.assertEqual(result.data["dismissals"], [])

    def test_another_profiles_context_never_leaks_in(self) -> None:
        """dismissals is per-context, not looked up server-side - nothing to leak, but assert the shape holds."""
        other = _plain_profile()
        entries = (DismissalEntry(id="x", kind="explainer", heading="H", body="B", page="/"),)
        result = execute("recent_dismissals", {}, _context(other, entries))
        self.assertEqual(len(result.data["dismissals"]), 1)
        result_empty = execute("recent_dismissals", {}, _context(self.profile))
        self.assertEqual(result_empty.data["dismissals"], [])


class ReopenExplainerToolTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()
        self.entries = (
            DismissalEntry(
                id="explainer-x", kind="explainer", heading="Labels", body="Tag your pins.", page="/organize/"
            ),
            DismissalEntry(
                id="tour-y",
                kind="tour",
                heading="Reorder",
                body="Drag to prioritize.",
                page="/organize/",
                prefix="ul_onboarding_v1_organize",
            ),
        )

    def test_is_registered_and_available(self) -> None:
        self.assertIn("reopen_explainer", REGISTRY)
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertIn("reopen_explainer", names)

    def test_client_action_is_declared(self) -> None:
        self.assertEqual(REGISTRY["reopen_explainer"].client_action, "reopen_explainer")

    def test_reopening_a_known_explainer_returns_its_kind_and_page(self) -> None:
        result = execute("reopen_explainer", {"id": "explainer-x"}, _context(self.profile, self.entries))
        self.assertNotIn("error", result.data)
        self.assertEqual(
            result.data,
            {"status": "reopened", "id": "explainer-x", "kind": "explainer", "page": "/organize/", "prefix": None},
        )

    def test_reopening_a_known_tour_card_returns_its_prefix(self) -> None:
        result = execute("reopen_explainer", {"id": "tour-y"}, _context(self.profile, self.entries))
        self.assertEqual(result.data["kind"], "tour")
        self.assertEqual(result.data["prefix"], "ul_onboarding_v1_organize")

    def test_an_unknown_id_is_an_error_block_not_a_raise(self) -> None:
        result = execute("reopen_explainer", {"id": "never-dismissed"}, _context(self.profile, self.entries))
        self.assertIn("error", result.data)

    def test_missing_args_is_an_error_block_not_a_raise(self) -> None:
        result = execute("reopen_explainer", {}, _context(self.profile, self.entries))
        self.assertIn("error", result.data)
