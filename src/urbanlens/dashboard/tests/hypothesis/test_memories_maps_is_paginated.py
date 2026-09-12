"""The Maps subpage renders every map the account has ever drawn, in one response.

N21 H31 and H37, which are the same page from two angles. `MemoriesMapsView`
builds a card per `MarkupMap` the profile owns with no pagination and no
ceiling, and each card calls `to_snapshot()` - so the response carries every
annotation of every map, and the page's own advertised workflow (draw a route
on a check-in, a comment, a visit) is what makes an account have many of them.

The per-map half is already bounded: `to_snapshot()` is capped at
`MARKUP_MAX_ITEMS_PER_RESPONSE`. What is left is the number of maps, and that
is what this covers. Paginated rather than capped, because a map the user drew
and can no longer reach is worse than a second page - and unlike a map layer,
a list of cards has an obvious place to put a Next button.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.memories import _MEMORIES_MAPS_PAGE_SIZE
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.profile.model import Profile


class _MapsPageCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _seed(self, count: int, items_each: int = 2) -> list[MarkupMap]:
        made = []
        for index in range(count):
            markup_map = baker.make(
                MarkupMap,
                profile=self.profile,
                title=f"Map {index:03d}",
                center_latitude=40.0,
                center_longitude=-74.0,
                zoom=13,
            )
            for item in range(items_each):
                baker.make(
                    PinMarkup,
                    parent_map=markup_map,
                    profile=self.profile,
                    markup_type="line",
                    geometry={
                        "type": "LineString",
                        "coordinates": [[-74.0, 40.0 + item * 0.001], [-74.001, 40.0 + item * 0.001]],
                    },
                )
            made.append(markup_map)
        return made

    def _get(self, page: int | None = None):
        return self.client.get(reverse("memories.maps"), {"page": page} if page else {})


class TheResponseIsBoundedTests(_MapsPageCase):
    """One response must not grow with the number of maps drawn."""

    def test_only_one_page_of_cards_is_rendered(self) -> None:
        self._seed(_MEMORIES_MAPS_PAGE_SIZE + 7)

        cards = self._get().context["map_cards"]

        self.assertEqual(len(cards), _MEMORIES_MAPS_PAGE_SIZE)

    def test_the_annotations_of_off_page_maps_are_not_serialised(self) -> None:
        """The cost this exists for is the snapshots, not the card count."""
        maps = self._seed(_MEMORIES_MAPS_PAGE_SIZE + 3)
        off_page = {m.pk for m in sorted(maps, key=lambda m: m.updated)[:3]}

        rendered = {card["map"].pk for card in self._get().context["map_cards"]}

        self.assertEqual(rendered & off_page, set(), "a map past the first page was still serialised")


class ThePageStillWorksTests(_MapsPageCase):
    """The half that stops the bound above passing against a page that renders nothing."""

    def test_a_small_account_sees_every_map(self) -> None:
        self._seed(3)

        cards = self._get().context["map_cards"]

        self.assertEqual(len(cards), 3)

    def test_each_card_still_carries_its_snapshot_and_count(self) -> None:
        self._seed(1, items_each=4)

        card = self._get().context["map_cards"][0]

        self.assertEqual(card["item_count"], 4)
        self.assertEqual(len(card["snapshot"]["markup"]), 4)

    def test_the_second_page_reaches_the_rest(self) -> None:
        """Paginated, not capped: nothing the user drew may become unreachable."""
        self._seed(_MEMORIES_MAPS_PAGE_SIZE + 2)

        cards = self._get(page=2).context["map_cards"]

        self.assertEqual(len(cards), 2)
