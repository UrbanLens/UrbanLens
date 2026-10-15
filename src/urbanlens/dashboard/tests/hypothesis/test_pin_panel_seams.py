"""
The Private Pin page, the wiki, the bootstrap and the Places layer take their panels, gallery providers and optional services from seams.

Every source here is a fake registered for the test, so none of it depends on which plugins are installed.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.points import (
    LOCATION_PREFETCHERS,
    LOCATION_PREWARMERS,
    NEARBY_PARK_PROVIDERS,
    PANEL_SOURCES,
)
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.request_upstreams import NationalParksUpstream
from urbanlens.dashboard.services.locations.prefetch import run_prefetchers
from urbanlens.dashboard.services.map.nearby_places import NearbySources, find_nearby_places
from urbanlens.dashboard.services.pins import bootstrap
from urbanlens.dashboard.services.pins.external_data import (
    GalleryMediaSource,
    InfoPanelSource,
    LocationCachePanelSource,
    PanelPlacement,
    bootstrap_panels,
    card_panels,
    gallery_label,
    gallery_sources,
)


class _Source(LocationCachePanelSource):
    """A cache-backed source that is always ready and fetches nothing."""

    def is_ready(self, pin) -> bool:
        return True

    def fetch(self, pin) -> None:
        return None


def _card(key: str, order: int, view: str = "pin.usgs_topo") -> _Source:
    source = type(
        f"Card_{key}",
        (_Source,),
        {
            "key": key,
            "cache_source": key,
            "section_id": f"{key}-section",
            "icon": "bolt",
            "title": f"Card {key}",
            "card_view": view,
            "card_order": order,
        },
    )
    return source()


def _gallery(key: str, order: int | None, label: str = "", *, wiki: bool = True) -> GalleryMediaSource:
    def media_items(self, data: dict) -> list[MediaItem]:
        return [
            MediaItem(
                url=f"https://example.test/{key}/{n}.jpg",
                thumb_url=f"https://example.test/{key}/t{n}.jpg",
                caption=f"{key} {n}",
                source=label or key,
            )
            for n, _ in enumerate(data.get("items", []))
        ]

    source = type(
        f"Gallery_{key}",
        (_Source, GalleryMediaSource),
        {
            "key": key,
            "cache_source": key,
            "media_items": media_items,
            "gallery_order": order,
            "gallery_label": label,
            "in_wiki_gallery": wiki,
        },
    )
    return source()


def _bootstrapped(key: str, order: int | None, *, gated: bool = True) -> _Source:
    source = type(
        f"Bootstrapped_{key}",
        (_Source,),
        {
            "key": key,
            "cache_source": key,
            "bootstrap_order": order,
            "gate": lambda self, pin: gated,
            "has_landed": lambda self, pin: False,
        },
    )
    return source()


class _Base(TestCase):
    def with_sources(self, *sources) -> None:
        context = PANEL_SOURCES.overridden([*PANEL_SOURCES, *sources])
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)


class RegistryTests(_Base):
    def test_the_gallery_is_the_declared_sources_in_their_order(self) -> None:
        self.with_sources(
            _gallery("seam_b", 202, "B"), _gallery("seam_a", 201, "A"), _gallery("seam_none", None, "None")
        )

        keys = [source.key for source in gallery_sources()]

        self.assertLess(keys.index("seam_a"), keys.index("seam_b"))
        self.assertNotIn("seam_none", keys)

    def test_the_wiki_gallery_leaves_out_the_sources_that_opt_out(self) -> None:
        self.with_sources(_gallery("seam_wiki", 201), _gallery("seam_pin_only", 202, wiki=False))

        self.assertIn("seam_pin_only", [source.key for source in gallery_sources()])
        wiki_keys = [source.key for source in gallery_sources(wiki=True)]
        self.assertIn("seam_wiki", wiki_keys)
        self.assertNotIn("seam_pin_only", wiki_keys)

    def test_a_label_is_the_sources_own_and_falls_back_to_its_key(self) -> None:
        self.with_sources(_gallery("seam_labelled", 201, "Labelled"), _gallery("seam_plain", 202))

        self.assertEqual(gallery_label("seam_labelled"), "Labelled")
        self.assertEqual(gallery_label("seam_plain"), "seam_plain")
        self.assertEqual(gallery_label("no_such_source"), "no_such_source")

    def test_cards_come_in_card_order_and_only_for_sources_that_name_a_view(self) -> None:
        late, early = _card("seam_late", 900), _card("seam_early", 800)
        plain = _bootstrapped("seam_plain_card", None)

        self.assertEqual(card_panels([late, plain, early]), [early, late])

    def test_the_bootstrap_panels_are_those_declaring_an_order_lowest_first(self) -> None:
        self.with_sources(
            _bootstrapped("seam_second", 902), _bootstrapped("seam_first", 901), _bootstrapped("seam_never", None)
        )

        keys = [source.key for source in bootstrap_panels()]

        self.assertLess(keys.index("seam_first"), keys.index("seam_second"))
        self.assertNotIn("seam_never", keys)


class PinPageTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)
        self.client.force_login(self.user)

    def _html(self) -> str:
        return self.client.get(reverse("pin.details", args=[self.pin.slug])).content.decode()

    def test_a_panel_naming_a_card_view_gets_its_card(self) -> None:
        self.with_sources(_card("seam_card", 901))

        html = self._html()

        self.assertRegex(
            html,
            r'id="seam_card-section"[^>]*\s+hx-get="' + re.escape(reverse("pin.usgs_topo", args=[self.pin.slug])) + '"',
        )
        self.assertIn('data-ul-lazy-section="pin:seam_card"', html)
        self.assertIn('data-collapse-section="seam_card"', html)

    def test_cards_follow_card_order(self) -> None:
        self.with_sources(_card("seam_later", 902), _card("seam_sooner", 901))

        html = self._html()

        self.assertLess(html.index('id="seam_sooner-section"'), html.index('id="seam_later-section"'))

    def test_a_card_known_to_be_empty_is_left_out(self) -> None:
        self.with_sources(_card("seam_empty", 901))
        with mock.patch("urbanlens.dashboard.services.pins.panel_probe.probe_panels") as probe:
            probe.return_value = mock.Mock(empty_cards={"seam_empty"}, empty_galleries=set())
            self.assertNotIn('id="seam_empty-section"', self._html())

    def test_every_gallery_source_gets_a_loader_in_order(self) -> None:
        self.with_sources(_gallery("seam_g2", 902, "G2"), _gallery("seam_g1", 901, "G1"))
        self.user.user_permissions.add(*Permission.objects.filter(codename="view_site_admin"))
        self.client.force_login(User.objects.get(pk=self.user.pk))

        loaders = re.findall(r'id="media-loader-([a-z0-9_]+)"', self._html())

        self.assertLess(loaders.index("seam_g1"), loaders.index("seam_g2"))
        self.assertEqual(set(loaders) - {"photos"}, {source.key for source in gallery_sources()})

    def test_a_gallery_tile_carries_its_sources_tab_label(self) -> None:
        source = _gallery("seam_tiles", 901, "Seam Tiles")
        self.with_sources(source)
        LocationCache.set(self.pin.location, "seam_tiles", {"items": [1]}, query_key="q")

        html = self.client.get(reverse("pin.media", args=[self.pin.slug, "seam_tiles"])).content.decode()

        self.assertIn('data-media-source="seam_tiles"', html)
        self.assertIn('data-media-source-label="Seam Tiles"', html)

    def test_an_info_panel_hidden_on_site_scope_is_left_out_of_a_parcel_page(self) -> None:
        source = type(
            "Hidden",
            (_Source, InfoPanelSource),
            {
                "key": "seam_hidden",
                "cache_source": "seam_hidden",
                "section_id": "seam-hidden-section",
                "title": "Seam hidden",
                "placement": PanelPlacement.STANDALONE,
                "hidden_on_site_scope": True,
                "render_context": lambda self, pin, data: None,
            },
        )()
        self.with_sources(source)

        self.assertIn('id="seam-hidden-section"', self._html())
        with mock.patch("urbanlens.dashboard.services.locations.site_scope.is_site_scope", return_value=True):
            self.assertNotIn('id="seam-hidden-section"', self._html())


class WikiPageTests(_Base):
    def test_the_wiki_loads_the_wiki_gallery_sources_in_order(self) -> None:
        baker.make(User)
        user = baker.make(User)
        location = baker.make("dashboard.Location")
        baker.make(Wiki, location=location, name="Mill Wiki")
        baker.make("dashboard.Pin", profile=user.profile, location=location, name="Mill", name_is_user_provided=True)
        self.with_sources(_gallery("seam_w2", 902), _gallery("seam_w1", 901), _gallery("seam_off", 903, wiki=False))
        self.client.force_login(user)

        html = self.client.get(reverse("location.wiki", args=[location.slug])).content.decode()

        loaders = re.findall(r'id="wiki-media-loader-([a-z0-9_]+)"', html)
        self.assertLess(loaders.index("seam_w1"), loaders.index("seam_w2"))
        self.assertIn("photos", loaders)
        self.assertNotIn("seam_off", loaders)


class _Prewarmer:
    def __init__(self, accepts: bool) -> None:
        self.accepts = accepts
        self.asked: list[Any] = []

    def prewarm(self, location) -> bool:
        self.asked.append(location)
        return self.accepts


class BootstrapTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.pin = baker.make_recipe("dashboard.pin")

    def test_the_site_panels_scheduled_are_the_bootstrap_panels_that_pass_their_gates(self) -> None:
        self.with_sources(_bootstrapped("seam_open", 901), _bootstrapped("seam_shut", 902, gated=False))
        # Whatever else is registered is irrelevant here; only the two fakes matter.
        with mock.patch("urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch", return_value=True):
            scheduled = bootstrap.schedule_site_panels(self.pin)

        self.assertIn("seam_open", scheduled)
        self.assertNotIn("seam_shut", scheduled)

    def test_every_prewarmer_is_asked_and_any_acceptance_counts(self) -> None:
        refusing, accepting = _Prewarmer(False), _Prewarmer(True)

        with LOCATION_PREWARMERS.overridden([refusing, accepting]):
            accepted = bootstrap.prewarm_location(self.pin.location)

        self.assertTrue(accepted)
        self.assertEqual((refusing.asked, accepting.asked), ([self.pin.location], [self.pin.location]))

    def test_a_location_without_coordinates_is_not_prewarmed(self) -> None:
        prewarmer = _Prewarmer(True)
        self.pin.location.latitude = None

        with LOCATION_PREWARMERS.overridden([prewarmer]):
            self.assertFalse(bootstrap.prewarm_location(self.pin.location))

        self.assertEqual(prewarmer.asked, [])

    def test_with_no_prewarmer_nothing_is_accepted(self) -> None:
        with LOCATION_PREWARMERS.overridden([]):
            self.assertFalse(bootstrap.prewarm_location(self.pin.location))


class PrefetchTests(TestCase):
    calls: ClassVar[list[str]] = []

    def test_one_prefetcher_failing_does_not_stop_the_others(self) -> None:
        seen: list[tuple[float, float]] = []

        class Failing:
            def prefetch(self, location, latitude, longitude) -> None:
                raise RuntimeError("down")

        class Recording:
            def prefetch(self, location, latitude, longitude) -> None:
                seen.append((latitude, longitude))

        with LOCATION_PREFETCHERS.overridden([Failing(), Recording()]):
            run_prefetchers(baker.make("dashboard.Location"), 41.5, -73.9)

        self.assertEqual(seen, [(41.5, -73.9)])


class _Parks:
    def __init__(self, available: bool, name: str) -> None:
        self._available = available
        self.name = name

    def available(self) -> bool:
        return self._available

    def parks_near(self, latitude: float, longitude: float, *, limit: int) -> list[dict[str, Any]]:
        return [{"place_id": self.name, "name": self.name, "lat": latitude, "lng": longitude}]


class NearbyParksTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        NationalParksUpstream.reset()
        self.addCleanup(NationalParksUpstream.reset)

    def _parks(self):
        return find_nearby_places(
            41.5,
            -73.9,
            radius=1000,
            sources=NearbySources(landmarks=False, parks=True, wikipedia=False),
            caller=None,
            ttl=60,
        )

    def test_the_first_available_catalogue_answers(self) -> None:
        with NEARBY_PARK_PROVIDERS.overridden([_Parks(False, "down"), _Parks(True, "up"), _Parks(True, "spare")]):
            answer = self._parks()

        self.assertEqual([place["name"] for place in answer.places], ["up"])
        self.assertEqual(answer.incomplete, [])

    def test_with_no_catalogue_there_are_no_parks_and_nothing_is_missing(self) -> None:
        with NEARBY_PARK_PROVIDERS.overridden([_Parks(False, "down")]):
            answer = self._parks()

        self.assertEqual((answer.places, answer.incomplete), ([], []))
