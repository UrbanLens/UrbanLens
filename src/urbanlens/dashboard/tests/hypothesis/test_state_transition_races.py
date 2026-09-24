"""State transitions that raced: each is now one guarded step with a database guarantee.

G2-6 pin-share reject, G2-15 trip member cap, G2-18 friend accept, G4-17 backup overlap,
G4-28 wiki field edits. Interleavings are forced - a stale in-memory row, or a barrier inside
the window - rather than left to timing.
"""

from __future__ import annotations

from contextlib import suppress
import threading
from unittest import mock

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from model_bakery import baker

from urbanlens.core.tests.concurrency import run_concurrently
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.friendship.meta import FriendshipType, Permission
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.invitation import TripInvitation, TripInvitationResponse
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.sharing.pin_sharing import apply_pin_share_response
from urbanlens.dashboard.services.trips.trip_errors import TripQuotaError


def _profile() -> Profile:
    return Profile.objects.get(user=baker.make(User))


def _barrier_after(real, barrier: threading.Barrier):  # noqa: ANN001, ANN202
    """Wrap *real* so every caller finishes it, then waits for the others - holding the race window open."""

    def wrapped(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        result = real(*args, **kwargs)
        with suppress(threading.BrokenBarrierError):
            barrier.wait()
        return result

    return wrapped


class PinShareAnswersSettleOnceTests(TestCase):
    """G2-6: reject wrote REJECTED from a stale row over a committed accept."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.recipient = _profile()
        sender = _profile()
        pin = create_pin_for_profile(sender, name="Old Mill", latitude=42.5, longitude=-73.5).pin
        self.share = PinShare.objects.create(
            pin=pin,
            location=pin.location,
            from_profile=sender,
            to_profile=self.recipient,
            status=PinShareStatus.PENDING,
        )

    def _two_views(self) -> tuple[PinShare, PinShare]:
        return PinShare.objects.get(pk=self.share.pk), PinShare.objects.get(pk=self.share.pk)

    def test_a_reject_that_lost_to_an_accept_leaves_it_accepted(self) -> None:
        accepting, rejecting = self._two_views()
        apply_pin_share_response(accepting, "accept")

        _pin, message = apply_pin_share_response(rejecting, "reject")

        self.share.refresh_from_db()
        self.assertEqual(self.share.status, PinShareStatus.ACCEPTED, "the reject overwrote an accept whose pin exists")
        self.assertTrue(Pin.objects.filter(profile=self.recipient).exists())
        self.assertNotEqual(message, "Shared pin rejected.")

    def test_an_accept_that_lost_to_a_reject_says_so(self) -> None:
        rejecting, accepting = self._two_views()
        apply_pin_share_response(rejecting, "reject")

        pin, message = apply_pin_share_response(accepting, "accept")

        self.assertIsNone(pin)
        self.assertNotIn("added", message.lower(), "a lost accept reported success")
        self.assertFalse(Pin.objects.filter(profile=self.recipient).exists())

    def test_a_retried_accept_still_reports_the_pin(self) -> None:
        first, retry = self._two_views()
        apply_pin_share_response(first, "accept")
        pin, message = apply_pin_share_response(retry, "accept")
        self.assertIsNotNone(pin)
        self.assertIn("added", message.lower())


class TripSeatRaceTests(TransactionTestCase):
    """G2-15: every add path counted the roster, then wrote it, with nothing between."""

    def setUp(self) -> None:
        super().setUp()
        for target in (
            "urbanlens.dashboard.services.core.celery.safely_enqueue_task",
            "urbanlens.dashboard.services.trips.trip_membership.notify_added_to_trip",
        ):
            patch = mock.patch(target)
            patch.start()
            self.addCleanup(patch.stop)
        for name, value in (("can_view_profile", True), ("are_blocked", False)):
            patch = mock.patch.object(Profile, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        baker.make(User)
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(max_trip_members=2)
        self.creator = _profile()
        self.trip = baker.make(Trip, creator=self.creator, allow_add_members="members")
        TripMembership.objects.create(trip=self.trip, profile=self.creator, status=TripMembership.STATUS_JOINED)

    def test_two_invites_for_the_last_seat_admit_one(self) -> None:
        from urbanlens.dashboard.services.auth import username as username_service
        from urbanlens.dashboard.services.trips.trip_membership import add_member_by_username

        invitees = [baker.make(User, username="invitee-a"), baker.make(User, username="invitee-b")]
        barrier = threading.Barrier(2, timeout=3)

        def invite(name: str) -> str:
            try:
                add_member_by_username(Trip.objects.get(pk=self.trip.pk), Profile.objects.get(pk=self.creator.pk), name)
            except TripQuotaError:
                return "full"
            return "added"

        with mock.patch.object(
            username_service, "find_user_by_username", _barrier_after(username_service.find_user_by_username, barrier)
        ):
            outcomes = run_concurrently([lambda name=user.username: invite(name) for user in invitees])

        self.assertEqual(TripMembership.objects.filter(trip=self.trip).count(), 2, f"the cap of 2 admitted {outcomes}")
        self.assertEqual(sorted(outcomes), ["added", "full"])

    def test_a_full_trip_leaves_the_email_invitation_unanswered(self) -> None:
        from urbanlens.dashboard.services.trips.trip_invitations import respond_to_trip

        TripMembership.objects.create(trip=self.trip, profile=_profile(), status=TripMembership.STATUS_JOINED)
        invitee = _profile()
        invitation = baker.make(
            TripInvitation, trip=self.trip, inviter=self.creator, invitee=invitee, email="x@example.com", email_hash="h"
        )
        with (
            mock.patch("urbanlens.dashboard.services.trips.trip_invitations.can_perform", return_value=True),
            self.assertRaises(TripQuotaError),
        ):
            respond_to_trip(invitation, invitee, accept=True)
        invitation.refresh_from_db()
        self.assertEqual(invitation.trip_response, TripInvitationResponse.PENDING)
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=invitee).exists())

    def test_inviting_friends_at_creation_stops_at_the_cap(self) -> None:
        from urbanlens.dashboard.services.trips.trip_crud import invite_members

        friends = [_profile() for _ in range(3)]
        with mock.patch("urbanlens.dashboard.services.social.connections.get_connections", return_value=friends):
            invited = invite_members(self.trip, self.creator, [friend.pk for friend in friends])
        self.assertEqual(invited, 1)
        self.assertEqual(TripMembership.objects.filter(trip=self.trip).count(), 2)


class DirectMessageTripInviteSeatTests(TestCase):
    """G2-15 sibling: inviting from a chat thread wrote the membership without consulting the cap."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(max_trip_members=2)
        self.sender = _profile()
        self.trip = baker.make(Trip, creator=self.sender)
        TripMembership.objects.create(trip=self.trip, profile=self.sender, status=TripMembership.STATUS_JOINED)
        TripMembership.objects.create(trip=self.trip, profile=_profile(), status=TripMembership.STATUS_JOINED)
        self.recipient = _profile()
        Friendship.objects.create(
            from_profile=self.sender,
            to_profile=self.recipient,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        Profile.objects.filter(pk=self.recipient.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
        self.recipient.refresh_from_db()

    def _assert_nothing_written(self) -> None:
        self.assertEqual(TripMembership.objects.filter(trip=self.trip).count(), 2)
        self.assertFalse(DirectMessage.objects.between(self.sender, self.recipient).exists())

    def test_a_full_trip_refuses_the_chat_invite(self) -> None:
        from urbanlens.dashboard.services.messaging.direct_message_shares import invite_to_trip_in_message

        with self.assertRaises(TripQuotaError):
            invite_to_trip_in_message(self.sender, self.recipient, self.trip, "join us")
        self._assert_nothing_written()

    def test_the_chat_invite_view_answers_a_full_trip_with_400(self) -> None:
        from django.urls import reverse

        self.client.force_login(self.sender.user)
        response = self.client.post(
            reverse("messages.share.trip", kwargs={"profile_slug": self.recipient.ensure_slug()}),
            {"trip_slug": self.trip.slug},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"full", response.content)
        self._assert_nothing_written()

    def test_re_inviting_someone_already_on_a_full_trip_still_sends(self) -> None:
        from urbanlens.dashboard.services.messaging.direct_message_shares import invite_to_trip_in_message

        TripMembership.objects.filter(trip=self.trip).exclude(profile=self.sender).update(profile=self.recipient)
        invite_to_trip_in_message(self.sender, self.recipient, self.trip, "reminder")
        self.assertEqual(TripMembership.objects.filter(trip=self.trip).count(), 2)
        self.assertTrue(DirectMessage.objects.between(self.sender, self.recipient).exists())


class FriendAcceptRaceTests(TransactionTestCase):
    """G2-18: two accepts sharing a profile both read its friend count before either wrote."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        patch.start()
        self.addCleanup(patch.stop)
        baker.make(User)
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(max_friends_per_user=1)
        self.acceptor = _profile()
        self.requests = [
            Friendship.objects.create(
                from_profile=_profile(),
                to_profile=self.acceptor,
                status=FriendshipStatus.REQUESTED,
                relationship_type=FriendshipType.FRIEND,
                permissions=Permission.VIEW_PROFILE,
            )
            for _ in range(2)
        ]

    def test_the_cap_holds_against_two_accepts_at_once(self) -> None:
        barrier = threading.Barrier(2, timeout=3)
        real = Friendship.profile_at_max_friends
        with mock.patch.object(Friendship, "profile_at_max_friends", staticmethod(_barrier_after(real, barrier))):
            run_concurrently(
                [lambda pk=request.pk: Friendship.objects.get(pk=pk).accept() for request in self.requests]
            )

        friends = Friendship.objects.filter(to_profile=self.acceptor, status=FriendshipStatus.ACCEPTED).count()
        self.assertEqual(friends, 1, f"max_friends_per_user=1 but the profile now has {friends}")

    def test_an_already_answered_request_is_not_accepted_again(self) -> None:
        stale = Friendship.objects.get(pk=self.requests[0].pk)
        Friendship.objects.filter(pk=stale.pk).update(status=FriendshipStatus.DECLINED)
        self.assertFalse(stale.accept())
        self.assertEqual(Friendship.objects.get(pk=stale.pk).status, FriendshipStatus.DECLINED)


class BackupOverlapTests(TestCase):
    """G4-17: the admin button, the schedule and an OSError retry could each start a pg_dump at once."""

    def test_a_second_backup_waits_its_turn(self) -> None:
        from urbanlens.dashboard import tasks
        from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock

        token = acquire_lock(tasks.DATABASE_BACKUP_LOCK_KEY, 60)
        self.addCleanup(release_lock, tasks.DATABASE_BACKUP_LOCK_KEY, token)
        with mock.patch("urbanlens.core.controllers.backups.db.DatabaseBackup.run", return_value=True) as run:
            self.assertFalse(tasks._run_database_backup())
        run.assert_not_called()

    def test_the_lock_is_released_after_a_backup(self) -> None:
        from urbanlens.dashboard import tasks

        with (
            mock.patch("urbanlens.core.controllers.backups.db.DatabaseBackup.run", return_value=True),
            mock.patch("urbanlens.core.controllers.backups.db.DatabaseBackup.create_backup_dir"),
        ):
            self.assertTrue(tasks._run_database_backup())
            self.assertTrue(tasks._run_database_backup(), "the first backup never released its lock")


class WikiEditConflictTests(TestCase):
    """G4-28: an edit overwrote a newer value and recorded a stale "from"."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.editor = _profile()
        self.wiki = baker.make(
            "dashboard.Wiki", location=baker.make("dashboard.Location"), name="Old Mill", description="original"
        )

    def test_an_edit_over_a_value_written_mid_request_is_refused(self) -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.wiki.wiki_edits import WikiEditConflictError, apply_wiki_edit

        loaded = Wiki.objects.get(pk=self.wiki.pk)
        other = Wiki.objects.get(pk=self.wiki.pk)
        other.description = "someone else's"
        other.save(update_fields=["description", "updated"])

        with self.assertRaises(WikiEditConflictError) as caught:
            apply_wiki_edit(loaded, self.editor, {"description": "mine"})

        self.assertEqual(caught.exception.fields, ["description"])
        self.assertEqual(Wiki.objects.get(pk=self.wiki.pk).description, "someone else's")

    def test_an_untouched_field_changed_since_the_form_opened_is_a_conflict_not_a_revert(self) -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.wiki.wiki_edits import (
            WikiEditConflictError,
            apply_wiki_edit,
            wiki_revision_marker,
        )

        opened_at = wiki_revision_marker(self.wiki)
        renamer = Wiki.objects.get(pk=self.wiki.pk)
        renamer.name = "Renamed Mill"
        renamer.save(update_fields=["name", "updated"])

        at_submit = Wiki.objects.get(pk=self.wiki.pk)
        with self.assertRaises(WikiEditConflictError) as caught:
            # The form posts every field, so the untouched name arrives as the value it opened with.
            apply_wiki_edit(
                at_submit, self.editor, {"name": "Old Mill", "description": "mine"}, base_revision_id=opened_at
            )

        self.assertIn("name", caught.exception.fields)
        self.assertEqual(Wiki.objects.get(pk=self.wiki.pk).name, "Renamed Mill")

    def test_an_edit_with_a_current_baseline_goes_through(self) -> None:
        from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit, wiki_revision_marker

        edit = apply_wiki_edit(
            self.wiki, self.editor, {"description": "mine"}, base_revision_id=wiki_revision_marker(self.wiki)
        )
        self.assertIsNotNone(edit)
        self.assertEqual(edit.changes["description"]["from"], "original")

    def test_the_edit_view_answers_a_conflict_with_409(self) -> None:
        import json

        from django.urls import reverse

        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.wiki.wiki_edits import wiki_revision_marker

        opened_at = wiki_revision_marker(self.wiki)
        other = Wiki.objects.get(pk=self.wiki.pk)
        other.description = "someone else's"
        other.save(update_fields=["description", "updated"])

        self.client.force_login(self.editor.user)
        with mock.patch(
            "urbanlens.dashboard.controllers.location_wiki.resolve_visible_wiki",
            return_value=(self.wiki.location, Wiki.objects.get(pk=self.wiki.pk), self.editor),
        ):
            response = self.client.post(
                reverse("location.wiki.edit", args=[self.wiki.location.slug or "x"]),
                json.dumps({"description": "mine", "base_revision_id": opened_at}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["conflicts"], ["description"])
