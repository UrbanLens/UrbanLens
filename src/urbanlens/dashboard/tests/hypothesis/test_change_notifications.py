"""P197: "Trip Updated" and "Community Wiki Updated" notify, a burst of changes folding into one notification.

Both were settings with nothing behind them: switching them changed nothing, because nothing sent either type.
"""

from __future__ import annotations

from datetime import timedelta
import os
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.notifications.change_notifications import FOLD_WINDOW
from urbanlens.dashboard.services.social.friendship import block_profile, mute_profile
from urbanlens.dashboard.services.trips.trip_activities import create_activity, delete_activity
from urbanlens.dashboard.services.trips.trip_crud import create_trip, update_trip
from urbanlens.dashboard.services.wiki.articles import save_article
from urbanlens.dashboard.services.wiki.wiki_aliases import create_wiki_alias
from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit
from urbanlens.dashboard.tasks import announce_trip_change_task, announce_wiki_change_task

_CONCEALMENT = "urbanlens.dashboard.services.wiki.concealment.concealment_active"


def _profile(name: str) -> Profile:
    user = baker.make(User, username=f"{name}{os.urandom(3).hex()}")
    # Visible to everyone, so a name in a message is the actor's own and not a masked placeholder.
    Profile.objects.filter(user=user).update(profile_visibility=VisibilityChoice.ANYONE)
    return Profile.objects.select_related("user").get(user=user)


def _notifications(profile: Profile, notification_type: str) -> list[NotificationLog]:
    return list(NotificationLog.objects.filter(profile=profile, notification_type=notification_type).order_by("pk"))


def _prefer(profile: Profile, **choices: str) -> None:
    NotificationPreference.objects.update_or_create(profile=profile, defaults=choices)


class TripUpdatedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(tasks_run_inline(announce_trip_change_task, announce_wiki_change_task))
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.alice = _profile("alice")
        self.bob = _profile("bob")
        self.invitee = _profile("invitee")
        self.trip, _ = create_trip(self.alice, name="Mill run")
        TripMembership.objects.create(trip=self.trip, profile=self.bob, status=TripMembership.STATUS_JOINED)
        TripMembership.objects.create(trip=self.trip, profile=self.invitee, status=TripMembership.STATUS_INVITED)
        Trip.objects.filter(pk=self.trip.pk).update(
            allow_add_activities=Trip.PERM_EVERYONE, allow_edit_activities=Trip.PERM_EVERYONE
        )
        self.trip.refresh_from_db()

    def _add_activity(self, actor: Profile, title: str = "Boiler house") -> None:
        with self.captureOnCommitCallbacks(execute=True):
            create_activity(self.trip, actor, title=title)

    def test_a_member_hears_another_member_added_an_activity(self) -> None:
        self._add_activity(self.alice)

        [notification] = _notifications(self.bob, NotificationType.TRIP_UPDATED)
        self.assertIn("Mill run", notification.title)
        self.assertIn("added an activity", notification.message)
        self.assertEqual(notification.url, reverse("trips.detail", kwargs={"trip_slug": self.trip.slug}))

    def test_the_actor_and_a_member_not_yet_joined_hear_nothing(self) -> None:
        self._add_activity(self.alice)

        self.assertEqual(_notifications(self.alice, NotificationType.TRIP_UPDATED), [])
        self.assertEqual(_notifications(self.invitee, NotificationType.TRIP_UPDATED), [])

    def test_a_burst_of_changes_is_one_notification_with_a_count(self) -> None:
        self._add_activity(self.alice, "One")
        self._add_activity(self.alice, "Two")
        with self.captureOnCommitCallbacks(execute=True):
            update_trip(self.trip, self.alice, changes={"description": "Bring torches"})

        [notification] = _notifications(self.bob, NotificationType.TRIP_UPDATED)
        self.assertEqual(notification.fold_count, 3)
        self.assertIn("made 3 changes", notification.message)

    def test_a_change_after_the_last_was_read_is_a_new_notification(self) -> None:
        self._add_activity(self.alice, "One")
        NotificationLog.objects.filter(profile=self.bob).update(status=Status.READ)

        self._add_activity(self.alice, "Two")

        self.assertEqual(len(_notifications(self.bob, NotificationType.TRIP_UPDATED)), 2)

    def test_a_change_after_the_window_is_a_new_notification(self) -> None:
        self._add_activity(self.alice, "One")
        NotificationLog.objects.filter(profile=self.bob).update(
            updated=timezone.now() - FOLD_WINDOW - timedelta(minutes=1)
        )

        self._add_activity(self.alice, "Two")

        self.assertEqual(len(_notifications(self.bob, NotificationType.TRIP_UPDATED)), 2)

    def test_changes_by_two_members_do_not_fold_together(self) -> None:
        carol = _profile("carol")
        TripMembership.objects.create(trip=self.trip, profile=carol, status=TripMembership.STATUS_JOINED)

        self._add_activity(self.alice, "One")
        self._add_activity(self.bob, "Two")

        self.assertEqual(len(_notifications(carol, NotificationType.TRIP_UPDATED)), 2)

    def test_an_email_goes_once_per_burst(self) -> None:
        _prefer(self.bob, trip_updated=DeliveryPreference.BOTH)
        with mock.patch(
            "urbanlens.dashboard.services.notifications.notification_delivery.send_notification_email"
        ) as email:
            self._add_activity(self.alice, "One")
            self._add_activity(self.alice, "Two")

        self.assertEqual(email.call_count, 1)

    def test_a_member_who_switched_it_off_hears_nothing(self) -> None:
        _prefer(self.bob, trip_updated=DeliveryPreference.NONE)

        self._add_activity(self.alice)

        self.assertEqual(_notifications(self.bob, NotificationType.TRIP_UPDATED), [])

    def test_a_member_who_muted_the_actor_hears_nothing(self) -> None:
        Friendship.objects.create(from_profile=self.bob, to_profile=self.alice, status=FriendshipStatus.ACCEPTED)
        mute_profile(self.bob, self.alice)

        self._add_activity(self.alice)

        self.assertEqual(_notifications(self.bob, NotificationType.TRIP_UPDATED), [])

    def test_a_blocked_pair_hear_nothing_of_each_other(self) -> None:
        block_profile(self.bob, self.alice)

        self._add_activity(self.alice)

        self.assertEqual(_notifications(self.bob, NotificationType.TRIP_UPDATED), [])

    def test_saving_the_trip_unchanged_announces_nothing(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            update_trip(self.trip, self.alice, changes={"name": "Mill run"})

        self.assertEqual(_notifications(self.bob, NotificationType.TRIP_UPDATED), [])

    def test_removing_an_activity_is_announced(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            activity = create_activity(self.trip, self.bob, title="Gone soon")
        NotificationLog.objects.all().delete()

        with self.captureOnCommitCallbacks(execute=True):
            delete_activity(self.trip, self.alice, activity.pk)

        [notification] = _notifications(self.bob, NotificationType.TRIP_UPDATED)
        self.assertIn("removed an activity", notification.message)


class WikiUpdatedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(tasks_run_inline(announce_trip_change_task, announce_wiki_change_task))
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.alice = _profile("alice")
        self.bob = _profile("bob")
        self.stranger = _profile("stranger")
        self.location = baker.make(Location, latitude="41.7", longitude="-73.9")
        self.wiki = Wiki.objects.create(location=self.location, name="Powerhouse")
        for profile in (self.alice, self.bob):
            baker.make(Pin, profile=profile, location=self.location, parent_pin=None)
        baker.make(Pin, profile=self.stranger, location=baker.make(Location, latitude="40.0", longitude="-70.0"))

    def _edit(self, actor: Profile, **changes: str) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            apply_wiki_edit(self.wiki, actor, changes)

    def test_someone_who_pinned_the_place_hears_of_an_edit(self) -> None:
        self._edit(self.alice, description="Roof partly collapsed")

        [notification] = _notifications(self.bob, NotificationType.WIKI_UPDATED)
        self.assertIn("Powerhouse", notification.title)
        self.assertIn("edited the wiki's details", notification.message)
        self.assertEqual(notification.url, reverse("location.wiki", kwargs={"location_slug": self.location.slug}))

    def test_the_editor_and_someone_elsewhere_hear_nothing(self) -> None:
        self._edit(self.alice, description="Roof partly collapsed")

        self.assertEqual(_notifications(self.alice, NotificationType.WIKI_UPDATED), [])
        self.assertEqual(_notifications(self.stranger, NotificationType.WIKI_UPDATED), [])

    def test_a_pin_nested_under_another_does_not_follow_the_place(self) -> None:
        carol = _profile("carol")
        site = baker.make(Pin, profile=carol, location=baker.make(Location, latitude="41.0", longitude="-73.0"))
        baker.make(Pin, profile=carol, location=self.location, parent_pin=site)

        self._edit(self.alice, description="Roof partly collapsed")

        self.assertEqual(_notifications(carol, NotificationType.WIKI_UPDATED), [])

    def test_an_automated_edit_announces_nothing(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            WikiEdit.objects.create(
                wiki=self.wiki, editor=None, changes={"child_wiki_merged": {"from": None, "to": "x"}}
            )
            save_article(editor=None, content="Seeded from an encyclopedia.", wiki=self.wiki)

        self.assertEqual(_notifications(self.bob, NotificationType.WIKI_UPDATED), [])

    def test_an_article_edit_is_announced(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            save_article(editor=self.alice, content="The boiler house first.", wiki=self.wiki)

        [notification] = _notifications(self.bob, NotificationType.WIKI_UPDATED)
        self.assertIn("edited the article", notification.message)

    def test_a_new_name_for_the_place_is_announced(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            create_wiki_alias(self.wiki, self.alice, name="Building 33")

        [notification] = _notifications(self.bob, NotificationType.WIKI_UPDATED)
        self.assertIn("added another name for the place", notification.message)

    def test_an_owner_added_on_the_wiki_is_announced(self) -> None:
        self.client.force_login(self.alice.user)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("location.wiki.ownership", args=[self.location.slug]), {"name": "Heritage LLC"})

        [notification] = _notifications(self.bob, NotificationType.WIKI_UPDATED)
        self.assertIn("added an owner", notification.message)

    def test_edits_of_different_kinds_fold_into_one(self) -> None:
        self._edit(self.alice, description="Roof partly collapsed")
        with self.captureOnCommitCallbacks(execute=True):
            save_article(editor=self.alice, content="The boiler house first.", wiki=self.wiki)

        [notification] = _notifications(self.bob, NotificationType.WIKI_UPDATED)
        self.assertEqual(notification.fold_count, 2)


class WikiUpdatedConcealmentTests(TestCase):
    """A recipient under concealment hears only of what concealment would show them."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(tasks_run_inline(announce_trip_change_task, announce_wiki_change_task))
        baker.make(User)
        self.alice = _profile("alice")
        self.bob = _profile("bob")
        self.location = baker.make(Location, latitude="41.7", longitude="-73.9")
        self.wiki = Wiki.objects.create(location=self.location, name="Powerhouse")
        for profile in (self.alice, self.bob):
            baker.make(Pin, profile=profile, location=self.location, parent_pin=None)

    def _edit(self, **changes: str) -> None:
        with (
            mock.patch(_CONCEALMENT, side_effect=lambda _wiki, viewer: viewer is not None and viewer.pk == self.bob.pk),
            self.captureOnCommitCallbacks(execute=True),
        ):
            apply_wiki_edit(self.wiki, self.alice, changes)

    def test_an_edit_by_someone_concealment_hides_is_not_announced(self) -> None:
        self._edit(description="Roof partly collapsed")

        self.assertEqual(_notifications(self.bob, NotificationType.WIKI_UPDATED), [])

    def test_an_edit_by_a_friend_is_announced(self) -> None:
        Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=FriendshipStatus.ACCEPTED)

        self._edit(description="Roof partly collapsed")

        self.assertEqual(len(_notifications(self.bob, NotificationType.WIKI_UPDATED)), 1)

    def test_an_edit_only_of_fields_concealment_always_blanks_is_not_announced(self) -> None:
        Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=FriendshipStatus.ACCEPTED)

        self._edit(cameras="everywhere")

        self.assertEqual(_notifications(self.bob, NotificationType.WIKI_UPDATED), [])
