"""Search must offer exactly the wikis whose pages the searcher can open.

Wiki access is a four-clause place-domain rule (``services.wiki.wiki_access``):
a pin on the exact Location, a pin sharing the place's domain root, membership
reached through aggregate places, or an explicit grant. The search providers
never called it. They asked ``location__pins__profile=profile`` - clause one
only - and added a ``created_by`` clause the authority does not have.

That is wrong in both directions, and both directions are visible to a user:

- **Too narrow.** A pin on a building inside the same parcel unlocks the wiki
  page, but the wiki did not come up in search. The user could reach it only by
  navigating from the pin.
- **Too broad.** Creating a wiki is not one of the four clauses, so a creator
  with no pin was offered a search result whose page answers 404 - and 404
  rather than 403 deliberately, so the "fix" of relaxing the page would leak
  the existence of a location.

Both are asserted against the real page, not against a restatement of the rule,
so the two cannot drift apart again without a failure here.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search.engine import GlobalSearchEngine
from urbanlens.dashboard.services.places import resolution

from .test_places_campus import make_place, square as _square

_WIKI_NAME = "Zephyr Grain Elevator"


def _wiki_titles(response) -> list[str]:
    for group in response.groups:
        if group.meta.slug == "wikis":
            return [result.title for result in group.results]
    return []


class SearchMatchesWikiAccessTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile

        self.parcel = make_place(PlaceKind.PARCEL, _square(-74.0, 40.0, 0.003))
        self.wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        resolution.resolve_location_place(self.wiki_location)
        self.wiki = baker.make(Wiki, location=self.wiki_location, name=_WIKI_NAME)

    def _page_status(self) -> int:
        self.client.force_login(self.user)
        return self.client.get(reverse("location.wiki", kwargs={"location_slug": self.wiki_location.slug})).status_code

    def test_a_pin_on_the_same_domain_finds_the_wiki(self) -> None:
        """The page opens for this user; search has to agree."""
        building = make_place(PlaceKind.BUILDING, _square(-74.001, 40.001, 0.0002), parent=self.parcel)
        inside = Location.objects.create(latitude=40.001, longitude=-74.001)
        resolution.resolve_location_place(inside)
        self.assertEqual(inside.place, building)
        baker.make(Pin, profile=self.profile, location=inside, parent_pin=None)

        self.assertEqual(self._page_status(), 200, "fixture is wrong - the page is not actually reachable")
        self.assertIn(_WIKI_NAME, _wiki_titles(GlobalSearchEngine().search(self.profile, "zephyr grain")))

    def test_an_exact_location_pin_still_finds_it(self) -> None:
        """Clause one, the case that always worked - kept as a control."""
        baker.make(Pin, profile=self.profile, location=self.wiki_location, parent_pin=None)

        self.assertEqual(self._page_status(), 200)
        self.assertIn(_WIKI_NAME, _wiki_titles(GlobalSearchEngine().search(self.profile, "zephyr grain")))

    def test_no_pin_anywhere_finds_nothing(self) -> None:
        self.assertEqual(self._page_status(), 404)
        self.assertNotIn(_WIKI_NAME, _wiki_titles(GlobalSearchEngine().search(self.profile, "zephyr grain")))

    def test_creating_a_wiki_does_not_by_itself_offer_it(self) -> None:
        """A result whose page 404s is worse than no result: it is a dead link
        that also confirms the place exists."""
        Wiki.objects.filter(pk=self.wiki.pk).update(created_by=self.profile)

        self.assertEqual(
            self._page_status(),
            404,
            "the authority grants access on created_by after all - then it belongs in wiki_access, not in search",
        )
        self.assertNotIn(_WIKI_NAME, _wiki_titles(GlobalSearchEngine().search(self.profile, "zephyr grain")))
