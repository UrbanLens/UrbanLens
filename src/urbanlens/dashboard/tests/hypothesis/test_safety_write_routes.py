"""Safety check-in routes that grant or revoke access, conclude a check-in, or delete its photos."""

from __future__ import annotations

import datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
    SafetyCheckinStatus,
    SafetyContactOptOut,
)


def _profile() -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(profile_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    return profile


def _checkin(profile: Profile, **kwargs) -> SafetyCheckin:
    defaults = {
        "profile": profile,
        "title": "Test hike",
        "checkin_by": timezone.now() + datetime.timedelta(hours=2),
        "grace_period": datetime.timedelta(hours=1),
    }
    defaults.update(kwargs)
    return baker.make(SafetyCheckin, **defaults)


class _SafetyFixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion
        self.owner = _profile()
        self.partner = _profile()
        self.stranger = _profile()
        self.checkin = _checkin(self.owner)

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])

    def _status(self) -> str:
        self.checkin.refresh_from_db()
        return self.checkin.status


class SafetyPartnerInviteRouteTests(_SafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("safety.checkin.partners", args=[self.checkin.slug])

    def test_owner_invites_a_partner_by_username(self) -> None:
        self.client.force_login(self.owner.user)

        response = self.client.post(self.url, {"username": self.partner.user.username})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(SafetyCheckinPartner.objects.filter(checkin=self.checkin).values_list("profile_id", "status")),
            [(self.partner.pk, SafetyCheckinPartnerStatus.INVITED)],
        )

    def test_a_stranger_cannot_add_themselves_to_someone_elses_checkin(self) -> None:
        self.client.force_login(self.stranger.user)

        response = self.client.post(self.url, {"username": self.stranger.user.username})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(SafetyCheckinPartner.objects.filter(checkin=self.checkin).exists())

    def test_an_accepted_partner_cannot_invite_further_partners(self) -> None:
        baker.make(
            SafetyCheckinPartner, checkin=self.checkin, profile=self.partner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        self.client.force_login(self.partner.user)

        response = self.client.post(self.url, {"username": self.stranger.user.username})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(SafetyCheckinPartner.objects.filter(checkin=self.checkin, profile=self.stranger).exists())

    def test_blank_and_self_invites_create_nothing(self) -> None:
        self.client.force_login(self.owner.user)

        for username in ("", self.owner.user.username):
            response = self.client.post(self.url, {"username": username})
            self.assertEqual(response.status_code, 200)

        self.assertFalse(SafetyCheckinPartner.objects.filter(checkin=self.checkin).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"username": self.partner.user.username}))
        self.assertFalse(SafetyCheckinPartner.objects.filter(checkin=self.checkin).exists())


class SafetyPartnerRemoveRouteTests(_SafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.row = baker.make(
            SafetyCheckinPartner, checkin=self.checkin, profile=self.partner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        self.url = reverse("safety.checkin.partners.remove", args=[self.checkin.slug, self.row.pk])

    def test_owner_removes_the_partner(self) -> None:
        self.client.force_login(self.owner.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=self.row.pk).exists())

    def test_the_partner_cannot_use_the_owners_remove_route(self) -> None:
        self.client.force_login(self.partner.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(SafetyCheckinPartner.objects.filter(pk=self.row.pk).exists())

    def test_a_partner_row_of_another_checkin_is_not_removed_through_this_one(self) -> None:
        other_checkin = _checkin(self.stranger)
        other_row = baker.make(SafetyCheckinPartner, checkin=other_checkin, profile=self.partner)
        self.client.force_login(self.owner.user)

        response = self.client.post(reverse("safety.checkin.partners.remove", args=[self.checkin.slug, other_row.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(SafetyCheckinPartner.objects.filter(pk=other_row.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertTrue(SafetyCheckinPartner.objects.filter(pk=self.row.pk).exists())


class SafetyPartnerInviteAnswerRouteTests(_SafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.row = baker.make(SafetyCheckinPartner, checkin=self.checkin, profile=self.partner, invited_by=self.owner)

    def _row_status(self) -> str | None:
        return SafetyCheckinPartner.objects.filter(pk=self.row.pk).values_list("status", flat=True).first()

    def test_the_invitee_accepts(self) -> None:
        self.client.force_login(self.partner.user)

        response = self.client.post(reverse("safety.partner.accept", args=[self.checkin.uuid]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._row_status(), SafetyCheckinPartnerStatus.ACCEPTED)

    def test_the_invitee_declines_and_the_row_is_gone(self) -> None:
        self.client.force_login(self.partner.user)

        response = self.client.post(reverse("safety.partner.decline", args=[self.checkin.uuid]))

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self._row_status())

    def test_someone_who_was_not_invited_cannot_accept_or_decline(self) -> None:
        self.client.force_login(self.stranger.user)

        for name in ("safety.partner.accept", "safety.partner.decline"):
            response = self.client.post(reverse(name, args=[self.checkin.uuid]))
            self.assertEqual(response.status_code, 404, name)

        self.assertEqual(self._row_status(), SafetyCheckinPartnerStatus.INVITED)
        self.assertFalse(SafetyCheckinPartner.objects.filter(checkin=self.checkin, profile=self.stranger).exists())

    def test_the_owner_cannot_accept_on_the_invitees_behalf(self) -> None:
        self.client.force_login(self.owner.user)

        response = self.client.post(reverse("safety.partner.accept", args=[self.checkin.uuid]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._row_status(), SafetyCheckinPartnerStatus.INVITED)

    def test_anonymous_is_redirected_to_login(self) -> None:
        for name in ("safety.partner.accept", "safety.partner.decline"):
            self.assert_login_redirect(self.client.post(reverse(name, args=[self.checkin.uuid])))

        self.assertEqual(self._row_status(), SafetyCheckinPartnerStatus.INVITED)


class SafetyPartnerMarkSafeRouteTests(_SafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.checkin.status = SafetyCheckinStatus.OVERDUE
        self.checkin.save()
        self.url = reverse("safety.partner.mark_safe", args=[self.checkin.uuid])

    def test_an_accepted_partner_marks_the_owner_safe(self) -> None:
        baker.make(
            SafetyCheckinPartner, checkin=self.checkin, profile=self.partner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        self.client.force_login(self.partner.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status(), SafetyCheckinStatus.FOUND_SAFE)

    def test_a_partner_who_only_was_invited_cannot(self) -> None:
        baker.make(
            SafetyCheckinPartner, checkin=self.checkin, profile=self.partner, status=SafetyCheckinPartnerStatus.INVITED
        )
        self.client.force_login(self.partner.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._status(), SafetyCheckinStatus.OVERDUE)

    def test_a_stranger_cannot(self) -> None:
        self.client.force_login(self.stranger.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._status(), SafetyCheckinStatus.OVERDUE)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertEqual(self._status(), SafetyCheckinStatus.OVERDUE)


class SafetyCheckinCancelRouteTests(_SafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("safety.checkin.cancel", args=[self.checkin.uuid])

    def test_owner_cancels(self) -> None:
        self.client.force_login(self.owner.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status(), SafetyCheckinStatus.CANCELLED)

    def test_an_accepted_partner_cannot_cancel(self) -> None:
        baker.make(
            SafetyCheckinPartner, checkin=self.checkin, profile=self.partner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        self.client.force_login(self.partner.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(self._status(), SafetyCheckinStatus.CANCELLED)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertNotEqual(self._status(), SafetyCheckinStatus.CANCELLED)


class SafetyLocationSharingToggleRouteTests(_SafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(
            live_location_sharing_enabled=True, live_latitude=42.5, live_longitude=-73.5
        )
        self.url = reverse("safety.checkin.location.toggle", args=[self.checkin.slug])

    def test_owner_turning_sharing_off_also_clears_the_last_position(self) -> None:
        self.client.force_login(self.owner.user)

        response = self.client.post(self.url, {"enabled": "0"})

        self.assertEqual(response.status_code, 200)
        self.checkin.refresh_from_db()
        self.assertFalse(self.checkin.live_location_sharing_enabled)
        self.assertIsNone(self.checkin.live_latitude)
        self.assertIsNone(self.checkin.live_longitude)

    def test_an_accepted_partner_cannot_toggle_the_owners_sharing(self) -> None:
        baker.make(
            SafetyCheckinPartner, checkin=self.checkin, profile=self.partner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        self.client.force_login(self.partner.user)

        response = self.client.post(self.url, {"enabled": "0"})

        self.assertEqual(response.status_code, 404)
        self.checkin.refresh_from_db()
        self.assertTrue(self.checkin.live_location_sharing_enabled)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"enabled": "0"}))
        self.checkin.refresh_from_db()
        self.assertTrue(self.checkin.live_location_sharing_enabled)


class SafetyGalleryImageRouteTests(_SafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.image = baker.make(Image, profile=self.owner, safety_checkin=self.checkin, latitude=1, longitude=1)
        self.image.image.save("photo.jpg", ContentFile(b"not-a-real-jpeg"), save=True)
        self.url = reverse("safety.checkin.gallery.image", args=[self.checkin.slug, self.image.pk])

    def test_owner_deletes_the_photo(self) -> None:
        self.client.force_login(self.owner.user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())

    def test_owner_repositions_the_photo(self) -> None:
        self.client.force_login(self.owner.user)

        response = self.client.post(self.url, {"latitude": 42.5, "longitude": -73.5}, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.image.refresh_from_db()
        self.assertAlmostEqual(float(self.image.latitude), 42.5)

    def test_an_accepted_partner_cannot_delete_or_move_the_owners_photo(self) -> None:
        baker.make(
            SafetyCheckinPartner, checkin=self.checkin, profile=self.partner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        self.client.force_login(self.partner.user)

        self.assertEqual(self.client.delete(self.url).status_code, 404)
        moved = self.client.post(self.url, {"latitude": 42.5, "longitude": -73.5}, content_type="application/json")

        self.assertEqual(moved.status_code, 404)
        self.image.refresh_from_db()
        self.assertEqual(float(self.image.latitude), 1)

    def test_malformed_coordinates_are_a_400(self) -> None:
        self.client.force_login(self.owner.user)

        for body in ("not json", "[]", '{"latitude": "north", "longitude": 1}', '{"latitude": 999, "longitude": 1}'):
            response = self.client.post(self.url, data=body, content_type="application/json")
            self.assertEqual(response.status_code, 400, body)

        self.image.refresh_from_db()
        self.assertEqual(float(self.image.latitude), 1)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.delete(self.url))
        self.assertTrue(Image.objects.filter(pk=self.image.pk).exists())


class SafetyMapDetachRouteTests(_SafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.map = baker.make(MarkupMap, profile=self.owner)
        self.checkin.markup_maps.add(self.map)
        self.url = reverse("safety.checkin.maps.detach", args=[self.checkin.slug, self.map.uuid])

    def test_owner_detaches_the_map_and_the_map_survives(self) -> None:
        self.client.force_login(self.owner.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.checkin.markup_maps.filter(pk=self.map.pk).exists())
        self.assertTrue(MarkupMap.objects.filter(pk=self.map.pk).exists())

    def test_a_stranger_gets_404_and_the_map_stays_attached(self) -> None:
        self.client.force_login(self.stranger.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(self.checkin.markup_maps.filter(pk=self.map.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertTrue(self.checkin.markup_maps.filter(pk=self.map.pk).exists())


class SafetyContactTokenRouteTests(_SafetyFixture):
    """Contacts have no account; the token in the URL is the credential."""

    def setUp(self) -> None:
        super().setUp()
        self.checkin.status = SafetyCheckinStatus.OVERDUE
        self.checkin.save()
        self.contact = baker.make(
            SafetyCheckinContact, checkin=self.checkin, email="friend@example.com", contact_profile=None
        )

    def test_the_token_holder_marks_the_owner_safe_without_logging_in(self) -> None:
        response = self.client.post(reverse("safety.contact.mark_safe", args=[self.contact.token]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status(), SafetyCheckinStatus.FOUND_SAFE)
        self.contact.refresh_from_db()
        self.assertIsNotNone(self.contact.found_safe_at)

    def test_an_unknown_token_is_404_and_nothing_resolves(self) -> None:
        response = self.client.post(reverse("safety.contact.mark_safe", args=["00000000-0000-4000-8000-000000000000"]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._status(), SafetyCheckinStatus.OVERDUE)

    def test_the_token_holder_opts_out_of_this_checkin(self) -> None:
        response = self.client.post(reverse("safety.contact.optout", args=[self.contact.token, "checkin"]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            SafetyContactOptOut.objects.filter(
                email="friend@example.com", scope="checkin", checkin=self.checkin
            ).exists()
        )

    def test_opt_out_with_an_unknown_scope_or_token_is_404_and_records_nothing(self) -> None:
        bad_scope = self.client.post(reverse("safety.contact.optout", args=[self.contact.token, "everything"]))
        bad_token = self.client.post(
            reverse("safety.contact.optout", args=["00000000-0000-4000-8000-000000000000", "global"])
        )

        self.assertEqual(bad_scope.status_code, 404)
        self.assertEqual(bad_token.status_code, 404)
        self.assertFalse(SafetyContactOptOut.objects.exists())

    def test_a_get_of_the_opt_out_link_confirms_rather_than_opting_out(self) -> None:
        response = self.client.get(reverse("safety.contact.optout", args=[self.contact.token, "global"]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SafetyContactOptOut.objects.exists())
