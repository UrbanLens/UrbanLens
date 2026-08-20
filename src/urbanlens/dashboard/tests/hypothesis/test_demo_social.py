"""The fabricated content of a demo account: friends, DMs, group chat, visits, trips, lists.

Runs the whole seeder with a real location pool, so every code path here -
comments on a shared wiki, trip activities on a pooled location, visits on a
real pin - actually executes, rather than short-circuiting on empty input the
way the plain smoke tests do.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.group_chats.model import GroupChatMembership, GroupMessage
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.demo.seeding import seed_demo_account


class DemoSocialContentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        locations = [baker.make(Location, google_place=None) for _ in range(6)]
        for location in locations:
            Wiki.objects.create(location=location, name=location.official_name or "Wiki", officially_created=True)
        with mock.patch("urbanlens.dashboard.services.demo.seeding.pool_locations", return_value=locations):
            self.owner = seed_demo_account().profile

    def test_the_owner_is_friends_with_every_persona(self) -> None:
        accepted = Friendship.objects.filter(from_profile=self.owner, status=FriendshipStatus.ACCEPTED).count()
        self.assertEqual(accepted, 4)

    def test_wiki_comments_exist_on_a_shared_location(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        self.assertGreater(Comment.objects.filter(wiki__isnull=False).count(), 0)

    def test_direct_messages_are_plaintext(self) -> None:
        messages = DirectMessage.objects.filter(sender=self.owner)
        self.assertGreater(messages.count(), 0)
        for message in messages:
            self.assertNotEqual(message.body, "")
            self.assertEqual(message.ciphertext, "")

    def test_the_group_chat_has_memberships_older_than_its_messages(self) -> None:
        oldest_membership = GroupChatMembership.objects.filter(profile=self.owner).earliest("created")
        earliest_message = GroupMessage.objects.filter(group=oldest_membership.group).earliest("created")
        self.assertLessEqual(oldest_membership.created, earliest_message.created)

    def test_visits_carry_a_real_visited_at_and_update_the_pin(self) -> None:
        visit = PinVisit.objects.filter(pin__profile=self.owner).order_by("-visited_at").first()
        self.assertIsNotNone(visit)
        # sync_last_visited sets the pin's last_visited to its *newest* visit;
        # this is the newest, so they should agree exactly.
        self.assertEqual(visit.pin.last_visited, visit.visited_at)

    def test_a_trip_exists_with_activities_on_pooled_locations(self) -> None:
        trip = Trip.objects.filter(creator=self.owner).first()
        self.assertIsNotNone(trip)
        self.assertGreater(TripActivity.objects.filter(trip=trip).count(), 0)

    def test_pin_lists_cover_the_owners_pins(self) -> None:
        lists = PinList.objects.filter(profile=self.owner)
        self.assertGreaterEqual(lists.count(), 1)
        self.assertGreater(sum(pin_list.items.count() for pin_list in lists), 0)

    def test_nothing_here_writes_a_notification_directly(self) -> None:
        """The choke point (bin/check_notification_choke_point.py) is the real guard;
        this just confirms the module this test covers writes none at all."""
        from urbanlens.dashboard.models.notifications.model import NotificationLog

        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_photos_are_real_local_files_not_external_hotlinks(self) -> None:
        from urbanlens.dashboard.models.images.model import Image

        photo = Image.objects.filter(profile=self.owner).first()
        self.assertIsNotNone(photo)
        self.assertTrue(photo.image.name)
        self.assertEqual(photo.source, "upload")
        # A real file was actually written to storage, not just a DB row
        # naming one - open() raises if the storage backend has nothing there.
        with photo.image.open("rb") as handle:
            self.assertGreater(len(handle.read()), 0)
