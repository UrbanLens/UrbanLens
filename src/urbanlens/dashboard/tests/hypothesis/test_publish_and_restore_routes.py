"""Routes that publish private work to a community wiki, restore wiki text, or toggle a trip's calendar sync."""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.utils import DataError
from django.http.request import RawPostDataException
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, TripCalendarLink
from urbanlens.dashboard.models.floorplans.model import Floorplan
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.wiki.articles import get_article, save_article

_PARCEL_BUILDINGS = "urbanlens.dashboard.services.pins.pin_wiki_sync.site_scope.parcel_buildings"


class _Users(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.stranger_user = baker.make(User)
        self.stranger = self.stranger_user.profile

    def _key(self, user: User, *scopes: ApiKeyScope) -> dict:
        _key, raw = generate_api_key(user, f"client {user.pk}")
        ApiKey.objects.filter(user=user).update(scopes=[scope.value for scope in scopes])
        return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])


class PinWikiSyncRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=41.70, longitude=-73.90)
        self.wiki = baker.make(Wiki, location=self.location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Old Mill")
        self.child = baker.make(
            Pin,
            profile=self.profile,
            parent_pin=self.pin,
            location=baker.make(Location, latitude=41.7005, longitude=-73.9005),
            name="Boiler house",
        )
        self.auth = self._key(self.user, ApiKeyScope.PINS_WRITE, ApiKeyScope.WIKI_READ, ApiKeyScope.WIKI_WRITE)
        mock.patch(_PARCEL_BUILDINGS, return_value=[]).start()
        self.addCleanup(mock.patch.stopall)

    def _push(self, pin: Pin, body, headers):
        url = reverse("external_api:pins.wiki-sync.push", args=[pin.slug])
        return self.client.post(url, body, content_type="application/json", **headers)

    def test_owner_publishes_a_child_pin_as_a_child_wiki(self) -> None:
        response = self._push(self.pin, {"child_pin_uuids": [str(self.child.uuid)]}, self.auth)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(list(self.wiki.child_wikis.values_list("name", flat=True)), ["Boiler house"])

    def test_a_stranger_cannot_publish_someone_elses_child_pins(self) -> None:
        stranger_auth = self._key(
            self.stranger_user, ApiKeyScope.PINS_WRITE, ApiKeyScope.WIKI_READ, ApiKeyScope.WIKI_WRITE
        )

        response = self._push(self.pin, {"child_pin_uuids": [str(self.child.uuid)]}, stranger_auth)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.wiki.child_wikis.exists())

    def test_another_users_child_pin_is_not_published_through_the_owners_parent(self) -> None:
        their_child = baker.make(
            Pin, profile=self.stranger, location=baker.make(Location, latitude=41.701, longitude=-73.901)
        )

        response = self._push(self.pin, {"child_pin_uuids": [str(their_child.uuid)]}, self.auth)

        self.assertEqual(response.json()["created"], 0)
        self.assertFalse(self.wiki.child_wikis.exists())

    def test_a_key_without_wiki_write_is_refused(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_WRITE.value])

        response = self._push(self.pin, {"child_pin_uuids": [str(self.child.uuid)]}, self.auth)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.wiki.child_wikis.exists())

    def test_anonymous_is_refused(self) -> None:
        response = self._push(self.pin, {"child_pin_uuids": [str(self.child.uuid)]}, {})

        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(self.wiki.child_wikis.exists())

    @pytest.mark.xfail(
        strict=True,
        raises=DataError,
        reason="P29 bug: ApiKeyAuthentication logs request.path into ApiKeyUsageLog.endpoint (varchar 255) unbounded, "
        "so any API-key write to a path over 255 characters - here a long pin slug - raises DataError (500)",
    )
    def test_a_long_pin_slug_is_a_404_not_a_500(self) -> None:
        response = self._push(Pin(slug="x" * 240), {"child_pin_uuids": [str(self.child.uuid)]}, self.auth)

        self.assertEqual(response.status_code, 404)

    def test_malformed_bodies_are_400(self) -> None:
        for body in ({}, {"child_pin_uuids": "all"}, {"child_pin_uuids": ["nope"]}, ["x"]):
            self.assertEqual(self._push(self.pin, body, self.auth).status_code, 400, body)
        self.assertFalse(self.wiki.child_wikis.exists())

    def test_pull_creates_personal_child_pins_only_for_the_owner(self) -> None:
        baker.make(
            Wiki,
            parent_wiki=self.wiki,
            name="Water tower",
            location=baker.make(Location, latitude=41.702, longitude=-73.902),
        )
        stranger_auth = self._key(self.stranger_user, ApiKeyScope.PINS_WRITE, ApiKeyScope.WIKI_READ)
        url = reverse("external_api:pins.wiki-sync.pull", args=[self.pin.slug])

        refused = self.client.post(url, **stranger_auth)
        pulled = self.client.post(url, **self.auth)

        self.assertEqual(refused.status_code, 404)
        self.assertEqual(pulled.status_code, 200, pulled.content)
        self.assertEqual(pulled.json()["created"], 1)
        self.assertFalse(Pin.objects.filter(profile=self.stranger).exists())
        self.assertEqual(self.pin.detail_pins.count(), 2)


class FloorplanPublishRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.place = baker.make(Place, kind=PlaceKind.BUILDING, provider="redata", provider_key="cris:p29-1")
        self.location = baker.make(Location, latitude=41.71, longitude=-73.91, place=self.place)
        self.wiki = baker.make(Wiki, place=self.place, location=self.location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Old Mill")
        self.floorplan = Floorplan.objects.create(place=self.place, profile=self.profile, name="mine")
        self.url = reverse("pin.floorplan.publish", args=[self.pin.slug])

    def _community_plans(self):
        return Floorplan.objects.filter(place=self.place, wiki__isnull=False)

    def test_owner_publishes_a_copy_and_keeps_their_own_plan_personal(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"version": str(self.floorplan.uuid)})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._community_plans().count(), 1)
        self.floorplan.refresh_from_db()
        self.assertIsNone(self.floorplan.wiki_id)

    def test_a_stranger_cannot_publish_through_the_owners_pin(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url, {"version": str(self.floorplan.uuid)})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._community_plans().exists())

    def test_another_users_plan_cannot_be_published_through_ones_own_pin(self) -> None:
        their_pin = baker.make(Pin, profile=self.stranger, location=self.location, name="Their Mill")
        self.client.force_login(self.stranger_user)

        response = self.client.post(
            reverse("pin.floorplan.publish", args=[their_pin.slug]), {"version": str(self.floorplan.uuid)}
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._community_plans().exists())

    def test_a_json_body_naming_the_plan_publishes_it(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"uuid": str(self.floorplan.uuid)}, content_type="application/json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._community_plans().count(), 1)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"version": str(self.floorplan.uuid)}))
        self.assertFalse(self._community_plans().exists())

    @pytest.mark.xfail(
        strict=True,
        raises=RawPostDataException,
        reason="P29 bug: FloorplanPublishView reads request.body after request.POST when 'version' is absent, so a "
        "multipart form post without it raises RawPostDataException (500)",
    )
    def test_a_form_post_without_a_version_is_a_4xx(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"name": "x"})

        self.assertIn(response.status_code, range(400, 500))
        self.assertFalse(self._community_plans().exists())

    @pytest.mark.xfail(
        strict=True,
        raises=ValidationError,
        reason="P29 bug: FloorplanPublishView filters Floorplan.uuid by the raw submitted value, so a non-uuid "
        "'version' raises ValidationError (500)",
    )
    def test_a_non_uuid_version_is_a_4xx(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"version": "not-a-uuid"})

        self.assertIn(response.status_code, range(400, 500))

    @pytest.mark.xfail(
        strict=True,
        raises=AttributeError,
        reason="P29 bug: FloorplanPublishView calls .get() on whatever JSON decodes, so a JSON array body raises (500)",
    )
    def test_a_json_array_body_is_a_4xx(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, data="[]", content_type="application/json")

        self.assertIn(response.status_code, range(400, 500))


class _ArticleFixture(_Users):
    """A wiki the user can see (they have a pin there) and the stranger cannot, with two article revisions."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, official_name="Old Mill")
        self.location.ensure_slug()
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.profile, location=self.location)
        _article, self.first = save_article(editor=self.profile, content="Original body", wiki=self.wiki)
        save_article(editor=self.profile, content="Vandalised", wiki=self.wiki)

    def _content(self) -> str:
        return get_article(wiki=self.wiki).content


class WikiArticleRestoreRouteTests(_ArticleFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("location.wiki.article.restore", args=[self.location.slug, self.first.pk])

    def test_a_viewer_restores_an_older_revision(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._content(), "Original body")

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._content(), "Vandalised")

    def test_a_revision_of_another_wikis_article_is_404(self) -> None:
        elsewhere = baker.make(Location)
        other_wiki = baker.make(Wiki, location=elsewhere)
        _article, foreign = save_article(editor=self.stranger, content="Elsewhere", wiki=other_wiki)
        self.client.force_login(self.user)

        response = self.client.post(reverse("location.wiki.article.restore", args=[self.location.slug, foreign.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._content(), "Vandalised")

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertEqual(self._content(), "Vandalised")


class ExternalWikiArticleRestoreRouteTests(_ArticleFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:wikis.article.revisions.restore", args=[self.location.slug, self.first.pk])

    def test_a_viewer_restores_an_older_revision(self) -> None:
        response = self.client.post(self.url, **self._key(self.user, ApiKeyScope.WIKI_READ, ApiKeyScope.WIKI_WRITE))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._content(), "Original body")

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        headers = self._key(self.stranger_user, ApiKeyScope.WIKI_READ, ApiKeyScope.WIKI_WRITE)

        response = self.client.post(self.url, **headers)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._content(), "Vandalised")

    def test_a_read_only_key_is_refused(self) -> None:
        response = self.client.post(self.url, **self._key(self.user, ApiKeyScope.WIKI_READ))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._content(), "Vandalised")

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self.client.post(self.url).status_code, (401, 403))
        self.assertEqual(self._content(), "Vandalised")


class ExternalTripCalendarSyncRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.trip = baker.make(Trip, creator=self.profile, name="Rust Belt")
        TripMembership.objects.create(trip=self.trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        self.link = baker.make(
            TripCalendarLink,
            trip=self.trip,
            activity=None,
            profile=self.profile,
            direction=CalendarSyncDirection.EXPORTED,
            auto_sync=False,
        )
        self.url = reverse("external_api:trips.calendar_sync", args=[self.trip.slug])

    def _auto_sync(self) -> bool:
        self.link.refresh_from_db()
        return self.link.auto_sync

    def _post(self, body, headers):
        return self.client.post(self.url, body, content_type="application/json", **headers)

    def test_owner_turns_auto_sync_on(self) -> None:
        response = self._post({"enabled": True}, self._key(self.user, ApiKeyScope.TRIPS_READ, ApiKeyScope.TRIPS_WRITE))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self._auto_sync())

    def test_a_non_member_gets_404_and_the_link_is_unchanged(self) -> None:
        headers = self._key(self.stranger_user, ApiKeyScope.TRIPS_READ, ApiKeyScope.TRIPS_WRITE)

        response = self._post({"enabled": True}, headers)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._auto_sync())

    def test_a_member_without_a_link_of_their_own_gets_400_and_the_owners_link_is_unchanged(self) -> None:
        member = baker.make(User)
        TripMembership.objects.create(trip=self.trip, profile=member.profile, status=TripMembership.STATUS_JOINED)

        response = self._post({"enabled": True}, self._key(member, ApiKeyScope.TRIPS_READ, ApiKeyScope.TRIPS_WRITE))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self._auto_sync())

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self._post({"enabled": True}, {}).status_code, (401, 403))
        self.assertFalse(self._auto_sync())

    def test_malformed_bodies_are_400(self) -> None:
        headers = self._key(self.user, ApiKeyScope.TRIPS_READ, ApiKeyScope.TRIPS_WRITE)

        for body in ({}, {"enabled": "maybe"}, ["x"]):
            self.assertEqual(self._post(body, headers).status_code, 400, body)
        self.assertFalse(self._auto_sync())
