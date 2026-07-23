"""XSS regression tests for the main map page and its data feed.

Invariants verified:
  - A Label (tag/category) name is never embedded raw into the `<script>` blocks
    that build `filter_labels_json` (map/index.html, view_map) or `tags_data_json`
    (map/data.html, init_map) - both are JSON payloads written directly into an
    executing <script> tag via `|safe`, so an unescaped `</script>` (or `<`/`&`)
    in a label name would let stored label data break out of the script and
    inject arbitrary markup/script.
  - Pin.icon / Pin.color are always JS-string-escaped (`|escapejs`) when embedded
    in map/data.html's inline `<script>` block, matching the other pin fields
    (name, description, status, ...) which were already escaped - a raw quote
    in either field would otherwise let stored pin data break out of the JS
    string literal.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

_SCRIPT_BREAKOUT_PAYLOAD = "</script><script>alert(document.domain)</script>"
_QUOTE_BREAKOUT_PAYLOAD = 'x";alert(document.domain);//'
# Pin.color is a CharField(max_length=20) - a shorter payload that still proves the
# JS-string-breakout point (an unescaped quote ending the "color": "..." literal).
_QUOTE_BREAKOUT_PAYLOAD_SHORT = 'x";alert(1)//'


class _MapXssTestCase(TestCase):
    user: User
    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)


class FilterLabelsJsonXssTests(_MapXssTestCase):
    """view_map ("/dashboard/map/") embeds `filter_labels_json` inline via `|safe`."""

    def test_malicious_label_name_is_not_embedded_raw(self) -> None:
        baker.make(Label, name=_SCRIPT_BREAKOUT_PAYLOAD, kind=KIND_TAG, profile=None)

        resp = self.client.get(reverse("map.view"))

        body = resp.content.decode()
        self.assertNotIn(_SCRIPT_BREAKOUT_PAYLOAD, body)
        # The label name must still be present, just safely encoded for a JS/JSON context.
        self.assertIn("script\\u003E", body)


class TagsDataJsonXssTests(_MapXssTestCase):
    """init_map ("/dashboard/map/init/") embeds each pin's `tags_data_json` inline via `|safe`."""

    def test_malicious_tag_name_is_not_embedded_raw(self) -> None:
        location = baker.make(Location, latitude=40.0, longitude=-75.0)
        pin = baker.make(Pin, profile=self.profile, location=location)
        label = baker.make(Label, name=_SCRIPT_BREAKOUT_PAYLOAD, kind=KIND_TAG, profile=None)
        pin.labels.add(label)

        resp = self.client.get(reverse("map.init"))

        body = resp.content.decode()
        self.assertNotIn(_SCRIPT_BREAKOUT_PAYLOAD, body)
        self.assertIn("script\\u003E", body)


class PinIconColorEscapejsTests(_MapXssTestCase):
    """init_map's inline pin object literal must JS-escape every string field, including icon/color."""

    def test_malicious_pin_icon_is_escaped(self) -> None:
        location = baker.make(Location, latitude=40.0, longitude=-75.0)
        baker.make(Pin, profile=self.profile, location=location, icon=_QUOTE_BREAKOUT_PAYLOAD, color=None)

        resp = self.client.get(reverse("map.init"))

        body = resp.content.decode()
        self.assertNotIn(_QUOTE_BREAKOUT_PAYLOAD, body)

    def test_malicious_pin_color_is_escaped(self) -> None:
        location = baker.make(Location, latitude=40.0, longitude=-75.0)
        baker.make(Pin, profile=self.profile, location=location, icon=None, color=_QUOTE_BREAKOUT_PAYLOAD_SHORT)

        resp = self.client.get(reverse("map.init"))

        body = resp.content.decode()
        self.assertNotIn(_QUOTE_BREAKOUT_PAYLOAD_SHORT, body)
