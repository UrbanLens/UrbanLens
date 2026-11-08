"""Shared placeless places: provisional and anchored tiers, empty wikis, consent to bring notes (ADR35 section 2, UL-367)."""

from __future__ import annotations

from datetime import timedelta
import re
from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.contrib.gis.measure import D
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
from urbanlens.dashboard.models.place.shared import (
    NotesOffer,
    NotesOfferStatus,
    SharedPlaceAttempt,
    SharedPlaceFlag,
    SharedPlaceMember,
)
from urbanlens.dashboard.models.visits.model import VisitSource
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.core.text_limits import MAX_WIKI_DESCRIPTION_LENGTH
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

    def pair(self, bob_north: float = 10.0, bob_east: float = 0.0):
        first = _pin(self.alice)
        second = _pin(self.bob, bob_north, east_m=bob_east)
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
        shared_places.evaluate_pin(joiner.pk)
        self.assertTrue(SharedPlaceMember.objects.filter(profile=self.carol).exists())
        self.assertEqual(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).count(), 1)

    def test_a_pin_beyond_the_anchor_radius_does_not_join(self) -> None:
        self.pair()
        outsider = _pin(self.carol, -26)
        shared_places.evaluate_pin(outsider.pk)
        self.assertFalse(SharedPlaceMember.objects.filter(profile=self.carol).exists())

    @override_settings(UL_SHARED_PLACE_ATTEMPT_LIMIT=2)
    def test_attempts_are_counted_per_rolling_window_and_over_the_limit_are_deferred_not_dropped(self) -> None:
        self.pair()
        prober = _profile()
        probes = [_pin(prober, 100 * (i + 1)) for i in range(3)]
        for index, probe in enumerate(probes):
            _pin(self.carol, 100 * (index + 1) + 5)
            shared_places.evaluate_pin(probe.pk)
        self.assertEqual(SharedPlaceAttempt.objects.filter(profile=prober).count(), 2)
        flag = SharedPlaceFlag.objects.get(profile=prober)
        self.assertEqual(flag.deferred_count, 1)
        deferred = probes[2]
        # The first two probes paired with Carol's pins; the deferred one learned nothing.
        self.assertEqual(SharedPlaceMember.objects.filter(profile=prober).count(), 2)
        self.assertFalse(Place.objects.filter(anchor__dwithin=(deferred.location.point, D(m=30))).exists())
        # The window rolls: the deferred pin is picked up by a later evaluation, not lost.
        SharedPlaceAttempt.objects.filter(profile=prober).update(created=timezone.now() - timedelta(hours=25))
        shared_places.evaluate_pin(deferred.pk)
        self.assertTrue(
            SharedPlaceAttempt.objects.filter(pin=deferred, created__gte=timezone.now() - timedelta(hours=1)).exists()
        )

    @override_settings(UL_SHARED_PLACE_ATTEMPT_LIMIT=1)
    def test_a_partner_at_their_own_limit_defers_the_pair_instead_of_answering_the_prober(self) -> None:
        first = _pin(self.alice)
        SharedPlaceAttempt.objects.create(profile=self.alice, pin=first)
        second = _pin(self.bob, 10)
        self.assertIsNone(shared_places.evaluate_pin(second.pk))
        self.assertFalse(Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).exists())
        SharedPlaceAttempt.objects.filter(profile=self.alice).update(created=timezone.now() - timedelta(hours=25))
        self.assertIsNotNone(shared_places.evaluate_pin(second.pk))

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


class AdmissionToAnExistingPlaceTests(SharedPlaceTestCase):
    """Admission is decided by the admitted pin's own distance from the place's one anchor, whichever route reached it."""

    def setUp(self) -> None:
        super().setUp()
        self.pair()
        self.place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.dave, self.erin = _profile(), _profile()

    def is_member(self, profile, place=None) -> bool:
        return SharedPlaceMember.objects.filter(place=place or self.place, profile=profile).exists()

    def test_an_older_neighbour_inside_a_place_does_not_carry_in_a_pin_outside_it(self) -> None:
        _pin(self.carol, 20, age_days=40)
        outside = _pin(self.dave, 36)  # 36 m from the anchor, 16 m from Carol's older pin
        self.assertIsNone(shared_places.evaluate_pin(outside.pk))
        self.assertFalse(self.is_member(self.dave))
        self.assertTrue(self.is_member(self.carol), "Carol stands inside the place on her own account")

    def test_a_public_reference_inside_a_place_does_not_carry_in_a_pin_outside_it(self) -> None:
        baker.make(GooglePlace, latitude=round(LAT + 20 * M_LAT, 6), longitude=LON, cid=None)
        outside = _pin(self.dave, 45)  # 25 m from the reference, 45 m from the anchor
        self.assertIsNone(shared_places.evaluate_pin(outside.pk))
        self.assertFalse(self.is_member(self.dave))

    def test_a_pin_refused_by_one_candidates_place_still_tries_the_next_candidate(self) -> None:
        _pin(self.carol, 20, age_days=50)
        erins = _pin(self.erin, 50, age_days=40)
        outside = _pin(self.dave, 36)
        formed = shared_places.evaluate_pin(outside.pk)
        self.assertIsNotNone(formed)
        self.assertNotEqual(formed.pk, self.place.pk)
        self.assertAlmostEqual(formed.anchor.y, erins.location.point.y, places=6)
        self.assertTrue(self.is_member(self.dave, formed))
        self.assertFalse(self.is_member(self.dave))

    def test_a_pin_refused_independence_is_not_reported_as_having_joined(self) -> None:
        carols = _pin(self.carol, 20)
        daves = _pin(self.dave, 15)
        with patch(f"{MODULE}.are_independent", side_effect=lambda a, b, exclude_cell=None: a != self.dave):
            placed = shared_places._create_place(self.place.anchor, PlaceEvidence.PROVISIONAL, [daves, carols])
        self.assertIsNone(placed)
        self.assertFalse(self.is_member(self.dave))
        self.assertTrue(self.is_member(self.carol))

    def test_a_members_pin_outside_the_place_does_not_count_as_joining_it(self) -> None:
        far = _pin(self.bob, 36)
        self.assertIsNone(shared_places._join(self.place, far))


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

    def accept(self, offer) -> int:
        self.client.force_login(self.alice.user)
        url = reverse("shared_place.offer", kwargs={"offer_id": offer.pk, "action": "accept"})
        return self.client.post(url).status_code

    def assertNothingBrought(self, offer) -> None:
        self.assertFalse(NotesOffer.objects.filter(pk=offer.pk, status=NotesOfferStatus.ACCEPTED).exists())
        self.shared_wiki.refresh_from_db()
        self.assertNotIn("gate", self.shared_wiki.description or "")

    def test_accepting_after_losing_membership_brings_nothing_and_says_so(self) -> None:
        offer = NotesOffer.objects.get()
        SharedPlaceMember.objects.filter(place=self.place, profile=self.alice).delete()
        self.assertEqual(self.accept(offer), 404)
        self.assertNothingBrought(offer)

    def test_accepting_when_the_notes_are_gone_is_not_recorded_as_accepted(self) -> None:
        offer = NotesOffer.objects.get()
        Wiki.objects.filter(location=self.first.location).update(description="  ")
        self.assertEqual(self.accept(offer), 404)
        self.assertNothingBrought(offer)

    def test_accepting_when_the_shared_wiki_is_full_is_not_recorded_as_accepted(self) -> None:
        offer = NotesOffer.objects.get()
        Wiki.objects.filter(pk=self.shared_wiki.pk).update(description="x" * MAX_WIKI_DESCRIPTION_LENGTH)
        self.assertEqual(self.accept(offer), 404)
        self.assertNothingBrought(offer)

    def test_accepting_cannot_publish_what_another_editor_wrote(self) -> None:
        offer = NotesOffer.objects.get()
        source = Wiki.objects.get(location=self.first.location)
        baker.make(WikiEdit, wiki=source, editor=self.carol, changes={"description": {"from": "", "to": "Carol's"}})
        self.assertEqual(self.accept(offer), 404)
        self.assertNothingBrought(offer)

    def test_a_correction_to_some_other_field_does_not_block_the_notes(self) -> None:
        offer = NotesOffer.objects.get()
        source = Wiki.objects.get(location=self.first.location)
        baker.make(WikiEdit, wiki=source, editor=self.carol, changes={"name": {"from": "A", "to": "B"}})
        self.assertEqual(self.accept(offer), 204)
        offer.refresh_from_db()
        self.assertEqual(offer.status, NotesOfferStatus.ACCEPTED)

    def test_notes_someone_else_edited_are_never_offered(self) -> None:
        carols = _pin(self.carol, 12)
        Wiki.objects.get_or_create_for_location(carols.location)
        source = Wiki.objects.get(location=carols.location)
        Wiki.objects.filter(pk=source.pk).update(description="Mostly Alice's text.")
        baker.make(WikiEdit, wiki=source, editor=self.alice, changes={"description": {"from": "", "to": "x"}})
        shared_places.evaluate_pin(carols.pk)
        self.assertTrue(SharedPlaceMember.objects.filter(place=self.place, profile=self.carol).exists())
        self.assertFalse(NotesOffer.objects.filter(profile=self.carol).exists())

    def test_losing_membership_withdraws_a_pending_offer_and_rejoining_offers_again(self) -> None:
        self.first.delete()
        shared_places.revalidate_memberships(self.alice)
        self.assertFalse(NotesOffer.objects.filter(profile=self.alice).exists())
        again = _pin(self.alice, 3)
        Wiki.objects.get_or_create_for_location(again.location)
        Wiki.objects.filter(location=again.location).update(description="Alice's notes, again.")
        shared_places.evaluate_pin(again.pk)
        self.assertEqual(NotesOffer.objects.get(profile=self.alice).status, NotesOfferStatus.PENDING)

    def test_losing_membership_keeps_an_answer_already_given(self) -> None:
        offer = NotesOffer.objects.get()
        self.assertTrue(shared_places.answer_notes_offer(offer, self.alice, accept=False))
        self.first.delete()
        shared_places.revalidate_memberships(self.alice)
        offer.refresh_from_db()
        self.assertEqual(offer.status, NotesOfferStatus.DECLINED)


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
    def test_a_fresh_pin_queues_nothing_because_it_cannot_have_matured(self) -> None:
        _pin(self.alice)
        with (
            patch("urbanlens.dashboard.tasks.evaluate_shared_places_for_pin.apply_async") as queued,
            self.captureOnCommitCallbacks(execute=True),
        ):
            baker.make(
                Pin, profile=self.bob, location=baker.make(Location, latitude=round(LAT + 10 * M_LAT, 6), longitude=LON)
            )
        queued.assert_not_called()
        self.assertFalse(SharedPlaceMember.objects.exists())

    def test_a_mature_pin_only_queues_work_and_decides_nothing(self) -> None:
        from django.core.cache import cache

        from urbanlens.dashboard.services.places.shared_place_triggers import schedule_pin_evaluation

        _pin(self.alice)
        mature = _pin(self.bob, 10)
        cache.delete(f"ul:shared-place:eval:{mature.pk}")
        with patch("urbanlens.dashboard.tasks.evaluate_shared_places_for_pin.apply_async") as queued:
            self.assertTrue(schedule_pin_evaluation(mature.pk, created=mature.created))
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


class HubPrivacyTests(SharedPlaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Bob's own pin shares neither coordinate with Alice's, so finding his on the page says nothing about hers.
        self.first, self.second = self.pair(bob_north=6, bob_east=8)
        self.place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.wiki = Wiki.objects.get(place=self.place)
        self.hub = self.wiki.location

    def test_the_hub_is_a_fresh_location_at_a_stored_offset_inside_the_radius_and_not_at_the_anchor(self) -> None:
        self.assertNotIn(self.hub.pk, set(Pin.objects.values_list("location_id", flat=True)))
        gap = shared_places._distance(self.hub.point, self.place.anchor)
        self.assertGreater(gap, 0.5)
        self.assertLessEqual(gap, 25.5)
        self.hub.refresh_from_db()
        self.assertEqual(self.hub.place_id, self.place.pk)

    def test_the_hub_never_reuses_an_existing_location(self) -> None:
        taken = _pin(self.carol, 500).location
        with patch.object(
            shared_places,
            "_random_offset_point",
            side_effect=[(float(taken.latitude), float(taken.longitude)), (round(LAT + 7 * M_LAT, 6), LON)],
        ):
            hub = shared_places._hub_location(self.place.anchor, 25)
        self.assertNotEqual(hub.pk, taken.pk)

    def _coordinate_strings(self, location) -> set[str]:
        return {
            f"{float(location.latitude):.6f}",
            f"{float(location.longitude):.6f}",
            str(location.latitude),
            str(location.longitude),
        }

    def test_the_wiki_page_shows_neither_the_anchor_nor_the_hub(self) -> None:
        self.client.force_login(self.bob.user)
        response = self.client.get(reverse("location.wiki", kwargs={"location_slug": self.hub.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        for text in self._coordinate_strings(self.hub) | self._coordinate_strings(self.first.location):
            self.assertNotIn(text, content)
        # Centred on the viewer's own pin instead.
        self.assertIn(f"{float(self.second.location.latitude):.6f}"[:8], content)

    def test_wiki_json_endpoints_show_neither_the_anchor_nor_the_hub(self) -> None:
        self.client.force_login(self.bob.user)
        forbidden = self._coordinate_strings(self.hub) | self._coordinate_strings(self.first.location)
        for name in (
            "location.wiki.boundary",
            "location.wiki.detail_pins.json",
            "location.wiki.markup.json",
            "location.wiki.gallery.json",
        ):
            response = self.client.get(reverse(name, kwargs={"location_slug": self.hub.slug}))
            body = response.content.decode()
            for text in forbidden:
                self.assertNotIn(text, body, f"{name} leaked {text}")


class InParcelDomainTests(SharedPlaceTestCase):
    """Inside a plausible parcel the shared place joins the parcel's domain, as a building does (R-52)."""

    def setUp(self) -> None:
        super().setUp()
        self.parcel = make_place(PlaceKind.PARCEL, _box(150))
        self.parcel_wiki = baker.make(
            Wiki,
            location=baker.make(Location, latitude=round(LAT + 100 * M_LAT, 6), longitude=round(LON + 40 * M_LON, 6)),
            place=self.parcel,
        )
        self.pair()
        self.place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.wiki = Wiki.objects.get(place=self.place)
        self.hub = self.wiki.location
        # Carol holds a pin elsewhere on the parcel, so she holds its access domain.
        self.holder_pin = _pin(self.carol, 120)
        Location.objects.filter(pk=self.holder_pin.location_id).update(place=self.parcel)
        self.dave = _profile()

    def test_the_hub_keeps_its_place_through_save_and_re_resolution(self) -> None:
        from urbanlens.dashboard.services.places import resolution

        self.hub.refresh_from_db()
        self.hub.save()
        resolution.resolve_location_place(self.hub)
        resolution.attach_location(self.hub, self.parcel)
        resolution.resolve_locations_in(self.parcel.geometry)
        self.hub.refresh_from_db()
        self.assertEqual(self.hub.place_id, self.place.pk)

    def test_a_parcel_holder_reaches_the_shared_wiki_through_every_predicate(self) -> None:
        self.assertTrue(wiki_access.location_visible_to(self.hub, self.carol))
        self.assertTrue(wiki_access.wiki_accessible_to(self.wiki, self.carol))
        self.assertTrue(wiki_access.place_visible_to(self.place, self.carol))
        self.assertIn(self.hub.pk, set(wiki_access.visible_wiki_locations(self.carol)))
        self.assertTrue(
            Location.objects.filter(wiki_access.visible_locations_filter(self.carol), pk=self.hub.pk).exists()
        )

    def test_someone_with_no_pin_on_the_parcel_is_denied_by_every_predicate(self) -> None:
        self.assertFalse(wiki_access.location_visible_to(self.hub, self.dave))
        self.assertFalse(wiki_access.wiki_accessible_to(self.wiki, self.dave))
        self.assertFalse(wiki_access.place_visible_to(self.place, self.dave))
        self.assertNotIn(self.hub.pk, set(wiki_access.visible_wiki_locations(self.dave)))
        self.assertFalse(
            Location.objects.filter(wiki_access.visible_locations_filter(self.dave), pk=self.hub.pk).exists()
        )

    def test_a_parcel_holders_view_shows_neither_the_anchor_nor_the_hub(self) -> None:
        self.client.force_login(self.carol.user)
        anchor = Pin.objects.get(profile=self.alice).location
        forbidden = {
            f"{float(value):.6f}"
            for value in (self.hub.latitude, self.hub.longitude, anchor.latitude, anchor.longitude)
        }
        for name in ("location.wiki", "location.wiki.boundary"):
            body = self.client.get(reverse(name, kwargs={"location_slug": self.hub.slug})).content.decode()
            for text in forbidden:
                self.assertNotIn(text, body, f"{name} leaked {text}")

    def test_the_parcels_child_wiki_payloads_carry_neither_the_shared_wiki_nor_its_hub(self) -> None:
        self.client.force_login(self.carol.user)
        forbidden = [float(value) for value in (self.hub.latitude, self.hub.longitude)]
        parcel_slug = self.parcel_wiki.location.slug or str(self.parcel_wiki.location.uuid)
        for name, query in (
            ("location.wiki.detail_pins.json", ""),
            ("location.wiki.detail_pins.json", "?children=1"),
            ("location.wiki.detail_pins.panel", ""),
            ("location.wiki.boundary", "?children=1"),
        ):
            response = self.client.get(reverse(name, kwargs={"location_slug": parcel_slug}) + query)
            self.assertEqual(response.status_code, 200, name)
            body = response.content.decode()
            self.assertNotIn(str(self.wiki.uuid), body, f"{name}{query} lists the shared wiki")
            # Compare parsed numbers, not substrings: "42.65" is a prefix of an unrelated "42.650898".
            numbers = [float(match) for match in re.findall(r"-?\d+\.\d+", body)]
            for value in forbidden:
                self.assertFalse(any(abs(number - value) < 1e-6 for number in numbers), f"{name}{query} leaked {value}")

    def nest_under_the_shared_wiki(self) -> Wiki:
        location = baker.make(Location, latitude=round(LAT + 2 * M_LAT, 6), longitude=round(LON + 2 * M_LON, 6))
        return baker.make(Wiki, location=location, parent_wiki=self.wiki, name="Inside the shared place")

    def test_a_shared_wiki_and_everything_under_it_leave_a_parents_descendants(self) -> None:
        nested = self.nest_under_the_shared_wiki()
        remaining = set(
            wiki_access.without_shared_placeless(self.parcel_wiki.descendants()).values_list("pk", flat=True)
        )
        self.assertNotIn(self.wiki.pk, remaining)
        self.assertNotIn(nested.pk, remaining)

    def test_an_ordinary_child_stays_among_a_parents_descendants(self) -> None:
        location = baker.make(Location, latitude=round(LAT + 90 * M_LAT, 6), longitude=LON)
        child = baker.make(Wiki, location=location, parent_wiki=self.parcel_wiki, name="Gatehouse")
        remaining = set(
            wiki_access.without_shared_placeless(self.parcel_wiki.descendants()).values_list("pk", flat=True)
        )
        self.assertIn(child.pk, remaining)

    def test_pulling_the_parcels_children_makes_no_pin_on_the_hub(self) -> None:
        from urbanlens.dashboard.services.pins.pin_wiki_sync import pull_children_from_wiki

        nested = self.nest_under_the_shared_wiki()
        parent = baker.make(Pin, profile=self.carol, location=self.parcel_wiki.location)
        self.assertEqual(pull_children_from_wiki(parent), 0)
        self.assertFalse(Pin.objects.filter(location__in=[self.hub, nested.location]).exists())

    def test_sending_child_pins_to_the_parcel_is_not_answered_by_the_shared_wiki(self) -> None:
        from urbanlens.dashboard.services.pins.pin_wiki_sync import send_pins_to_wiki

        parent = baker.make(Pin, profile=self.carol, location=self.parcel_wiki.location)
        beside_hub = baker.make(
            Location, latitude=round(float(self.hub.latitude) + 2 * M_LAT, 6), longitude=float(self.hub.longitude)
        )
        child = baker.make(Pin, profile=self.carol, location=beside_hub, parent_pin=parent, name="Beside the hub")
        self.assertEqual(send_pins_to_wiki(parent, [child], self.carol), 1)

    def test_a_shared_wiki_alone_does_not_give_the_parcel_a_child_wiki_affordance(self) -> None:
        self.client.force_login(self.carol.user)
        parcel_slug = self.parcel_wiki.location.slug or str(self.parcel_wiki.location.uuid)
        response = self.client.get(reverse("location.wiki", kwargs={"location_slug": parcel_slug}))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_child_wikis"])

    def test_a_parcel_holder_cannot_draw_on_the_shared_wiki(self) -> None:
        self.client.force_login(self.carol.user)
        response = self.client.post(
            reverse("location.wiki.boundary", kwargs={"location_slug": self.hub.slug}),
            data={"boundary_type": "property", "polygon": None},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_the_shared_wiki_nests_under_the_parcels(self) -> None:
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.parent_wiki_id, self.parcel_wiki.pk)

    def test_a_member_whose_pin_lies_off_the_parcel_still_reaches_the_nested_wiki(self) -> None:
        # Bob's own pin resolved nowhere (no domain); membership alone carries him.
        Location.objects.filter(pk__in=Pin.objects.filter(profile=self.bob).values("location_id")).update(place=None)
        self.assertTrue(wiki_access.wiki_accessible_to(self.wiki, self.bob))
        self.assertTrue(wiki_access.place_visible_to(self.place, self.bob))


class TopLevelIsolationTests(SharedPlaceTestCase):
    """With no plausible parcel the shared place is its own domain, which only membership reaches."""

    def setUp(self) -> None:
        super().setUp()
        self.pair()
        self.place = Place.objects.get(evidence=PlaceEvidence.PROVISIONAL)
        self.wiki = Wiki.objects.get(place=self.place)
        self.hub = self.wiki.location

    def test_a_grant_on_the_places_own_domain_does_not_open_it_to_a_non_member(self) -> None:
        with patch.object(wiki_access, "accessible_domain_ids", return_value={self.place.pk}):
            self.assertFalse(wiki_access.place_visible_to(self.place, self.carol))
            self.assertFalse(
                Location.objects.filter(wiki_access.visible_locations_filter(self.carol), pk=self.hub.pk).exists()
            )
        self.assertFalse(wiki_access.location_visible_to(self.hub, self.carol))
        self.assertTrue(wiki_access.place_visible_to(self.place, self.alice))


def _signals(profile, *, age: int = 400, cells=("aaaaaa", "bbbbbb", "cccccc", "dddddd", "eeeeee"), weeks: int = 6):
    from urbanlens.dashboard.models.trust_signals import ProfileTrustSignals

    return ProfileTrustSignals.objects.update_or_create(
        profile=profile,
        defaults={
            "computed_at": timezone.now(),
            "account_age_days": age,
            "active_weeks_90": weeks,
            "pin_count": 10,
            "pin_cells": list(cells),
            "spread_cells": 3,
            "photo_visit_days": 2,
        },
    )[0]


class RealTrustCheckTests(TestCase):
    """The join and formation gates with the REAL ``is_real_user`` / ``are_independent``, on ``ProfileTrustSignals`` rows."""

    def setUp(self) -> None:
        super().setUp()
        self.alice, self.bob, self.carol = _profile(), _profile(), _profile()
        _signals(self.alice, cells=("a1aaaa", "a2aaaa", "a3aaaa", "a4aaaa", "a5aaaa"))
        _signals(self.bob, cells=("b1bbbb", "b2bbbb", "b3bbbb", "b4bbbb", "b5bbbb"))
        _pin(self.alice)
        self.bob_pin = _pin(self.bob, 10)

    def formed(self) -> Place | None:
        return Place.objects.filter(evidence=PlaceEvidence.PROVISIONAL).first()

    def test_two_real_independent_profiles_form_a_place(self) -> None:
        shared_places.evaluate_pin(self.bob_pin.pk)
        self.assertIsNotNone(self.formed())

    def test_a_profile_that_is_not_a_real_user_forms_nothing(self) -> None:
        _signals(self.bob, age=2, weeks=0, cells=("b1bbbb", "b2bbbb", "b3bbbb", "b4bbbb", "b5bbbb"))
        shared_places.evaluate_pin(self.bob_pin.pk)
        self.assertIsNone(self.formed())

    def test_profiles_with_identical_pin_cells_are_not_independent(self) -> None:
        _signals(self.bob, cells=("a1aaaa", "a2aaaa", "a3aaaa", "a4aaaa", "a5aaaa"))
        shared_places.evaluate_pin(self.bob_pin.pk)
        self.assertIsNone(self.formed())

    def test_a_joiner_independent_of_the_founder_but_not_of_another_member_does_not_join(self) -> None:
        shared_places.evaluate_pin(self.bob_pin.pk)
        place = self.formed()
        _signals(
            self.carol, cells=("b1bbbb", "b2bbbb", "b3bbbb", "b4bbbb", "b5bbbb")
        )  # a twin of bob, unrelated to alice
        joiner = _pin(self.carol, -20)
        shared_places.evaluate_pin(joiner.pk)
        self.assertFalse(SharedPlaceMember.objects.filter(place=place, profile=self.carol).exists())

    def test_a_joiner_independent_of_every_member_joins(self) -> None:
        shared_places.evaluate_pin(self.bob_pin.pk)
        place = self.formed()
        _signals(self.carol, cells=("c1cccc", "c2cccc", "c3cccc", "c4cccc", "c5cccc"))
        shared_places.evaluate_pin(_pin(self.carol, -20).pk)
        self.assertTrue(SharedPlaceMember.objects.filter(place=place, profile=self.carol).exists())


class CheapGatesFirstTests(SharedPlaceTestCase):
    def test_a_fresh_pin_costs_no_place_resolution(self) -> None:
        _pin(self.alice)
        fresh = _pin(self.bob, 10, age_days=1)
        with (
            patch.object(Place.objects.__class__, "resolve_for_point") as resolve,
            patch(f"{MODULE}._nearest_reference") as reference,
        ):
            shared_places.evaluate_pin(fresh.pk)
        resolve.assert_not_called()
        reference.assert_not_called()

    def test_a_pin_with_community_off_is_skipped_before_any_lookup(self) -> None:
        _pin(self.alice)
        other = _pin(self.bob, 10)
        type(self.bob).objects.filter(pk=self.bob.pk).update(community_enabled=False)
        with patch(f"{MODULE}._nearest_synthetic_place") as nearest:
            shared_places.evaluate_pin(other.pk)
        nearest.assert_not_called()

    def test_the_range_task_revalidates_once_per_profile_and_honours_the_filters(self) -> None:
        from urbanlens.dashboard import tasks

        mine = [_pin(self.alice, 500 + 50 * i) for i in range(3)]
        old = _pin(self.alice, 900)
        Pin.objects.filter(pk=old.pk).update(created=timezone.now() - timedelta(days=400))
        off = _profile()
        type(off).objects.filter(pk=off.pk).update(community_enabled=False)
        skipped = _pin(off, 1000)
        pks = [p.pk for p in mine] + [old.pk, skipped.pk]
        with (
            patch(f"{MODULE}.revalidate_memberships") as revalidate,
            patch(f"{MODULE}.evaluate_pin", return_value=None) as evaluate,
        ):
            tasks.evaluate_shared_places_range(min(pks), max(pks))
        self.assertEqual(revalidate.call_count, 1)
        evaluated = {call.args[0] for call in evaluate.call_args_list}
        self.assertEqual(evaluated, {p.pk for p in mine})
