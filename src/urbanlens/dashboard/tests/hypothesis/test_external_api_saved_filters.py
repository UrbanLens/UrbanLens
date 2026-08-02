"""Tests for the external API's saved-filter surface.

The load-bearing case here is ``test_criteria_change_resyncs_derived_lists``:
``PinList.smart_filter`` is a one-time copy of a SavedFilter's criteria, not a
live reference, so a PATCH that changes criteria without resyncing leaves every
derived smart list silently stale. That is the single easiest correctness bug in
this feature, and the ``lists_resynced`` count is what proves it didn't happen.

Also covers the criteria-ownership check, which has no internal equivalent: the
web form constrains its label and custom-field pickers in the UI, not at the
data layer, so a naive port would let a client probe other users' primary-key
space through result counts.
"""

from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_BASE = "/dashboard/api/external/v1/saved-filters/"


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class SavedFilterApiTestCase(TestCase):
    """Shared fixture: a user with a key granting both list scopes."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Filters client")
        self._grant(ApiKeyScope.LISTS_READ, ApiKeyScope.LISTS_WRITE)

    def _grant(self, *scopes: ApiKeyScope) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[scope.value for scope in scopes])

    def _make_filter(self, name: str = "Rated", **kwargs) -> SavedFilter:
        kwargs.setdefault("criteria", {"min_rating": 3})
        return SavedFilter.objects.create(profile=self.profile, name=name, **kwargs)


class SavedFilterCollectionTests(SavedFilterApiTestCase):
    """GET/POST ``saved-filters/``."""

    def test_unauthenticated_is_401(self) -> None:
        self.assertEqual(self.client.get(_BASE).status_code, 401)

    def test_requires_lists_read_scope(self) -> None:
        self._grant(ApiKeyScope.LABELS_READ)
        self.assertEqual(self.client.get(_BASE, **_bearer(self.raw_key)).status_code, 403)

    def test_pagination_envelope_shape(self) -> None:
        self._make_filter()
        body = self.client.get(_BASE, **_bearer(self.raw_key)).json()
        self.assertEqual(sorted(body.keys()), ["count", "next", "previous", "results"])

    def test_only_own_filters_are_listed(self) -> None:
        other = baker.make(User)
        SavedFilter.objects.create(profile=Profile.objects.get(user=other), name="Theirs", criteria={})
        self._make_filter("Mine")
        body = self.client.get(_BASE, **_bearer(self.raw_key)).json()
        self.assertEqual([row["name"] for row in body["results"]], ["Mine"])

    def test_create(self) -> None:
        response = self.client.post(
            _BASE,
            {"name": "High rated", "criteria": {"min_rating": 4}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(SavedFilter.objects.get(name="High rated").criteria, {"min_rating": 4})

    def test_duplicate_name_is_refused(self) -> None:
        self._make_filter("Dupe")
        response = self.client.post(_BASE, {"name": "Dupe"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_criteria_must_be_an_object(self) -> None:
        response = self.client.post(
            _BASE,
            {"name": "Bad", "criteria": ["not", "an", "object"]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)


class SavedFilterCriteriaOwnershipTests(SavedFilterApiTestCase):
    """Criteria may only reference labels/custom fields the caller may use."""

    def test_own_label_is_accepted(self) -> None:
        mine = Label.objects.create(profile=self.profile, name="Mine", kind="tag")
        response = self.client.post(
            _BASE,
            {"name": "Ok", "criteria": {"tags": [mine.pk]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 201)

    def test_global_label_is_accepted(self) -> None:
        shared = Label.objects.create(profile=None, name="Shared", kind="category")
        response = self.client.post(
            _BASE,
            {"name": "Ok", "criteria": {"tags": [shared.pk]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 201)

    def test_another_users_label_is_refused(self) -> None:
        other = baker.make(User)
        foreign = Label.objects.create(profile=Profile.objects.get(user=other), name="Secret", kind="tag")
        response = self.client.post(
            _BASE,
            {"name": "Probe", "criteria": {"tags": [foreign.pk]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SavedFilter.objects.filter(name="Probe").exists())

    def test_foreign_label_inside_label_groups_is_refused(self) -> None:
        """label_groups is a second place label pks hide - it must be checked too."""
        other = baker.make(User)
        foreign = Label.objects.create(profile=Profile.objects.get(user=other), name="Secret", kind="tag")
        response = self.client.post(
            _BASE,
            {"name": "Probe", "criteria": {"label_groups": [{"op": "and", "ids": [foreign.pk]}]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_another_users_custom_field_is_refused(self) -> None:
        other = baker.make(User)
        foreign_field = baker.make("dashboard.CustomField", profile=Profile.objects.get(user=other))
        response = self.client.post(
            _BASE,
            {"name": "Probe", "criteria": {"custom_fields": [{"field_id": foreign_field.pk, "contains": "x"}]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_nonexistent_label_pk_is_refused(self) -> None:
        response = self.client.post(
            _BASE,
            {"name": "Probe", "criteria": {"tags": [999_999]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)


class SavedFilterDetailTests(SavedFilterApiTestCase):
    """GET/PATCH/DELETE ``saved-filters/{uuid}/``."""

    def setUp(self) -> None:
        super().setUp()
        self.saved_filter = self._make_filter()

    def _url(self, saved_filter: SavedFilter | None = None) -> str:
        return f"{_BASE}{(saved_filter or self.saved_filter).uuid}/"

    def test_get(self) -> None:
        body = self.client.get(self._url(), **_bearer(self.raw_key)).json()
        self.assertEqual(body["criteria"], {"min_rating": 3})

    def test_another_users_filter_is_404(self) -> None:
        other = baker.make(User)
        theirs = SavedFilter.objects.create(profile=Profile.objects.get(user=other), name="Theirs", criteria={})
        self.assertEqual(self.client.get(self._url(theirs), **_bearer(self.raw_key)).status_code, 404)

    def test_unknown_uuid_is_404(self) -> None:
        self.assertEqual(self.client.get(f"{_BASE}{uuid4()}/", **_bearer(self.raw_key)).status_code, 404)

    def test_rename_reports_no_resync(self) -> None:
        response = self.client.patch(self._url(), {"name": "Renamed"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lists_resynced"], 0)

    def test_criteria_change_resyncs_derived_lists(self) -> None:
        """A criteria edit must refresh every list derived from this filter."""
        for name in ("Derived A", "Derived B"):
            PinList.objects.create(
                profile=self.profile,
                name=name,
                is_smart=True,
                smart_filter=self.saved_filter.criteria,
                source_saved_filter=self.saved_filter,
            )
        # A list that does not point at this filter must not be touched.
        unrelated = PinList.objects.create(profile=self.profile, name="Unrelated", smart_filter={"min_rating": 1})

        response = self.client.patch(
            self._url(),
            {"criteria": {"min_rating": 5}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lists_resynced"], 2)

        for derived in PinList.objects.filter(source_saved_filter=self.saved_filter):
            self.assertEqual(derived.smart_filter, {"min_rating": 5})
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.smart_filter, {"min_rating": 1})

    def test_identical_criteria_is_not_treated_as_a_change(self) -> None:
        PinList.objects.create(
            profile=self.profile,
            name="Derived",
            is_smart=True,
            smart_filter=self.saved_filter.criteria,
            source_saved_filter=self.saved_filter,
        )
        response = self.client.patch(
            self._url(),
            {"criteria": {"min_rating": 3}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.json()["lists_resynced"], 0)

    def test_patch_refuses_foreign_label_criteria(self) -> None:
        other = baker.make(User)
        foreign = Label.objects.create(profile=Profile.objects.get(user=other), name="Secret", kind="tag")
        response = self.client.patch(
            self._url(),
            {"criteria": {"tags": [foreign.pk]}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)
        self.saved_filter.refresh_from_db()
        self.assertEqual(self.saved_filter.criteria, {"min_rating": 3})

    def test_delete_leaves_derived_lists_with_their_snapshot(self) -> None:
        derived = PinList.objects.create(
            profile=self.profile,
            name="Derived",
            smart_filter=self.saved_filter.criteria,
            source_saved_filter=self.saved_filter,
        )
        response = self.client.delete(self._url(), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 204)

        derived.refresh_from_db()
        self.assertIsNone(derived.source_saved_filter)
        self.assertEqual(derived.smart_filter, {"min_rating": 3})
