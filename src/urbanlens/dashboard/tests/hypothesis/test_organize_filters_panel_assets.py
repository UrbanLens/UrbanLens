"""The Organize page's Filters panel brings Leaflet and Leaflet.draw with it, and they must survive the swap.

htmx parses a response with DOMParser and keeps its <body>. A response that opens with <link> or <script>
has those hoisted into <head> by the parser and dropped, so the saved-filter dialog's region map never
loaded on this page.
"""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

# Elements an HTML parser places in <head> when they come before any body content.
_HEAD_ONLY = {"base", "link", "meta", "noscript", "script", "style", "template", "title"}


class FiltersPanelAssetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(baker.make(User))

    def test_the_panel_opens_with_body_content(self) -> None:
        response = self.client.get(reverse("lists.list") + "?tab=filters", headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 200)
        first = re.match(r"\s*<([a-zA-Z]+)", response.content.decode())
        assert first is not None
        self.assertNotIn(first.group(1).lower(), _HEAD_ONLY)

    def test_the_panel_still_brings_the_draw_tools(self) -> None:
        response = self.client.get(reverse("lists.list") + "?tab=filters", headers={"HX-Request": "true"})
        self.assertContains(response, "leaflet.draw.js")
