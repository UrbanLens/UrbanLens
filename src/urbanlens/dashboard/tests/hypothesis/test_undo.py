"""Tests for the generic undo-delete framework: service, handlers, and settings-page views."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.base import get_handler
from urbanlens.dashboard.services.undo.service import (
    UndoExpiredError,
    clear_undo_history,
    get_undo_history,
    restore_undo_action,
    stash_for_undo,
)

_LOCMEM_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _make_wiki(**kwargs) -> Wiki:
    kwargs.setdefault("location", baker.make("dashboard.Location"))
    return baker.make(Wiki, **kwargs)


def _make_checkin(profile, **kwargs) -> SafetyCheckin:
    defaults = {
        "profile": profile,
        "title": "Test hike",
        "checkin_by": timezone.now() - datetime.timedelta(hours=2),
        "grace_period": datetime.timedelta(hours=1),
    }
    defaults.update(kwargs)
    return baker.make(SafetyCheckin, **defaults)


@override_settings(CACHES=_LOCMEM_CACHES)
class UndoServiceTests(TestCase):
    """Core stash/restore/clear/history behavior, independent of any one model."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill")

    def test_stash_creates_db_row_and_cache_entry(self) -> None:
        undo_action = stash_for_undo("pin", [self.pin], self.profile)
        self.assertTrue(UndoAction.objects.filter(pk=undo_action.pk).exists())
        self.assertIsNotNone(cache.get(f"dashboard:undo:{undo_action.cache_key}"))
        self.assertEqual(undo_action.model_label, "pin")
        self.assertIn("Old Mill", undo_action.object_repr)

    def test_restore_recreates_instance_and_consumes_entry(self) -> None:
        old_pk = self.pin.pk
        undo_action = stash_for_undo("pin", [self.pin], self.profile)
        self.pin.delete()

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)
        self.assertNotEqual(restored[0].pk, old_pk)
        self.assertEqual(restored[0].name, "Old Mill")
        self.assertFalse(UndoAction.objects.filter(pk=undo_action.pk).exists())
        self.assertIsNone(cache.get(f"dashboard:undo:{undo_action.cache_key}"))

    def test_restore_missing_payload_raises_and_deletes_row(self) -> None:
        undo_action = stash_for_undo("pin", [self.pin], self.profile)
        self.pin.delete()
        cache.delete(f"dashboard:undo:{undo_action.cache_key}")

        with pytest.raises(UndoExpiredError):
            restore_undo_action(undo_action)
        self.assertFalse(UndoAction.objects.filter(pk=undo_action.pk).exists())

    def test_clear_undo_history_removes_rows_and_cache(self) -> None:
        a1 = stash_for_undo("pin", [self.pin], self.profile)
        other_pin = baker.make(Pin, profile=self.profile)
        a2 = stash_for_undo("pin", [other_pin], self.profile)

        count = clear_undo_history(self.profile)

        self.assertEqual(count, 2)
        self.assertFalse(UndoAction.objects.filter(pk__in=[a1.pk, a2.pk]).exists())
        self.assertIsNone(cache.get(f"dashboard:undo:{a1.cache_key}"))
        self.assertIsNone(cache.get(f"dashboard:undo:{a2.cache_key}"))

    def test_get_undo_history_scoped_to_profile_and_newest_first(self) -> None:
        other_user = baker.make(User)
        stash_for_undo("pin", [baker.make(Pin, profile=other_user.profile)], other_user.profile)
        first = stash_for_undo("pin", [self.pin], self.profile)
        second_pin = baker.make(Pin, profile=self.profile)
        second = stash_for_undo("pin", [second_pin], self.profile)

        history = list(get_undo_history(self.profile))

        self.assertEqual([a.pk for a in history], [second.pk, first.pk])

    def test_unknown_handler_label_raises(self) -> None:
        with pytest.raises(ValueError, match="not_a_real_model"):
            get_handler("not_a_real_model")


@override_settings(CACHES=_LOCMEM_CACHES)
class PinUndoHandlerTests(TestCase):
    """Pin's own fields, labels, and personal detail-pin subtree round-trip."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_restores_labels(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Tagged")
        label = baker.make(Label, kind=KIND_TAG, profile=self.profile)
        pin.labels.add(label)

        undo_action = stash_for_undo("pin", [pin], self.profile)
        pin.delete()
        restored = restore_undo_action(undo_action)[0]

        self.assertIn(label, restored.labels.all())

    def test_restores_detail_pin_subtree_hierarchy(self) -> None:
        parent = baker.make(Pin, profile=self.profile, name="Parent")
        baker.make(Pin, profile=self.profile, name="Child", parent_pin=parent)

        subtree = list(Pin.objects.filter(pk=parent.pk).with_descendants())
        undo_action = stash_for_undo("pin", subtree, self.profile)
        for descendant in subtree:
            descendant.delete()

        restore_undo_action(undo_action)

        restored_parent = Pin.objects.get(profile=self.profile, name="Parent")
        restored_child = Pin.objects.get(profile=self.profile, name="Child")
        self.assertEqual(restored_child.parent_pin_id, restored_parent.pk)


@override_settings(CACHES=_LOCMEM_CACHES)
class WikiUndoHandlerTests(TestCase):
    """Wiki's own fields, labels, and child-wiki subtree round-trip."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_restores_fields_and_labels(self) -> None:
        wiki = _make_wiki(name="Old Factory", description="Rusty.")
        label = baker.make(Label, kind=KIND_TAG, profile=self.profile)
        wiki.labels.add(label)

        undo_action = stash_for_undo("wiki", [wiki], self.profile)
        wiki.delete()
        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.name, "Old Factory")
        self.assertEqual(restored.description, "Rusty.")
        self.assertIn(label, restored.labels.all())

    def test_restores_child_wiki_hierarchy(self) -> None:
        parent = _make_wiki(name="Parent Site")
        child = _make_wiki(name="Child Building", parent_wiki=parent)

        from urbanlens.dashboard.services.undo.handlers.wiki import with_wiki_descendants

        subtree = with_wiki_descendants([child])
        undo_action = stash_for_undo("wiki", subtree, self.profile)
        for descendant in subtree:
            descendant.delete()

        restore_undo_action(undo_action)

        restored_child = Wiki.objects.get(name="Child Building")
        self.assertEqual(restored_child.parent_wiki_id, parent.pk)


@override_settings(CACHES=_LOCMEM_CACHES)
class SafetyCheckinUndoHandlerTests(TestCase):
    """SafetyCheckin's own fields and emergency-contact snapshots round-trip."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_restores_fields_and_contacts(self) -> None:
        checkin = _make_checkin(self.profile, title="Weekend Hike")
        baker.make(SafetyCheckinContact, checkin=checkin, name="Alex", email="alex@example.com")

        undo_action = stash_for_undo("safety_checkin", [checkin], self.profile)
        checkin.delete()
        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.title, "Weekend Hike")
        self.assertEqual(restored.contacts.count(), 1)
        self.assertEqual(restored.contacts.first().name, "Alex")


@override_settings(CACHES=_LOCMEM_CACHES)
class TripUndoHandlerTests(TestCase):
    """Trip's own fields and membership/RSVP roster round-trip."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_restores_fields_and_memberships(self) -> None:
        trip = baker.make(Trip, creator=self.profile, name="Coast Trip")
        member = baker.make(User).profile
        TripMembership.objects.create(trip=trip, profile=member, rsvp="yes", is_organizer=False)

        undo_action = stash_for_undo("trip", [trip], self.profile)
        trip.delete()
        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.name, "Coast Trip")
        membership = restored.memberships.get(profile=member)
        self.assertEqual(membership.rsvp, "yes")


@override_settings(CACHES=_LOCMEM_CACHES)
class DeleteViewsStashUndoTests(TestCase):
    """The wired delete views stash an UndoAction before deleting."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_pin_delete_view_stashes_undo(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Doomed Pin")
        self.client.delete(f"/dashboard/rest/pins/{pin.uuid}/")
        self.assertTrue(UndoAction.objects.filter(profile=self.profile, model_label="pin").exists())

    def test_trip_delete_view_stashes_undo(self) -> None:
        trip = baker.make(Trip, creator=self.profile, name="Doomed Trip")
        self.client.delete(reverse("trips.delete", args=[trip.slug]))
        self.assertTrue(UndoAction.objects.filter(profile=self.profile, model_label="trip").exists())

    def test_safety_checkin_delete_view_stashes_undo(self) -> None:
        from urbanlens.dashboard.models.safety.model import SafetyCheckinStatus

        checkin = _make_checkin(self.profile, status=SafetyCheckinStatus.CHECKED_IN, resolved_at=timezone.now())
        self.client.post(reverse("safety.checkin.delete", args=[checkin.slug]))
        self.assertTrue(UndoAction.objects.filter(profile=self.profile, model_label="safety_checkin").exists())

    def test_wiki_child_delete_view_stashes_undo(self) -> None:
        parent_location = baker.make("dashboard.Location")
        parent_wiki = _make_wiki(location=parent_location)
        child = _make_wiki(name="Child Marker", parent_wiki=parent_wiki)
        baker.make(Pin, profile=self.profile, location=parent_location)
        self.client.delete(reverse("location.wiki.detail_pin.edit", args=[parent_location.slug, child.uuid]))
        self.assertTrue(UndoAction.objects.filter(profile=self.profile, model_label="wiki").exists())


@override_settings(CACHES=_LOCMEM_CACHES)
class UndoHistoryViewTests(TestCase):
    """GET /settings/undo-history/ lists a profile's active undo entries."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_lists_own_entries(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Listed Pin")
        stash_for_undo("pin", [pin], self.profile)

        response = self.client.get(reverse("undo.history"))

        self.assertContains(response, "Listed Pin")

    def test_excludes_other_profiles_entries(self) -> None:
        other_user = baker.make(User)
        other_pin = baker.make(Pin, profile=other_user.profile, name="Someone Elses Pin")
        stash_for_undo("pin", [other_pin], other_user.profile)

        response = self.client.get(reverse("undo.history"))

        self.assertNotContains(response, "Someone Elses Pin")


@override_settings(CACHES=_LOCMEM_CACHES)
class UndoRestoreViewTests(TestCase):
    """POST /undo/<uuid>/restore/ restores an entry, scoped to its owning profile."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_restores_and_removes_entry(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Restorable")
        undo_action = stash_for_undo("pin", [pin], self.profile)
        pin.delete()

        response = self.client.post(reverse("undo.restore", args=[undo_action.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Pin.objects.filter(profile=self.profile, name="Restorable").exists())
        self.assertFalse(UndoAction.objects.filter(pk=undo_action.pk).exists())

    def test_other_profile_cannot_restore(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Not Yours")
        undo_action = stash_for_undo("pin", [pin], self.profile)
        pin.delete()

        other_user = baker.make(User)
        self.client.force_login(other_user)
        response = self.client.post(reverse("undo.restore", args=[undo_action.uuid]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Pin.objects.filter(name="Not Yours").exists())

    def test_expired_payload_returns_error_toast_without_500(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Gone")
        undo_action = stash_for_undo("pin", [pin], self.profile)
        pin.delete()
        cache.delete(f"dashboard:undo:{undo_action.cache_key}")

        response = self.client.post(reverse("undo.restore", args=[undo_action.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("expired", response.headers.get("HX-Trigger", ""))


@override_settings(CACHES=_LOCMEM_CACHES)
class UndoClearViewTests(TestCase):
    """POST /settings/undo-history/clear/ wipes a profile's entire undo history."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_clears_all_entries(self) -> None:
        stash_for_undo("pin", [baker.make(Pin, profile=self.profile)], self.profile)
        stash_for_undo("pin", [baker.make(Pin, profile=self.profile)], self.profile)

        response = self.client.post(reverse("undo.clear"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(UndoAction.objects.filter(profile=self.profile).exists())
