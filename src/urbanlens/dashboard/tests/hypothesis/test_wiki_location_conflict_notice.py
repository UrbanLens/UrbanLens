"""The wiki page's "other property" conflict notice.

This notice used to list every Location whose boundary covered the viewer's
pin - which, on a property with imported buildings, meant every building on it,
none of which was a competing answer. Since resolution onto a single ``Place``,
the list holds only genuinely competing properties (two unrelated parcels whose
county geometry overlaps), and ``services.places.ambiguity`` filters those to
domains the viewer can already reach.

That filter is what makes linking the candidates safe. Before it, a name here
had to render as inert text: a wiki page 404s for anyone without access, and
the whole premise of the notice was that the viewer wasn't there yet.
"""

from __future__ import annotations

import types

from django.template.loader import render_to_string
from django.utils.html import escape

from urbanlens.core.tests.testcase import SimpleTestCase


def _fake_location(**overrides: object) -> types.SimpleNamespace:
    defaults: dict[str, object] = {
        "slug": "some-place",
        "uuid": "11111111-1111-1111-1111-111111111111",
        "display_name": "41°43'53.3\"N 73°55'36.2\"W",
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class WikiLocationConflictNoticeTests(SimpleTestCase):
    def _render(self, other_locations, user_pin=None):
        location = _fake_location(slug="current-place")
        return render_to_string(
            "dashboard/pages/location/wiki.html",
            {
                "other_locations": other_locations,
                "user_pin": user_pin,
                "location": location,
                # wiki.html's add-link dialog and About card both reference
                # `wiki.location.slug` unconditionally (outside any {% if %}
                # guard) to build a reverse-url argument, which raises
                # NoReverseMatch if `wiki` is missing from context - unlike a
                # bare undefined variable, which Django resolves to "" and
                # renders silently. The real view always supplies a genuine
                # Wiki here (you can't view a wiki page without one existing).
                "wiki": types.SimpleNamespace(location=location),
            },
        )

    def test_other_property_name_links_to_its_wiki(self) -> None:
        """Safe only because the candidate list is pre-filtered to what the
        viewer can already open - see the module docstring."""
        other = _fake_location()
        html = self._render([other])
        self.assertIn(f'href="/dashboard/location/{other.slug}/wiki/"', html)
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
