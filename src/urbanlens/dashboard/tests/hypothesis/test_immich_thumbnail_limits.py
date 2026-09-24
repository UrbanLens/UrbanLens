"""One account pointed at a slow Immich server must not be able to fill every request thread.

Both thumbnail routes fetch from a server the account holder chose, on the request. Each is
throttled per account, takes a slot in a per-process bound, and a slot in a fleet-wide per-account
bound; over either bound it answers 503 with Retry-After at once rather than waiting for a slot.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import resolve, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.immich import ImmichProfileSlots, ImmichThumbnailSlots
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionStatus
from urbanlens.dashboard.services.core import bounded_cache

THUMBNAIL = "urbanlens.dashboard.controllers.immich.ImmichGateway.get_asset_thumbnail"


class _ThumbnailRouteBase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.account = ImmichAccount.objects.create(
            profile=self.profile, server_url="https://photos.example.com", api_key="k"
        )
        self.pin = baker.make(Pin, profile=self.profile)
        self.suggestion = baker.make(
            PinSuggestion,
            profile=self.profile,
            status=PinSuggestionStatus.PENDING,
            sample_assets=[{"asset_id": "asset-1"}],
        )
        self.client.force_login(self.user)
        ImmichThumbnailSlots.reset()
        self.addCleanup(ImmichThumbnailSlots.reset)
        self.addCleanup(bounded_cache.delete_quietly, f"ul_immich_thumb_{self.account.pk}_asset-1", label="test")

    def urls(self) -> list[str]:
        return [
            reverse("pin.immich.thumbnail", args=[self.pin.slug, "asset-1"]),
            reverse("memories.locations.immich_thumbnail", args=[self.suggestion.pk, "asset-1"]),
        ]


class ThumbnailRoutesAreThrottledTests(_ThumbnailRouteBase):
    def test_both_routes_are_throttled_per_account_on_get(self) -> None:
        for url in self.urls():
            with self.subTest(url=url):
                view = resolve(url).func
                self.assertEqual(getattr(view, "throttle_scope", None), "immich.thumbnail")
                self.assertIn("GET", getattr(view, "throttle_methods", ()))


class ProcessSlotTests(_ThumbnailRouteBase):
    def test_a_full_process_answers_503_without_contacting_immich(self) -> None:
        for url in self.urls():
            with (
                self.subTest(url=url),
                mock.patch.object(ImmichThumbnailSlots, "limit", return_value=1),
                mock.patch(THUMBNAIL) as fetch,
            ):
                ImmichThumbnailSlots.reset()
                semaphore = ImmichThumbnailSlots.semaphore()
                semaphore.acquire()
                try:
                    response = self.client.get(url)
                finally:
                    semaphore.release()

                self.assertEqual(response.status_code, 503)
                self.assertTrue(response.headers.get("Retry-After"))
                fetch.assert_not_called()

    def test_a_cached_thumbnail_needs_no_slot(self) -> None:
        bounded_cache.set_if_small(f"ul_immich_thumb_{self.account.pk}_asset-1", b"img", "image/jpeg", 60, label="test")
        with mock.patch.object(ImmichThumbnailSlots, "limit", return_value=1), mock.patch(THUMBNAIL) as fetch:
            ImmichThumbnailSlots.reset()
            semaphore = ImmichThumbnailSlots.semaphore()
            semaphore.acquire()
            try:
                response = self.client.get(self.urls()[0])
            finally:
                semaphore.release()

        self.assertEqual(response.status_code, 200)
        fetch.assert_not_called()


class AccountSlotTests(_ThumbnailRouteBase):
    def test_an_account_holding_all_its_slots_is_refused_elsewhere(self) -> None:
        """The slots are shared by every process, so holding them here stands in for another worker holding them."""
        for url in self.urls():
            with (
                self.subTest(url=url),
                mock.patch.object(ImmichProfileSlots, "limit", return_value=1),
                mock.patch(THUMBNAIL) as fetch,
                ImmichProfileSlots.hold(self.profile.pk) as held,
            ):
                self.assertTrue(held)
                response = self.client.get(url)

                self.assertEqual(response.status_code, 503)
                fetch.assert_not_called()

    def test_another_accounts_full_slots_do_not_refuse_this_one(self) -> None:
        other = baker.make(User).profile
        with (
            mock.patch.object(ImmichProfileSlots, "limit", return_value=1),
            mock.patch(THUMBNAIL, return_value=(b"img", "image/jpeg")),
            ImmichProfileSlots.hold(other.pk),
        ):
            response = self.client.get(self.urls()[0])

        self.assertEqual(response.status_code, 200)

    def test_a_fetch_gives_its_slots_back(self) -> None:
        with (
            mock.patch.object(ImmichProfileSlots, "limit", return_value=1),
            mock.patch(THUMBNAIL, return_value=(b"img", "image/jpeg")),
        ):
            self.assertEqual(self.client.get(self.urls()[0]).status_code, 200)
            with ImmichProfileSlots.hold(self.profile.pk) as held:
                self.assertTrue(held, "the fetch kept its account slot")
            self.assertEqual(ImmichThumbnailSlots.semaphore()._value, ImmichThumbnailSlots.limit())

    def test_a_failed_fetch_gives_its_slots_back(self) -> None:
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        with (
            mock.patch.object(ImmichProfileSlots, "limit", return_value=1),
            mock.patch(THUMBNAIL, side_effect=GatewayRequestError("slow")),
        ):
            self.assertEqual(self.client.get(self.urls()[0]).status_code, 502)
            with ImmichProfileSlots.hold(self.profile.pk) as held:
                self.assertTrue(held)
