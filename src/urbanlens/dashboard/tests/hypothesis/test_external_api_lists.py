"""Tests for the external API's pin-list surface.

Covers ``lists/``, ``lists/{slug}/``, ``lists/{slug}/items/`` (including the
body-carrying DELETE), ``items/reorder/`` and ``resync/`` - the happy paths,
the scope gate, cross-profile isolation, the pagination envelope, and the two
behaviors that are easy to get silently wrong: the per-list cap and the
"only resync when the smart rules actually changed" rule.
"""

from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

_BASE = "/dashboard/api/external/v1/lists/"


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class ListsApiTestCase(TestCase):
    """Shared fixture: a user with a key granting both list scopes."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Lists client")
        self._grant(ApiKeyScope.LISTS_READ, ApiKeyScope.LISTS_WRITE)

    def _grant(self, *scopes: ApiKeyScope) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[scope.value for scope in scopes])

    def _make_pin(self, name: str, latitude: float = 42.0, longitude: float = -73.0):
        return create_pin_for_profile(self.profile, name=name, latitude=latitude, longitude=longitude).pin

    def _make_list(self, name: str = "Favorites", **kwargs) -> PinList:
        return PinList.objects.create(profile=self.profile, name=name, **kwargs)


class PinListsCollectionTests(ListsApiTestCase):
    """GET/POST ``lists/``."""

    def test_unauthenticated_is_401(self) -> None:
        self.assertEqual(self.client.get(_BASE).status_code, 401)

    def test_requires_lists_read_scope(self) -> None:
        self._grant(ApiKeyScope.LISTS_WRITE)
        self.assertEqual(self.client.get(_BASE, **_bearer(self.raw_key)).status_code, 403)

    def test_post_requires_lists_write_scope(self) -> None:
        self._grant(ApiKeyScope.LISTS_READ)
        response = self.client.post(_BASE, {"name": "Nope"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 403)

    def test_pins_scope_does_not_grant_lists(self) -> None:
        self._grant(ApiKeyScope.PINS_READ, ApiKeyScope.PINS_WRITE)
        self.assertEqual(self.client.get(_BASE, **_bearer(self.raw_key)).status_code, 403)

    def test_pagination_envelope_shape(self) -> None:
        self._make_list("A")
        body = self.client.get(_BASE, **_bearer(self.raw_key)).json()
        self.assertEqual(sorted(body.keys()), ["count", "next", "previous", "results"])
        self.assertEqual(body["count"], 1)

    def test_only_own_lists_are_listed(self) -> None:
        other = baker.make(User)
        PinList.objects.create(profile=Profile.objects.get(user=other), name="Theirs")
        self._make_list("Mine")
        body = self.client.get(_BASE, **_bearer(self.raw_key)).json()
        self.assertEqual([row["name"] for row in body["results"]], ["Mine"])

    def test_boundary_is_not_serialized_on_the_collection(self) -> None:
        self._make_list("A")
        row = self.client.get(_BASE, **_bearer(self.raw_key)).json()["results"][0]
        self.assertNotIn("smart_boundary", row)
        self.assertFalse(row["has_boundary"])

    def test_is_smart_filter(self) -> None:
        self._make_list("Plain")
        self._make_list("Smart", is_smart=True)
        body = self.client.get(f"{_BASE}?is_smart=true", **_bearer(self.raw_key)).json()
        self.assertEqual([row["name"] for row in body["results"]], ["Smart"])

    def test_create_returns_201_and_owns_the_list(self) -> None:
        response = self.client.post(_BASE, {"name": "Roadtrip"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(PinList.objects.get(name="Roadtrip").profile, self.profile)

    def test_duplicate_name_is_refused(self) -> None:
        self._make_list("Dupe")
        response = self.client.post(_BASE, {"name": "Dupe"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_create_rejects_a_non_polygon_boundary(self) -> None:
        response = self.client.post(
            _BASE,
            {"name": "Bad geom", "smart_boundary": {"type": "Point", "coordinates": [1, 2]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_criteria_naming_another_users_label(self) -> None:
        other = baker.make(User)
        foreign = baker.make("dashboard.Label", profile=Profile.objects.get(user=other), name="Secret", kind="tag")
        response = self.client.post(
            _BASE,
            {"name": "Probe", "smart_filter": {"tags": [foreign.pk]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)


class PinListDetailTests(ListsApiTestCase):
    """GET/PATCH/DELETE ``lists/{slug}/``."""

    def setUp(self) -> None:
        super().setUp()
        self.pin_list = self._make_list("Favorites")

    def _url(self, pin_list: PinList | None = None) -> str:
        return f"{_BASE}{(pin_list or self.pin_list).slug}/"

    def test_get_includes_boundary_key(self) -> None:
        body = self.client.get(self._url(), **_bearer(self.raw_key)).json()
        self.assertIn("smart_boundary", body)
        self.assertIsNone(body["smart_boundary"])

    def test_another_users_list_is_404(self) -> None:
        other = baker.make(User)
        theirs = PinList.objects.create(profile=Profile.objects.get(user=other), name="Theirs")
        self.assertEqual(self.client.get(self._url(theirs), **_bearer(self.raw_key)).status_code, 404)

    def test_unknown_slug_is_404(self) -> None:
        self.assertEqual(self.client.get(f"{_BASE}{uuid4()}/", **_bearer(self.raw_key)).status_code, 404)

    def test_patch_renames(self) -> None:
        response = self.client.patch(self._url(), {"name": "Renamed"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.pin_list.refresh_from_db()
        self.assertEqual(self.pin_list.name, "Renamed")

    def test_rename_alone_does_not_resync(self) -> None:
        """A rename must not trigger a full membership recompute.

        The item below is non-manual and matches no rule, so it would be
        removed by a resync - its survival is what proves none ran.
        """
        pin = self._make_pin("Kept")
        item = PinListItem.objects.create(pin_list=self.pin_list, pin=pin, order=0, added_via=PinListItem.ADDED_SMART_FILTER)
        self.client.patch(self._url(), {"name": "Renamed"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertTrue(PinListItem.objects.filter(pk=item.pk).exists())

    def test_changing_smart_rules_resyncs(self) -> None:
        pin = self._make_pin("Stale")
        PinListItem.objects.create(pin_list=self.pin_list, pin=pin, order=0, added_via=PinListItem.ADDED_SMART_FILTER)
        response = self.client.patch(self._url(), {"is_smart": True}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        # No rules match it any more, and it was not manually added, so the
        # resync removed it.
        self.assertEqual(self.pin_list.items.count(), 0)

    def test_patch_pointing_at_a_saved_filter_copies_its_criteria(self) -> None:
        saved = SavedFilter.objects.create(profile=self.profile, name="Rated", criteria={"min_rating": 3})
        response = self.client.patch(
            self._url(),
            {"source_saved_filter_uuid": str(saved.uuid)},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.pin_list.refresh_from_db()
        self.assertEqual(self.pin_list.smart_filter, {"min_rating": 3})
        self.assertEqual(self.pin_list.source_saved_filter, saved)

    def test_patch_with_unknown_saved_filter_is_400(self) -> None:
        response = self.client.patch(
            self._url(),
            {"source_saved_filter_uuid": str(uuid4())},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_removes_the_list_but_not_the_pins(self) -> None:
        pin = self._make_pin("Survivor")
        PinListItem.objects.create(pin_list=self.pin_list, pin=pin, order=0)
        response = self.client.delete(self._url(), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(PinList.objects.filter(pk=self.pin_list.pk).exists())
        pin.refresh_from_db()  # still there

    def test_delete_requires_write_scope(self) -> None:
        self._grant(ApiKeyScope.LISTS_READ)
        self.assertEqual(self.client.delete(self._url(), **_bearer(self.raw_key)).status_code, 403)


class PinListItemsTests(ListsApiTestCase):
    """GET/POST/DELETE ``lists/{slug}/items/`` and the reorder sub-path."""

    def setUp(self) -> None:
        super().setUp()
        self.pin_list = self._make_list("Favorites")
        self.pin_a = self._make_pin("A", 42.1, -73.1)
        self.pin_b = self._make_pin("B", 42.2, -73.2)

    def _items_url(self) -> str:
        return f"{_BASE}{self.pin_list.slug}/items/"

    def _add(self, *pins) -> dict:
        response = self.client.post(
            self._items_url(),
            {"pin_uuids": [str(pin.uuid) for pin in pins]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        return response.json()

    def test_add_pins(self) -> None:
        body = self._add(self.pin_a, self.pin_b)
        self.assertEqual(body["added"], 2)
        self.assertEqual(self.pin_list.items.count(), 2)

    def test_adding_the_same_pin_twice_is_skipped_not_an_error(self) -> None:
        self._add(self.pin_a)
        body = self._add(self.pin_a, self.pin_b)
        self.assertEqual(body["added"], 1)
        self.assertEqual(self.pin_list.items.count(), 2)

    def test_unknown_and_foreign_uuids_are_dropped_silently(self) -> None:
        other = baker.make(User)
        foreign = create_pin_for_profile(Profile.objects.get(user=other), name="Theirs", latitude=1.0, longitude=1.0).pin
        response = self.client.post(
            self._items_url(),
            {"pin_uuids": [str(self.pin_a.uuid), str(foreign.uuid), str(uuid4())]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added"], 1)

    def test_cap_is_enforced_and_reported(self) -> None:
        settings_row = SiteSettings.get_current()
        settings_row.max_pins_per_list = 1
        settings_row.save()

        body = self._add(self.pin_a, self.pin_b)
        self.assertEqual(body["added"], 1)
        self.assertEqual(body["skipped_over_cap"], 1)
        self.assertEqual(body["max_pins"], 1)

    def test_items_pagination_envelope(self) -> None:
        self._add(self.pin_a, self.pin_b)
        body = self.client.get(self._items_url(), **_bearer(self.raw_key)).json()
        self.assertEqual(sorted(body.keys()), ["count", "next", "previous", "results"])
        self.assertEqual(body["count"], 2)
        self.assertEqual(sorted(body["results"][0]["pin"].keys()), ["latitude", "longitude", "name", "slug", "uuid"])

    def test_delete_removes_named_pins(self) -> None:
        self._add(self.pin_a, self.pin_b)
        response = self.client.delete(
            self._items_url(),
            {"pin_uuids": [str(self.pin_a.uuid)]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], 1)
        self.assertEqual(self.pin_list.items.count(), 1)

    def test_items_on_another_users_list_are_404(self) -> None:
        other = baker.make(User)
        theirs = PinList.objects.create(profile=Profile.objects.get(user=other), name="Theirs")
        response = self.client.get(f"{_BASE}{theirs.slug}/items/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)

    def test_reorder(self) -> None:
        self._add(self.pin_a, self.pin_b)
        items = list(self.pin_list.items.order_by("order"))
        reversed_ids = [items[1].pk, items[0].pk]

        response = self.client.post(
            f"{_BASE}{self.pin_list.slug}/items/reorder/",
            {"item_ids": reversed_ids},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reordered"], 2)
        self.assertEqual([item.pk for item in self.pin_list.items.order_by("order")], reversed_ids)

    def test_reorder_route_is_not_swallowed_by_the_detail_route(self) -> None:
        """``items/reorder/`` must resolve to the reorder view, not a list slug."""
        response = self.client.post(
            f"{_BASE}{self.pin_list.slug}/items/reorder/",
            {"item_ids": [1]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)


class PinListResyncTests(ListsApiTestCase):
    """POST ``lists/{slug}/resync/``."""

    def test_resync_returns_the_pin_count(self) -> None:
        pin_list = self._make_list("Smart", is_smart=True)
        pin = self._make_pin("Orphan")
        PinListItem.objects.create(pin_list=pin_list, pin=pin, order=0, added_via=PinListItem.ADDED_SMART_FILTER)

        response = self.client.post(f"{_BASE}{pin_list.slug}/resync/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        # No rules to match, non-manual membership - the resync drops it.
        self.assertEqual(response.json()["pin_count"], 0)

    def test_resync_requires_write_scope(self) -> None:
        pin_list = self._make_list("Smart", is_smart=True)
        self._grant(ApiKeyScope.LISTS_READ)
        self.assertEqual(self.client.post(f"{_BASE}{pin_list.slug}/resync/", **_bearer(self.raw_key)).status_code, 403)

    def test_resync_on_another_users_list_is_404(self) -> None:
        other = baker.make(User)
        theirs = PinList.objects.create(profile=Profile.objects.get(user=other), name="Theirs")
        self.assertEqual(self.client.post(f"{_BASE}{theirs.slug}/resync/", **_bearer(self.raw_key)).status_code, 404)


class PinListMarkupMapTests(ListsApiTestCase):
    """POST ``lists/{slug}/markup-map/``."""

    def test_creates_a_markup_map_and_returns_its_uuid(self) -> None:
        pin_list = self._make_list("Roadtrip")
        pin = self._make_pin("Waypoint")
        PinListItem.objects.create(pin_list=pin_list, pin=pin, order=0)

        response = self.client.post(f"{_BASE}{pin_list.slug}/markup-map/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("markup_map_uuid", response.json())

        pin_list.refresh_from_db()
        self.assertIsNotNone(pin_list.markup_map_id)
        self.assertEqual(str(pin_list.markup_map.uuid), response.json()["markup_map_uuid"])

    def test_reuses_the_existing_map_on_a_second_call(self) -> None:
        pin_list = self._make_list("Roadtrip")
        pin = self._make_pin("Waypoint")
        PinListItem.objects.create(pin_list=pin_list, pin=pin, order=0)

        first = self.client.post(f"{_BASE}{pin_list.slug}/markup-map/", **_bearer(self.raw_key)).json()
        second = self.client.post(f"{_BASE}{pin_list.slug}/markup-map/", **_bearer(self.raw_key)).json()
        self.assertEqual(first["markup_map_uuid"], second["markup_map_uuid"])

    def test_empty_list_is_a_400(self) -> None:
        pin_list = self._make_list("Empty")
        response = self.client.post(f"{_BASE}{pin_list.slug}/markup-map/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_requires_lists_write_scope(self) -> None:
        pin_list = self._make_list("Roadtrip")
        self._grant(ApiKeyScope.LISTS_READ)
        response = self.client.post(f"{_BASE}{pin_list.slug}/markup-map/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 403)

    def test_another_users_list_is_404(self) -> None:
        other = baker.make(User)
        theirs = PinList.objects.create(profile=Profile.objects.get(user=other), name="Theirs")
        response = self.client.post(f"{_BASE}{theirs.slug}/markup-map/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)
