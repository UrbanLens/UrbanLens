"""Tests for PinImportFailure - the review queue for pins whose Google Maps CID
never resolved to a location during import.

Covers:
- services.pin_import_failures.record_pin_import_failure - creates a PENDING
  row, and is idempotent per (profile, cid) even across differing
  name/description/reason on a later call (re-running the same import must
  never resurrect or duplicate an entry).
- tasks.resolve_deferred_pin_locations - records a failure for a cid the
  resolver confirms unresolvable, and for every cid still unresolved when
  each of the three give-up branches (auth_failed, consecutive_request_failures
  cap, consecutive_no_progress cap) fires; auto-resolves a pending failure the
  moment its cid succeeds, on this call or a later retry; never duplicates a
  failure row across repeated calls.
- services.pin_import_failures.resolve_pin_import_failure /
  dismiss_pin_import_failure / auto_resolve_pin_import_failure_for_cid.
- controllers.pin_import_failures - the HTTP review-queue actions (resolve,
  dismiss, queue partial), with ownership/already-handled guards.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure, PinImportFailureReason, PinImportFailureStatus
from urbanlens.dashboard.services.apis.locations.cid_resolution import PROVIDER_REDATA, CidResolutionResult
from urbanlens.dashboard.services.pin_creation import PinCreationError
from urbanlens.dashboard.services.pin_import_failures import (
    auto_resolve_pin_import_failure_for_cid,
    dismiss_pin_import_failure,
    record_pin_import_failure,
    resolve_pin_import_failure,
)

# DB-backed @given tests never touch self.client - only ORM/service calls -
# per this repo's convention of keeping hypothesis's per-example DB flush away
# from the Django test client.
_db_settings = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _resolve_cids_path() -> str:
    return "urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids"


class RecordPinImportFailureTests(TestCase):
    """record_pin_import_failure - creation and (profile, cid) idempotency."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_creates_a_pending_row_with_correct_fields(self) -> None:
        record_pin_import_failure(self.profile, 12345, name="Cresson Sanatorium", description="Old asylum", reason=PinImportFailureReason.NO_LOCATION_FOUND)

        failure = PinImportFailure.objects.get(profile=self.profile, cid=12345)
        self.assertEqual(failure.name, "Cresson Sanatorium")
        self.assertEqual(failure.description, "Old asylum")
        self.assertEqual(failure.reason, PinImportFailureReason.NO_LOCATION_FOUND)
        self.assertEqual(failure.status, PinImportFailureStatus.PENDING)
        self.assertIsNone(failure.pin_id)

    def test_second_call_same_profile_and_cid_does_not_duplicate(self) -> None:
        record_pin_import_failure(self.profile, 12345, name="First Name", description="First desc", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        record_pin_import_failure(self.profile, 12345, name="Different Name", description="Different desc", reason=PinImportFailureReason.LOOKUP_STALLED)

        self.assertEqual(PinImportFailure.objects.filter(profile=self.profile, cid=12345).count(), 1)
        failure = PinImportFailure.objects.get(profile=self.profile, cid=12345)
        # The second call's differing values must never overwrite the first.
        self.assertEqual(failure.name, "First Name")
        self.assertEqual(failure.description, "First desc")
        self.assertEqual(failure.reason, PinImportFailureReason.NO_LOCATION_FOUND)

    def test_repeated_call_does_not_resurrect_a_resolved_entry(self) -> None:
        record_pin_import_failure(self.profile, 12345, name="X", description="", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        failure = PinImportFailure.objects.get(profile=self.profile, cid=12345)
        failure.status = PinImportFailureStatus.RESOLVED
        failure.save(update_fields=["status"])

        record_pin_import_failure(self.profile, 12345, name="X", description="", reason=PinImportFailureReason.NO_LOCATION_FOUND)

        self.assertEqual(PinImportFailure.objects.filter(profile=self.profile, cid=12345).count(), 1)
        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.RESOLVED)

    def test_different_cids_each_get_their_own_row(self) -> None:
        record_pin_import_failure(self.profile, 111, name="A", description="", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        record_pin_import_failure(self.profile, 222, name="B", description="", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        self.assertEqual(PinImportFailure.objects.filter(profile=self.profile).count(), 2)

    @_db_settings
    @given(
        cid=st.integers(min_value=1, max_value=10**15),
        name=st.text(max_size=50),
        description=st.text(max_size=50),
        second_name=st.text(max_size=50),
        second_description=st.text(max_size=50),
    )
    def test_idempotent_for_any_cid_and_text_pair(self, cid: int, name: str, description: str, second_name: str, second_description: str) -> None:
        """No matter what varies between two calls for the same (profile, cid),
        exactly one row must ever exist, and it must keep the first values."""
        user = baker.make(User)
        profile = user.profile

        record_pin_import_failure(profile, cid, name=name, description=description, reason=PinImportFailureReason.NO_LOCATION_FOUND)
        record_pin_import_failure(profile, cid, name=second_name, description=second_description, reason=PinImportFailureReason.LOOKUP_ERROR)

        rows = list(PinImportFailure.objects.filter(profile=profile, cid=cid))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, name)
        self.assertEqual(rows[0].description, description)


class ResolvePinImportFailureTests(TestCase):
    """resolve_pin_import_failure - success via address/lat-lng, and error propagation."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile

    def _failure(self, **kwargs) -> PinImportFailure:
        defaults = {
            "profile": self.profile,
            "cid": 12345,
            "name": "Cresson Sanatorium",
            "description": "Old asylum <b>with html</b>",
            "reason": PinImportFailureReason.NO_LOCATION_FOUND,
        }
        defaults.update(kwargs)
        return PinImportFailure.objects.create(**defaults)

    def test_resolve_via_address_success(self) -> None:
        failure = self._failure()
        with (
            mock.patch("urbanlens.dashboard.services.pin_creation.get_pin_by_address", return_value=(40.0, -74.0)),
            mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"),
        ):
            pin = resolve_pin_import_failure(failure, self.profile, address="123 Main St, Springfield")

        self.assertIsInstance(pin, Pin)
        self.assertAlmostEqual(float(pin.location.latitude), 40.0, places=4)
        self.assertAlmostEqual(float(pin.location.longitude), -74.0, places=4)

        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.RESOLVED)
        self.assertEqual(failure.pin_id, pin.pk)

    def test_resolve_via_lat_lng_success(self) -> None:
        failure = self._failure()
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"):
            pin = resolve_pin_import_failure(failure, self.profile, latitude=41.5, longitude=-72.5)

        self.assertAlmostEqual(float(pin.location.latitude), 41.5, places=4)
        self.assertAlmostEqual(float(pin.location.longitude), -72.5, places=4)

        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.RESOLVED)
        self.assertEqual(failure.pin_id, pin.pk)

    def test_resolve_passes_name_and_description_through_unmodified(self) -> None:
        """Per its own docstring: this is a one-off manual recovery, not a replay
        of the import pipeline - failure.description must not be re-run through
        strip_html/extract_image_urls/extract_link_urls."""
        failure = self._failure(description="Raw <b>html</b> and a [link](http://example.com)")
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"):
            pin = resolve_pin_import_failure(failure, self.profile, latitude=41.5, longitude=-72.5)

        self.assertEqual(pin.description, failure.description)

    def test_resolve_propagates_pin_creation_error_and_leaves_the_row_pending(self) -> None:
        failure = self._failure()

        with self.assertRaises(PinCreationError):
            resolve_pin_import_failure(failure, self.profile)

        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.PENDING)
        self.assertIsNone(failure.pin_id)


class DismissPinImportFailureTests(TestCase):
    """dismiss_pin_import_failure - marks DISMISSED without touching pin."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_dismiss_sets_dismissed_status(self) -> None:
        failure = PinImportFailure.objects.create(profile=self.profile, cid=12345, name="X", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        dismiss_pin_import_failure(failure)
        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.DISMISSED)
        self.assertIsNone(failure.pin_id)


class AutoResolvePinImportFailureForCidTests(TestCase):
    """auto_resolve_pin_import_failure_for_cid - clears a pending row on success."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_resolves_a_matching_pending_row(self) -> None:
        failure = PinImportFailure.objects.create(profile=self.profile, cid=12345, name="X", reason=PinImportFailureReason.LOOKUP_STALLED)
        auto_resolve_pin_import_failure_for_cid(self.profile, 12345, self.pin)
        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.RESOLVED)
        self.assertEqual(failure.pin_id, self.pin.pk)

    def test_noop_when_no_failure_exists_for_the_cid(self) -> None:
        # Must be safe to call unconditionally for a cid that never had a failure entry.
        auto_resolve_pin_import_failure_for_cid(self.profile, 99999, self.pin)
        self.assertEqual(PinImportFailure.objects.filter(profile=self.profile, cid=99999).count(), 0)

    def test_does_not_touch_an_already_resolved_row(self) -> None:
        other_pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        failure = PinImportFailure.objects.create(
            profile=self.profile, cid=12345, name="X", reason=PinImportFailureReason.LOOKUP_STALLED, status=PinImportFailureStatus.RESOLVED, pin=other_pin,
        )
        auto_resolve_pin_import_failure_for_cid(self.profile, 12345, self.pin)
        failure.refresh_from_db()
        self.assertEqual(failure.pin_id, other_pin.pk)

    def test_does_not_touch_a_dismissed_row(self) -> None:
        failure = PinImportFailure.objects.create(profile=self.profile, cid=12345, name="X", reason=PinImportFailureReason.LOOKUP_STALLED, status=PinImportFailureStatus.DISMISSED)
        auto_resolve_pin_import_failure_for_cid(self.profile, 12345, self.pin)
        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.DISMISSED)
        self.assertIsNone(failure.pin_id)


class ResolveDeferredPinLocationsFailureRecordingTests(TestCase):
    """resolve_deferred_pin_locations - failure recording on the unresolvable path,
    the three give-up branches, auto-resolve on success, and no duplication on rerun."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile

    def _lists(self, pins: list[dict]) -> list[dict]:
        return [{"stem": "", "create_category": False, "label_ids": [], "pins": pins}]

    def test_unresolvable_cid_creates_no_location_found_failure(self) -> None:
        deferred_lists = self._lists([{"name": "Cresson Sanatorium", "description": "Old asylum", "cid": 12345}])
        result = CidResolutionResult(provider=PROVIDER_REDATA, unresolvable={12345})

        with mock.patch(_resolve_cids_path(), return_value=result), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            summary = tasks.resolve_deferred_pin_locations(self.profile.pk, deferred_lists, auto_tag=False)

        self.assertEqual(summary, {"created": 0, "exists": 0, "skipped": 1})
        failure = PinImportFailure.objects.get(profile=self.profile, cid=12345)
        self.assertEqual(failure.reason, PinImportFailureReason.NO_LOCATION_FOUND)
        self.assertEqual(failure.name, "Cresson Sanatorium")
        self.assertEqual(failure.description, "Old asylum")
        self.assertEqual(failure.status, PinImportFailureStatus.PENDING)

    def test_rerunning_the_same_import_does_not_duplicate_the_failure(self) -> None:
        deferred_lists = self._lists([{"name": "Cresson Sanatorium", "description": "Old asylum", "cid": 12345}])
        result = CidResolutionResult(provider=PROVIDER_REDATA, unresolvable={12345})

        with mock.patch(_resolve_cids_path(), return_value=result), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            tasks.resolve_deferred_pin_locations(self.profile.pk, deferred_lists, auto_tag=False)
            tasks.resolve_deferred_pin_locations(self.profile.pk, deferred_lists, auto_tag=False)

        self.assertEqual(PinImportFailure.objects.filter(profile=self.profile, cid=12345).count(), 1)

    def test_auth_failed_records_lookup_error_for_every_cid(self) -> None:
        pins = [
            {"name": "Black Point Ruins", "description": "", "cid": 111},
            {"name": "Fort Wetherill", "description": "", "cid": 222},
        ]
        result = CidResolutionResult(provider=PROVIDER_REDATA, pending=[111, 222], auth_failed=True)

        with mock.patch(_resolve_cids_path(), return_value=result), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            summary = tasks.resolve_deferred_pin_locations(self.profile.pk, self._lists(pins), auto_tag=False)

        self.assertEqual(summary, {"created": 0, "exists": 0, "skipped": 2})
        for cid in (111, 222):
            failure = PinImportFailure.objects.get(profile=self.profile, cid=cid)
            self.assertEqual(failure.reason, PinImportFailureReason.LOOKUP_ERROR)
            self.assertEqual(failure.status, PinImportFailureStatus.PENDING)

        notification = NotificationLog.objects.get(profile=self.profile)
        self.assertEqual(notification.url, reverse("memories.locations"))

    def test_consecutive_request_failures_cap_records_lookup_error_for_every_cid(self) -> None:
        pins = [
            {"name": "Black Point Ruins", "description": "", "cid": 111},
            {"name": "Fort Wetherill", "description": "", "cid": 222},
        ]
        result = CidResolutionResult(provider=PROVIDER_REDATA, pending=[111, 222], request_failed=True)

        with mock.patch(_resolve_cids_path(), return_value=result), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            summary = tasks.resolve_deferred_pin_locations(
                self.profile.pk,
                self._lists(pins),
                auto_tag=False,
                consecutive_request_failures=tasks._MAX_CONSECUTIVE_REDATA_FAILURES - 1,
            )

        self.assertEqual(summary, {"created": 0, "exists": 0, "skipped": 2})
        for cid in (111, 222):
            failure = PinImportFailure.objects.get(profile=self.profile, cid=cid)
            self.assertEqual(failure.reason, PinImportFailureReason.LOOKUP_ERROR)

        notification = NotificationLog.objects.get(profile=self.profile)
        self.assertEqual(notification.url, reverse("memories.locations"))

    def test_consecutive_no_progress_cap_records_lookup_stalled_for_every_cid(self) -> None:
        pins = [
            {"name": "Black Point Ruins", "description": "", "cid": 111},
            {"name": "Fort Wetherill", "description": "", "cid": 222},
        ]
        result = CidResolutionResult(provider=PROVIDER_REDATA, pending=[111, 222], request_failed=False)

        with mock.patch(_resolve_cids_path(), return_value=result), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            summary = tasks.resolve_deferred_pin_locations(
                self.profile.pk,
                self._lists(pins),
                auto_tag=False,
                consecutive_no_progress=tasks._MAX_CONSECUTIVE_NO_PROGRESS_RETRIES - 1,
            )

        self.assertEqual(summary, {"created": 0, "exists": 0, "skipped": 2})
        for cid in (111, 222):
            failure = PinImportFailure.objects.get(profile=self.profile, cid=cid)
            self.assertEqual(failure.reason, PinImportFailureReason.LOOKUP_STALLED)

        notification = NotificationLog.objects.get(profile=self.profile)
        self.assertEqual(notification.url, reverse("memories.locations"))

    def test_pin_that_resolves_later_auto_resolves_its_prior_pending_failure(self) -> None:
        """A cid that failed on one call and then resolves on a later call (a retry,
        or a fresh re-import) must have its pending failure cleared and pointed at
        the new pin - this is the auto-resolve half of the feature."""
        pins = [{"name": "Cresson Sanatorium", "description": "Old asylum", "cid": 12345, "label_ids": []}]
        unresolved = CidResolutionResult(provider=PROVIDER_REDATA, unresolvable={12345})

        with mock.patch(_resolve_cids_path(), return_value=unresolved), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            tasks.resolve_deferred_pin_locations(self.profile.pk, self._lists(pins), auto_tag=False)

        failure = PinImportFailure.objects.get(profile=self.profile, cid=12345)
        self.assertEqual(failure.status, PinImportFailureStatus.PENDING)

        resolved = CidResolutionResult(provider=PROVIDER_REDATA, resolved={12345: (41.348754, -71.453896)})
        with mock.patch(_resolve_cids_path(), return_value=resolved), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            summary = tasks.resolve_deferred_pin_locations(self.profile.pk, self._lists(pins), auto_tag=False)

        self.assertEqual(summary["created"], 1)
        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.RESOLVED)
        pin = Pin.objects.get(profile=self.profile, name="Cresson Sanatorium")
        self.assertEqual(failure.pin_id, pin.pk)
        # Still exactly one failure row for this cid - resolving never duplicates it.
        self.assertEqual(PinImportFailure.objects.filter(profile=self.profile, cid=12345).count(), 1)

    def test_successful_resolution_with_no_prior_failure_does_not_create_one(self) -> None:
        pins = [{"name": "Known Place", "description": "", "cid": 55555, "label_ids": []}]
        resolved = CidResolutionResult(provider=PROVIDER_REDATA, resolved={55555: (41.0, -75.0)})

        with mock.patch(_resolve_cids_path(), return_value=resolved), mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            summary = tasks.resolve_deferred_pin_locations(self.profile.pk, self._lists(pins), auto_tag=False)

        self.assertEqual(summary["created"], 1)
        self.assertEqual(PinImportFailure.objects.filter(profile=self.profile, cid=55555).count(), 0)


class PinImportFailureResolveViewTests(TestCase):
    """POST /memories/locations/import-failures/<id>/resolve/."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _failure(self, **kwargs) -> PinImportFailure:
        defaults = {
            "profile": self.profile,
            "cid": 12345,
            "name": "Cresson Sanatorium",
            "description": "",
            "reason": PinImportFailureReason.NO_LOCATION_FOUND,
        }
        defaults.update(kwargs)
        return PinImportFailure.objects.create(**defaults)

    def test_resolve_with_valid_coordinates_places_a_pin_and_toasts(self) -> None:
        failure = self._failure()
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"):
            response = self.client.post(
                reverse("memories.locations.import_failures.resolve", args=[failure.pk]),
                {"latitude": "41.5", "longitude": "-72.5"},
            )

        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertIn("Pin placed for", trigger["showToast"]["message"])
        self.assertEqual(trigger["showToast"]["level"], "success")
        self.assertTrue(trigger["refreshQueue"])

        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.RESOLVED)
        self.assertIsNotNone(failure.pin_id)

    def test_invalid_coordinates_rerenders_the_card_with_an_error_toast(self) -> None:
        failure = self._failure()
        response = self.client.post(
            reverse("memories.locations.import_failures.resolve", args=[failure.pk]),
            {"latitude": "not-a-number", "longitude": "-72.5"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'id="pin-import-failure-card-{failure.pk}"', response.content.decode())
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["message"], "Enter a valid latitude and longitude.")
        self.assertEqual(trigger["showToast"]["level"], "error")

        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.PENDING)

    def test_pin_creation_error_rerenders_the_card_with_the_safe_message(self) -> None:
        failure = self._failure()
        response = self.client.post(reverse("memories.locations.import_failures.resolve", args=[failure.pk]), {})

        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["level"], "error")
        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.PENDING)

    def test_cannot_resolve_another_profiles_failure(self) -> None:
        other = baker.make(User)
        failure = self._failure(profile=other.profile)
        response = self.client.post(
            reverse("memories.locations.import_failures.resolve", args=[failure.pk]),
            {"latitude": "41.5", "longitude": "-72.5"},
        )
        self.assertEqual(response.status_code, 404)
        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.PENDING)

    def test_already_handled_failure_is_a_noop(self) -> None:
        failure = self._failure(status=PinImportFailureStatus.RESOLVED)
        response = self.client.post(
            reverse("memories.locations.import_failures.resolve", args=[failure.pk]),
            {"latitude": "41.5", "longitude": "-72.5"},
        )
        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["message"], "That entry has already been handled.")
        self.assertEqual(trigger["showToast"]["level"], "info")

    def test_requires_login(self) -> None:
        self.client.logout()
        failure = self._failure()
        response = self.client.post(reverse("memories.locations.import_failures.resolve", args=[failure.pk]), {"latitude": "1", "longitude": "1"})
        self.assertEqual(response.status_code, 302)


class PinImportFailureDismissViewTests(TestCase):
    """POST /memories/locations/import-failures/<id>/dismiss/."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_dismiss_marks_it_dismissed_and_toasts(self) -> None:
        failure = PinImportFailure.objects.create(profile=self.profile, cid=12345, name="X", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        response = self.client.post(reverse("memories.locations.import_failures.dismiss", args=[failure.pk]))

        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["message"], "Removed from the list.")
        self.assertTrue(trigger["refreshQueue"])

        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.DISMISSED)

    def test_cannot_dismiss_another_profiles_failure(self) -> None:
        other = baker.make(User)
        failure = PinImportFailure.objects.create(profile=other.profile, cid=12345, name="X", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        response = self.client.post(reverse("memories.locations.import_failures.dismiss", args=[failure.pk]))
        self.assertEqual(response.status_code, 404)
        failure.refresh_from_db()
        self.assertEqual(failure.status, PinImportFailureStatus.PENDING)

    def test_already_handled_failure_is_a_noop(self) -> None:
        failure = PinImportFailure.objects.create(profile=self.profile, cid=12345, name="X", reason=PinImportFailureReason.NO_LOCATION_FOUND, status=PinImportFailureStatus.DISMISSED)
        response = self.client.post(reverse("memories.locations.import_failures.dismiss", args=[failure.pk]))
        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["message"], "That entry has already been handled.")


class PinImportFailureQueuePartialViewTests(TestCase):
    """GET /memories/locations/import-failures/queue/ - pending-only, paginated."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_only_pending_rows_are_returned(self) -> None:
        pending = PinImportFailure.objects.create(profile=self.profile, cid=1, name="Pending", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        PinImportFailure.objects.create(profile=self.profile, cid=2, name="Resolved", reason=PinImportFailureReason.NO_LOCATION_FOUND, status=PinImportFailureStatus.RESOLVED)
        PinImportFailure.objects.create(profile=self.profile, cid=3, name="Dismissed", reason=PinImportFailureReason.NO_LOCATION_FOUND, status=PinImportFailureStatus.DISMISSED)

        response = self.client.get(reverse("memories.locations.import_failures.queue"))

        self.assertEqual(response.status_code, 200)
        failures = list(response.context["failures"])
        self.assertEqual(failures, [pending])

    def test_pagination_respects_page_size_and_excludes_other_profiles(self) -> None:
        other = baker.make(User)
        PinImportFailure.objects.create(profile=other.profile, cid=999, name="Someone else's", reason=PinImportFailureReason.NO_LOCATION_FOUND)
        for i in range(13):
            PinImportFailure.objects.create(profile=self.profile, cid=1000 + i, name=f"Place {i}", reason=PinImportFailureReason.NO_LOCATION_FOUND)

        first_page = self.client.get(reverse("memories.locations.import_failures.queue"))
        self.assertEqual(len(first_page.context["failures"]), 12)

        second_page = self.client.get(reverse("memories.locations.import_failures.queue"), {"page": 2})
        self.assertEqual(len(second_page.context["failures"]), 1)

        all_cids = {f.cid for f in first_page.context["failures"]} | {f.cid for f in second_page.context["failures"]}
        self.assertEqual(len(all_cids), 13)
        self.assertNotIn(999, all_cids)

    def test_empty_queue_renders_the_empty_state(self) -> None:
        response = self.client.get(reverse("memories.locations.import_failures.queue"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("No import issues right now.", response.content.decode())
