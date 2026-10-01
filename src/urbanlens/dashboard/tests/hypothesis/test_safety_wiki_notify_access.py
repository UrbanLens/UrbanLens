"""A safety check-in can only post to, or link, a community wiki its owner (or viewer) could already open."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.visits.safety import (
    apply_checkin_edit,
    create_checkin,
    escalate_checkin,
    find_visible_community_wiki,
    post_checkin_to_community_wiki,
)

#: Coordinates no other fixture in the suite uses.
LAT = 44.1873
LNG = -71.6112
FAR_LAT = LAT + 3.0


def _profile() -> Profile:
    return Profile.objects.get(user=baker.make(User))


def _wiki_at(latitude: float = LAT, longitude: float = LNG, name: str = "Hidden Quarry") -> Wiki:
    location = baker.make(Location, latitude=latitude, longitude=longitude)
    return baker.make(Wiki, location=location, name=name)


def _pin(profile: Profile, wiki: Wiki) -> None:
    baker.make(Pin, profile=profile, location=wiki.location, parent_pin=None)


def _create(profile: Profile, *, latitude: float = LAT, notify: bool = True) -> SafetyCheckin:
    return create_checkin(
        profile=profile,
        title="Solo trip",
        checkin_by=timezone.now() + datetime.timedelta(hours=4),
        grace_period=datetime.timedelta(hours=1),
        destination_latitude=latitude,
        destination_longitude=LNG,
        notify_community_wiki=notify,
    )


def _overdue(profile: Profile, **kwargs) -> SafetyCheckin:
    defaults = {
        "profile": profile,
        "title": "Solo trip",
        "checkin_by": timezone.now() - datetime.timedelta(hours=2),
        "grace_period": datetime.timedelta(hours=1),
        "destination_latitude": str(LAT),
        "destination_longitude": str(LNG),
        "status": SafetyCheckinStatus.AWAITING_CHECKIN,
        "notify_community_wiki": True,
    }
    defaults.update(kwargs)
    return baker.make(SafetyCheckin, **defaults)


class CreateCheckinWikiFlagTests(TestCase):
    def test_an_owner_who_cannot_see_the_wiki_cannot_opt_into_it(self) -> None:
        _wiki_at()
        owner = _profile()

        checkin = _create(owner)

        self.assertFalse(checkin.notify_community_wiki)

    def test_an_owner_with_access_keeps_the_flag(self) -> None:
        wiki = _wiki_at()
        owner = _profile()
        _pin(owner, wiki)

        self.assertTrue(_create(owner).notify_community_wiki)


class EditCheckinWikiFlagTests(TestCase):
    def test_setting_the_flag_without_access_is_refused(self) -> None:
        _wiki_at()
        owner = _profile()
        checkin = _create(owner, notify=False)

        outcome = apply_checkin_edit(checkin, editor=owner, notify_community_wiki=True)

        self.assertFalse(checkin.notify_community_wiki)
        self.assertTrue(outcome.warnings)

    def test_the_refusal_reads_the_same_as_for_empty_space(self) -> None:
        _wiki_at()
        owner = _profile()
        hidden = _create(owner, notify=False)
        hidden_warnings = apply_checkin_edit(hidden, editor=owner, notify_community_wiki=True).warnings
        SafetyCheckin.objects.filter(pk=hidden.pk).update(status=SafetyCheckinStatus.CANCELLED)
        empty = _create(owner, latitude=FAR_LAT, notify=False)

        empty_warnings = apply_checkin_edit(empty, editor=owner, notify_community_wiki=True).warnings

        self.assertEqual(hidden_warnings, empty_warnings)

    def test_moving_the_destination_onto_a_hidden_wiki_drops_the_flag(self) -> None:
        visible = _wiki_at(latitude=FAR_LAT, name="Known Mill")
        _wiki_at()
        owner = _profile()
        _pin(owner, visible)
        checkin = _create(owner, latitude=FAR_LAT)
        self.assertTrue(checkin.notify_community_wiki)

        apply_checkin_edit(checkin, editor=owner, destination=(LAT, LNG))

        self.assertFalse(checkin.notify_community_wiki)

    def test_an_owner_with_access_can_set_the_flag(self) -> None:
        wiki = _wiki_at()
        owner = _profile()
        _pin(owner, wiki)
        checkin = _create(owner, notify=False)

        outcome = apply_checkin_edit(checkin, editor=owner, notify_community_wiki=True)

        self.assertTrue(checkin.notify_community_wiki)
        self.assertEqual(outcome.warnings, [])


class PostToCommunityWikiTests(TestCase):
    def setUp(self) -> None:
        self.wiki = _wiki_at()
        self.member = _profile()
        _pin(self.member, self.wiki)
        self.owner = _profile()

    def _assert_nothing_posted(self, checkin: SafetyCheckin) -> None:
        checkin.refresh_from_db()
        self.assertIsNone(checkin.wiki_notified_at)
        self.assertFalse(Comment.objects.filter(wiki=self.wiki).exists())
        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.member, notification_type=NotificationType.WIKI_SAFETY_CHECKIN
            ).exists()
        )

    def test_a_stored_flag_does_not_let_an_outsider_post_or_message_members(self) -> None:
        checkin = _overdue(self.owner)

        escalate_checkin(checkin)

        self._assert_nothing_posted(checkin)

    def test_access_lost_after_opting_in_stops_the_post(self) -> None:
        pin = baker.make(Pin, profile=self.owner, location=self.wiki.location, parent_pin=None)
        checkin = _overdue(self.owner)
        pin.delete()

        post_checkin_to_community_wiki(checkin)

        self._assert_nothing_posted(checkin)

    def test_an_owner_with_access_still_posts(self) -> None:
        _pin(self.owner, self.wiki)
        checkin = _overdue(self.owner)

        escalate_checkin(checkin)

        checkin.refresh_from_db()
        self.assertIsNotNone(checkin.wiki_notified_at)
        self.assertTrue(Comment.objects.filter(wiki=self.wiki, profile=self.owner).exists())


class FindVisibleCommunityWikiTests(TestCase):
    def test_a_hidden_wiki_at_the_same_point_does_not_mask_a_visible_one(self) -> None:
        _wiki_at(name="Hidden First")
        visible = _wiki_at(latitude=LAT + 0.0001, name="Known Second")
        owner = _profile()
        _pin(owner, visible)

        self.assertEqual(find_visible_community_wiki(LAT, LNG, owner), visible)

    def test_nothing_for_a_profile_without_access(self) -> None:
        _wiki_at()

        self.assertIsNone(find_visible_community_wiki(LAT, LNG, _profile()))


class CommunityStatusWikiLinkTests(TestCase):
    def setUp(self) -> None:
        self.wiki = _wiki_at()
        owner = _profile()
        _pin(owner, self.wiki)
        self.checkin = _overdue(
            owner, status=SafetyCheckinStatus.OVERDUE, escalated_at=timezone.now(), wiki_notified_at=timezone.now()
        )
        self.url = reverse("safety.checkin.detail", kwargs={"checkin_slug": str(self.checkin.uuid)})

    def test_a_viewer_without_access_is_not_shown_the_wiki(self) -> None:
        self.client.force_login(_profile().user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Hidden Quarry")
        self.assertIsNone(response.context["wiki"])

    def test_a_viewer_with_access_gets_the_link(self) -> None:
        viewer = _profile()
        _pin(viewer, self.wiki)
        self.client.force_login(viewer.user)

        response = self.client.get(self.url)

        self.assertContains(response, "Hidden Quarry")


class PartnerDetailWikiTests(TestCase):
    def setUp(self) -> None:
        from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner, SafetyCheckinPartnerStatus

        self.wiki = _wiki_at()
        owner = _profile()
        _pin(owner, self.wiki)
        checkin = _create(owner)
        self.partner = _profile()
        SafetyCheckinPartner.objects.create(
            checkin=checkin, profile=self.partner, invited_by=owner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        self.url = reverse("safety.checkin.detail", kwargs={"checkin_slug": str(checkin.uuid)})

    def test_a_partner_without_access_is_not_shown_the_owners_wiki(self) -> None:
        self.client.force_login(self.partner.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["viewer_is_partner"])
        self.assertIsNone(response.context["destination_wiki"])
        self.assertNotContains(response, "Hidden Quarry")

    def test_a_partner_with_access_sees_it(self) -> None:
        _pin(self.partner, self.wiki)
        self.client.force_login(self.partner.user)

        response = self.client.get(self.url)

        self.assertEqual(response.context["destination_wiki"], self.wiki)
