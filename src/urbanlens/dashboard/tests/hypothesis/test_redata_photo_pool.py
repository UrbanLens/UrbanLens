"""The Nearby Photos tab: this site's photos REData places near a pin, shown only where the viewer may already see them."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.db.models import Q
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.redata_photo_pool import NearbyPhotosSource
from urbanlens.dashboard.services.apis.photos.redata_photos_gateway import RedataPhotosGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

_PARCEL = "5d0c9a8e-3f4b-4a2c-9e1d-7b6a5c4d3e2f"


def _photo_row(photo_id: str, confidence: float | None, *, parcel_uuid: str | None = None) -> dict:
    """A row in REData's ``PhotoSerializer`` shape."""
    return {
        "uuid": "e3a1c2b4-0000-4000-8000-000000000001",
        "photo_id": photo_id,
        "confidence": confidence,
        "scorer": "model",
        "model_version": 3,
        "scored_at": "2026-09-30T00:00:00Z",
        "source": None,
        "uploader_photo_count": 12,
        "photo_latitude": None,
        "photo_longitude": None,
        "location_latitude": 41.7001,
        "location_longitude": -73.9001,
        "taken_at": None,
        "years_from_abandoned": None,
        "years_from_built": None,
        "distance_to_parcel_boundary_m": None,
        "parcel_uuid": parcel_uuid,
        "image_stats": {},
        "upvotes": 0,
        "downvotes": 0,
        "created": "2026-09-30T00:00:00Z",
        "updated": "2026-09-30T00:00:00Z",
    }


class PhotoPoolGatewayTests(SimpleTestCase):
    def _gateway(self, body: object) -> RedataPhotosGateway:
        response = mock.Mock(status_code=200)
        response.json.return_value = body
        session = mock.Mock()
        session.get.return_value = response
        return RedataPhotosGateway(base_url="https://redata.example.test", api_key="k", session=session)

    def test_lookup_near_reads_the_collection(self) -> None:
        gateway = self._gateway({"count": 2, "results": [_photo_row("a", 0.9), {"confidence": 0.1}]})
        rows = gateway.lookup_near(41.7, -73.9)
        self.assertEqual([row["photo_id"] for row in rows], ["a"])
        self.assertTrue(gateway.session.get.call_args.args[0].endswith("/api/v1/photos/lookup/"))
        self.assertEqual(gateway.session.get.call_args.kwargs["params"], {"lat": 41.7, "lng": -73.9})

    def test_parcel_photos_path(self) -> None:
        gateway = self._gateway({"count": 1, "results": [_photo_row("b", 0.5)]})
        self.assertEqual(len(gateway.photos_for_parcel(_PARCEL)), 1)
        self.assertTrue(gateway.session.get.call_args.args[0].endswith(f"/api/v1/parcels/{_PARCEL}/photos/"))


class _PoolCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(settings, "redata_api_url", None))
        baker.make(User)
        self.viewer_user = baker.make(User)
        self.viewer = self.viewer_user.profile
        self.viewer.viewer_photo_filter = VisibilityChoice.ANYONE
        self.viewer.save()
        self.other = baker.make(User).profile
        self.other.photo_upload_visibility = VisibilityChoice.ANYONE
        self.other.save()

        self.here = baker.make(Location, latitude="41.700000", longitude="-73.900000", google_place=None)
        self.pin = baker.make(Pin, profile=self.viewer, location=self.here, name="Old Mill")
        self.next_door = baker.make(Location, latitude="41.700400", longitude="-73.900400", google_place=None)
        self.far_side = baker.make(Location, latitude="41.700800", longitude="-73.900800", google_place=None)
        # The viewer has pinned next door, so its wiki is in their reach; the far side's is not.
        baker.make(Pin, profile=self.viewer, location=self.next_door)
        self.next_door_wiki = baker.make(Wiki, location=self.next_door)
        self.far_side_wiki = baker.make(Wiki, location=self.far_side)

        self.own_next_door = self._image(self.viewer, self.next_door, caption="My shot of the boiler house")
        self.shared_next_door = self._image(self.other, self.next_door, wiki=self.next_door_wiki, caption="Shared")
        self.unshared_next_door = self._image(self.other, self.next_door, caption="Only on their private pin")
        self.shared_out_of_reach = self._image(self.other, self.far_side, wiki=self.far_side_wiki, caption="Far wiki")
        self.own_here = self._image(self.viewer, self.here, caption="Already on this pin")

    def _image(self, profile, location: Location | None, *, wiki: Wiki | None = None, caption: str) -> Image:
        return baker.make(
            Image,
            profile=profile,
            location=location,
            wiki=wiki,
            pin=None,
            image=f"pins/{caption.replace(' ', '_')}.jpg",
            caption=caption,
            pending_scan=False,
        )

    def _payload(self, *images: Image) -> dict:
        return {"photos": [{"photo_id": str(image.uuid), "confidence": 0.5, "on_parcel": False} for image in images]}

    def _all_images(self) -> list[Image]:
        return [
            self.own_next_door,
            self.shared_next_door,
            self.unshared_next_door,
            self.shared_out_of_reach,
            self.own_here,
        ]


class NearbyPhotosVisibilityTests(_PoolCase):
    def test_only_what_the_viewer_may_already_see_elsewhere_is_shown(self) -> None:
        data = NearbyPhotosSource().for_viewer(self._payload(*self._all_images()), self.viewer, self.here)
        captions = sorted(item["caption"] for item in data["items"])
        self.assertEqual(captions, ["My shot of the boiler house", "Shared"])

    def test_the_cached_row_alone_yields_no_tile(self) -> None:
        """A reader that forgets to narrow the row for its viewer shows nothing rather than someone's photo."""
        self.assertEqual(NearbyPhotosSource().media_items(self._payload(*self._all_images())), [])

    def test_a_photo_shown_to_the_viewer_only_through_a_direct_message_is_not_listed(self) -> None:
        named = Q(pk=self.unshared_next_door.pk)
        with mock.patch("urbanlens.dashboard.models.images.queryset._named_this_viewer", return_value=named):
            self.assertTrue(Image.objects.filter(pk=self.unshared_next_door.pk).visible_to(self.viewer).exists())
            data = NearbyPhotosSource().for_viewer(self._payload(self.unshared_next_door), self.viewer, self.here)
        self.assertEqual(data["items"], [])

    def test_a_check_in_photo_filed_in_a_wiki_out_of_reach_is_not_listed(self) -> None:
        """Naming the viewer on a check-in lets them see its photos there, not wherever else the photo is filed."""
        named = Q(pk=self.shared_out_of_reach.pk)
        with mock.patch("urbanlens.dashboard.models.images.queryset._named_this_viewer", return_value=named):
            self.assertTrue(Image.objects.filter(pk=self.shared_out_of_reach.pk).visible_to(self.viewer).exists())
            data = NearbyPhotosSource().for_viewer(self._payload(self.shared_out_of_reach), self.viewer, self.here)
        self.assertEqual(data["items"], [])

    def test_a_photo_shared_to_this_places_own_wiki_is_left_to_that_wiki(self) -> None:
        here_wiki = baker.make(Wiki, location=self.here)
        unfiled = self._image(self.other, None, wiki=here_wiki, caption="Sent straight to this wiki")
        data = NearbyPhotosSource().for_viewer(self._payload(unfiled), self.viewer, self.here)
        self.assertEqual(data["items"], [])

    def test_cached_order_is_kept(self) -> None:
        payload = self._payload(self.shared_next_door, self.own_next_door)
        data = NearbyPhotosSource().for_viewer(payload, self.viewer, self.here)
        self.assertEqual([item["caption"] for item in data["items"]], ["Shared", "My shot of the boiler house"])
        self.assertEqual([item["source"] for item in data["items"]], ["Member photo", "Your photo"])


class NearbyPhotosFetchTests(_PoolCase):
    def _fetch(self, nearby: list[dict], parcel: list[dict] | Exception | None = None) -> mock.Mock:
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "k"),
            mock.patch.object(RedataPhotosGateway, "lookup_near", return_value=nearby),
            mock.patch.object(
                RedataPhotosGateway,
                "photos_for_parcel",
                **({"side_effect": parcel} if isinstance(parcel, Exception) else {"return_value": parcel or []}),
            ) as photos_for_parcel,
        ):
            NearbyPhotosSource().fetch(self.pin)
        return photos_for_parcel

    def test_gated_off_without_redata(self) -> None:
        self.assertFalse(NearbyPhotosSource().gate(self.pin))

    def test_without_a_resolved_parcel_only_the_nearby_lookup_is_asked(self) -> None:
        photos_for_parcel = self._fetch([_photo_row("a", 0.2), _photo_row("b", 0.9)])
        photos_for_parcel.assert_not_called()
        cached = LocationCache.get_fresh(self.here, "redata_photo_pool")
        assert cached is not None
        self.assertEqual([row["photo_id"] for row in cached.data["photos"]], ["b", "a"])

    def test_the_parcels_photos_come_first_with_their_best_score(self) -> None:
        LocationCache.set(self.here, "property_records", {"available": True, "uuid": _PARCEL})
        parcel = [_photo_row("on-parcel", 0.3, parcel_uuid=_PARCEL)]
        self._fetch([_photo_row("near", 0.99), _photo_row("on-parcel", 0.6, parcel_uuid=_PARCEL)], parcel)
        cached = LocationCache.get_fresh(self.here, "redata_photo_pool")
        assert cached is not None
        self.assertEqual(
            cached.data["photos"],
            [
                {"photo_id": "on-parcel", "confidence": 0.6, "on_parcel": True},
                {"photo_id": "near", "confidence": 0.99, "on_parcel": False},
            ],
        )

    def test_a_parcel_redata_does_not_hold_leaves_the_nearby_photos(self) -> None:
        LocationCache.set(self.here, "property_records", {"available": True, "uuid": _PARCEL})
        self._fetch([_photo_row("near", 0.5)], GatewayRequestError("404"))
        cached = LocationCache.get_fresh(self.here, "redata_photo_pool")
        assert cached is not None
        self.assertEqual([row["photo_id"] for row in cached.data["photos"]], ["near"])

    def test_a_failed_lookup_propagates_for_the_fetch_policy(self) -> None:
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "k"),
            mock.patch.object(RedataPhotosGateway, "lookup_near", side_effect=GatewayRequestError("down")),
            pytest.raises(GatewayRequestError),
        ):
            NearbyPhotosSource().fetch(self.pin)


class NearbyPhotosGalleryTests(_PoolCase):
    def test_the_pin_gallery_shows_the_viewers_tiles_and_offers_no_save(self) -> None:
        LocationCache.set(self.here, "redata_photo_pool", self._payload(*self._all_images()), query_key="q")
        self.client.force_login(self.viewer_user)
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "k"),
        ):
            response = self.client.get(reverse("pin.media", args=[self.pin.slug, "redata_photo_pool"]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("My shot of the boiler house", body)
        self.assertIn("Shared", body)
        self.assertNotIn("Only on their private pin", body)
        self.assertNotIn("Far wiki", body)
        self.assertNotIn('draggable="true"', body)
        self.assertNotIn("media-item-select-check", body)
        self.assertNotIn("media-copy/", body)


class MembersMediaPicturesTests(TestCase):
    def test_a_members_upload_is_served_where_it_is_never_copied_as_a_remote_image(self) -> None:
        """A media host on another domain is still this site's own media, behind its own access checks."""
        from urbanlens.dashboard.plugins.builtin.redata_nearby_media import NearbyMediaSource
        from urbanlens.dashboard.services.apis.assets.base import MediaItem
        from urbanlens.dashboard.services.media.previews import GalleryUrls

        item = MediaItem(
            url="https://media.example.test/media/pins/a.jpg",
            thumb_url="https://media.example.test/media/pins/a-thumb.jpg",
            caption="",
            source="Member photo",
        )
        self.assertEqual(NearbyPhotosSource().pictures([item]), [GalleryUrls(thumb=item.thumb_url, view="")])
        self.assertIn("media-copy/", NearbyMediaSource().pictures([item])[0].thumb)


class NearbyPhotosAreNeverCopiedTests(_PoolCase):
    """A member's photo stays where they put it: the tab's tiles offer no vote, save or send."""

    _MATERIALIZE = "urbanlens.dashboard.services.media.media_materialize.materialize_media_item"

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.viewer_user)

    def test_the_tiles_carry_no_vote_or_relevance_button(self) -> None:
        LocationCache.set(self.here, "redata_photo_pool", self._payload(self.shared_next_door), query_key="q")
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "k"),
        ):
            body = self.client.get(reverse("pin.media", args=[self.pin.slug, "redata_photo_pool"])).content.decode()
        self.assertIn("Shared", body)
        self.assertNotIn('data-media-action="relevant"', body)
        self.assertNotIn('data-media-action="vote-up"', body)

    def test_marking_one_relevant_records_the_vote_without_a_copy(self) -> None:
        with mock.patch(self._MATERIALIZE) as materialize:
            response = self.client.post(
                reverse("pin.media.relevance", args=[self.pin.slug]),
                {"source": "redata_photo_pool", "url": self.shared_next_door.display_url, "is_relevant": True},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        materialize.assert_not_called()
        self.assertFalse(Image.objects.filter(pin=self.pin).exists())

    def test_sending_one_to_the_wiki_is_refused(self) -> None:
        baker.make(Wiki, location=self.here)
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            response = self.client.post(
                reverse("pin.media.send_to_wiki", args=[self.pin.slug]),
                {"items": [{"source": "redata_photo_pool", "url": self.shared_next_door.display_url}]},
                content_type="application/json",
            )
        enqueue.assert_not_called()
        self.assertEqual(response.json()["queued"], 0)
