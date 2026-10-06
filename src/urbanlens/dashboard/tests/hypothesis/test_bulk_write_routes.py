"""Bulk delete, edit and convert routes: they touch only the caller's own rows, and refuse bad bodies."""

from __future__ import annotations

from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.auth.api_keys import generate_api_key


class _BulkFixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.other_user = baker.make(User)
        self.other = self.other_user.profile
        api_key, self.raw_key = generate_api_key(self.user, "bulk client")
        ApiKey.objects.filter(pk=api_key.pk).update(
            scopes=[ApiKeyScope.PINS_WRITE.value, ApiKeyScope.LABELS_WRITE.value]
        )

    def _api(self, name: str, body, *, key: str | None = "own"):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"} if key == "own" else {}
        return self.client.post(reverse(name), body, content_type="application/json", **headers)


class ExternalPinBulkDeleteRouteTests(_BulkFixture):
    def setUp(self) -> None:
        super().setUp()
        self.mine = baker.make(Pin, profile=self.profile)
        self.child = baker.make(Pin, profile=self.profile, parent_pin=self.mine)
        self.theirs = baker.make(Pin, profile=self.other)

    def test_deletes_own_pins_with_their_children_and_ignores_anothers(self) -> None:
        response = self._api("external_api:pins.bulk.delete", {"uuids": [str(self.mine.uuid), str(self.theirs.uuid)]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["deleted"], 1)
        self.assertFalse(Pin.objects.filter(pk__in=[self.mine.pk, self.child.pk]).exists())
        self.assertTrue(Pin.objects.filter(pk=self.theirs.pk).exists())

    def test_naming_only_anothers_pins_is_404_and_deletes_nothing(self) -> None:
        response = self._api("external_api:pins.bulk.delete", {"uuids": [str(self.theirs.uuid)]})

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Pin.objects.filter(pk=self.theirs.pk).exists())

    def test_anonymous_is_refused(self) -> None:
        response = self._api("external_api:pins.bulk.delete", {"uuids": [str(self.mine.uuid)]}, key=None)

        self.assertIn(response.status_code, (401, 403))
        self.assertTrue(Pin.objects.filter(pk=self.mine.pk).exists())

    def test_malformed_bodies_are_400(self) -> None:
        for body in ({}, {"uuids": []}, {"uuids": ["not-a-uuid"]}, {"uuids": "all"}, [str(self.mine.uuid)]):
            response = self._api("external_api:pins.bulk.delete", body)
            self.assertEqual(response.status_code, 400, body)

        self.assertTrue(Pin.objects.filter(pk=self.mine.pk).exists())


class ExternalPinBulkEditRouteTests(_BulkFixture):
    def setUp(self) -> None:
        super().setUp()
        self.mine = baker.make(Pin, profile=self.profile, description="old")
        self.theirs = baker.make(Pin, profile=self.other, description="theirs")

    def _description(self, pin: Pin) -> str | None:
        pin.refresh_from_db()
        return pin.description

    def test_edits_own_pins_and_leaves_anothers_alone(self) -> None:
        response = self._api(
            "external_api:pins.bulk.edit",
            {"uuids": [str(self.mine.uuid), str(self.theirs.uuid)], "description": "new"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(self._description(self.mine), "new")
        self.assertEqual(self._description(self.theirs), "theirs")

    def test_another_users_label_cannot_be_attached(self) -> None:
        their_label = baker.make(Label, profile=self.other, kind=KIND_TAG, name="Theirs")

        response = self._api(
            "external_api:pins.bulk.edit",
            {"uuids": [str(self.mine.uuid)], "add_label_uuids": [str(their_label.uuid)]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.mine.labels.filter(pk=their_label.pk).exists())

    def test_another_users_pin_cannot_become_the_parent(self) -> None:
        response = self._api(
            "external_api:pins.bulk.edit",
            {"uuids": [str(self.mine.uuid)], "parent_uuid": str(self.theirs.uuid), "description": "new"},
        )

        self.assertEqual(response.status_code, 400)
        self.mine.refresh_from_db()
        self.assertIsNone(self.mine.parent_pin_id)
        self.assertEqual(self.mine.description, "old")

    def test_a_pin_is_never_made_its_own_parent(self) -> None:
        response = self._api(
            "external_api:pins.bulk.edit", {"uuids": [str(self.mine.uuid)], "parent_uuid": str(self.mine.uuid)}
        )

        self.assertLess(response.status_code, 500)
        self.mine.refresh_from_db()
        self.assertIsNone(self.mine.parent_pin_id)

    def test_naming_only_anothers_pins_is_404(self) -> None:
        response = self._api("external_api:pins.bulk.edit", {"uuids": [str(self.theirs.uuid)], "description": "x"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._description(self.theirs), "theirs")

    def test_anonymous_is_refused(self) -> None:
        response = self._api(
            "external_api:pins.bulk.edit", {"uuids": [str(self.mine.uuid)], "description": "x"}, key=None
        )

        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self._description(self.mine), "old")

    def test_malformed_bodies_are_400(self) -> None:
        for body in ({"description": "x"}, {"uuids": [str(self.mine.uuid)], "rating": 9}, ["x"]):
            response = self._api("external_api:pins.bulk.edit", body)
            self.assertEqual(response.status_code, 400, body)

        self.assertEqual(self._description(self.mine), "old")


class ExternalLabelBulkDeleteRouteTests(_BulkFixture):
    def setUp(self) -> None:
        super().setUp()
        self.mine = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Mine")
        self.protected = baker.make(Label, profile=self.profile, kind=KIND_STATUS, name="Protected", is_protected=True)
        self.theirs = baker.make(Label, profile=self.other, kind=KIND_TAG, name="Theirs")

    def test_deletes_own_unprotected_labels_only(self) -> None:
        uuids = [str(label.uuid) for label in (self.mine, self.protected, self.theirs)]

        response = self._api("external_api:labels.bulk.delete", {"uuids": uuids})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["deleted"], 1)
        self.assertFalse(Label.objects.filter(pk=self.mine.pk).exists())
        self.assertEqual(Label.objects.filter(pk__in=[self.protected.pk, self.theirs.pk]).count(), 2)

    def test_naming_only_anothers_labels_is_404(self) -> None:
        response = self._api("external_api:labels.bulk.delete", {"uuids": [str(self.theirs.uuid)]})

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Label.objects.filter(pk=self.theirs.pk).exists())

    def test_anonymous_is_refused(self) -> None:
        response = self._api("external_api:labels.bulk.delete", {"uuids": [str(self.mine.uuid)]}, key=None)

        self.assertIn(response.status_code, (401, 403))
        self.assertTrue(Label.objects.filter(pk=self.mine.pk).exists())

    def test_malformed_bodies_are_400(self) -> None:
        for body in ({}, {"uuids": []}, {"uuids": [str(uuid4()), "nope"]}, "x"):
            response = self._api("external_api:labels.bulk.delete", body)
            self.assertEqual(response.status_code, 400, body)

        self.assertTrue(Label.objects.filter(pk=self.mine.pk).exists())


class ExternalLabelBulkConvertRouteTests(_BulkFixture):
    def setUp(self) -> None:
        super().setUp()
        self.mine = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Mine")
        self.theirs = baker.make(Label, profile=self.other, kind=KIND_TAG, name="Theirs")

    def _kind(self, label: Label) -> str:
        label.refresh_from_db()
        return label.kind

    def test_converts_own_labels_and_leaves_anothers(self) -> None:
        response = self._api(
            "external_api:labels.bulk.convert",
            {"uuids": [str(self.mine.uuid), str(self.theirs.uuid)], "target_kind": KIND_CATEGORY},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._kind(self.mine), KIND_CATEGORY)
        self.assertEqual(self._kind(self.theirs), KIND_TAG)

    def test_naming_only_anothers_labels_is_404(self) -> None:
        response = self._api(
            "external_api:labels.bulk.convert", {"uuids": [str(self.theirs.uuid)], "target_kind": KIND_CATEGORY}
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._kind(self.theirs), KIND_TAG)

    def test_anonymous_is_refused(self) -> None:
        response = self._api(
            "external_api:labels.bulk.convert", {"uuids": [str(self.mine.uuid)], "target_kind": KIND_CATEGORY}, key=None
        )

        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self._kind(self.mine), KIND_TAG)

    def test_an_unconvertible_target_kind_is_400(self) -> None:
        for kind in ("user", "media", "nonsense"):
            response = self._api(
                "external_api:labels.bulk.convert", {"uuids": [str(self.mine.uuid)], "target_kind": kind}
            )
            self.assertEqual(response.status_code, 400, kind)

        self.assertEqual(self._kind(self.mine), KIND_TAG)


class InternalLabelBulkConvertRouteTests(TestCase):
    """``label.bulk_convert_{status,tag,category}``: fixed-target variants of ``label.bulk_convert``."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.other_user = baker.make(User)
        self.mine = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Mine")
        self.theirs = baker.make(Label, profile=self.other_user.profile, kind=KIND_TAG, name="Theirs")

    def _post(self, name: str, body, *, kind: str = "tags"):
        return self.client.post(reverse(name, kwargs={"label_kind": kind}), body, content_type="application/json")

    def _kind(self, label: Label) -> str:
        label.refresh_from_db()
        return label.kind

    def test_each_variant_converts_to_its_fixed_kind(self) -> None:
        self.client.force_login(self.user)

        for name, kind, target in (
            ("label.bulk_convert_category", "tags", KIND_CATEGORY),
            ("label.bulk_convert_tag", "categories", KIND_TAG),
            ("label.bulk_convert_status", "tags", KIND_STATUS),
        ):
            response = self._post(name, {"ids": [self.mine.pk]}, kind=kind)
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual(self._kind(self.mine), target, name)

    def test_another_users_label_is_untouched(self) -> None:
        self.client.force_login(self.user)

        for name in ("label.bulk_convert_category", "label.bulk_convert_status"):
            self._post(name, {"ids": [self.theirs.pk]})

        self.assertEqual(self._kind(self.theirs), KIND_TAG)

    def test_anonymous_is_redirected_to_login(self) -> None:
        response = self._post("label.bulk_convert_category", {"ids": [self.mine.pk]})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL))
        self.assertEqual(self._kind(self.mine), KIND_TAG)

    def test_malformed_bodies_are_400(self) -> None:
        self.client.force_login(self.user)

        for body in ("not json", "[]", {"ids": []}, {"ids": ["x"]}):
            response = self._post("label.bulk_convert_category", body)
            self.assertEqual(response.status_code, 400, body)

        self.assertEqual(self._kind(self.mine), KIND_TAG)

    def test_a_non_list_parent_ids_is_a_4xx(self) -> None:
        self.client.force_login(self.user)

        response = self._post("label.bulk_convert_category", {"ids": [self.mine.pk], "add_parent_ids": 5})

        self.assertIn(response.status_code, range(400, 500))
