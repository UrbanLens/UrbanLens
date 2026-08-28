"""Tests for the external API's undo domain.

Covers the two invariants unique to this domain: a credential missing a
model_label's paired domain scope has that entry *omitted* from the list
(never a 403), and restoring requires both ``undo:write`` and that same
paired domain-write scope. The rest follows this API's usual anti-enumeration
rule - another profile's entry, or an unknown uuid, is always a 404.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo.model import UNDO_RETENTION, UndoAction
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.undo.service import stash_for_undo


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _UndoApiTestCase(TestCase):
    """Shared fixture: a key owner and an unrelated second profile."""

    def setUp(self) -> None:
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)

    def _key_with_scopes(self, scopes: list[str], user: User | None = None) -> str:
        """Issue a key carrying exactly *scopes* and return its raw value."""
        api_key, raw = generate_api_key(user or self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw


class UndoListTests(_UndoApiTestCase):
    """GET /undo/ - scope-gated aggregation across model_labels."""

    def test_missing_undo_read_is_refused(self) -> None:
        """A key without undo:read gets 403, not a filtered-empty list."""
        raw_key = self._key_with_scopes([ApiKeyScope.PINS_READ.value])
        response = self.client.get(reverse("external_api:undo"), **_bearer(raw_key))
        self.assertEqual(response.status_code, 403)

    def test_ungranted_domain_is_omitted_not_forbidden(self) -> None:
        """A pin entry is listed; a safety_checkin entry is omitted and named in `omitted`."""
        pin = baker.make(Pin, profile=self.profile)
        stash_for_undo("pin", [pin], self.profile)
        baker.make(UndoAction, profile=self.profile, model_label="safety_checkin", object_repr="Overdue check-in", payload={})

        raw_key = self._key_with_scopes([ApiKeyScope.UNDO_READ.value, ApiKeyScope.PINS_READ.value])
        response = self.client.get(reverse("external_api:undo"), **_bearer(raw_key))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        labels = {entry["model_label"] for entry in body["entries"]}
        self.assertEqual(labels, {"pin"})
        self.assertEqual(body["omitted"], ["safety_checkin"])

    def test_full_grants_omit_nothing(self) -> None:
        """A credential holding every domain's read scope reports no omissions."""
        pin = baker.make(Pin, profile=self.profile)
        stash_for_undo("pin", [pin], self.profile)
        baker.make(UndoAction, profile=self.profile, model_label="safety_checkin", object_repr="Overdue check-in", payload={})

        raw_key = self._key_with_scopes(
            [ApiKeyScope.UNDO_READ.value, ApiKeyScope.PINS_READ.value, ApiKeyScope.WIKI_READ.value, ApiKeyScope.TRIPS_READ.value, ApiKeyScope.LISTS_READ.value, ApiKeyScope.SAFETY_READ.value]
        )
        response = self.client.get(reverse("external_api:undo"), **_bearer(raw_key))
        self.assertEqual(response.json()["omitted"], [])
        self.assertEqual(len(response.json()["entries"]), 2)

    def test_never_lists_another_profiles_entries(self) -> None:
        """Another profile's undo history never leaks into this list."""
        baker.make(UndoAction, profile=self.other_profile, model_label="pin", object_repr="Someone else's pin", payload={})
        raw_key = self._key_with_scopes([ApiKeyScope.UNDO_READ.value, ApiKeyScope.PINS_READ.value])
        response = self.client.get(reverse("external_api:undo"), **_bearer(raw_key))
        self.assertEqual(response.json()["entries"], [])

    def test_expired_entries_are_excluded(self) -> None:
        """An entry past UNDO_RETENTION doesn't appear in the active list."""
        entry = baker.make(UndoAction, profile=self.profile, model_label="pin", object_repr="Old pin", payload={})
        UndoAction.objects.filter(pk=entry.pk).update(created=timezone.now() - UNDO_RETENTION - datetime.timedelta(days=1))
        raw_key = self._key_with_scopes([ApiKeyScope.UNDO_READ.value, ApiKeyScope.PINS_READ.value])
        response = self.client.get(reverse("external_api:undo"), **_bearer(raw_key))
        self.assertEqual(response.json()["entries"], [])


class UndoRestoreTests(_UndoApiTestCase):
    """POST /undo/{uuid}/restore/ - domain-write-scope pairing and expiry."""

    def test_missing_undo_write_is_refused(self) -> None:
        """A key without undo:write gets 403 outright."""
        entry = baker.make(UndoAction, profile=self.profile, model_label="pin", object_repr="A pin", payload={})
        raw_key = self._key_with_scopes([ApiKeyScope.PINS_WRITE.value])
        url = reverse("external_api:undo.restore", kwargs={"undo_uuid": entry.uuid})
        response = self.client.post(url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 403)

    def test_missing_domain_write_scope_is_404_not_403(self) -> None:
        """undo:write alone, without the paired pins:write, can't restore a pin entry."""
        entry = baker.make(UndoAction, profile=self.profile, model_label="pin", object_repr="A pin", payload={})
        raw_key = self._key_with_scopes([ApiKeyScope.UNDO_WRITE.value])
        url = reverse("external_api:undo.restore", kwargs={"undo_uuid": entry.uuid})
        response = self.client.post(url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(UndoAction.objects.filter(pk=entry.pk).exists())

    def test_unknown_uuid_is_404(self) -> None:
        """A uuid with no matching row at all is a 404."""
        raw_key = self._key_with_scopes([ApiKeyScope.UNDO_WRITE.value, ApiKeyScope.PINS_WRITE.value])
        url = reverse("external_api:undo.restore", kwargs={"undo_uuid": "00000000-0000-0000-0000-000000000000"})
        response = self.client.post(url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 404)

    def test_another_profiles_entry_is_404_not_403(self) -> None:
        """Another profile's undo entry is indistinguishable from a nonexistent one."""
        entry = baker.make(UndoAction, profile=self.other_profile, model_label="pin", object_repr="Not yours", payload={})
        raw_key = self._key_with_scopes([ApiKeyScope.UNDO_WRITE.value, ApiKeyScope.PINS_WRITE.value])
        url = reverse("external_api:undo.restore", kwargs={"undo_uuid": entry.uuid})
        response = self.client.post(url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(UndoAction.objects.filter(pk=entry.pk).exists())

    def test_expired_entry_returns_410_and_is_deleted(self) -> None:
        """An entry past retention answers 410 (not 404) and is cleaned up on the attempt."""
        entry = baker.make(UndoAction, profile=self.profile, model_label="pin", object_repr="Old pin", payload={})
        UndoAction.objects.filter(pk=entry.pk).update(created=timezone.now() - UNDO_RETENTION - datetime.timedelta(days=1))
        raw_key = self._key_with_scopes([ApiKeyScope.UNDO_WRITE.value, ApiKeyScope.PINS_WRITE.value])
        url = reverse("external_api:undo.restore", kwargs={"undo_uuid": entry.uuid})
        response = self.client.post(url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 410)
        self.assertFalse(UndoAction.objects.filter(pk=entry.pk).exists())

    def test_successful_restore_recreates_the_pin_and_marks_the_entry_undone(self) -> None:
        """A real stash -> delete -> restore round-trip recreates the pin."""
        pin = baker.make(Pin, profile=self.profile, name="Steel Mill", name_is_user_provided=True)
        entry = stash_for_undo("pin", [pin], self.profile)
        pin_pk = pin.pk
        pin.delete()

        raw_key = self._key_with_scopes([ApiKeyScope.UNDO_WRITE.value, ApiKeyScope.PINS_WRITE.value])
        url = reverse("external_api:undo.restore", kwargs={"undo_uuid": entry.uuid})
        response = self.client.post(url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"restored": True})
        self.assertFalse(Pin.objects.filter(pk=pin_pk).exists())
        self.assertTrue(Pin.objects.filter(profile=self.profile, name="Steel Mill").exists())
        entry.refresh_from_db()
        self.assertIsNotNone(entry.undone_at)
