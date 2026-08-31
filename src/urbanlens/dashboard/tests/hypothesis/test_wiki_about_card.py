"""A wiki with no description/dates/security/links must still offer to add a link.

`_wiki_about_card.html`'s outer guard used to be
`{% if wiki.description or wiki.date_abandoned or wiki.effective_date_last_active
or wiki.links.exists %}`, hiding the whole card - including the links row,
whose "add a link" button (see `_pin_links_row.html`'s `dialog_id`) is the only
entry point for adding one - the moment all four were empty. A wiki that has
never had any of those set could never get its first link short of using
"Suggest Edits" to set some other field first.

See PROBLEMS.md, "a wiki with zero description/dates/security/links has no way
to add its first link".
"""

from __future__ import annotations

from django.template.loader import render_to_string
from django.test import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.models.links.model import WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki

#: Explicit rather than relying on the field defaults - model_bakery does not
#: reliably leave a choices field at its Django-level default, and a random
#: non-"unknown" pick here would render a security chip and silently defeat
#: the "otherwise empty" premise of these tests.
_ALL_UNKNOWN = {field_name: "unknown" for field_name, _label in SECURITY_FIELDS}


def _render_about_card(wiki: Wiki) -> str:
    return render_to_string("dashboard/partials/wiki/_wiki_about_card.html", {"wiki": wiki})


class EmptyWikiRendersTheAddLinkAffordanceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, official_name="Nothing Set Yet")
        self.wiki = baker.make(Wiki, location=self.location, description=None, date_abandoned=None, **_ALL_UNKNOWN)

    def test_the_card_still_renders(self) -> None:
        html = _render_about_card(self.wiki)
        self.assertIn('id="wiki-about-card"', html)

    def test_the_add_link_button_is_present(self) -> None:
        html = _render_about_card(self.wiki)
        self.assertIn("wiki-link-add-dialog", html)
        self.assertIn("No links yet.", html)

    def test_no_description_dates_or_security_chips_render(self) -> None:
        html = _render_about_card(self.wiki)
        self.assertNotIn("wiki-description", html)
        self.assertNotIn("wiki-meta-dates", html)
        self.assertNotIn("security-indicators", html)

    def test_coordinates_are_shown(self) -> None:
        html = _render_about_card(self.wiki)
        self.assertIn("detail-item--coordinates", html)
        self.assertIn("Coordinates", html)


class WikiWithOnlyALinkStillRendersTests(TestCase):
    """Anti-vacuity: the card must actually reflect content, not render blindly."""

    def test_the_link_shows_up(self) -> None:
        location = baker.make(Location, official_name="Has One Link")
        wiki = baker.make(Wiki, location=location, description=None, date_abandoned=None)
        baker.make(WikiLink, wiki=wiki, url="https://example.com/history", name="Local history")

        html = _render_about_card(wiki)

        self.assertIn("example.com", html)
        self.assertNotIn("No links yet.", html)


class WikiAboutCardIdentityFieldsTests(TestCase):
    """Place Name / Official Name / Address / coordinates mirror the pin details card."""

    def test_official_name_and_coordinates_render(self) -> None:
        location = baker.make(
            Location,
            official_name="Riverside Mill",
            latitude="41.73610",
            longitude="-73.75790",
            street_number="42",
            route="Mill St",
        )
        wiki = baker.make(Wiki, location=location, name="Riverside", description=None, date_abandoned=None, **_ALL_UNKNOWN)
        html = _render_about_card(wiki)
        self.assertIn("Official Name", html)
        self.assertIn("Riverside Mill", html)
        self.assertIn("Coordinates", html)
        self.assertIn("41.73610", html)
        self.assertIn("-73.75790", html)
