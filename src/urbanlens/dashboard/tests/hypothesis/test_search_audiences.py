"""A pin's own names find results that only accounts with exactly the same names are shown (P188).

Every account at a place shares one search per provider, built from the names anyone who can see the place knows it
by. A pin's own names get one more search, cached for exactly that set of names. The upstream is faked at each
gateway's boundary; every call answers with results marked ``p188-<n>``, ``n`` being the call's position, so a page
can be traced back to the searches that filled it.
"""

from __future__ import annotations

from contextlib import ExitStack
import itertools
import re
from typing import TYPE_CHECKING, Any
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias, WikiAlias
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.assets.wikimedia import WikimediaGateway
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.apis.locations.redata_search_gateway import RedataSearchGateway
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.photos.pin_photos import external_photos_for_pin
from urbanlens.dashboard.services.pins.external_data import get_panel_source, run_panel_fetch
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

if TYPE_CHECKING:
    from collections.abc import Iterator

OFFICIAL = "Hudson River State Hospital"
PANELS = ("wikimedia", "searxng_images", "gdelt")
PROVIDERS = (*PANELS, "web")
_MARK = re.compile(r"p188-(\d+)\b")


def marks(text: str) -> set[int]:
    """The upstream calls whose results appear in ``text``."""
    return {int(found) for found in _MARK.findall(text)}


def _naming(query: str) -> str:
    """A result's caption naming what was searched for, so the gallery's relevance rule keeps it (P196).

    Custom names are searched casefolded; capitals let an acronym such as HRSH match as one.
    """
    return query.replace('"', "").upper()


class Upstream:
    """Every provider, faked at its gateway boundary; each call answers with one result unique to that call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.viewer = ""
        self._counter = itertools.count(1)

    def _record(self, provider: str, query: str) -> int:
        number = next(self._counter)
        self.calls.append((provider, self.viewer, query))
        return number

    def wikimedia(self, _gateway: object, search_term: str, address: str | None = None) -> Iterator[MediaItem]:
        number = self._record("wikimedia", search_term)
        yield MediaItem(
            url=f"https://upload.wikimedia.org/p188-{number}.jpg",
            thumb_url=f"https://upload.wikimedia.org/thumb/p188-{number}.jpg",
            caption=f"{_naming(search_term)} p188-{number}",
            source="Wikimedia Commons",
            page_url=f"https://commons.wikimedia.org/wiki/File:p188-{number}.jpg",
        )

    def web_images(self, _gateway: object, query: str, *, max_results: int = 10, images: bool = False) -> list[dict]:
        number = self._record("searxng_images", query)
        return [
            {
                "thumbnail": f"https://images.example/p188-{number}.jpg",
                "link": f"https://images.example/page/p188-{number}",
                "title": f"{_naming(query)} p188-{number}",
            },
        ]

    def news(
        self, _gateway: object, query: str, *, max_results: int = 10, months: int | None = None
    ) -> LocationContextEnvelope:
        number = self._record("gdelt", query)
        articles = [
            {
                "title": f"Poughkeepsie story p188-{number}",
                "link": f"https://news.example/p188-{number}",
                "date": "20240101T000000Z",
            },
        ]
        return LocationContextEnvelope(count=len(articles), complete=True, results=articles)

    def web(self, query: str) -> list[dict[str, Any]]:
        number = self._record("web", query)
        return [{"title": f"Result p188-{number}", "link": f"https://web.example/p188-{number}", "snippet": ""}]

    def installed(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(WikimediaGateway, "_generate_media", autospec=True, side_effect=self.wikimedia)
        )
        stack.enter_context(
            mock.patch.object(RedataSearchGateway, "search_web", autospec=True, side_effect=self.web_images)
        )
        stack.enter_context(mock.patch.object(RedataSearchGateway, "search_news", autospec=True, side_effect=self.news))
        stack.enter_context(mock.patch("urbanlens.dashboard.controllers.pin.search_web", side_effect=self.web))
        stack.enter_context(
            mock.patch("urbanlens.dashboard.plugins.builtin.searxng_images.redata_configured", return_value=True)
        )
        stack.enter_context(
            mock.patch("urbanlens.dashboard.plugins.builtin.gdelt.redata_configured", return_value=True)
        )
        stack.enter_context(
            mock.patch("urbanlens.dashboard.services.photos.pin_photos.schedule_panel_fetch", return_value=False)
        )
        return stack

    def calls_with_numbers(self) -> list[tuple[int, str, str]]:
        """``(n, viewer, query)`` for every call, in order."""
        return [(index + 1, viewer, query) for index, (_, viewer, query) in enumerate(self.calls)]

    def numbers(self, provider: str) -> list[tuple[int, str, str]]:
        """``(n, viewer, query)`` for every call to ``provider``, in order."""
        return [
            (index + 1, viewer, query) for index, (name, viewer, query) in enumerate(self.calls) if name == provider
        ]


class AudienceTestCase(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is promoted to site admin
        self.role = baker.make(SubscriptionRole, features=SiteFeature.SEARCH)
        self.upstream = Upstream()
        stack = self.upstream.installed()
        stack.__enter__()
        self.addCleanup(stack.close)

    def place(self, **fields: Any) -> Location:
        fields.setdefault("official_name", OFFICIAL)
        fields.setdefault("official_name_source", "historic_register")
        return baker.make(
            Location,
            latitude=41.7321,
            longitude=-73.9262,
            locality="Poughkeepsie",
            administrative_area_level_1="NY",
            country="US",
            **fields,
        )

    def account(self, location: Location, *aliases: str) -> Pin:
        user = baker.make(User)
        grant_subscription(user, self.role, user, None)
        pin = baker.make(Pin, profile=Profile.objects.get(user=user), location=location)
        for alias in aliases:
            PinAlias.objects.create(pin=pin, name=alias)
        return pin

    def fetch(self, label: str, pin: Pin) -> None:
        """Everything ``pin``'s page would fetch: the three panels and the web search."""
        self.upstream.viewer = label
        for key in PANELS:
            run_panel_fetch(key, pin, None)
        self.client.force_login(pin.profile.user)
        response = self.client.get(reverse("pin.web_search", args=[pin.slug]))
        self.assertIn(response.status_code, (200, 204))

    def visible(self, pin: Pin) -> dict[str, set[int]]:
        """Per provider, the upstream calls whose results ``pin``'s owner is shown."""
        user = pin.profile.user
        photos = external_photos_for_pin(pin, pin.profile, user).photos
        seen = {key: set() for key in PROVIDERS}
        for photo in photos:
            if photo.source in seen:
                seen[photo.source] |= marks(photo.url) | marks(photo.page_url)
        news = get_panel_source("gdelt").api_payload(pin) or {}
        seen["gdelt"] = marks(str(news))
        self.client.force_login(user)
        seen["web"] = marks(self.client.get(reverse("pin.web_search", args=[pin.slug])).content.decode())
        return seen


class SevenAccountsTests(AudienceTestCase):
    """Jess's example: one shared search, one per distinct set of custom names, nobody sees another set's results."""

    SETS = {
        "Bob": (),
        "John": (),
        "Mildred": ("HRSH",),
        "Fred": ("Apple", "Blueberry"),
        "Dolby": ("HRSH", "Blueberry"),
        "Albert": ("HRSH",),
        "Casey": ("Blueberry", "HRSH"),
    }
    #: The account whose fetch made each set's search.
    FIRST = {
        "Bob": "Bob",
        "John": "Bob",
        "Mildred": "Mildred",
        "Fred": "Fred",
        "Dolby": "Dolby",
        "Albert": "Mildred",
        "Casey": "Dolby",
    }

    def setUp(self) -> None:
        super().setUp()
        location = self.place()
        self.pins = {label: self.account(location, *names) for label, names in self.SETS.items()}
        for label, pin in self.pins.items():
            self.fetch(label, pin)

    def test_each_provider_searched_once_for_everyone_and_once_per_distinct_set(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                callers = [viewer for _, viewer, _ in self.upstream.numbers(provider)]
                self.assertEqual(callers, ["Bob", "Mildred", "Fred", "Dolby"])

    def test_no_search_carries_a_name_outside_the_set_it_is_cached_for(self) -> None:
        every_name = {name for names in self.SETS.values() for name in names}
        for provider in PROVIDERS:
            for _, viewer, query in self.upstream.numbers(provider):
                with self.subTest(provider=provider, viewer=viewer):
                    foreign = {
                        name for name in every_name - set(self.SETS[viewer]) if name.casefold() in query.casefold()
                    }
                    self.assertEqual(foreign, set(), query)

    def test_each_account_sees_the_shared_results_and_its_own_sets_and_nothing_else(self) -> None:
        for provider in PROVIDERS:
            by_viewer = {viewer: number for number, viewer, _ in self.upstream.numbers(provider)}
            for label, pin in self.pins.items():
                with self.subTest(provider=provider, account=label):
                    expected = {by_viewer["Bob"], by_viewer[self.FIRST[label]]}
                    self.assertEqual(self.visible(pin)[provider], expected)


class SecondAccountTests(AudienceTestCase):
    """An account without custom names never sees what another account's custom-name search found, anywhere."""

    def setUp(self) -> None:
        super().setUp()
        self.location = self.place()
        baker.make(Wiki, location=self.location, name=OFFICIAL)
        self.owner = self.account(self.location, "Secret Ward")
        self.other = self.account(self.location)
        self.fetch("owner", self.owner)
        self.fetch("other", self.other)
        self.private = {
            number for number, viewer, query in self.upstream.calls_with_numbers() if "secret ward" in query.casefold()
        }
        self.shared = {
            number for number, _, query in self.upstream.calls_with_numbers() if "secret ward" not in query.casefold()
        }

    def get(self, pin: Pin, url: str, **extra: Any) -> str:
        self.client.force_login(pin.profile.user)
        response = self.client.get(url, **extra)
        self.assertLess(response.status_code, 300, url)
        return response.content.decode()

    def api(self, pin: Pin, key: str) -> str:
        api_key, raw = generate_api_key(pin.profile.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.PANELS_READ.value])
        url = reverse("external_api:pins.panels.detail", kwargs={"pin_slug": pin.slug, "panel_key": key})
        return self.get(pin, url, HTTP_AUTHORIZATION=f"Bearer {raw}")

    def pages(self, pin: Pin) -> dict[str, str]:
        wiki_media = "location.wiki.media"
        return {
            "pin media wikimedia": self.get(pin, reverse("pin.media", args=[pin.slug, "wikimedia"])),
            "pin media web images": self.get(pin, reverse("pin.media", args=[pin.slug, "searxng_images"])),
            "photos tab": self.get(pin, reverse("pin.albums", args=[pin.slug]) + "?external=1&limit=100"),
            "wiki media wikimedia": self.get(pin, reverse(wiki_media, args=[self.location.slug, "wikimedia"])),
            "wiki media web images": self.get(pin, reverse(wiki_media, args=[self.location.slug, "searxng_images"])),
            "api wikimedia": self.api(pin, "wikimedia"),
            "api web images": self.api(pin, "searxng_images"),
            "api news": self.api(pin, "gdelt"),
            "news panel": self.get(pin, reverse("pin.panel", args=[pin.slug, "gdelt"])),
            "web search": self.get(pin, reverse("pin.web_search", args=[pin.slug])),
        }

    def test_the_owner_has_custom_results_to_leak(self) -> None:
        self.assertEqual(len(self.private), len(PROVIDERS))
        owner_pages = self.pages(self.owner)
        for page in ("pin media wikimedia", "pin media web images", "api news", "web search"):
            with self.subTest(page=page):
                self.assertTrue(marks(owner_pages[page]) & self.private)

    def test_the_other_account_sees_none_of_them_on_any_page(self) -> None:
        for page, body in self.pages(self.other).items():
            with self.subTest(page=page):
                self.assertEqual(marks(body) & self.private, set())

    def test_the_other_account_still_sees_the_shared_results(self) -> None:
        other_pages = self.pages(self.other)
        for page in ("pin media wikimedia", "pin media web images", "api news", "web search"):
            with self.subTest(page=page):
                self.assertTrue(marks(other_pages[page]) & self.shared)

    def test_wiki_pages_show_only_shared_results_even_to_the_owner(self) -> None:
        owner_pages = self.pages(self.owner)
        for page in ("wiki media wikimedia", "wiki media web images"):
            with self.subTest(page=page):
                self.assertEqual(marks(owner_pages[page]) & self.private, set())


class NoSharedNameTests(AudienceTestCase):
    """A place nobody has named publicly has no shared search; its owner's own names still find results."""

    def setUp(self) -> None:
        super().setUp()
        self.location = self.place(official_name="")
        self.owner = self.account(self.location, "Secret Ward")
        self.other = self.account(self.location)

    def test_only_the_owners_search_is_made(self) -> None:
        self.fetch("owner", self.owner)
        self.fetch("other", self.other)

        for provider in ("wikimedia", "searxng_images", "web"):
            with self.subTest(provider=provider):
                calls = self.upstream.numbers(provider)
                self.assertEqual([viewer for _, viewer, _ in calls], ["owner"])
                self.assertIn("secret ward", calls[0][2].casefold())

    def test_the_owner_sees_their_results_and_the_other_account_sees_none(self) -> None:
        self.fetch("owner", self.owner)
        self.fetch("other", self.other)

        owner, other = self.visible(self.owner), self.visible(self.other)
        for provider in ("wikimedia", "searxng_images", "web"):
            with self.subTest(provider=provider):
                self.assertTrue(owner[provider])
                self.assertEqual(other[provider], set())


class SharedNamesAreNotCustomTests(AudienceTestCase):
    """A pin alias that only restates a public name, in any case or spacing, costs no extra search."""

    def test_restating_the_official_name_or_a_wiki_alias_adds_no_search(self) -> None:
        location = self.place()
        wiki = baker.make(Wiki, location=location, name=OFFICIAL)
        WikiAlias.objects.create(wiki=wiki, name="HRSH")
        WikiAlias.objects.create(wiki=wiki, name="Kirkbride Asylum", kind=AliasType.NICKNAME)
        plain = self.account(location)
        restating = self.account(location, "  hudson RIVER   state hospital ", "hrsh")

        self.fetch("plain", plain)
        self.fetch("restating", restating)

        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual([viewer for _, viewer, _ in self.upstream.numbers(provider)], ["plain"])

    def test_a_nickname_is_never_searched(self) -> None:
        location = self.place()
        pin = self.account(location)
        PinAlias.objects.create(pin=pin, name="The Castle", kind=AliasType.NICKNAME)

        self.fetch("nicknamed", pin)

        for _, _, query in self.upstream.calls:
            self.assertNotIn("Castle", query)
        self.assertEqual(LocationCache.objects.filter(location=location).exclude(audience="").count(), 0)


class SiteAdoptionTests(RedataConfiguredMixin, TestCase):
    """A building nested under a site takes the site's shared answer, never one cached for someone's own names."""

    def test_a_building_copies_only_the_sites_shared_row(self) -> None:
        profile = Profile.objects.get(user=baker.make(User))
        site = baker.make(Pin, profile=profile, location=baker.make(Location, latitude=41.7321, longitude=-73.9262))
        building = baker.make(
            Pin, profile=profile, parent_pin=site, location=baker.make(Location, latitude=41.7326, longitude=-73.9258)
        )
        source = get_panel_source("nps")
        LocationCache.set(site.location, source.cache_source, {"park_code": "vama"}, query_key="site")
        LocationCache.set(site.location, source.cache_source, {"park_code": "own"}, query_key="own", audience="f" * 64)

        with mock.patch.object(type(source), "fetch") as fetch:
            run_panel_fetch("nps", building, None)

        fetch.assert_not_called()
        rows = LocationCache.objects.filter(location=building.location, source=source.cache_source)
        self.assertEqual([(row.audience, row.data) for row in rows], [("", {"park_code": "vama"})])
