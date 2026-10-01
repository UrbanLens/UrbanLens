"""Tests for services.spotguessr.eligibility - "pinned by every participant"."""

from __future__ import annotations

from itertools import count

from django.contrib.gis.geos import Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.spotguessr.eligibility import eligible_locations, has_eligible_locations

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class EligibleLocationsTests(TestCase):
    def setUp(self) -> None:
        self.alice = _make_profile()
        self.bob = _make_profile()

    def test_no_profiles_returns_nothing(self) -> None:
        self.assertFalse(eligible_locations([]).exists())

    def test_solo_player_sees_their_own_pins(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        self.assertEqual(list(eligible_locations([self.alice])), [location])

    def test_solo_player_never_sees_locations_they_havent_pinned(self) -> None:
        _make_location()
        self.assertEqual(list(eligible_locations([self.alice])), [])

    def test_location_must_be_pinned_by_every_participant(self) -> None:
        both_pinned = _make_location()
        only_alice = _make_location()
        baker.make(Pin, profile=self.alice, location=both_pinned)
        baker.make(Pin, profile=self.bob, location=both_pinned)
        baker.make(Pin, profile=self.alice, location=only_alice)

        self.assertEqual(list(eligible_locations([self.alice, self.bob])), [both_pinned])

    def test_require_visited_by_all_excludes_pinned_but_unvisited(self) -> None:
        location = _make_location()
        pin = baker.make(Pin, profile=self.alice, location=location)

        self.assertEqual(list(eligible_locations([self.alice], require_visited_by_all=True)), [])

        baker.make(PinVisit, pin=pin, visited_at=timezone.now())
        self.assertEqual(list(eligible_locations([self.alice], require_visited_by_all=True)), [location])

    def test_exclude_location_ids_removes_already_used_locations(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.alice, location=location)
        self.assertEqual(list(eligible_locations([self.alice], exclude_location_ids=[location.pk])), [])

    def test_geo_bounds_restricts_to_locations_inside_the_polygon(self) -> None:
        inside = baker.make(Location, latitude="42.650000", longitude="-73.760000")
        outside = baker.make(Location, latitude="10.000000", longitude="10.000000")
        baker.make(Pin, profile=self.alice, location=inside)
        baker.make(Pin, profile=self.alice, location=outside)

        bounds = Polygon.from_bbox((-74.0, 42.0, -73.0, 43.0))
        bounds.srid = 4326
        self.assertEqual(list(eligible_locations([self.alice], geo_bounds=bounds)), [inside])


class HasEligibleLocationsTests(TestCase):
    """The cheap pre-check SpotGuessrStartView uses to avoid creating a session
    that can never play a single round - see its docstring for the rationale."""

    def test_false_for_a_profile_with_no_pins(self) -> None:
        profile = _make_profile()
        self.assertFalse(has_eligible_locations([profile]))

    def test_true_once_a_pin_exists(self) -> None:
        profile = _make_profile()
        baker.make(Pin, profile=profile, location=_make_location())
        self.assertTrue(has_eligible_locations([profile]))

    def test_false_when_geo_bounds_excludes_every_pin(self) -> None:
        profile = _make_profile()
        baker.make(Pin, profile=profile, location=_make_location())
        far_away = Polygon.from_bbox((10.0, 10.0, 11.0, 11.0))
        far_away.srid = 4326
        self.assertFalse(has_eligible_locations([profile], geo_bounds=far_away))


class SoloLabelFilterTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.label = baker.make(Label, name="Factories")

    def test_no_label_id_returns_every_pinned_location(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=self.profile, location=location)
        self.assertIn(location, eligible_locations([self.profile]))

    def test_label_id_excludes_locations_whose_pin_lacks_the_label(self) -> None:
        labeled_location = _make_location()
        unlabeled_location = _make_location()
        labeled_pin = baker.make(Pin, profile=self.profile, location=labeled_location)
        labeled_pin.labels.add(self.label)
        baker.make(Pin, profile=self.profile, location=unlabeled_location)

        results = eligible_locations([self.profile], label_id=self.label.pk)
        self.assertIn(labeled_location, results)
        self.assertNotIn(unlabeled_location, results)

    def test_label_filter_includes_descendant_labels(self) -> None:
        parent = baker.make(Label, name="Urbex")
        child = baker.make(Label, name="Steel Mills")
        child.parents.add(parent)
        location = _make_location()
        pin = baker.make(Pin, profile=self.profile, location=location)
        pin.labels.add(child)

        self.assertIn(location, eligible_locations([self.profile], label_id=parent.pk))

    def test_unresolvable_label_id_yields_nothing_rather_than_erroring(self) -> None:
        baker.make(Pin, profile=self.profile, location=_make_location())
        self.assertFalse(has_eligible_locations([self.profile], label_id=999_999))


class MultiplayerLabelFilterCannotLeakTests(TestCase):
    """The label filter must never surface a location some participant hasn't pinned themselves -
    it can only narrow rule 1's "pinned by every participant" pool, never substitute for it."""

    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        self.label = baker.make(Label, name="Factories")

    def test_a_location_only_the_host_pinned_stays_ineligible_even_when_labeled(self) -> None:
        host_only_location = _make_location()
        host_pin = baker.make(Pin, profile=self.host, location=host_only_location)
        host_pin.labels.add(self.label)
        # self.guest never pins host_only_location at all.

        results = eligible_locations([self.host, self.guest], label_id=self.label.pk)
        self.assertNotIn(host_only_location, results)

    def test_pinned_by_both_and_labeled_by_only_the_host_is_still_eligible(self) -> None:
        """ "At least one participant's pin has the label" - not "every" participant's."""
        shared_location = _make_location()
        host_pin = baker.make(Pin, profile=self.host, location=shared_location)
        host_pin.labels.add(self.label)
        baker.make(Pin, profile=self.guest, location=shared_location)  # guest's own pin, unlabeled

        results = eligible_locations([self.host, self.guest], label_id=self.label.pk)
        self.assertIn(shared_location, results)

    def test_pinned_by_both_but_labeled_by_neither_is_excluded(self) -> None:
        shared_location = _make_location()
        baker.make(Pin, profile=self.host, location=shared_location)
        baker.make(Pin, profile=self.guest, location=shared_location)

        results = eligible_locations([self.host, self.guest], label_id=self.label.pk)
        self.assertNotIn(shared_location, results)

    def test_someone_elses_private_label_id_cannot_be_used_to_probe_their_pins(self) -> None:
        """A bystander's own private label, applied to a location the session participants
        haven't pinned, must not make that location appear eligible for this session."""
        bystander = _make_profile()
        bystander_label = baker.make(Label, name="Bystander's secret spots", profile=bystander)
        bystander_location = _make_location()
        bystander_pin = baker.make(Pin, profile=bystander, location=bystander_location)
        bystander_pin.labels.add(bystander_label)

        results = eligible_locations([self.host, self.guest], label_id=bystander_label.pk)
        self.assertNotIn(bystander_location, results)
        self.assertFalse(has_eligible_locations([self.host, self.guest], label_id=bystander_label.pk))
