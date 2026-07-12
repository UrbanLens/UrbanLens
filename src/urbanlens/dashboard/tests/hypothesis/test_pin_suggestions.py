"""Tests for batch photo-location ingestion: matching/clustering, accept/reject, and the
Tools-page local-scan upload endpoint.

Covers:
- ingest_location_hits - matches a hit against an existing pin's default
  (circle-fallback) boundary, clusters unmatched hits by proximity, and
  merges into existing pending suggestions on a re-run instead of duplicating.
- accept_pin_suggestion - logs one PinVisit per distinct date (skipping dates
  already visited), creates a new pin only when none matched, applies
  suggested_name only when the target pin has no name yet, and respects
  visit_logging_allowed.
- reject_pin_suggestion - flips status with no other side effects.
- PinSuggestionActionView - accept/reject over HTTP, ownership and
  already-handled guards.
- PhotoLocationScanUploadView - payload validation for the client-uploaded
  cluster list.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.pin_suggestions import LocationHit, accept_pin_suggestion, ingest_location_hits, reject_pin_suggestion

_PIN_LAT = Decimal("40.000000")
_PIN_LON = Decimal("-74.000000")


def _hit(lat: float, lon: float, day: str, label: str | None = None) -> LocationHit:
    return LocationHit(latitude=lat, longitude=lon, taken_at=datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(12, 0), tzinfo=datetime.UTC), label=label)


class IngestLocationHitsTests(TestCase):
    """Matching against existing pins and clustering unmatched hits."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)

    def test_hit_within_existing_pin_boundary_creates_matched_suggestion(self) -> None:
        # ~11m from the pin - well within the default 50m circle fallback.
        summary = ingest_location_hits(self.profile, [_hit(40.0001, -74.0, "2024-01-01")], origin=PinSuggestionOrigin.IMMICH)
        self.assertEqual(summary.matched_suggestions, 1)
        self.assertEqual(summary.new_pin_suggestions, 0)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(suggestion.pin_id, self.pin.pk)
        self.assertEqual(suggestion.visit_dates, ["2024-01-01"])
        self.assertEqual(suggestion.hit_count, 1)

    def test_hit_far_from_pins_creates_new_pin_suggestion(self) -> None:
        # ~111m away - outside the default 50m circle fallback.
        summary = ingest_location_hits(self.profile, [_hit(40.001, -74.0, "2024-01-01")], origin=PinSuggestionOrigin.IMMICH)
        self.assertEqual(summary.matched_suggestions, 0)
        self.assertEqual(summary.new_pin_suggestions, 1)
        suggestion = PinSuggestion.objects.get()
        self.assertIsNone(suggestion.pin_id)
        self.assertTrue(suggestion.is_new_pin)

    def test_nearby_unmatched_hits_cluster_together(self) -> None:
        hits = [_hit(41.0, -76.0, "2024-02-01"), _hit(41.0001, -76.0001, "2024-02-02")]
        summary = ingest_location_hits(self.profile, hits, origin=PinSuggestionOrigin.LOCAL_SCAN)
        self.assertEqual(summary.new_pin_suggestions, 1)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(suggestion.hit_count, 2)
        self.assertEqual(sorted(suggestion.visit_dates), ["2024-02-01", "2024-02-02"])

    def test_distant_unmatched_hits_stay_separate(self) -> None:
        hits = [_hit(41.0, -76.0, "2024-02-01"), _hit(42.0, -77.0, "2024-02-02")]
        summary = ingest_location_hits(self.profile, hits, origin=PinSuggestionOrigin.LOCAL_SCAN)
        self.assertEqual(summary.new_pin_suggestions, 2)

    def test_rerunning_ingest_merges_into_existing_pending_suggestion(self) -> None:
        ingest_location_hits(self.profile, [_hit(40.0001, -74.0, "2024-01-01")], origin=PinSuggestionOrigin.IMMICH)
        ingest_location_hits(self.profile, [_hit(40.0001, -74.0, "2024-01-02")], origin=PinSuggestionOrigin.IMMICH)

        self.assertEqual(PinSuggestion.objects.count(), 1)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(sorted(suggestion.visit_dates), ["2024-01-01", "2024-01-02"])
        self.assertEqual(suggestion.hit_count, 2)

    def test_rerunning_new_pin_ingest_merges_by_proximity(self) -> None:
        ingest_location_hits(self.profile, [_hit(41.0, -76.0, "2024-02-01")], origin=PinSuggestionOrigin.LOCAL_SCAN)
        ingest_location_hits(self.profile, [_hit(41.00005, -76.00005, "2024-02-02")], origin=PinSuggestionOrigin.LOCAL_SCAN)

        self.assertEqual(PinSuggestion.objects.count(), 1)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(suggestion.hit_count, 2)


class AcceptPinSuggestionTests(TestCase):
    """accept_pin_suggestion logs dated visits and reuses/creates pins correctly."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)

    def _matched_suggestion(self, dates: list[str]) -> PinSuggestion:
        return PinSuggestion.objects.create(
            profile=self.profile,
            pin=self.pin,
            latitude=_PIN_LAT,
            longitude=_PIN_LON,
            origin=PinSuggestionOrigin.IMMICH,
            visit_dates=dates,
            hit_count=len(dates),
        )

    def test_accept_matched_suggestion_creates_one_visit_per_date(self) -> None:
        suggestion = self._matched_suggestion(["2024-01-01", "2024-01-02"])
        pin, visits = accept_pin_suggestion(suggestion, self.profile)

        self.assertEqual(pin, self.pin)
        self.assertEqual(len(visits), 2)
        self.assertEqual(set(PinVisit.objects.filter(pin=self.pin).values_list("source", flat=True)), {VisitSource.HISTORY})
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.ACCEPTED)

    def test_accept_matched_suggestion_skips_dates_already_visited(self) -> None:
        baker.make_recipe("dashboard.pin_visit", pin=self.pin, visited_at=datetime.datetime(2024, 1, 1, 9, 0, tzinfo=datetime.UTC))
        suggestion = self._matched_suggestion(["2024-01-01", "2024-01-02"])

        _pin, visits = accept_pin_suggestion(suggestion, self.profile)

        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0].visited_at.date().isoformat(), "2024-01-02")
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 2)

    def test_accept_new_pin_suggestion_creates_pin_and_visit(self) -> None:
        suggestion = PinSuggestion.objects.create(
            profile=self.profile,
            pin=None,
            latitude=Decimal("41.000000"),
            longitude=Decimal("-76.000000"),
            origin=PinSuggestionOrigin.LOCAL_SCAN,
            visit_dates=["2024-02-01"],
            hit_count=1,
            suggested_name="Test Place",
        )
        with mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None):
            pin, visits = accept_pin_suggestion(suggestion, self.profile)

        self.assertNotEqual(pin.pk, self.pin.pk)
        self.assertEqual(pin.profile_id, self.profile.pk)
        self.assertEqual(pin.name, "Test Place")
        self.assertEqual(len(visits), 1)

    def test_accept_new_pin_suggestion_does_not_rename_an_already_named_pin(self) -> None:
        self.pin.name = "Existing Name"
        self.pin.save(update_fields=["name"])
        suggestion = PinSuggestion.objects.create(
            profile=self.profile,
            pin=None,
            latitude=_PIN_LAT,
            longitude=_PIN_LON,
            origin=PinSuggestionOrigin.LOCAL_SCAN,
            visit_dates=["2024-03-01"],
            hit_count=1,
            suggested_name="Should Not Apply",
        )
        pin, _visits = accept_pin_suggestion(suggestion, self.profile)

        self.assertEqual(pin.pk, self.pin.pk)
        self.assertEqual(pin.name, "Existing Name")

    def test_accept_skips_visits_when_visit_logging_disabled(self) -> None:
        self.profile.track_pin_visits = False
        self.profile.save(update_fields=["track_pin_visits"])
        suggestion = self._matched_suggestion(["2024-01-01"])

        pin, visits = accept_pin_suggestion(suggestion, self.profile)

        self.assertEqual(pin, self.pin)
        self.assertEqual(visits, [])
        self.assertFalse(PinVisit.objects.filter(pin=self.pin).exists())


class RejectPinSuggestionTests(TestCase):
    """reject_pin_suggestion flips status without side effects."""

    def test_reject_sets_status_rejected(self) -> None:
        user = baker.make(User)
        suggestion = PinSuggestion.objects.create(
            profile=user.profile,
            pin=None,
            latitude=_PIN_LAT,
            longitude=_PIN_LON,
            origin=PinSuggestionOrigin.IMMICH,
            visit_dates=["2024-01-01"],
        )
        reject_pin_suggestion(suggestion)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.REJECTED)
        self.assertFalse(Pin.objects.filter(profile=user.profile).exists())


class PinSuggestionActionViewTests(TestCase):
    """Accept/reject over HTTP, with ownership and already-handled guards."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)

    def _suggestion(self, **kwargs) -> PinSuggestion:
        defaults = {"profile": self.profile, "pin": self.pin, "latitude": _PIN_LAT, "longitude": _PIN_LON, "origin": PinSuggestionOrigin.IMMICH, "visit_dates": ["2024-01-01"], "hit_count": 1}
        defaults.update(kwargs)
        return PinSuggestion.objects.create(**defaults)

    def test_accept_logs_visit_and_marks_accepted(self) -> None:
        suggestion = self._suggestion()
        response = self.client.post(reverse("memories.locations.action", args=[suggestion.pk, "accept"]))
        self.assertEqual(response.status_code, 200)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.ACCEPTED)
        self.assertTrue(PinVisit.objects.filter(pin=self.pin, source=VisitSource.HISTORY).exists())

    def test_reject_marks_rejected_without_creating_a_visit(self) -> None:
        suggestion = self._suggestion()
        response = self.client.post(reverse("memories.locations.action", args=[suggestion.pk, "reject"]))
        self.assertEqual(response.status_code, 200)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.REJECTED)
        self.assertFalse(PinVisit.objects.filter(pin=self.pin).exists())

    def test_unknown_action_is_404(self) -> None:
        suggestion = self._suggestion()
        response = self.client.post(reverse("memories.locations.action", args=[suggestion.pk, "explode"]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_act_on_another_profiles_suggestion(self) -> None:
        other = baker.make(User)
        suggestion = self._suggestion(profile=other.profile, pin=None)
        response = self.client.post(reverse("memories.locations.action", args=[suggestion.pk, "accept"]))
        self.assertEqual(response.status_code, 404)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.PENDING)

    def test_already_handled_suggestion_is_a_noop(self) -> None:
        suggestion = self._suggestion(status=PinSuggestionStatus.ACCEPTED)
        response = self.client.post(reverse("memories.locations.action", args=[suggestion.pk, "accept"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 0)


class PhotoLocationScanUploadViewTests(TestCase):
    """Validation and ingestion for the Tools-page local-scan upload endpoint."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, body: dict):
        return self.client.post(reverse("tools.photo_scan.upload"), data=json.dumps(body), content_type="application/json")

    def test_valid_clusters_create_suggestions(self) -> None:
        response = self._post({"clusters": [{"latitude": 41.0, "longitude": -76.0, "dates": ["2024-05-01", "2024-05-02"], "count": 3, "label": "IMG_0001.jpg"}]})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["new_pin_suggestions"], 1)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(suggestion.origin, PinSuggestionOrigin.LOCAL_SCAN)
        self.assertEqual(suggestion.hit_count, 3)
        self.assertEqual(sorted(suggestion.visit_dates), ["2024-05-01", "2024-05-02"])

    def test_invalid_json_body_is_400(self) -> None:
        response = self.client.post(reverse("tools.photo_scan.upload"), data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_empty_clusters_list_is_400(self) -> None:
        response = self._post({"clusters": []})
        self.assertEqual(response.status_code, 400)

    def test_too_many_clusters_is_400(self) -> None:
        clusters = [{"latitude": 40.0 + i * 0.01, "longitude": -74.0, "dates": ["2024-01-01"], "count": 1} for i in range(501)]
        response = self._post({"clusters": clusters})
        self.assertEqual(response.status_code, 400)

    def test_cluster_with_invalid_coordinates_is_skipped(self) -> None:
        response = self._post({"clusters": [{"latitude": 999, "longitude": -76.0, "dates": ["2024-05-01"]}]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_upload_reuses_ingest_pipeline_for_matched_pins(self) -> None:
        location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        response = self._post({"clusters": [{"latitude": 40.0001, "longitude": -74.0, "dates": ["2024-05-01"], "count": 1}]})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["matched_suggestions"], 1)
        self.assertEqual(data["new_pin_suggestions"], 0)
