"""Tests for gaming-proof share-chain provenance (services.sharing.share_provenance).

Covers the exposure model end to end:
- receiving a share records a LocationExposure for the recipient
- a pin created *after* the exposure (never accepted through the share) still
  chains its onward shares back to the original share
- the two gaming vectors from the spec are closed:
  * move the pin away, drop a fresh pin at the original spot, share that one
  * move the pin to a new location and share it from there
- pin delete / re-create cycles don't reset the chain
- a recipient who already had the place pinned gets no exposure (the share
  wasn't their initial information)
- the sender's chain only grows by the recipient's *shares*, never by their
  pin add/delete churn
- two further gaming vectors, closed for reasons distinct from the above:
  * delete the infected pin, drop a new one nearby but not at the exact same
    coordinates (a genuinely different Location row within the exposure
    radius still resolves to the same exposure)
  * create a pin far away with no history, then move it into an exposed
    radius and share from there (resolution always reads the pin's *current*
    location, never its creation history)
- the radius is a hard boundary (just outside it never chains), exposure is
  strictly per-profile (another profile's exposure never leaks), and a pin's
  own lineage always wins over an environmental exposure match
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.db import DatabaseError, transaction
from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_sharing import _create_pin_from_share
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import ExposureSource, LocationExposure, PinShare, PinShareStatus
from urbanlens.dashboard.services.sharing.share_provenance import (
    record_share_exposure,
    resolve_origin_share,
)

# DB-backed @given tests below never touch self.client - only ORM/service
# calls - per this repo's documented rule that hypothesis's per-example DB
# flush and the Django test client don't mix.
_db_settings = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _befriend(a, b) -> None:
    Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)


class _ProvenanceTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.users = {name: baker.make(User, username=name) for name in ("sarah", "john", "kim")}
        self.profiles = {name: user.profile for name, user in self.users.items()}
        self.location = baker.make(Location, latitude="42.100000", longitude="-73.900000", official_name="Old Mill")
        self.far_location = baker.make(Location, latitude="43.500000", longitude="-75.200000", official_name="Far Away")
        self.sarah_pin = Pin.objects.create(profile=self.profiles["sarah"], location=self.location)

    def _share(self, pin: Pin, from_name: str, to_name: str, **kwargs) -> PinShare:
        """Create a share the way the share flows do: snapshot location + record exposure."""
        share = PinShare.objects.create(
            pin=pin,
            location=pin.location,
            from_profile=self.profiles[from_name],
            to_profile=self.profiles[to_name],
            parent_share=resolve_origin_share(self.profiles[from_name].pk, pin=pin),
            status=kwargs.pop("status", PinShareStatus.PENDING),
            **kwargs,
        )
        record_share_exposure(share)
        return share


class ExposureRecordingTests(_ProvenanceTestCase):
    """Receiving a share infects the recipient's view of the location."""

    def test_share_records_exposure_for_recipient(self):
        share = self._share(self.sarah_pin, "sarah", "john")
        exposure = LocationExposure.objects.get(profile=self.profiles["john"])
        self.assertEqual(exposure.location_id, self.location.pk)
        self.assertEqual(exposure.share_id, share.pk)

    def test_no_exposure_when_recipient_already_pinned(self):
        # John pinned the place himself, before any share reached him.
        Pin.objects.create(profile=self.profiles["john"], location=self.location)
        self._share(self.sarah_pin, "sarah", "john", status=PinShareStatus.ALREADY_PINNED)
        self.assertFalse(LocationExposure.objects.filter(profile=self.profiles["john"]).exists())

    def test_prior_pinner_shares_without_parent(self):
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
        self._share(self.sarah_pin, "sarah", "john", status=PinShareStatus.ALREADY_PINNED)
        onward = self._share(john_pin, "john", "kim")
        self.assertIsNone(onward.parent_share_id)


class ExposureRecordingFailureIsolationTests(_ProvenanceTestCase):
    """A DatabaseError recording the exposure is swallowed inside its own nested
    atomic() savepoint (docs/PROBLEMS.md - naively wrapping the whole share-creation
    view in atomic() would otherwise convert this tolerated bookkeeping gap into a
    hard 500 for the entire share; nesting just this write keeps it genuinely
    all-or-nothing without risking that).
    """

    def _pending_share(self) -> PinShare:
        return PinShare.objects.create(
            pin=self.sarah_pin,
            location=self.sarah_pin.location,
            from_profile=self.profiles["sarah"],
            to_profile=self.profiles["john"],
            status=PinShareStatus.PENDING,
        )

    def test_a_database_error_recording_the_exposure_is_swallowed(self):
        share = self._pending_share()

        with mock.patch(
            "urbanlens.dashboard.models.pin_share.exposure.LocationExposure.objects.record",
            side_effect=DatabaseError("boom"),
        ):
            result = record_share_exposure(share)

        self.assertIsNone(result)
        self.assertFalse(LocationExposure.objects.filter(share=share).exists())

    def test_the_swallowed_failure_does_not_poison_an_outer_transaction(self):
        """The precise scenario the nested atomic() protects against: a caller that
        wraps its own multi-write sequence in atomic() must still be able to write
        after record_share_exposure swallows a DatabaseError, rather than hit
        TransactionManagementError from an outer transaction Django marked broken.

        Needs a *real* DB-level failure, not a synthetic ``side_effect`` exception -
        Postgres only aborts the transaction/savepoint for an error it actually
        raised, so a bare mocked exception that never touches the DB wouldn't
        exercise the thing being protected against. A duplicate (profile, location,
        share) triple gets there via a genuine IntegrityError against
        ``db_locexp_one_per_pfl_loc_share`` - unlike a bogus FK target, a unique
        constraint is never deferrable, so it is guaranteed to raise immediately.
        """
        share = self._pending_share()
        location = share.shared_location
        LocationExposure.objects.create(
            profile_id=share.to_profile_id,
            location_id=location.pk,
            share_id=share.pk,
            source=ExposureSource.SHARE_RECEIVED,
        )

        def _violate_unique(*, profile_id, location_id, share_id, source):
            return LocationExposure.objects.create(
                profile_id=profile_id, location_id=location_id, share_id=share_id, source=source
            )

        with transaction.atomic():
            with mock.patch(
                "urbanlens.dashboard.models.pin_share.exposure.LocationExposure.objects.record",
                side_effect=_violate_unique,
            ):
                result = record_share_exposure(share)
            self.assertIsNone(result)
            # If the failed INSERT had escaped record_share_exposure's own atomic()
            # savepoint, Postgres would leave this outer transaction aborted and
            # this write would raise TransactionManagementError instead of
            # succeeding.
            share.message = "still writable"
            share.save(update_fields=["message"])

        share.refresh_from_db()
        self.assertEqual(share.message, "still writable")
        self.assertEqual(LocationExposure.objects.filter(share=share).count(), 1)


class ExposureResolutionTests(_ProvenanceTestCase):
    """A pin created after exposure chains back, no matter what the pins do."""

    def test_fresh_pin_at_exposed_location_chains_back(self):
        # John never accepts the share - he pins the place himself later.
        share = self._share(self.sarah_pin, "sarah", "john")
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
        onward = self._share(john_pin, "john", "kim")
        self.assertEqual(onward.parent_share_id, share.pk)

    def test_pin_delete_and_recreate_does_not_break_chain(self):
        share = self._share(self.sarah_pin, "sarah", "john")
        for _ in range(3):
            john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
            john_pin.delete()
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
        onward = self._share(john_pin, "john", "kim")
        self.assertEqual(onward.parent_share_id, share.pk)

    def test_repin_churn_does_not_grow_sharers_chain(self):
        share = self._share(self.sarah_pin, "sarah", "john")
        for _ in range(3):
            john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
            john_pin.delete()
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
        # Sarah's chain is still just her one share...
        self.assertEqual(PinShare.chain_share_count([share.pk]), 1)
        # ...until John actually shares onward, which makes it two.
        self._share(john_pin, "john", "kim")
        self.assertEqual(PinShare.chain_share_count([share.pk]), 2)

    def test_gaming_move_away_then_fresh_pin_at_original_spot(self):
        # John accepts, moves his pin far away, then drops a brand-new pin at
        # the original location and shares that one.
        share = self._share(self.sarah_pin, "sarah", "john")
        john_pin = _create_pin_from_share(share)
        john_pin.location = self.far_location
        john_pin.save()
        fresh_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
        onward = self._share(fresh_pin, "john", "kim")
        self.assertEqual(onward.parent_share_id, share.pk)

    def test_gaming_move_pin_then_share_from_new_location(self):
        # John accepts, moves the pin to a new location, and shares from there.
        share = self._share(self.sarah_pin, "sarah", "john")
        john_pin = _create_pin_from_share(share)
        john_pin.location = self.far_location
        john_pin.save()
        onward = self._share(john_pin, "john", "kim")
        self.assertEqual(onward.parent_share_id, share.pk)

    def test_move_infects_new_location_for_future_pins(self):
        # After the move, even a *fresh* pin at the new location (the moved
        # pin deleted first) still chains back.
        share = self._share(self.sarah_pin, "sarah", "john")
        john_pin = _create_pin_from_share(share)
        john_pin.location = self.far_location
        john_pin.save()
        john_pin.delete()
        fresh_pin = Pin.objects.create(profile=self.profiles["john"], location=self.far_location)
        onward = self._share(fresh_pin, "john", "kim")
        self.assertEqual(onward.parent_share_id, share.pk)

    def test_unrelated_location_does_not_chain(self):
        self._share(self.sarah_pin, "sarah", "john")
        elsewhere = Pin.objects.create(profile=self.profiles["john"], location=self.far_location)
        onward = self._share(elsewhere, "john", "kim")
        self.assertIsNone(onward.parent_share_id)

    def test_nearby_pin_within_radius_chains_back(self):
        # ~50 m north of the shared location - inside the 150 m radius.
        share = self._share(self.sarah_pin, "sarah", "john")
        nearby = baker.make(Location, latitude="42.100450", longitude="-73.900000")
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=nearby)
        onward = self._share(john_pin, "john", "kim")
        self.assertEqual(onward.parent_share_id, share.pk)

    def test_earliest_exposure_wins(self):
        share_from_sarah = self._share(self.sarah_pin, "sarah", "john")
        kim_pin = Pin.objects.create(profile=self.profiles["kim"], location=self.location)
        self._share(kim_pin, "kim", "john")
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
        onward = self._share(john_pin, "john", "kim")
        self.assertEqual(onward.parent_share_id, share_from_sarah.pk)


class AdditionalGamingScenarioTests(_ProvenanceTestCase):
    """The two follow-up gaming vectors, plus the boundary conditions around them.

    Two follow-up scenarios (delete + drop nearby; create far away + drag
    in) work because resolution never consults a pin's creation history,
    only its *current* location, re-queried live at share time - so it
    doesn't matter whether that location was arrived at by accepting a
    share, dropping a pin nearby, or dragging an unrelated pin into the zone.

    A third question - "a pin in the affected area, then moved away, then
    shared" - splits into two genuinely different cases depending on whether
    the pin carries its own lineage (``source_share``/``inferred_source_share``):
    if it does, it chains at *any* distance, forever (lineage is checked
    before any distance computation); if it doesn't, it's resolved live by
    proximity and a move beyond the radius genuinely severs it - *unless* an
    earlier move already carried a nearby (non-exact) exposure onto the
    pin's current spot, which is the bug this test class also catches: the
    move-propagation step used to only match the *exact* old Location row,
    silently dropping a merely-nearby exposure on a second move.
    """

    def test_delete_infected_pin_then_new_pin_on_different_location_row_within_radius(self):
        # John accepts, deletes the resulting pin entirely, then drops a
        # brand-new pin ~100 m away - a genuinely different Location row,
        # not the same one reused - and shares that.
        share = self._share(self.sarah_pin, "sarah", "john")
        john_pin = _create_pin_from_share(share)
        john_pin.delete()
        nearby_location = baker.make(Location, latitude="42.100900", longitude="-73.900000")
        self.assertNotEqual(nearby_location.pk, self.location.pk)
        fresh_pin = Pin.objects.create(profile=self.profiles["john"], location=nearby_location)

        onward = self._share(fresh_pin, "john", "kim")

        self.assertEqual(onward.parent_share_id, share.pk)

    def test_create_pin_far_away_then_move_into_exposed_radius_chains_back(self):
        # John never touches Sarah's share at all - he creates his own,
        # completely unrelated pin far away first, and only later drags it
        # into the exposed location's radius before sharing it.
        share = self._share(self.sarah_pin, "sarah", "john")
        unrelated_pin = Pin.objects.create(profile=self.profiles["john"], location=self.far_location)

        unrelated_pin.location = self.location
        unrelated_pin.save()
        onward = self._share(unrelated_pin, "john", "kim")

        self.assertEqual(onward.parent_share_id, share.pk)

    def test_move_into_exposed_radius_never_touched_first_still_has_no_direct_lineage(self):
        # Confirms the moved-in pin itself picks up no source_share/inferred
        # link from the move - the chain match is a live lookup at share
        # time, not a stamp applied at move time.
        self._share(self.sarah_pin, "sarah", "john")
        unrelated_pin = Pin.objects.create(profile=self.profiles["john"], location=self.far_location)

        unrelated_pin.location = self.location
        unrelated_pin.save()

        self.assertIsNone(unrelated_pin.source_share_id)
        self.assertIsNone(unrelated_pin.inferred_source_share_id)

    def test_pin_that_never_entered_exposed_radius_does_not_chain(self):
        # This pin never carries lineage of its own (never accepted a share,
        # never stamped by an earlier share event) and never sat within the
        # exposed radius at any point - only landing ~200 m away, just
        # outside the 150 m cutoff. Contrast with
        # test_lineage_carrying_pin_moved_far_away_still_chains below: a pin
        # WITH its own lineage chains at any distance; this one has none to
        # fall back on, so the live proximity check is all it has, and it
        # legitimately fails that check.
        share = self._share(self.sarah_pin, "sarah", "john")
        beyond_radius = baker.make(Location, latitude="42.101800", longitude="-73.900000")
        unrelated_pin = Pin.objects.create(profile=self.profiles["john"], location=self.far_location)

        unrelated_pin.location = beyond_radius
        unrelated_pin.save()
        onward = self._share(unrelated_pin, "john", "kim")

        self.assertIsNone(onward.parent_share_id)
        self.assertNotEqual(share.parent_share_id, onward.pk)

    def test_lineage_carrying_pin_moved_far_away_still_chains(self):
        # The mirror image of the test above, using the identical ~200 m
        # distance: this pin IS the one accepted from Sarah's share, so it
        # carries source_share_id directly. Moving it beyond the exposure
        # radius (even arbitrarily far - distance is irrelevant here) must
        # not lose the chain, because resolution checks the pin's own
        # lineage field before it ever computes a distance.
        share = self._share(self.sarah_pin, "sarah", "john")
        john_pin = _create_pin_from_share(share)
        beyond_radius = baker.make(Location, latitude="42.101800", longitude="-73.900000")

        john_pin.location = beyond_radius
        john_pin.save()
        onward = self._share(john_pin, "john", "kim")

        self.assertEqual(onward.parent_share_id, share.pk)

    def test_second_move_of_never_shared_nearby_pin_still_chains(self):
        # The gap this closes: a pin sitting *near* (not exactly on, and
        # never shared/stamped while there) an exposed location gets moved a
        # SECOND time, before it was ever shared from the first spot. The
        # move-propagation step must find that nearby exposure by radius,
        # not by exact Location-row match, or the chain silently breaks here.
        share = self._share(self.sarah_pin, "sarah", "john")
        nearby_location = baker.make(Location, latitude="42.100900", longitude="-73.900000")
        self.assertNotEqual(nearby_location.pk, self.location.pk)
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=nearby_location)
        self.assertIsNone(john_pin.source_share_id)
        self.assertIsNone(john_pin.inferred_source_share_id)

        # Second move: away to a third, unrelated spot, still never shared.
        john_pin.location = self.far_location
        john_pin.save()
        onward = self._share(john_pin, "john", "kim")

        self.assertEqual(onward.parent_share_id, share.pk)

    def test_exposure_is_per_profile_not_global_to_the_location(self):
        # The location is exposed for John, but Kim was never told about it -
        # dragging her own unrelated pin into its radius must not let her
        # inherit John's (or Sarah's) chain.
        self._share(self.sarah_pin, "sarah", "john")
        kim_pin = Pin.objects.create(profile=self.profiles["kim"], location=self.far_location)

        kim_pin.location = self.location
        kim_pin.save()
        onward = self._share(kim_pin, "kim", "john")

        self.assertIsNone(onward.parent_share_id)

    def test_pins_own_lineage_wins_over_environmental_exposure_after_move(self):
        # John carries two independent lineages: his own accepted pin at
        # Sarah's place (share_a), and a separately accepted pin from Kim's
        # own unrelated place (share_b). Moving the share_b pin into
        # share_a's exposed radius must not overwrite its own, more specific
        # lineage - source_share_id always wins over a proximity match.
        share_a = self._share(self.sarah_pin, "sarah", "john")
        kim_original_pin = Pin.objects.create(profile=self.profiles["kim"], location=self.far_location)
        share_b = self._share(kim_original_pin, "kim", "john")
        john_pin_b = _create_pin_from_share(share_b)

        john_pin_b.location = self.location  # into share_a's exposed radius
        john_pin_b.save()
        onward = self._share(john_pin_b, "john", "kim")

        self.assertEqual(onward.parent_share_id, share_b.pk)
        self.assertNotEqual(onward.parent_share_id, share_a.pk)


class ShareViewIntegrationTests(_ProvenanceTestCase):
    """The share dialog endpoint resolves parents through exposures."""

    def test_view_chains_fresh_pin_through_exposure(self):
        share = self._share(self.sarah_pin, "sarah", "john")
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.location)
        _befriend(self.profiles["john"], self.profiles["kim"])
        self.client.force_login(self.users["john"])

        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": john_pin.slug}),
            {"profile_id": self.profiles["kim"].pk},
        )

        self.assertEqual(response.status_code, 200)
        reshare = PinShare.objects.get(pin=john_pin, to_profile=self.profiles["kim"])
        self.assertEqual(reshare.parent_share_id, share.pk)
        self.assertEqual(reshare.location_id, self.location.pk)
        # The heuristic result is stamped so the pin now carries its lineage.
        john_pin.refresh_from_db()
        self.assertEqual(john_pin.inferred_source_share_id, share.pk)

    def test_view_records_recipient_exposure(self):
        _befriend(self.profiles["sarah"], self.profiles["john"])
        self.client.force_login(self.users["sarah"])

        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": self.sarah_pin.slug}),
            {"profile_id": self.profiles["john"].pk},
        )

        self.assertEqual(response.status_code, 200)
        share = PinShare.objects.get(pin=self.sarah_pin, to_profile=self.profiles["john"])
        self.assertTrue(
            LocationExposure.objects.filter(profile=self.profiles["john"], location=self.location, share=share).exists()
        )


class LocationExposureQuerySetTests(_ProvenanceTestCase):
    """Direct coverage of LocationExposureQuerySet.near() / LocationExposureManager.record()
    - previously a private module-level helper (_exposures_near) and three
    near-identical get_or_create calls duplicated across share_provenance.py,
    now a proper queryset/manager pair matching the rest of the codebase's
    convention."""

    def test_near_finds_an_exposure_within_radius(self) -> None:
        share = self._share(self.sarah_pin, "sarah", "john")

        found = LocationExposure.objects.near(self.profiles["john"].pk, self.location, radius_meters=150)

        self.assertTrue(found.filter(share=share).exists())

    def test_near_excludes_an_exposure_outside_radius(self) -> None:
        self._share(self.sarah_pin, "sarah", "john")

        found = LocationExposure.objects.near(self.profiles["john"].pk, self.far_location, radius_meters=150)

        self.assertFalse(found.exists())

    def test_near_is_scoped_to_the_given_profile(self) -> None:
        self._share(self.sarah_pin, "sarah", "john")

        found = LocationExposure.objects.near(self.profiles["kim"].pk, self.location, radius_meters=150)

        self.assertFalse(found.exists())

    def test_record_creates_a_new_exposure(self) -> None:
        share = self._share(self.sarah_pin, "sarah", "kim")
        LocationExposure.objects.filter(profile=self.profiles["kim"], location=self.location, share=share).delete()

        exposure, created = LocationExposure.objects.record(
            profile_id=self.profiles["kim"].pk,
            location_id=self.location.pk,
            share_id=share.pk,
            source=ExposureSource.PIN_MOVED,
        )

        self.assertTrue(created)
        self.assertEqual(exposure.source, ExposureSource.PIN_MOVED)

    def test_record_is_idempotent_for_the_same_profile_location_share(self) -> None:
        share = self._share(self.sarah_pin, "sarah", "kim")

        _exposure, created = LocationExposure.objects.record(
            profile_id=self.profiles["kim"].pk,
            location_id=self.location.pk,
            share_id=share.pk,
            source=ExposureSource.PIN_MOVED,
        )

        self.assertFalse(created)
        self.assertEqual(
            LocationExposure.objects.filter(profile=self.profiles["kim"], location=self.location, share=share).count(),
            1,
        )


class PinShareQuerySetTests(_ProvenanceTestCase):
    """Direct coverage of PinShareQuerySet's new methods - previously the same
    dup-share existence check was independently re-written (pin-keyed and
    location-keyed variants) across services/trip_share_tracking.py,
    services/map_sharing.py, and services/dm_location_detection.py."""

    def test_already_shared_with_true_for_a_pin_keyed_match(self) -> None:
        self._share(self.sarah_pin, "sarah", "john")

        result = PinShare.objects.already_shared_with(self.profiles["john"], pin=self.sarah_pin)

        self.assertTrue(result.exists())

    def test_already_shared_with_false_for_a_different_recipient(self) -> None:
        self._share(self.sarah_pin, "sarah", "john")

        result = PinShare.objects.already_shared_with(self.profiles["kim"], pin=self.sarah_pin)

        self.assertFalse(result.exists())

    def test_already_shared_with_true_for_a_location_keyed_match(self) -> None:
        PinShare.objects.create(
            location=self.location,
            from_profile=self.profiles["sarah"],
            to_profile=self.profiles["john"],
            status=PinShareStatus.PENDING,
        )

        result = PinShare.objects.already_shared_with(self.profiles["john"], location=self.location)

        self.assertTrue(result.exists())

    def test_reusable_for_returns_the_earliest_pending_or_detected_share(self) -> None:
        older = PinShare.objects.create(
            location=self.location,
            from_profile=self.profiles["sarah"],
            to_profile=self.profiles["john"],
            status=PinShareStatus.PENDING,
        )
        PinShare.objects.create(
            location=self.location,
            from_profile=self.profiles["sarah"],
            to_profile=self.profiles["john"],
            status=PinShareStatus.PENDING,
        )

        found = PinShare.objects.reusable_for(self.profiles["john"], self.location).first()

        self.assertEqual(found, older)

    def test_reusable_for_excludes_accepted_shares(self) -> None:
        PinShare.objects.create(
            location=self.location,
            from_profile=self.profiles["sarah"],
            to_profile=self.profiles["john"],
            status=PinShareStatus.ACCEPTED,
        )

        found = PinShare.objects.reusable_for(self.profiles["john"], self.location).first()

        self.assertIsNone(found)

    def test_pending_pin_ids_for_finds_a_pending_share_among_candidates(self) -> None:
        other_pin = Pin.objects.create(profile=self.profiles["sarah"], location=self.far_location)
        PinShare.objects.create(
            pin=other_pin,
            location=other_pin.location,
            from_profile=self.profiles["sarah"],
            to_profile=self.profiles["john"],
            status=PinShareStatus.PENDING,
        )

        ids = set(PinShare.objects.pending_pin_ids_for(self.profiles["john"], [self.sarah_pin, other_pin]))

        self.assertEqual(ids, {other_pin.pk})

    def test_sent_by_returns_only_that_profiles_outgoing_shares(self) -> None:
        outgoing = self._share(self.sarah_pin, "sarah", "john")
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.far_location)
        self._share(john_pin, "john", "kim")

        result = list(PinShare.objects.sent_by(self.profiles["sarah"]))

        self.assertEqual(result, [outgoing])

    def test_received_by_returns_only_that_profiles_incoming_shares(self) -> None:
        incoming = self._share(self.sarah_pin, "sarah", "john")
        john_pin = Pin.objects.create(profile=self.profiles["john"], location=self.far_location)
        self._share(john_pin, "john", "kim")

        result = list(PinShare.objects.received_by(self.profiles["john"]))

        self.assertEqual(result, [incoming])


class ArbitraryChainDepthPropertyTests(_ProvenanceTestCase):
    """Property-based generalization of the fixed 2-3-hop chains exercised
    throughout this module: the provenance invariant - every share, however
    deep the reshare chain, must be traceable back to a single true origin
    share (``parent_share`` eventually ``None``), with no cycle and no lost
    link - must hold for an arbitrary generated chain length/branching factor,
    not just the hand-picked examples above.

    Each accepted pin in the chain carries its own ``source_share`` lineage
    (set directly by ``_create_pin_from_share``), so ``parent_share`` forms a
    genuine linked chain back to the origin one hop at a time - not a single
    jump straight to the origin past the first hop. ``PinShare.chain_share_count``
    (see ``test_repin_churn_does_not_grow_sharers_chain`` above) already relies
    on exactly this structure.
    """

    @given(chain_length=st.integers(min_value=2, max_value=6))
    @_db_settings
    def test_arbitrary_length_reshare_chain_always_walks_back_to_the_true_origin(self, chain_length: int) -> None:
        origin_share = self._share(self.sarah_pin, "sarah", "john")
        current_pin = _create_pin_from_share(origin_share)
        current_from = "john"
        shares_in_chain = [origin_share]

        for i in range(chain_length):
            next_name = f"chain_{i}"
            self.profiles[next_name] = baker.make(User, username=f"chaindepth_{i}").profile
            onward = self._share(current_pin, current_from, next_name)
            shares_in_chain.append(onward)
            current_pin = _create_pin_from_share(onward)
            current_from = next_name

        # Every share in the chain, walked via parent_share, reaches the true
        # origin in exactly as many steps as its position in the chain -
        # never fewer (a lost link), more (a spurious extra hop), and never
        # loops back on itself (a cycle).
        for index, share in enumerate(shares_in_chain):
            walked_pks: set[int] = set()
            node = share
            while node is not None:
                self.assertNotIn(node.pk, walked_pks, "cycle detected in parent_share chain")
                walked_pks.add(node.pk)
                node = node.parent_share
            self.assertEqual(len(walked_pks), index + 1)
            self.assertIn(origin_share.pk, walked_pks)

    @given(chain_length=st.integers(min_value=2, max_value=6))
    @_db_settings
    def test_every_profile_in_an_arbitrary_length_chain_gets_exactly_one_exposure(self, chain_length: int) -> None:
        origin_share = self._share(self.sarah_pin, "sarah", "john")
        current_pin = _create_pin_from_share(origin_share)
        current_from = "john"
        chain_profiles = [self.profiles["john"]]

        for i in range(chain_length):
            next_name = f"expo_chain_{i}"
            self.profiles[next_name] = baker.make(User, username=f"expochain_{i}").profile
            onward = self._share(current_pin, current_from, next_name)
            chain_profiles.append(self.profiles[next_name])
            current_pin = _create_pin_from_share(onward)
            current_from = next_name

        # Regardless of how many hops preceded it, each recipient's exposure
        # to the shared location is recorded exactly once - never zero (a
        # missed recording) and never duplicated (a repeated one).
        for profile in chain_profiles:
            self.assertEqual(LocationExposure.objects.filter(profile=profile, location=self.location).count(), 1)

    @given(num_branches=st.integers(min_value=2, max_value=5))
    @_db_settings
    def test_arbitrary_number_of_branches_from_one_pin_all_trace_back_to_the_same_origin(
        self, num_branches: int
    ) -> None:
        """John re-shares the same accepted pin to an arbitrary number of
        independent recipients (a branching reshare fan-out, not a linear
        chain) - every branch must chain back to the same origin share,
        however many branches there are."""
        origin_share = self._share(self.sarah_pin, "sarah", "john")
        john_pin = _create_pin_from_share(origin_share)

        for i in range(num_branches):
            branch_name = f"branch_{i}"
            self.profiles[branch_name] = baker.make(User, username=f"branch_{i}").profile
            onward = self._share(john_pin, "john", branch_name)
            self.assertEqual(onward.parent_share_id, origin_share.pk)
            # The exposure created for this branch's recipient references the
            # branch's own direct share (onward), not the resolved origin -
            # resolve_origin_share only decides parent_share; record_share_exposure
            # always records the share that directly caused the exposure.
            self.assertEqual(
                LocationExposure.objects.filter(
                    profile=self.profiles[branch_name], location=self.location, share=onward
                ).count(),
                1,
            )
