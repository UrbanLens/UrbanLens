"""The wiki page must use the shared _page_hero.html component (like every
other page) instead of its own one-off gradient banner - see docs/prompts's
resolution note for why. Also covers the About card's link-chip row picking
up real styling now that it's no longer scoped to only the pin details page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.links.model import WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.wiki.model import Wiki


class WikiPageHeroTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user: User = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.4", longitude="-73.4", street_number="123", route="Main St")
        self.wiki: Wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def _get(self):
        return self.client.get(reverse("location.wiki", args=[self.location.slug]))

    def test_wiki_page_renders_the_shared_page_hero(self) -> None:
        content = self._get().content.decode()
        self.assertIn('class="ul-page-hero ul-page-hero--top" id="wiki-hero"', content)
        self.assertIn('class="ul-page-hero__title wiki-title">Old Mill</h1>', content)

    def test_wiki_page_no_longer_has_its_own_one_off_banner(self) -> None:
        self.assertNotContains(self._get(), 'class="wiki-banner"')

    def test_wiki_address_shown_in_hero_subtitle(self) -> None:
        content = self._get().content.decode()
        self.assertIn('class="ul-page-hero__subtitle wiki-address"', content)
        self.assertIn("123 Main St", content)

    def test_notice_and_action_buttons_still_render_below_the_hero(self) -> None:
        content = self._get().content.decode()
        self.assertIn("wiki-notice", content)
        self.assertIn("Suggest edits", content)

    def test_hero_omits_address_subtitle_when_location_has_none(self) -> None:
        self.location.street_number = ""
        self.location.route = ""
        self.location.save(update_fields=["street_number", "route"])
        content = self._get().content.decode()
        self.assertNotIn("wiki-address", content)


class WikiAboutCardLinkStylingTests(TestCase):
    """The About card's links row (_pin_links_row.html, shared with the pin
    details page) used to rely on CSS scoped to body.page-location-details
    only - it rendered on the wiki page with no chip/spacing styling at all."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user: User = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.4", longitude="-73.4")
        self.wiki: Wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def test_wiki_link_chip_renders_with_its_styling_hook_classes(self) -> None:
        baker.make(WikiLink, wiki=self.wiki, url="https://example.com/history")
        content = self.client.get(reverse("location.wiki", args=[self.location.slug])).content.decode()
        self.assertIn("pin-links-value", content)
        self.assertIn('class="pin-link-chip"', content)
