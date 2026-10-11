"""Before an incident a check-in shows to no contact, only to partners the owner chose; a contact sees it once alerted.

The safety check-in goal: contacts get the trip plan "only if the user fails to check in on time", and earlier access
"must be explicitly chosen and consent-focused" - the accepted partner tier.
"""

from __future__ import annotations

import datetime
from pathlib import Path
import shutil
import tempfile

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
    SafetyCheckinStatus,
)

_IMAGE_BYTES = b"fake-image-bytes-for-preincident-visibility"


def _verified(username: str, email: str) -> User:
    user = baker.make(User, username=username, email=email, is_active=True)
    user.profile.verified_primary_email = user.profile.primary_email_normalized
    user.profile.save(update_fields=["verified_primary_email"])
    return user


class _VisibilityTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner = baker.make(User, username="hiker").profile
        self.friend_user = baker.make(User, username="friend")
        self.member_user = _verified("member", "member@example.com")
        self.checkin = baker.make(
            SafetyCheckin,
            profile=self.owner,
            title="Secret tunnel walk",
            checkin_by=timezone.now() + datetime.timedelta(hours=3),
            grace_period=datetime.timedelta(hours=1),
            status=SafetyCheckinStatus.SCHEDULED,
            notify_community_wiki=False,
        )
        self.by_account = baker.make(
            SafetyCheckinContact, checkin=self.checkin, contact_profile=self.friend_user.profile, email=None
        )
        self.by_email = baker.make(
            SafetyCheckinContact, checkin=self.checkin, contact_profile=None, email="member@example.com"
        )

    def _alert(self, *contacts: SafetyCheckinContact) -> None:
        now = timezone.now()
        SafetyCheckinContact.objects.filter(pk__in=[contact.pk for contact in contacts]).update(notified_at=now)
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(escalated_at=now, status=SafetyCheckinStatus.OVERDUE)

    def _shared_titles(self, user: User) -> list[str]:
        self.client.force_login(user)
        response = self.client.get(reverse("safety.home"))
        self.assertEqual(response.status_code, 200)
        return [checkin.title for checkin in response.context["shared_checkins"]]

    def _status_page(self, user: User) -> int:
        self.client.force_login(user)
        return self.client.get(
            reverse("safety.checkin.detail", kwargs={"checkin_slug": str(self.checkin.uuid)})
        ).status_code


class SharedWithYouTests(_VisibilityTestCase):
    def test_a_contact_sees_nothing_of_an_upcoming_checkin(self) -> None:
        self.assertEqual(self._shared_titles(self.friend_user), [])
        self.assertEqual(self._shared_titles(self.member_user), [])

    def test_a_contact_sees_it_once_alerted(self) -> None:
        self._alert(self.by_account, self.by_email)

        self.assertEqual(self._shared_titles(self.friend_user), ["Secret tunnel walk"])
        self.assertEqual(self._shared_titles(self.member_user), ["Secret tunnel walk"])

    def test_a_contact_an_interrupted_escalation_never_reached_still_sees_nothing(self) -> None:
        self._alert(self.by_email)

        self.assertEqual(self._shared_titles(self.friend_user), [])

    def test_the_rendered_page_does_not_name_it_before_an_incident(self) -> None:
        self.client.force_login(self.friend_user)

        self.assertNotIn("Secret tunnel walk", self.client.get(reverse("safety.home")).content.decode())

    def test_an_accepted_partner_still_sees_it_before_an_incident(self) -> None:
        partner_user = baker.make(User, username="partner")
        baker.make(
            SafetyCheckinPartner,
            checkin=self.checkin,
            profile=partner_user.profile,
            invited_by=self.owner,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )
        self.client.force_login(partner_user)

        response = self.client.get(reverse("safety.home"))

        self.assertEqual([checkin.title for checkin in response.context["partnered_checkins"]], ["Secret tunnel walk"])


class SharedStatusPageTests(_VisibilityTestCase):
    def test_a_contact_cannot_open_it_before_an_incident(self) -> None:
        self.assertEqual(self._status_page(self.friend_user), 404)
        self.assertEqual(self._status_page(self.member_user), 404)

    def test_a_contact_can_open_it_once_alerted(self) -> None:
        self._alert(self.by_account, self.by_email)

        self.assertEqual(self._status_page(self.friend_user), 200)
        self.assertEqual(self._status_page(self.member_user), 200)


class CheckinPhotoVisibilityTests(_VisibilityTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.image = baker.make(
            Image,
            image="pin_images/preincident.png",
            profile=self.owner,
            safety_checkin=self.checkin,
            pending_scan=False,
        )

    def _visible(self, user: User) -> bool:
        return Image.objects.visible_to(user.profile).filter(pk=self.image.pk).exists()

    def test_a_contact_cannot_see_its_photos_before_an_incident(self) -> None:
        self.assertFalse(self._visible(self.friend_user))
        self.assertFalse(self._visible(self.member_user))

    def test_a_contact_can_see_them_once_alerted(self) -> None:
        self._alert(self.by_account, self.by_email)

        self.assertTrue(self._visible(self.friend_user))
        self.assertTrue(self._visible(self.member_user))


class ContactTokenBeforeAlertTests(_VisibilityTestCase):
    """A token is emailed only with an alert, so one that works earlier was leaked or guessed: it must reveal and do nothing."""

    def setUp(self) -> None:
        super().setUp()
        self._media_root = tempfile.mkdtemp(prefix="ul_preincident_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)
        (Path(self._media_root) / "pin_images").mkdir(parents=True)
        (Path(self._media_root) / "pin_images" / "tok.png").write_bytes(_IMAGE_BYTES)
        self.image = baker.make(
            Image, image="pin_images/tok.png", profile=self.owner, safety_checkin=self.checkin, pending_scan=False
        )
        self.token = self.by_email.token

    def _statuses(self) -> dict[str, int]:
        return {
            "portal": self.client.get(reverse("safety.contact.portal", args=[self.token])).status_code,
            "photo": self.client.get(reverse("safety.contact.photo", args=[self.token, self.image.pk])).status_code,
            "markup": self.client.get(reverse("safety.contact.markup.json", args=[self.token])).status_code,
            "optout": self.client.get(reverse("safety.contact.optout", args=[self.token, "global"])).status_code,
            "message": self.client.post(
                reverse("safety.contact.messages", args=[self.token]), {"body": "hello"}
            ).status_code,
        }

    def test_every_token_route_is_a_404_before_the_alert(self) -> None:
        self.assertEqual(set(self._statuses().values()), {404})

    def test_a_leaked_token_cannot_mark_the_owner_safe_and_head_off_escalation(self) -> None:
        response = self.client.post(reverse("safety.contact.mark_safe", args=[self.token]))

        self.assertEqual(response.status_code, 404)
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.status, SafetyCheckinStatus.SCHEDULED)

    def test_the_token_works_once_its_contact_is_alerted(self) -> None:
        self._alert(self.by_email)

        statuses = self._statuses()

        self.assertEqual(statuses["portal"], 200)
        self.assertEqual(statuses["photo"], 200)
        self.assertEqual(statuses["markup"], 200)
        self.assertEqual(statuses["optout"], 200)

    def test_an_alerted_contact_s_token_keeps_working_after_resolution(self) -> None:
        self._alert(self.by_email)
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(
            status=SafetyCheckinStatus.CHECKED_IN, resolved_at=timezone.now()
        )

        self.assertEqual(self.client.get(reverse("safety.contact.portal", args=[self.token])).status_code, 200)
