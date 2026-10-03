"""The REData reference-documents plugin: the archive providers' shared rate budget, and no nearby panel (P225)."""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.pins.external_data import get_panel_source

_NEARBY = "redata_reference_documents_nearby"
_migration = importlib.import_module(
    "urbanlens.dashboard.migrations.0042_location_cache_drop_nearby_reference_documents"
)


class NearbyPanelRemovedTests(TestCase):
    """The panel titled HRSH's Wikidata facts with the first Wikipedia article REData listed within a kilometre,
    "Marist University": REData doesn't sort those articles by distance, and the panel matched by radius alone."""

    def test_no_panel_source_answers_for_its_key(self) -> None:
        self.assertIsNone(get_panel_source(_NEARBY))

    def test_a_places_subscriber_gets_404_from_its_url(self) -> None:
        baker.make(User)  # the first user in a fresh test DB becomes a site admin, who holds every feature
        location = baker.make(Location, latitude=41.72, longitude=-73.93)
        pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)
        grant_subscription(
            pin.profile.user, baker.make(SubscriptionRole, features=SiteFeature.PLACES), pin.profile.user, None
        )
        LocationCache.set(location, _NEARBY, {"documents": [{"provider": "wikipedia", "title": "Marist University"}]})
        self.client.force_login(pin.profile.user)

        response = self.client.get(reverse("pin.panel", args=[pin.slug, _NEARBY]))

        self.assertEqual(response.status_code, 404)

    def test_the_migration_drops_its_cached_rows_and_keeps_every_other_source(self) -> None:
        location = baker.make(Location)
        LocationCache.set(location, _NEARBY, {"documents": []})
        LocationCache.set(location, "wikipedia", {"title": "Hudson River State Hospital"})

        _migration.drop_nearby_reference_documents(apps, None)

        self.assertEqual(
            sorted(LocationCache.objects.filter(location=location).values_list("source", flat=True)), ["wikipedia"]
        )


class RateLimitTests(TestCase):
    """The Media gallery's archive providers all search through REData's reference documents, on one shared budget."""

    def test_the_shared_service_key_keeps_its_declared_defaults(self) -> None:
        config = rate_limiter.get_limit_config("redata_reference_documents")
        self.assertEqual(config.display_name, "REData Reference Documents")
        self.assertEqual(config.calls_per_minute, 20)
        self.assertIsNone(config.calls_per_day)
        self.assertNotIn("near-coordinate", config.notes)
