"""The wiki page's "other location" conflict notice must not link to the other
location's own wiki page - a wiki page 404s for anyone without a pin there
(see services.wiki_access.resolve_visible_wiki), and the whole premise of this
notice is that the viewer's pin is NOT at that other location yet. Only the
"Switch" button (which relinks the pin first) can ever actually get there.
"""

from __future__ import annotations

import types

from django.template.loader import render_to_string
from django.utils.html import escape

from urbanlens.core.tests.testcase import SimpleTestCase


def _fake_location(**overrides: object) -> types.SimpleNamespace:
    defaults: dict[str, object] = {"slug": "some-place", "uuid": "11111111-1111-1111-1111-111111111111", "display_name": "41°43'53.3\"N 73°55'36.2\"W"}
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class WikiLocationConflictNoticeTests(SimpleTestCase):
    def _render(self, other_locations, user_pin=None):
        return render_to_string(
            "dashboard/pages/location/wiki.html",
            {
                "other_locations": other_locations,
                "user_pin": user_pin,
                "location": _fake_location(slug="current-place"),
            },
        )

    def test_other_location_name_is_not_a_link(self) -> None:
        other = _fake_location()
        html = self._render([other])
        self.assertNotIn(f'href="/dashboard/location/{other.slug}/wiki/"', html)
        self.assertIn('<span class="wiki-lcn-link">', html)
        # display_name contains quote characters (a DMS coordinate string),
        # which get HTML-escaped on render - compare against the escaped form.
        self.assertIn(escape(other.display_name), html)

    def test_switch_button_still_present_when_viewer_has_a_pin(self) -> None:
        other = _fake_location()
        user_pin = types.SimpleNamespace(slug="my-pin")
        html = self._render([other], user_pin=user_pin)
        self.assertIn("wiki-lcn-switch-btn", html)
        self.assertIn(f"/dashboard/map/pin/{user_pin.slug}/link/{other.slug}/", html)

    def test_notice_omitted_entirely_with_no_conflicting_locations(self) -> None:
        html = self._render([])
        self.assertNotIn("wiki-location-conflict", html)
