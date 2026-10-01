"""Tests for services.ai.tools.help - get_page_help, through registry.execute()."""

from __future__ import annotations

from datetime import UTC, datetime

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.services.ai.tools.registry import REGISTRY, ToolContext, available_tools, execute


def _plain_profile():
    """A profile with SiteFeature.AI granted - see test_ai_tools_registry.py's own docstring for why."""
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return _make_profile()


def _context(profile) -> ToolContext:
    return ToolContext(profile=profile, now=datetime.now(tz=UTC))


class GetPageHelpToolTests(TestCase):
    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_is_registered(self) -> None:
        self.assertIn("get_page_help", REGISTRY)

    def test_appears_in_available_tools(self) -> None:
        names = {spec.name for spec in available_tools(_context(self.profile))}
        self.assertIn("get_page_help", names)

    def test_a_known_page_returns_its_help(self) -> None:
        result = execute("get_page_help", {"page": "map.view"}, _context(self.profile))
        self.assertNotIn("error", result.data)
        self.assertEqual(result.data["title"], "Map")
        self.assertTrue(result.data["key_actions"])
        self.assertEqual(result.summary, "Looked up page help")

    def test_an_unknown_page_is_an_error_block_not_a_raise(self) -> None:
        result = execute("get_page_help", {"page": "not.a.real.page"}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_missing_args_is_an_error_block_not_a_raise(self) -> None:
        result = execute("get_page_help", {}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_a_url_in_the_page_argument_is_rejected(self) -> None:
        result = execute("get_page_help", {"page": "https://example.com"}, _context(self.profile))
        self.assertIn("error", result.data)
