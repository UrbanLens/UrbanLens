"""Core's pages say nothing in an operator's voice. Only a plugin's template overrides put that copy back."""

from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import re
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
import urbanlens.dashboard

CORE_TEMPLATES = (Path(urbanlens.dashboard.__file__).parent / "templates").resolve()

#: First-person singular narration marks one person's voice. Questions a reader asks ("How can I...") are not it.
FIRST_PERSON_SINGULAR = re.compile(
    r"\bI(?:'m|'ve|'ll|'d)\b|\bI (?:made|built|wrote|rotate|bought|added|put|never|really|just|also)\b|\bmy own\b|\bmyself\b"
)


def core_only_templates() -> list[dict]:
    """The template settings with only core's own template directory, so no plugin override is searched."""
    templates = deepcopy(settings.TEMPLATES)
    for engine in templates:
        if engine["BACKEND"].endswith("DjangoTemplates"):
            engine["DIRS"] = [directory for directory in engine["DIRS"] if Path(directory).resolve() == CORE_TEMPLATES]
    return templates


class _VisibleText(HTMLParser):
    """Collects the text a reader sees, skipping script and style bodies."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)


def visible_text(page: str) -> str:
    parser = _VisibleText()
    parser.feed(page)
    return " ".join(" ".join(parser.chunks).split())


class CorePagesCarryNeutralCopyTests(TestCase):
    PAGES = ("about", "faq", "privacy", "terms", "values", "thanks")

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")

    def _render(self, name: str) -> str:
        if name in ("settings.view", "onboarding.welcome"):
            self.client.force_login(self.user)
        else:
            self.client.logout()
        with patch("urbanlens.dashboard.controllers.thanks.get_github_contributors", return_value=[]):
            return self.client.get(reverse(name)).content.decode()

    def test_core_pages_speak_in_no_single_persons_voice(self) -> None:
        with override_settings(TEMPLATES=core_only_templates()):
            for name in (*self.PAGES, "settings.view", "onboarding.welcome"):
                with self.subTest(page=name):
                    match = FIRST_PERSON_SINGULAR.search(text := visible_text(self._render(name)))
                    self.assertIsNone(match, text[max(0, match.start() - 60) : match.end() + 60] if match else "")

    def test_core_privacy_and_terms_speak_for_whoever_runs_the_site(self) -> None:
        with override_settings(TEMPLATES=core_only_templates()):
            self.assertIn("ask the people who run", self._render("privacy").lower())
            self.assertIn("the people who run this site", self._render("terms"))

    def test_core_values_page_lists_the_principles_its_headings_describe(self) -> None:
        with override_settings(TEMPLATES=core_only_templates()):
            page = self._render("values")
        for principle in (
            "Consent",
            "Privacy",
            "Transparency",
            "Security",
            "Responsibility",
            "Reliability",
            "Functionality",
        ):
            with self.subTest(principle=principle):
                self.assertIn(f'<h3 class="values-item-title">{principle}</h3>', page)

    def test_core_privacy_names_the_cookies_and_browser_side_third_parties(self) -> None:
        with override_settings(TEMPLATES=core_only_templates()):
            page = self._render("privacy")
        for fragment in ("ul_media", "Google Fonts", "Nominatim", "map tile providers"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, page)
        self.assertNotIn("signing back in cancels", page)

    def test_core_500_claims_no_monitoring(self) -> None:
        with override_settings(TEMPLATES=core_only_templates()):
            body = render_to_string("dashboard/pages/errors/500.html", request=self._anonymous_request())
        self.assertNotIn("notified", body)

    def _anonymous_request(self) -> HttpRequest:
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        request.session = {}
        return request
