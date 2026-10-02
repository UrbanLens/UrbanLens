"""A tab panel's content loads when its tab opens, not with the page (P53).

htmx counts a hidden element as revealed (its bounding box is all zeros), so `revealed` fires for an element inside
a hidden tab panel as soon as the page is processed, exactly as `load` does. `intersect` waits until it is shown.
"""

from __future__ import annotations

from pathlib import Path
import re

from django.contrib.auth.models import User
from django.urls import reverse
import lxml.html
from model_bakery import baker

from urbanlens import dashboard
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

#: Triggers that fire for an element nobody can see.
_EAGER = frozenset({"load", "revealed"})


def _events(trigger: str) -> set[str]:
    """The event names in an ``hx-trigger`` value: ``load[x], intersect once`` -> ``{"load", "intersect"}``."""
    return {re.split(r"[\s\[]", spec.strip(), maxsplit=1)[0] for spec in trigger.split(",") if spec.strip()}


def _eager_in_hidden_tabs(html: str) -> tuple[list[str], int]:
    tree = lxml.html.fromstring(html)
    panels = tree.xpath("//*[@data-tab-panel][@hidden]")
    return [
        f"{panel.get('data-tab-panel')}: {element.get('hx-get')} ({element.get('hx-trigger')})"
        for panel in panels
        for element in panel.xpath(".//*[@hx-trigger]")
        if _events(element.get("hx-trigger", "")) & _EAGER
    ], len(panels)


class HiddenTabPanelsDeferTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, parent_pin=None)
        self.client.force_login(self.user)

    def assert_tabs_defer(self, url: str) -> None:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        eager, panels = _eager_in_hidden_tabs(response.content.decode())
        self.assertGreater(panels, 0, "the page has no hidden tab panels left to check")
        self.assertEqual(eager, [])

    def test_the_private_pin_page(self) -> None:
        self.assert_tabs_defer(reverse("pin.details", kwargs={"pin_slug": self.pin.slug}))

    def test_the_wiki_page(self) -> None:
        baker.make(Wiki, location=self.location)
        self.assert_tabs_defer(reverse("location.wiki", args=[self.location.ensure_slug()]))

    def test_the_wiki_history_panel_reloads_on_the_event_an_edit_sends(self) -> None:
        """shared/wiki-page.ts dispatches the event; an edit made before the tab is opened needs no reload."""
        baker.make(Wiki, location=self.location)
        response = self.client.get(reverse("location.wiki", args=[self.location.ensure_slug()]))
        panel = lxml.html.fromstring(response.content.decode()).get_element_by_id("wiki-tab-content")
        source = (Path(dashboard.__file__).parent / "frontend" / "ts" / "shared" / "wiki-page.ts").read_text(
            encoding="utf-8"
        )
        event = re.search(r'WIKI_HISTORY_CHANGED = "([^"]+)"', source)

        if event is None:
            self.fail("wiki-page.ts no longer names WIKI_HISTORY_CHANGED")
        self.assertIn(f"{event.group(1)} from:body", panel.get("hx-trigger", ""))
