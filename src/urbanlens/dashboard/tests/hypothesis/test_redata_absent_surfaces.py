"""Without REData (``UL_REDATA_API_URL`` unset), every REData-backed surface is cleanly unavailable.

Unavailable means: not scheduled, nothing logged as a failure, and nothing cached - an empty answer cached now would
still be served after REData is configured, for the rest of the cache window.
"""

from __future__ import annotations

from contextlib import ExitStack
import logging
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.core.rate_limiter import external_calls_forbidden
from urbanlens.UrbanLens.settings.app import settings

_REFERENCE_DOCUMENT_SOURCES = ("smithsonian", "loc", "digital_commonwealth", "internet_archive", "chronicling_america")


class _ReachedRedata(BaseException):  # noqa: N818 - a sentinel, not an error a caller should handle
    """Raised where a REData gateway would have been built; a BaseException so no ``except Exception`` hides it."""


def _redata_gateway_bases() -> list[type]:
    from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import RedataCidGateway
    from urbanlens.dashboard.services.apis.locations.google.redata_places_gateway import RedataPlacesGateway
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway
    from urbanlens.dashboard.services.apis.redata_json_gateway import RedataJsonGateway

    return [RedataLocationContextGateway, RedataGateway, RedataJsonGateway, RedataPlacesGateway, RedataCidGateway]


class RedataAbsentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for attribute in ("redata_api_url", "redata_api_key"):
            patcher = mock.patch.object(settings, attribute, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        # Poughkeepsie, NY: inside every geographic gate (USA, New York, CRIS), with an address and a name.
        location = baker.make(
            Location,
            latitude=41.7321,
            longitude=-73.9262,
            street_number="2",
            route="Hudson Heights Drive",
            locality="Poughkeepsie",
            administrative_area_level_1="NY",
            country="US",
            official_name="Hudson River State Hospital",
            official_name_source="google_places",
        )
        self.pin = baker.make(
            Pin,
            profile=baker.make(User).profile,
            location=location,
            parent_pin=None,
            name="Hudson River State Hospital",
        )

    def _never_building_a_redata_gateway(self) -> ExitStack:
        stack = ExitStack()
        for base in _redata_gateway_bases():
            stack.enter_context(mock.patch.object(base, "__post_init__", side_effect=_ReachedRedata(base.__name__)))
        stack.enter_context(external_calls_forbidden())
        return stack

    def test_no_panel_that_passes_its_gate_reaches_for_redata(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import gate_allows, panel_sources

        reached = []
        for source in panel_sources().values():
            if not gate_allows(source, self.pin):
                continue
            with self._never_building_a_redata_gateway():
                try:
                    source.fetch(self.pin)
                except _ReachedRedata:
                    reached.append(source.key)
                except Exception:  # noqa: S112 - only whether REData was reached matters here
                    continue

        self.assertEqual(reached, [], "these panels pass their gate without REData, then fail every fetch")

    def test_no_imagery_provider_reaches_for_redata(self) -> None:
        from urbanlens.dashboard.plugins import plugin_registry

        reached = []
        for provider in [*plugin_registry.street_view_providers(), *plugin_registry.satellite_providers()]:
            with self._never_building_a_redata_gateway():
                try:
                    if hasattr(provider, "get_street_view_slides"):
                        provider.get_street_view_slides(41.7321, -73.9262)
                    else:
                        provider.get_satellite_slides(41.7321, -73.9262)
                except _ReachedRedata:
                    reached.append(provider.service_key)
                except Exception:  # noqa: S112 - only whether REData was reached matters here
                    continue

        self.assertEqual(reached, [])

    def test_the_parcel_tab_is_not_scheduled(self) -> None:
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        self.assertFalse(PropertyRecordsPanelSource().gate(self.pin))

    def test_the_reference_document_galleries_are_unavailable_and_cache_nothing(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import get_panel_source

        for key in _REFERENCE_DOCUMENT_SOURCES:
            with self.subTest(source=key):
                source = get_panel_source(key)
                assert source is not None
                self.assertFalse(source.gate(self.pin))

                items, _ = source.make_gateway().get_media(self.pin.location, ["Hudson River State Hospital"])

                self.assertEqual(items, [])
                self.assertFalse(
                    LocationCache.objects.filter(location=self.pin.location, source=source.cache_source).exists()
                )

    def test_the_street_view_providers_are_settled_unavailable_without_a_warning(self) -> None:
        from urbanlens.dashboard.services.apis.locations.redata_media_gateway import (
            KartaViewStreetViewProvider,
            MapillaryStreetViewProvider,
            PanoramaxStreetViewProvider,
        )
        from urbanlens.dashboard.services.pins.external_data import collect_street_view_slides

        providers = [MapillaryStreetViewProvider(), KartaViewStreetViewProvider(), PanoramaxStreetViewProvider()]
        with (
            mock.patch("urbanlens.dashboard.services.pins.external_data._street_view_gateways", return_value=providers),
            self.assertNoLogs("urbanlens.dashboard.services.pins.external_data", level=logging.WARNING),
        ):
            slides, results = collect_street_view_slides(41.7321, -73.9262)

        self.assertEqual(slides, [])
        self.assertTrue(all(result.ok for result in results), "an unconfigured provider is settled, not degraded")
        for provider in providers:
            with self.subTest(provider=provider.service_key):
                self.assertFalse(provider.get_street_view_slides(41.7321, -73.9262).from_cache, "nothing was cached")

    def test_queued_cris_tasks_end_quietly(self) -> None:
        """Queued while REData was configured, they run after it was taken away."""
        from urbanlens.dashboard.tasks import extract_cris_attachments, fill_cris_campus_details

        with self._never_building_a_redata_gateway():
            self.assertEqual(extract_cris_attachments(self.pin.location_id, "a1b2", [1, 2]), 0)
            self.assertEqual(fill_cris_campus_details(self.pin.location_id), 0)
