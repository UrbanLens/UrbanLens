"""Shared placeless places: provisional and anchored tiers, empty wikis, consent to bring notes (ADR35 section 2, UL-367)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceEvidence, PlaceKind
from urbanlens.dashboard.models.place.shared import NotesOffer, NotesOfferStatus, SharedPlaceMember
from urbanlens.dashboard.models.visits.model import VisitSource
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import shared_places
from urbanlens.dashboard.services.wiki import wiki_access

from .place_helpers import make_place

LAT, LON = 42.65, -73.75
M_LAT = 1 / 111_320
M_LON = M_LAT / 0.7361
MODULE = "urbanlens.dashboard.services.places.shared_places"


def _box(half_m: float) -> MultiPolygon:
    dx, dy = half_m * M_LON, half_m * M_LAT
    return MultiPolygon(
        Polygon(
            (
                (LON - dx, LAT - dy),
                (LON + dx, LAT - dy),
                (LON + dx, LAT + dy),
                (LON - dx, LAT + dy),
                (LON - dx, LAT - dy),
            ),
            srid=4326,
        ),
        srid=4326,
    )


def _profile():
    return baker.make("auth.User").profile


def _pin(profile, north_m: float = 0.0, *, age_days: int = 30, east_m: float = 0.0) -> Pin:
    location = baker.make(Location, latitude=round(LAT + north_m * M_LAT, 6), longitude=round(LON + east_m * M_LON, 6))
    pin = baker.make(Pin, profile=profile, location=location)
    Pin.objects.filter(pk=pin.pk).update(created=timezone.now() - timedelta(days=age_days))
    return Pin.objects.get(pk=pin.pk)


class SharedPlaceTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.real = {}
        for name, value in (
            ("is_real_user", lambda profile: True),
            ("are_independent", lambda a, b, exclude_cell=None: True),
        ):
            patcher = patch(f"{MODULE}.{name}", side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alice, self.bob, self.carol = _profile(), _profile(), _profile()

    def pair(self, bob_north: float = 10.0):
        first = _pin(self.alice)
        second = _pin(self.bob, bob_north)
        shared_places.evaluate_pin(second.pk)
        return first, second


class TierOneTests(SharedPlaceTestCase):
    def test_two_qualifying_profiles_form_a_provisional_place_at_the_earlier_pins_point(self) -> None:
        first, _second = self.pair()
        place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.assertAlmostEqual(place.anchor.y, first.location.point.y, places=6)
        self.assertEqual(set(place.shared_members.values_list("profile_id", flat=True)), {self.alice.pk, self.bob.pk})

    def test_a_pin_under_seven_days_old_does_not_count(self) -> None:
        _pin(self.alice)
        fresh = _pin(self.bob, 10, age_days=1)
        shared_places.evaluate_pin(fresh.pk)
        self.assertFalse(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).exists())

    def test_a_fresh_pin_with_a_photo_verified_visit_counts(self) -> None:
        _pin(self.alice)
        fresh = _pin(self.bob, 10, age_days=1)
        baker.make(
            "dashboard.PinVisit", pin=fresh, source=VisitSource.PHOTO, tentative=False, visited_at=timezone.now()
        )
        shared_places.evaluate_pin(fresh.pk)
        self.assertTrue(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).exists())

    def test_a_profile_failing_the_real_user_gate_forms_nothing(self) -> None:
        _pin(self.alice)
        other = _pin(self.bob, 10)
        with patch(f"{MODULE}.is_real_user", side_effect=lambda profile: profile.pk != self.alice.pk):
            shared_places.evaluate_pin(other.pk)
        self.assertFalse(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).exists())

    def test_a_pair_failing_independence_forms_nothing(self) -> None:
        _pin(self.alice)
        other = _pin(self.bob, 10)
        with patch(f"{MODULE}.are_independent", return_value=False):
            shared_places.evaluate_pin(other.pk)
        self.assertFalse(SharedPlaceMember.objects.exists())

    def test_pins_further_apart_than_the_radius_do_not_pair(self) -> None:
        _pin(self.alice)
        far = _pin(self.bob, 40)
        shared_places.evaluate_pin(far.pk)
        self.assertFalse(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).exists())

    def test_a_later_pin_joins_by_distance_to_the_anchor_alone(self) -> None:
        self.pair()
        joiner = _pin(self.carol, -24)  # 24 m from the anchor, 34 m from the second pin
        with patch(f"{MODULE}.are_independent", return_value=False):
            shared_places.evaluate_pin(joiner.pk)
        self.assertTrue(SharedPlaceMember.objects.filter(profile=self.carol).exists())
        self.assertEqual(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).count(), 1)

    def test_a_pin_beyond_the_anchor_radius_does_not_join(self) -> None:
        self.pair()
        outsider = _pin(self.carol, -26)
        shared_places.evaluate_pin(outsider.pk)
        self.assertFalse(SharedPlaceMember.objects.filter(profile=self.carol).exists())

    @override_settings(UL_SHARED_PLACE_JOIN_LIMIT=1)
    def test_the_per_account_join_rate_limit_throttles_a_grid_of_probe_pins(self) -> None:
        self.pair()
        probes = [_pin(self.alice, 100 * (i + 1)) for i in range(3)]
        for index, probe in enumerate(probes):
            partner = _pin(self.carol, 100 * (index + 1) + 5)
            shared_places.evaluate_pin(partner.pk)
        self.assertLessEqual(
            SharedPlaceMember.objects.filter(
                profile=self.alice, created__gte=timezone.now() - timedelta(hours=1)
            ).count(),
            1,
        )

    def test_synthetic_places_never_resolve_a_coordinate(self) -> None:
        self.pair()
        self.assertIsNone(Place.objects.resolve_for_point(LAT, LON))
        for pin in Pin.objects.all():
            pin.location.refresh_from_db()
            self.assertIsNone(pin.location.place_id)


class TierTwoTests(SharedPlaceTestCase):
    def test_a_public_reference_within_thirty_metres_anchors_the_place_there(self) -> None:
        baker.make(GooglePlace, latitude=round(LAT + 28 * M_LAT, 6), longitude=LON, cid=None)
        _pin(self.alice, 27)
        other = _pin(self.bob, 29)
        shared_places.evaluate_pin(other.pk)
        place = Place.objects.get(evidence=PlaceEvidence.ANCHORED)
        self.assertAlmostEqual(place.anchor.y, LAT + 28 * M_LAT, places=5)

    def test_an_anchored_pair_can_be_wider_than_the_provisional_radius(self) -> None:
        baker.make(GooglePlace, latitude=LAT, longitude=LON, cid=None)
        _pin(self.alice, 28)
        other = _pin(self.bob, -28)
        shared_places.evaluate_pin(other.pk)
        self.assertTrue(Place.objects.filter(evidence=PlaceEvidence.ANCHORED).exists())
        self.assertFalse(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).exists())


class EmptyWikiAndConsentTests(SharedPlaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = _pin(self.alice)
        Wiki.objects.get_or_create_for_location(self.first.location)
        Wiki.objects.filter(location=self.first.location).update(description="Alice's notes about the gate.")
        self.second = _pin(self.bob, 10)
        shared_places.evaluate_pin(self.second.pk)
        self.place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.shared_wiki = Wiki.objects.get(place=self.place)

    def test_the_shared_wiki_starts_empty(self) -> None:
        self.assertFalse((self.shared_wiki.description or "").strip())
        self.assertFalse(self.shared_wiki.articles.exists() if hasattr(self.shared_wiki, "articles") else False)

    def test_an_offer_exists_only_for_a_member_with_tier_zero_content(self) -> None:
        self.assertEqual(NotesOffer.objects.get().profile, self.alice)

    def test_nothing_flows_until_the_owner_accepts(self) -> None:
        self.shared_wiki.refresh_from_db()
        self.assertFalse((self.shared_wiki.description or "").strip())
        self.assertEqual(NotesOffer.objects.get().status, NotesOfferStatus.PENDING)

    def test_declining_copies_nothing(self) -> None:
        offer = NotesOffer.objects.get()
        self.client.force_login(self.alice.user)
        response = self.client.post(reverse("shared_place.offer", kwargs={"offer_id": offer.pk, "action": "decline"}))
        self.assertEqual(response.status_code, 204)
        self.shared_wiki.refresh_from_db()
        self.assertFalse((self.shared_wiki.description or "").strip())
        offer.refresh_from_db()
        self.assertEqual(offer.status, NotesOfferStatus.DECLINED)

    def test_accepting_brings_the_owners_notes_across(self) -> None:
        offer = NotesOffer.objects.get()
        self.client.force_login(self.alice.user)
        response = self.client.post(reverse("shared_place.offer", kwargs={"offer_id": offer.pk, "action": "accept"}))
        self.assertEqual(response.status_code, 204)
        self.shared_wiki.refresh_from_db()
        self.assertEqual(self.shared_wiki.description, "Alice's notes about the gate.")

    def test_someone_elses_offer_is_indistinguishable_from_none(self) -> None:
        offer = NotesOffer.objects.get()
        self.client.force_login(self.bob.user)
        real = self.client.post(reverse("shared_place.offer", kwargs={"offer_id": offer.pk, "action": "accept"}))
        missing = self.client.post(
            reverse("shared_place.offer", kwargs={"offer_id": offer.pk + 999, "action": "accept"})
        )
        self.assertEqual(real.status_code, missing.status_code)
        self.assertEqual(real.status_code, 404)
        self.shared_wiki.refresh_from_db()
        self.assertFalse((self.shared_wiki.description or "").strip())

    def test_both_members_get_the_same_symmetric_notice(self) -> None:
        notices = NotificationLog.objects.filter(title="Someone else pinned this too")
        self.assertEqual({n.profile_id for n in notices}, {self.alice.pk, self.bob.pk})
        self.assertEqual(len({(n.title, n.message) for n in notices}), 1)
        for notice in notices:
            self.assertNotIn(self.alice.user.username, notice.message)
            self.assertNotIn(self.bob.user.username, notice.message)
            self.assertIsNone(notice.source_profile_id)


class AccessTests(SharedPlaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = self.pair()
        self.place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.hub = Wiki.objects.get(place=self.place).location

    def test_members_reach_the_shared_wiki_and_strangers_do_not(self) -> None:
        self.assertTrue(wiki_access.location_visible_to(self.hub, self.alice))
        self.assertTrue(wiki_access.location_visible_to(self.hub, self.bob))
        self.assertFalse(wiki_access.location_visible_to(self.hub, self.carol))
        self.assertIn(self.hub.pk, set(wiki_access.visible_wiki_locations(self.alice)))
        self.assertNotIn(self.hub.pk, set(wiki_access.visible_wiki_locations(self.carol)))
        self.assertTrue(
            Location.objects.filter(wiki_access.visible_locations_filter(self.alice), pk=self.hub.pk).exists()
        )
        self.assertFalse(
            Location.objects.filter(wiki_access.visible_locations_filter(self.carol), pk=self.hub.pk).exists()
        )

    def test_membership_does_not_open_the_other_members_tier_zero_wiki(self) -> None:
        self.assertFalse(wiki_access.location_visible_to(self.second.location, self.alice))
        self.assertFalse(wiki_access.location_visible_to(self.first.location, self.bob))

    def test_deleting_the_members_pin_revokes_access(self) -> None:
        self.second.delete()
        shared_places.revalidate_memberships(self.bob)
        self.assertFalse(wiki_access.location_visible_to(self.hub, self.bob))
        self.assertTrue(wiki_access.location_visible_to(self.hub, self.alice))


class ParcelNestingTests(SharedPlaceTestCase):
    def test_inside_a_plausible_parcel_with_no_building_the_place_is_a_part_of_child(self) -> None:
        parcel = make_place(PlaceKind.PARCEL, _box(120))
        self.pair()
        place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.assertEqual(place.parent_id, parcel.pk)
        self.assertEqual(place.domain_root_id, parcel.domain_root_id)

    def test_above_the_plausibility_cap_the_place_is_top_level(self) -> None:
        make_place(PlaceKind.PARCEL, _box(30_000))
        self.pair()
        place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.assertIsNone(place.parent_id)
        self.assertEqual(place.domain_root_id, place.pk)

    def test_a_building_under_the_pin_means_it_is_not_placeless(self) -> None:
        building = make_place(PlaceKind.BUILDING, _box(60))
        Place.objects.filter(pk=building.pk).update(evidence=PlaceEvidence.FOOTPRINT)
        self.pair()
        self.assertFalse(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).exists())


class CeleryOnlyTests(SharedPlaceTestCase):
    def test_pin_creation_only_queues_work_and_decides_nothing(self) -> None:
        _pin(self.alice)
        with (
            patch("urbanlens.dashboard.tasks.evaluate_shared_places_for_pin.apply_async") as queued,
            self.captureOnCommitCallbacks(execute=True),
        ):
            baker.make(
                Pin, profile=self.bob, location=baker.make(Location, latitude=round(LAT + 10 * M_LAT, 6), longitude=LON)
            )
        queued.assert_called_once()
        self.assertFalse(SharedPlaceMember.objects.exists())

    def test_saves_inside_the_debounce_window_collapse_to_one_evaluation(self) -> None:
        from django.core.cache import cache

        from urbanlens.dashboard.services.places.shared_place_triggers import schedule_pin_evaluation

        cache.delete("ul:shared-place:eval:424242")
        with patch("urbanlens.dashboard.tasks.evaluate_shared_places_for_pin.apply_async") as queued:
            schedule_pin_evaluation(424242)
            schedule_pin_evaluation(424242)
        queued.assert_called_once()

    def test_the_task_does_the_work(self) -> None:
        from urbanlens.dashboard import tasks

        _pin(self.alice)
        second = _pin(self.bob, 10)
        self.assertTrue(tasks.evaluate_shared_places_for_pin(second.pk))
        self.assertTrue(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).exists())
