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
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_suggestions import _PAGE_SIZE
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import MAX_SUGGESTION_PHOTOS, PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.pin_suggestions import LocationHit, accept_pin_suggestion, ingest_location_hits, reject_pin_suggestion

_PIN_LAT = Decimal("40.000000")
_PIN_LON = Decimal("-74.000000")


def _hit(lat: float, lon: float, day: str, label: str | None = None, asset_id: str | None = None, source_key: str | None = None) -> LocationHit:
    return LocationHit(
        latitude=lat,
        longitude=lon,
        taken_at=datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(12, 0), tzinfo=datetime.UTC),
        label=label,
        asset_id=asset_id,
        source_key=source_key,
    )


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


class IngestLocationHitsTrackingDisabledTests(TestCase):
    """ingest_location_hits is a no-op when the profile has visit-history tracking off."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.profile.track_pin_visits = False
        self.profile.save(update_fields=["track_pin_visits"])
        self.location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)

    def test_matched_hit_creates_no_suggestion(self) -> None:
        summary = ingest_location_hits(self.profile, [_hit(40.0001, -74.0, "2024-01-01")], origin=PinSuggestionOrigin.IMMICH)
        self.assertEqual(summary.matched_suggestions, 0)
        self.assertEqual(summary.new_pin_suggestions, 0)
        self.assertEqual(summary.hits_processed, 0)
        self.assertEqual(PinSuggestion.objects.count(), 0)

    def test_unmatched_hit_creates_no_suggestion(self) -> None:
        summary = ingest_location_hits(self.profile, [_hit(41.0, -76.0, "2024-02-01")], origin=PinSuggestionOrigin.LOCAL_SCAN)
        self.assertEqual(summary.new_pin_suggestions, 0)
        self.assertEqual(PinSuggestion.objects.count(), 0)


class SampleAssetsAndSuggestionKeysTests(TestCase):
    """sample_assets capping/dedup and suggestion_ids_by_key reporting."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_immich_asset_ids_populate_sample_assets(self) -> None:
        hits = [_hit(41.0, -76.0, "2024-02-01", asset_id="a1")]
        ingest_location_hits(self.profile, hits, origin=PinSuggestionOrigin.IMMICH)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(suggestion.sample_assets, [{"asset_id": "a1", "taken_at": "2024-02-01"}])

    def test_merging_hits_dedupes_sample_assets_by_id(self) -> None:
        ingest_location_hits(self.profile, [_hit(41.0, -76.0, "2024-02-01", asset_id="a1")], origin=PinSuggestionOrigin.IMMICH)
        ingest_location_hits(self.profile, [_hit(41.00005, -76.00005, "2024-02-02", asset_id="a1")], origin=PinSuggestionOrigin.IMMICH)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(len(suggestion.sample_assets), 1)

    def test_sample_assets_never_exceeds_max_suggestion_photos(self) -> None:
        for i in range(MAX_SUGGESTION_PHOTOS + 5):
            ingest_location_hits(self.profile, [_hit(41.0, -76.0, "2024-02-01", asset_id=f"asset-{i}")], origin=PinSuggestionOrigin.IMMICH)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(len(suggestion.sample_assets), MAX_SUGGESTION_PHOTOS)

    def test_two_clusters_merging_into_one_suggestion_both_map_to_its_pk(self) -> None:
        hits = [_hit(41.0, -76.0, "2024-02-01", source_key="cluster-a"), _hit(41.00005, -76.00005, "2024-02-02", source_key="cluster-b")]
        summary = ingest_location_hits(self.profile, hits, origin=PinSuggestionOrigin.LOCAL_SCAN)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(summary.suggestion_ids_by_key, {"cluster-a": suggestion.pk, "cluster-b": suggestion.pk})

    def test_matched_pin_hits_are_also_reported_in_suggestion_ids_by_key(self) -> None:
        location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        summary = ingest_location_hits(self.profile, [_hit(40.0001, -74.0, "2024-01-01", source_key="cluster-a")], origin=PinSuggestionOrigin.IMMICH)
        suggestion = PinSuggestion.objects.get(pin=pin)
        self.assertEqual(summary.suggestion_ids_by_key, {"cluster-a": suggestion.pk})


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
        result = accept_pin_suggestion(suggestion, self.profile)

        self.assertEqual(result.pin, self.pin)
        self.assertEqual(len(result.visits), 2)
        self.assertEqual(set(PinVisit.objects.filter(pin=self.pin).values_list("source", flat=True)), {VisitSource.HISTORY})
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.ACCEPTED)

    def test_accept_matched_suggestion_skips_dates_already_visited(self) -> None:
        baker.make_recipe("dashboard.pin_visit", pin=self.pin, visited_at=datetime.datetime(2024, 1, 1, 9, 0, tzinfo=datetime.UTC))
        suggestion = self._matched_suggestion(["2024-01-01", "2024-01-02"])

        result = accept_pin_suggestion(suggestion, self.profile)

        self.assertEqual(len(result.visits), 1)
        self.assertEqual(result.visits[0].visited_at.date().isoformat(), "2024-01-02")
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
            result = accept_pin_suggestion(suggestion, self.profile)

        self.assertNotEqual(result.pin.pk, self.pin.pk)
        self.assertEqual(result.pin.profile_id, self.profile.pk)
        self.assertEqual(result.pin.name, "Test Place")
        self.assertEqual(len(result.visits), 1)

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
        result = accept_pin_suggestion(suggestion, self.profile)

        self.assertEqual(result.pin.pk, self.pin.pk)
        self.assertEqual(result.pin.name, "Existing Name")

    def test_accept_skips_visits_when_visit_logging_disabled(self) -> None:
        self.profile.track_pin_visits = False
        self.profile.save(update_fields=["track_pin_visits"])
        suggestion = self._matched_suggestion(["2024-01-01"])

        result = accept_pin_suggestion(suggestion, self.profile)

        self.assertEqual(result.pin, self.pin)
        self.assertEqual(result.visits, [])
        self.assertFalse(PinVisit.objects.filter(pin=self.pin).exists())


class AcceptPinSuggestionPhotoTests(TestCase):
    """accept_pin_suggestion attaches selected candidate photos and discards the rest."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)

    def _matched_suggestion(self, dates: list[str], **kwargs) -> PinSuggestion:
        defaults = {
            "profile": self.profile,
            "pin": self.pin,
            "latitude": _PIN_LAT,
            "longitude": _PIN_LON,
            "origin": PinSuggestionOrigin.LOCAL_SCAN,
            "visit_dates": dates,
            "hit_count": len(dates),
        }
        defaults.update(kwargs)
        return PinSuggestion.objects.create(**defaults)

    def test_selected_local_image_attaches_to_pin_and_matching_visit(self) -> None:
        suggestion = self._matched_suggestion(["2024-01-01", "2024-01-02"])
        image = baker.make(Image, profile=self.profile, pin=None, pin_suggestion=suggestion, taken_at=datetime.datetime(2024, 1, 2, 10, 0, tzinfo=datetime.UTC))

        result = accept_pin_suggestion(suggestion, self.profile, image_ids=[image.pk])

        image.refresh_from_db()
        self.assertEqual(image.pin_id, self.pin.pk)
        self.assertEqual(image.location_id, self.pin.location_id)
        self.assertIsNone(image.pin_suggestion_id)
        target_visit = next(v for v in result.visits if v.visited_at.date().isoformat() == "2024-01-02")
        self.assertEqual(image.visit_id, target_visit.pk)

    def test_unselected_candidate_images_are_deleted(self) -> None:
        suggestion = self._matched_suggestion(["2024-01-01"])
        kept = baker.make(Image, profile=self.profile, pin=None, pin_suggestion=suggestion)
        discarded = baker.make(Image, profile=self.profile, pin=None, pin_suggestion=suggestion)

        accept_pin_suggestion(suggestion, self.profile, image_ids=[kept.pk])

        self.assertTrue(Image.objects.filter(pk=kept.pk).exists())
        self.assertFalse(Image.objects.filter(pk=discarded.pk).exists())

    def test_image_id_for_a_different_suggestion_is_ignored(self) -> None:
        suggestion = self._matched_suggestion(["2024-01-01"])
        other_suggestion = self._matched_suggestion(["2024-03-01"])
        unrelated_image = baker.make(Image, profile=self.profile, pin=None, pin_suggestion=other_suggestion)

        accept_pin_suggestion(suggestion, self.profile, image_ids=[unrelated_image.pk])

        unrelated_image.refresh_from_db()
        self.assertIsNone(unrelated_image.pin_id)
        self.assertEqual(unrelated_image.pin_suggestion_id, other_suggestion.pk)

    def test_image_with_no_taken_at_falls_back_to_first_visit(self) -> None:
        suggestion = self._matched_suggestion(["2024-01-01", "2024-01-02"])
        image = baker.make(Image, profile=self.profile, pin=None, pin_suggestion=suggestion, taken_at=None)

        result = accept_pin_suggestion(suggestion, self.profile, image_ids=[image.pk])

        image.refresh_from_db()
        self.assertIn(image.visit_id, [v.pk for v in result.visits])

    def test_already_logged_date_still_resolves_to_the_existing_visit(self) -> None:
        existing_visit = baker.make_recipe("dashboard.pin_visit", pin=self.pin, visited_at=datetime.datetime(2024, 1, 1, 9, 0, tzinfo=datetime.UTC))
        suggestion = self._matched_suggestion(["2024-01-01"])
        image = baker.make(Image, profile=self.profile, pin=None, pin_suggestion=suggestion, taken_at=datetime.datetime(2024, 1, 1, 10, 0, tzinfo=datetime.UTC))

        result = accept_pin_suggestion(suggestion, self.profile, image_ids=[image.pk])

        self.assertEqual(result.visits, [])
        image.refresh_from_db()
        self.assertEqual(image.visit_id, existing_visit.pk)

    def test_selected_immich_asset_maps_to_the_correct_visit(self) -> None:
        suggestion = self._matched_suggestion(["2024-01-01", "2024-01-02"], origin=PinSuggestionOrigin.IMMICH, sample_assets=[{"asset_id": "a1", "taken_at": "2024-01-02"}])

        result = accept_pin_suggestion(suggestion, self.profile, asset_ids=["a1"])

        target_visit = next(v for v in result.visits if v.visited_at.date().isoformat() == "2024-01-02")
        self.assertEqual(result.immich_import_visits, {"a1": target_visit.pk})

    def test_asset_id_outside_sample_assets_is_ignored(self) -> None:
        suggestion = self._matched_suggestion(["2024-01-01"], origin=PinSuggestionOrigin.IMMICH)

        result = accept_pin_suggestion(suggestion, self.profile, asset_ids=["not-a-sample"])

        self.assertEqual(result.immich_import_visits, {})


class RejectPinSuggestionTests(TestCase):
    """reject_pin_suggestion flips status and discards any staged candidate photos."""

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

    def test_reject_deletes_candidate_images(self) -> None:
        user = baker.make(User)
        suggestion = PinSuggestion.objects.create(
            profile=user.profile, pin=None, latitude=_PIN_LAT, longitude=_PIN_LON, origin=PinSuggestionOrigin.LOCAL_SCAN, visit_dates=["2024-01-01"],
        )
        image = baker.make(Image, profile=user.profile, pin=None, pin_suggestion=suggestion)

        reject_pin_suggestion(suggestion)

        self.assertFalse(Image.objects.filter(pk=image.pk).exists())

    def test_reject_does_not_touch_other_suggestions_images(self) -> None:
        user = baker.make(User)
        suggestion = PinSuggestion.objects.create(
            profile=user.profile, pin=None, latitude=_PIN_LAT, longitude=_PIN_LON, origin=PinSuggestionOrigin.LOCAL_SCAN, visit_dates=["2024-01-01"],
        )
        other_suggestion = PinSuggestion.objects.create(
            profile=user.profile, pin=None, latitude=_PIN_LAT, longitude=_PIN_LON, origin=PinSuggestionOrigin.LOCAL_SCAN, visit_dates=["2024-03-01"],
        )
        kept_image = baker.make(Image, profile=user.profile, pin=None, pin_suggestion=other_suggestion)

        reject_pin_suggestion(suggestion)

        self.assertTrue(Image.objects.filter(pk=kept_image.pk).exists())


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

    def test_tracking_disabled_is_403(self) -> None:
        self.profile.track_pin_visits = False
        self.profile.save(update_fields=["track_pin_visits"])
        response = self._post({"clusters": [{"latitude": 41.0, "longitude": -76.0, "dates": ["2024-05-01"], "count": 1}]})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PinSuggestion.objects.exists())

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

    def test_response_reports_the_suggestion_id_for_each_submitted_cluster(self) -> None:
        response = self._post({"clusters": [{"id": "cluster-a", "latitude": 41.0, "longitude": -76.0, "dates": ["2024-05-01"], "count": 1}]})
        self.assertEqual(response.status_code, 200)
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(response.json()["suggestion_ids"], {"cluster-a": suggestion.pk})


class PinSuggestionImmichThumbnailViewTests(TestCase):
    """Suggestion-scoped Immich thumbnail proxy: ownership + asset-id scoping."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")
        self.suggestion = PinSuggestion.objects.create(
            profile=self.profile,
            pin=None,
            latitude=_PIN_LAT,
            longitude=_PIN_LON,
            origin=PinSuggestionOrigin.IMMICH,
            visit_dates=["2024-01-01"],
            sample_assets=[{"asset_id": "a1", "taken_at": "2024-01-01"}],
        )

    def test_returns_thumbnail_bytes_for_a_known_asset(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.pin_suggestions.ImmichGateway.get_asset_thumbnail", return_value=(b"jpeg-bytes", "image/jpeg")):
            response = self.client.get(reverse("memories.locations.immich_thumbnail", args=[self.suggestion.pk, "a1"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"jpeg-bytes")

    def test_404s_for_an_asset_id_not_in_sample_assets(self) -> None:
        response = self.client.get(reverse("memories.locations.immich_thumbnail", args=[self.suggestion.pk, "not-a-sample"]))
        self.assertEqual(response.status_code, 404)

    def test_404s_for_another_profiles_suggestion(self) -> None:
        other = baker.make(User)
        suggestion = PinSuggestion.objects.create(
            profile=other.profile,
            pin=None,
            latitude=_PIN_LAT,
            longitude=_PIN_LON,
            origin=PinSuggestionOrigin.IMMICH,
            visit_dates=["2024-01-01"],
            sample_assets=[{"asset_id": "a1", "taken_at": "2024-01-01"}],
        )
        response = self.client.get(reverse("memories.locations.immich_thumbnail", args=[suggestion.pk, "a1"]))
        self.assertEqual(response.status_code, 404)


class PhotoLocationScanPhotoUploadViewTests(TestCase):
    """Opt-in candidate-photo upload endpoint: validation and ownership scoping."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.suggestion = PinSuggestion.objects.create(
            profile=self.profile, pin=None, latitude=_PIN_LAT, longitude=_PIN_LON, origin=PinSuggestionOrigin.LOCAL_SCAN, visit_dates=["2024-01-01"],
        )

    def _upload(self, suggestion_id, *, filename: str = "a.jpg", content: bytes = b"fake-jpeg-bytes", content_type: str = "image/jpeg"):
        image_file = SimpleUploadedFile(filename, content, content_type=content_type)
        return self.client.post(reverse("tools.photo_scan.upload_photo"), {"suggestion_id": suggestion_id, "image": image_file})

    def test_valid_upload_creates_an_unattached_candidate_image(self) -> None:
        response = self._upload(self.suggestion.pk)
        self.assertEqual(response.status_code, 201)
        image = Image.objects.get(pin_suggestion=self.suggestion)
        self.assertIsNone(image.pin_id)
        self.assertEqual(image.profile_id, self.profile.pk)

    def test_404_for_another_profiles_suggestion(self) -> None:
        other = baker.make(User)
        suggestion = PinSuggestion.objects.create(
            profile=other.profile, pin=None, latitude=_PIN_LAT, longitude=_PIN_LON, origin=PinSuggestionOrigin.LOCAL_SCAN, visit_dates=["2024-01-01"],
        )
        response = self._upload(suggestion.pk)
        self.assertEqual(response.status_code, 404)

    def test_404_for_immich_origin_suggestion(self) -> None:
        suggestion = PinSuggestion.objects.create(
            profile=self.profile, pin=None, latitude=_PIN_LAT, longitude=_PIN_LON, origin=PinSuggestionOrigin.IMMICH, visit_dates=["2024-01-01"],
        )
        response = self._upload(suggestion.pk)
        self.assertEqual(response.status_code, 404)

    def test_400_once_photo_cap_is_reached(self) -> None:
        for i in range(MAX_SUGGESTION_PHOTOS):
            baker.make(Image, profile=self.profile, pin=None, pin_suggestion=self.suggestion, checksum=f"cksum-{i}")
        response = self._upload(self.suggestion.pk)
        self.assertEqual(response.status_code, 400)

    def test_non_image_content_type_is_rejected(self) -> None:
        response = self._upload(self.suggestion.pk, filename="a.txt", content=b"not an image", content_type="text/plain")
        self.assertEqual(response.status_code, 400)


class PinSuggestionBulkActionViewTests(TestCase):
    """Bulk accept/reject: owned+pending only, no photo selection."""

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

    def _post(self, action: str, ids: list[int]):
        return self.client.post(reverse("memories.locations.bulk", args=[action]), data=json.dumps({"suggestion_ids": ids}), content_type="application/json")

    def test_accepts_multiple_owned_pending_suggestions(self) -> None:
        first = self._suggestion()
        second = self._suggestion(visit_dates=["2024-02-01"])
        response = self._post("accept", [first.pk, second.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["processed"], 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, PinSuggestionStatus.ACCEPTED)
        self.assertEqual(second.status, PinSuggestionStatus.ACCEPTED)

    def test_skips_another_profiles_suggestion(self) -> None:
        other = baker.make(User)
        foreign = self._suggestion(profile=other.profile, pin=None)
        mine = self._suggestion()
        response = self._post("reject", [foreign.pk, mine.pk])
        self.assertEqual(response.json()["processed"], 1)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, PinSuggestionStatus.PENDING)
        mine.refresh_from_db()
        self.assertEqual(mine.status, PinSuggestionStatus.REJECTED)

    def test_skips_already_handled_suggestions(self) -> None:
        handled = self._suggestion(status=PinSuggestionStatus.ACCEPTED)
        response = self._post("reject", [handled.pk])
        self.assertEqual(response.json()["processed"], 0)

    def test_empty_suggestion_ids_is_400(self) -> None:
        response = self._post("accept", [])
        self.assertEqual(response.status_code, 400)

    def test_unknown_action_is_404(self) -> None:
        suggestion = self._suggestion()
        response = self.client.post(
            reverse("memories.locations.bulk", args=["explode"]), data=json.dumps({"suggestion_ids": [suggestion.pk]}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)


class PinSuggestionQueueViewSelectMapTests(TestCase):
    """The Locations page's map/selection UX is shared with Memories > Visits -
    see pin-select-map.js. Regression guard for the shared class names."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_page_with_suggestions_shows_the_shared_select_map(self) -> None:
        location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        PinSuggestion.objects.create(profile=self.profile, pin=pin, latitude=_PIN_LAT, longitude=_PIN_LON, origin=PinSuggestionOrigin.IMMICH, visit_dates=["2024-01-01"], hit_count=1)

        response = self.client.get(reverse("memories.locations"))
        self.assertContains(response, 'id="pin-suggestions-map"')
        self.assertContains(response, "pin-select-map")
        self.assertContains(response, 'class="pin-select-cb"')
        self.assertContains(response, "ul-bulk-bar-pin_suggestions")

    def test_empty_queue_has_no_map(self) -> None:
        response = self.client.get(reverse("memories.locations"))
        self.assertNotContains(response, 'id="pin-suggestions-map"')


class PinSuggestionQueuePaginationTests(TestCase):
    """The Locations queue view paginates rather than loading every suggestion at once."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        for i in range(_PAGE_SIZE + 3):
            PinSuggestion.objects.create(
                profile=self.profile,
                pin=None,
                latitude=_PIN_LAT + Decimal(i) / Decimal(1000),
                longitude=_PIN_LON,
                origin=PinSuggestionOrigin.IMMICH,
                visit_dates=[f"2024-01-{i + 1:02d}"],
            )

    def test_first_page_returns_at_most_page_size_suggestions(self) -> None:
        response = self.client.get(reverse("memories.locations"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["suggestions"]), _PAGE_SIZE)
        self.assertEqual(response.context["pin_suggestions_count"], _PAGE_SIZE + 3)

    def test_second_page_returns_the_remainder(self) -> None:
        response = self.client.get(reverse("memories.locations"), {"page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["suggestions"]), 3)

    def test_out_of_range_page_clamps_instead_of_500ing(self) -> None:
        response = self.client.get(reverse("memories.locations"), {"page": 999})
        self.assertEqual(response.status_code, 200)
