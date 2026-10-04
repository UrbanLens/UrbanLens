"""P224: turning "child pin details" on or off swaps the panels that read it in place, without reloading the page."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki

if TYPE_CHECKING:
    from django.http import HttpResponse

#: Every panel of the pin page whose content depends on the setting, with the endpoint it loads from.
_REGIONS = {
    "pin-gallery-panel": "pin.gallery",
    "visit-history-panel": "pin.visits",
    "albums-panel": "pin.albums",
    "comment-panel": "pin.comments",
    "article-sources-list": "pin.article.sources",
}
_TOGGLE = "child-details-toggle"


class _Elements(HTMLParser):
    """Each element's attributes, by id."""

    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if "id" in values:
            self.by_id.setdefault(values["id"], values)


def _elements(response: HttpResponse) -> dict[str, dict[str, str]]:
    parser = _Elements()
    parser.feed(response.content.decode())
    return parser.by_id


class _Pin(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile, name="Maple Street House")
        self.child: Pin = baker.make_recipe(
            "dashboard.pin", profile=self.user.profile, parent_pin=self.pin, pin_type=PinType.BUILDING, name="Barn"
        )

    def page(self, query: str = "") -> HttpResponse:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]) + query)
        self.assertEqual(response.status_code, 200)
        return response

    def regions(self, query: str) -> HttpResponse:
        response = self.client.get(reverse("pin.child_details", args=[self.pin.slug]) + query)
        self.assertEqual(response.status_code, 200)
        return response


class ToggleTests(_Pin):
    def test_the_toggle_swaps_in_place_and_keeps_the_setting_in_the_address(self) -> None:
        toggle = _elements(self.page("?children=0"))[_TOGGLE]

        self.assertEqual(toggle["hx-get"], reverse("pin.child_details", args=[self.pin.slug]) + "?children=1")
        self.assertEqual(toggle["hx-swap"], "none")
        self.assertEqual(toggle["hx-replace-url"], reverse("pin.details", args=[self.pin.slug]) + "?children=1")
        self.assertEqual(toggle["href"], toggle["hx-replace-url"])

    def test_the_toggle_turns_it_back_off(self) -> None:
        toggle = _elements(self.page("?children=1"))[_TOGGLE]

        self.assertTrue(toggle["hx-get"].endswith("?children=0"))
        self.assertEqual(toggle["aria-pressed"], "true")

    def test_an_open_album_survives_the_toggle(self) -> None:
        toggle = _elements(self.page("?children=0&album=summer-2024"))[_TOGGLE]

        self.assertIn("album=summer-2024", toggle["hx-get"])
        self.assertIn("album=summer-2024", toggle["hx-replace-url"])


class RegionTests(_Pin):
    def test_every_region_comes_back_as_an_out_of_band_swap(self) -> None:
        elements = _elements(self.regions("?children=1"))

        for region in [_TOGGLE, *_REGIONS]:
            with self.subTest(region=region):
                self.assertEqual(elements[region].get("hx-swap-oob"), "true")

    def test_every_region_is_on_the_page_to_be_swapped(self) -> None:
        page = _elements(self.page())

        self.assertEqual([region for region in [_TOGGLE, *_REGIONS] if region not in page], [])

    def test_a_region_still_has_its_id_once_it_has_loaded(self) -> None:
        """Panels that load by replacing themselves keep the id, or a second toggle would find nothing to swap."""
        page = _elements(self.page("?children=1"))
        for region in ("visit-history-panel", "albums-panel", "comment-panel"):
            with self.subTest(region=region):
                self.assertEqual(page[region].get("hx-swap"), "outerHTML")
                loaded = self.client.get(page[region]["hx-get"])
                self.assertEqual(loaded.status_code, 200)
                self.assertIn(region, _elements(loaded))

    def test_turned_on_the_regions_ask_for_the_children(self) -> None:
        elements = _elements(self.regions("?children=1"))

        for region, url_name in _REGIONS.items():
            with self.subTest(region=region):
                url = elements[region]["hx-get"]
                self.assertTrue(url.startswith(reverse(url_name, args=[self.pin.slug])))
                self.assertNotIn("children=0", url)
                if region != "article-sources-list":  # Sources includes the children unless told not to
                    self.assertIn("children=1", url)

    def test_turned_off_the_regions_leave_them_out(self) -> None:
        elements = _elements(self.regions("?children=0"))

        for region in _REGIONS:
            with self.subTest(region=region):
                self.assertNotIn("children=1", elements[region]["hx-get"])
        self.assertIn("children=0", elements["article-sources-list"]["hx-get"])

    def test_the_regions_match_what_the_page_renders(self) -> None:
        for setting in ("0", "1"):
            page, regions = (
                _elements(self.page(f"?children={setting}")),
                _elements(self.regions(f"?children={setting}")),
            )
            for region in _REGIONS:
                with self.subTest(setting=setting, region=region):
                    self.assertEqual(regions[region]["hx-get"], page[region]["hx-get"])

    def test_the_toggle_comes_back_flipped(self) -> None:
        toggle = _elements(self.regions("?children=1"))[_TOGGLE]

        self.assertEqual(toggle["aria-pressed"], "true")
        self.assertTrue(toggle["hx-get"].endswith("?children=0"))

    def test_an_open_album_stays_open(self) -> None:
        elements = _elements(self.regions("?children=1&album=summer-2024&album_pin=barn"))

        self.assertIn("album=summer-2024", elements["albums-panel"]["hx-get"])
        self.assertIn("album_pin=barn", elements["albums-panel"]["hx-get"])
        self.assertIn("album=summer-2024", elements[_TOGGLE]["hx-replace-url"])

    def test_the_map_is_told(self) -> None:
        for setting, include in (("1", True), ("0", False)):
            with self.subTest(setting=setting):
                trigger = json.loads(self.regions(f"?children={setting}")["HX-Trigger"])
                self.assertEqual(trigger, {"childDetailsChanged": {"include": include}})

    def test_someone_elses_pin_is_not_found(self) -> None:
        other = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

        response = self.client.get(reverse("pin.child_details", args=[other.slug]) + "?children=1")

        self.assertEqual(response.status_code, 404)


class MapConfigTests(_Pin):
    def test_the_map_reads_the_setting_and_adds_it_to_its_own_requests(self) -> None:
        for setting in ("0", "1"):
            with self.subTest(setting=setting):
                config = _elements(self.page(f"?children={setting}"))["map-annotations-config"]
                self.assertEqual(config["data-child-details"], setting)
                for url in (
                    "data-markup-json-url",
                    "data-detail-pins-json-url",
                    "data-photo-gallery-json-url",
                    "data-boundary-url",
                ):
                    self.assertNotIn("children=", config[url])


#: The wiki page's panels that depend on the setting. Its photo gallery loads into the Manage view's container.
_WIKI_REGIONS = {
    "wiki-gallery-manage": None,
    "albums-panel": "location.wiki.albums",
    "comment-panel": "location.wiki.comments",
    "article-sources-list": "location.wiki.article.sources",
}


class _Wiki(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, name="Maple Street House")
        baker.make(Wiki, location=baker.make(Location), name="Barn", parent_wiki=self.wiki)
        # A pin on the location is what makes the wiki visible to its viewer.
        baker.make(Pin, profile=self.user.profile, location=self.location)

    def page(self, query: str = "") -> HttpResponse:
        response = self.client.get(reverse("location.wiki", args=[self.location.slug]) + query)
        self.assertEqual(response.status_code, 200)
        return response

    def regions(self, query: str) -> HttpResponse:
        response = self.client.get(reverse("location.wiki.child_details", args=[self.location.slug]) + query)
        self.assertEqual(response.status_code, 200)
        return response


class WikiToggleTests(_Wiki):
    def test_the_toggle_swaps_in_place_and_keeps_the_setting_in_the_address(self) -> None:
        toggle = _elements(self.page("?children=0"))[_TOGGLE]

        self.assertEqual(
            toggle["hx-get"], reverse("location.wiki.child_details", args=[self.location.slug]) + "?children=1"
        )
        self.assertEqual(toggle["hx-replace-url"], reverse("location.wiki", args=[self.location.slug]) + "?children=1")
        self.assertEqual(toggle["href"], toggle["hx-replace-url"])


class WikiRegionTests(_Wiki):
    def test_every_region_is_on_the_page_and_comes_back_out_of_band(self) -> None:
        page = _elements(self.page())
        regions = _elements(self.regions("?children=1"))

        for region in [_TOGGLE, *_WIKI_REGIONS]:
            with self.subTest(region=region):
                self.assertIn(region, page)
                self.assertIn(regions[region].get("hx-swap-oob"), ("true", "innerHTML"))

    def test_the_gallery_container_keeps_its_view_state(self) -> None:
        """The Manage view's container is filled, not replaced, so whether it is shown stays the page's business."""
        manage = _elements(self.page())["wiki-gallery-manage"]
        swapped = _elements(self.regions("?children=1"))["wiki-gallery-manage"]

        self.assertEqual(manage.get("data-media-view-panel"), "manage")
        self.assertEqual(swapped.get("hx-swap-oob"), "innerHTML")

    def test_a_swapped_gallery_loads_once_it_is_shown(self) -> None:
        """The Manage view may already have been opened, so the swapped-in loader cannot wait for its first opening."""
        gallery = _elements(self.regions("?children=1"))["wiki-gallery-panel"]

        self.assertEqual(gallery["hx-trigger"], "intersect once")
        self.assertEqual(gallery["hx-get"], reverse("location.wiki.gallery", args=[self.location.slug]) + "?children=1")

    def test_the_regions_match_what_the_page_renders(self) -> None:
        for setting in ("0", "1"):
            page, regions = (
                _elements(self.page(f"?children={setting}")),
                _elements(self.regions(f"?children={setting}")),
            )
            for region in ("wiki-gallery-panel", "albums-panel", "comment-panel", "article-sources-list"):
                with self.subTest(setting=setting, region=region):
                    self.assertEqual(regions[region]["hx-get"], page[region]["hx-get"])

    def test_the_map_is_told(self) -> None:
        trigger = json.loads(self.regions("?children=0")["HX-Trigger"])

        self.assertEqual(trigger, {"childDetailsChanged": {"include": False}})

    def test_the_map_reads_the_setting(self) -> None:
        config = _elements(self.page("?children=1"))["map-annotations-config"]

        self.assertEqual(config["data-child-details"], "1")
        for url in ("data-markup-json-url", "data-detail-pins-json-url", "data-photo-gallery-json-url"):
            self.assertNotIn("children=", config[url])

    def test_a_wiki_the_viewer_cannot_see_is_not_found(self) -> None:
        hidden = baker.make(Wiki, location=baker.make(Location), name="Elsewhere")

        response = self.client.get(reverse("location.wiki.child_details", args=[hidden.location.slug]) + "?children=1")

        self.assertEqual(response.status_code, 404)
