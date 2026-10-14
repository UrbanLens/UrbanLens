"""The debug overlay's "clear cache" forgets the cached results the pin page shows, whichever plugin provided them."""

from __future__ import annotations

from collections.abc import Generator

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.plugins import points
from urbanlens.dashboard.services.apis.locations.base import StreetViewProvider


class _ProbeStreetView(StreetViewProvider):
    service_key = "probe_street_view"

    def _generate_street_view_slides(
        self, latitude: float, longitude: float, *, radius: float = 50, limit: int = 5
    ) -> Generator:
        yield from ()


class ClearDebugCacheTests(TestCase):
    def setUp(self) -> None:
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.admin = baker.make(User)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_site_admin"))
        location = Location.objects.create(latitude=39.5, longitude=-75.5)
        self.pin = baker.make(Pin, profile=Profile.objects.get(user=self.admin), location=location)
        for source in ("wikipedia", "web_search", "boundary"):
            LocationCache.objects.create(location=location, source=source, data={"cached": True})
        self.slide_key = _ProbeStreetView().slide_cache_key(39.5, -75.5)
        cache.set(self.slide_key, ["slide"])

    def _clear(self, user: User):
        self.client.force_login(user)
        with points.STREET_VIEW_PROVIDERS.overridden([_ProbeStreetView]):
            return self.client.post(reverse("pin.debug.clear_cache", args=[self.pin.slug]))

    def test_what_the_page_shows_and_registered_carousels_are_forgotten(self) -> None:
        response = self._clear(self.admin)

        self.assertEqual(response.json(), {"cleared": 2})
        self.assertEqual(
            list(LocationCache.objects.filter(location=self.pin.location).values_list("source", flat=True)),
            ["boundary"],
        )
        self.assertIsNone(cache.get(self.slide_key))

    def test_only_a_site_admin_may(self) -> None:
        member = baker.make(User)
        self.pin.profile = Profile.objects.get(user=member)
        self.pin.save(update_fields=["profile"])

        self.assertEqual(self._clear(member).status_code, 403)
        self.assertEqual(LocationCache.objects.filter(location=self.pin.location).count(), 3)
