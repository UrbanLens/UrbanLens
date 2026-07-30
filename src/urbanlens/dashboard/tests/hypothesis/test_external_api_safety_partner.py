"""Tests for the external API's partner-facing safety endpoints.

Three things were previously web-only, and each left the API able to reach a
state it had no way to leave:

* an invited partner could be created through the API but could only *answer*
  in a browser;
* a partner could not see the check-ins they had accepted;
* a partner could not mark the owner safe - the single most useful thing a
  watcher does, and the one that stops an escalation that runs on a five-minute
  beat.

The security property under nearly every test here is the same one: a partner
row is created in the ``INVITED`` state and only becomes authority when
``ACCEPTED``. Every read on this surface is a person's plan, destination and
companions; the invitation-answering endpoints are additionally scoped to the
caller's *own* row, because a lookup keyed only by check-in uuid would let
anyone walk other people's invitations - and an invitation names both a
check-in and the person out on it.
"""

from __future__ import annotations

import datetime
import uuid as uuid_module

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinPartner, SafetyCheckinPartnerStatus, SafetyCheckinStatus
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.safety import create_checkin


def _bearer(raw_key: str) -> dict:
    """Build the auth header kwargs for a bearer-key request.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Extra kwargs for ``self.client``.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _SafetyPartnerTestCase(TestCase):
    """Shared setup: an owner with a live check-in, and a profile invited to watch it."""

    def setUp(self) -> None:
        """Create the cast, issue keys, and invite the watcher."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.owner_user = baker.make(User, username="explorer")
        self.owner = Profile.objects.get(user=self.owner_user)
        self.watcher_user = baker.make(User, username="watcher")
        self.watcher = Profile.objects.get(user=self.watcher_user)

        self.owner_key = self._issue_key(self.owner_user)
        self.watcher_key = self._issue_key(self.watcher_user)

        self.checkin = create_checkin(
            profile=self.owner,
            title="Quarry trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=6),
            grace_period=datetime.timedelta(hours=1),
            plan_details="North rim, back by dark",
            contact_message="Please call me",
            contacts=[(None, "friend@example.com", "Friend")],
        )
        self.partner = SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=self.watcher, invited_by=self.owner)

        self.invites_url = reverse("external_api:safety.partner_invites")
        self.accept_url = reverse("external_api:safety.partner_invites.accept", kwargs={"checkin_uuid": self.checkin.uuid})
        self.decline_url = reverse("external_api:safety.partner_invites.decline", kwargs={"checkin_uuid": self.checkin.uuid})
        self.partner_list_url = reverse("external_api:safety.partner_checkins")
        self.partner_detail_url = reverse("external_api:safety.partner_checkins.detail", kwargs={"checkin_uuid": self.checkin.uuid})
        self.mark_safe_url = reverse("external_api:safety.partner_checkins.mark_safe", kwargs={"checkin_uuid": self.checkin.uuid})

    def _issue_key(self, user: User, scopes: list[str] | None = None) -> str:
        """Issue an API key for *user*.

        Args:
            user: The key's owner.
            scopes: Scope values to grant, defaulting to safety read + write.

        Returns:
            The plaintext key.
        """
        key, raw = generate_api_key(user, "Test")
        # scopes is editable=False, so it is set directly rather than through a
        # form. The default grant deliberately excludes safety:*.
        ApiKey.objects.filter(pk=key.pk).update(scopes=scopes or [ApiKeyScope.SAFETY_READ.value, ApiKeyScope.SAFETY_WRITE.value])
        return raw

    def _accept(self) -> None:
        """Promote the fixture invitation to ACCEPTED directly.

        Used by tests about what an *accepted* partner may do, so they do not
        depend on the accept endpoint working.
        """
        SafetyCheckinPartner.objects.filter(pk=self.partner.pk).update(status=SafetyCheckinPartnerStatus.ACCEPTED, accepted_at=timezone.now())


class SafetyPartnerInviteListTests(_SafetyPartnerTestCase):
    """The listing that makes accept/decline reachable at all."""

    def test_invitee_sees_their_pending_invite(self) -> None:
        """Without this listing, an API-only client can never learn the uuid to accept.

        The invitee is not the owner, so the check-in is in none of their lists,
        and until they accept, the partner-checkin endpoints correctly refuse to
        resolve it. This is the only route in.
        """
        response = self.client.get(self.invites_url, **_bearer(self.watcher_key))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        row = payload["results"][0]
        self.assertEqual(row["checkin_uuid"], str(self.checkin.uuid))
        self.assertEqual(row["checkin_title"], "Quarry trip")
        self.assertEqual(row["owner_username"], "explorer")
        self.assertEqual(row["invited_by_username"], "explorer")
        self.assertEqual(row["status"], SafetyCheckinPartnerStatus.INVITED)

    def test_listing_is_only_the_callers_own_invites(self) -> None:
        """A caller must not be able to enumerate other people's invitations.

        An invitation names a check-in *and* the person out on it, so a listing
        that leaked other people's rows would disclose who is currently out
        somewhere, to anyone holding any safety-scoped key.
        """
        someone_else = Profile.objects.get(user=baker.make(User, username="bystander"))
        SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=someone_else, invited_by=self.owner)

        results = self.client.get(self.invites_url, **_bearer(self.watcher_key)).json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["owner_username"], "explorer")

        # And the reverse direction: the owner has sent an invite but received none.
        self.assertEqual(self.client.get(self.invites_url, **_bearer(self.owner_key)).json()["count"], 0)

    def test_accepted_rows_are_not_pending_invites(self) -> None:
        """An accepted row is a standing role, not an invitation.

        It must leave a list whose only actions are accept and decline, or a
        client renders "Accept?" for a responsibility already taken on.
        """
        self._accept()
        self.assertEqual(self.client.get(self.invites_url, **_bearer(self.watcher_key)).json()["count"], 0)

    def test_declined_invite_disappears(self) -> None:
        """Declining deletes the row, so it stops being listed."""
        self.client.post(self.decline_url, **_bearer(self.watcher_key))
        self.assertEqual(self.client.get(self.invites_url, **_bearer(self.watcher_key)).json()["count"], 0)

    def test_listing_requires_safety_read(self) -> None:
        """Scopes are per method, and safety:* is not in the default grant."""
        raw = self._issue_key(self.watcher_user, scopes=[ApiKeyScope.PINS_READ.value])
        self.assertEqual(self.client.get(self.invites_url, **_bearer(raw)).status_code, 403)


class SafetyPartnerInviteAcceptTests(_SafetyPartnerTestCase):
    """Accepting an invitation."""

    def test_accept_promotes_the_row(self) -> None:
        """The happy path: an invite becomes a standing partnership."""
        response = self.client.post(self.accept_url, **_bearer(self.watcher_key))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], SafetyCheckinPartnerStatus.ACCEPTED)
        self.assertIsNotNone(payload["accepted_at"])

        self.partner.refresh_from_db()
        self.assertEqual(self.partner.status, SafetyCheckinPartnerStatus.ACCEPTED)

    def test_accept_grants_the_partner_read(self) -> None:
        """Acceptance is what turns an invitation into authority."""
        self.assertEqual(self.client.get(self.partner_detail_url, **_bearer(self.watcher_key)).status_code, 404)
        self.client.post(self.accept_url, **_bearer(self.watcher_key))
        self.assertEqual(self.client.get(self.partner_detail_url, **_bearer(self.watcher_key)).status_code, 200)

    def test_repeat_accept_is_200_not_an_error(self) -> None:
        """A retried request must not look like a failure to take on the role.

        Mobile clients retry, and "the first attempt succeeded but the response
        was lost" is the common case. Answering 409 would tell the user they had
        failed to accept a safety responsibility they had in fact accepted.
        """
        self.assertEqual(self.client.post(self.accept_url, **_bearer(self.watcher_key)).status_code, 200)
        second = self.client.post(self.accept_url, **_bearer(self.watcher_key))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], SafetyCheckinPartnerStatus.ACCEPTED)

    def test_repeat_accept_does_not_re_notify_the_owner(self) -> None:
        """Idempotent means no side effects the second time, not just no error.

        The conditional UPDATE inside the service is what makes this true; a
        read-then-write would send the owner a second "partner accepted" notice
        (and post a second system chat message) on every retry.
        """
        self.client.post(self.accept_url, **_bearer(self.watcher_key))
        first_count = NotificationLog.objects.filter(profile=self.owner, notification_type=NotificationType.SAFETY_CHECKIN_PARTNER_ACCEPTED).count()
        self.assertEqual(first_count, 1)

        self.client.post(self.accept_url, **_bearer(self.watcher_key))
        self.assertEqual(NotificationLog.objects.filter(profile=self.owner, notification_type=NotificationType.SAFETY_CHECKIN_PARTNER_ACCEPTED).count(), 1)

    def test_cannot_accept_someone_elses_invitation(self) -> None:
        """The queryset is scoped to the caller's own row, not just the check-in.

        A lookup keyed only on ``checkin__uuid`` would let any caller accept a
        partnership that was offered to somebody else, granting themselves a
        live view of where a stranger physically is.
        """
        intruder_user = baker.make(User, username="intruder")
        response = self.client.post(self.accept_url, **_bearer(self._issue_key(intruder_user)))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "No such invitation."})
        self.partner.refresh_from_db()
        self.assertEqual(self.partner.status, SafetyCheckinPartnerStatus.INVITED)

    def test_unknown_checkin_uuid_is_404(self) -> None:
        """A uuid naming nothing is the same "nothing" as an invitation-less one."""
        url = reverse("external_api:safety.partner_invites.accept", kwargs={"checkin_uuid": uuid_module.uuid4()})
        self.assertEqual(self.client.post(url, **_bearer(self.watcher_key)).status_code, 404)

    def test_accept_requires_safety_write(self) -> None:
        """A read-only key cannot take on a watching role."""
        raw = self._issue_key(self.watcher_user, scopes=[ApiKeyScope.SAFETY_READ.value])
        self.assertEqual(self.client.post(self.accept_url, **_bearer(raw)).status_code, 403)


class SafetyPartnerInviteDeclineTests(_SafetyPartnerTestCase):
    """Declining an invitation, and resigning an accepted one."""

    def test_decline_removes_the_row(self) -> None:
        """The happy path returns no content, because nothing is left to return."""
        response = self.client.post(self.decline_url, **_bearer(self.watcher_key))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=self.partner.pk).exists())

    def test_repeat_decline_is_404(self) -> None:
        """The honest answer once the row is gone.

        Reporting 204 for a row that no longer exists would make this endpoint
        indistinguishable from one that silently failed to delete anything.
        """
        self.assertEqual(self.client.post(self.decline_url, **_bearer(self.watcher_key)).status_code, 204)
        self.assertEqual(self.client.post(self.decline_url, **_bearer(self.watcher_key)).status_code, 404)

    def test_accepted_partner_can_resign(self) -> None:
        """No status filter guards decline, so it doubles as "step down".

        A watcher who can no longer take responsibility must be able to say so,
        and the alternative - only the owner may remove them - leaves someone
        holding a live view of another person's position against their will.
        """
        self._accept()
        self.assertEqual(self.client.post(self.decline_url, **_bearer(self.watcher_key)).status_code, 204)
        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=self.partner.pk).exists())
        self.assertEqual(self.client.get(self.partner_detail_url, **_bearer(self.watcher_key)).status_code, 404)

    def test_cannot_decline_someone_elses_invitation(self) -> None:
        """Same own-row scoping as accept - otherwise this is a griefing vector.

        Without it, anyone could delete another person's partner row and quietly
        strip a check-in of the watcher the owner chose.
        """
        intruder_user = baker.make(User, username="intruder")
        response = self.client.post(self.decline_url, **_bearer(self._issue_key(intruder_user)))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(SafetyCheckinPartner.objects.filter(pk=self.partner.pk).exists())

    def test_decline_requires_safety_write(self) -> None:
        """A read-only key cannot dismantle a partnership."""
        raw = self._issue_key(self.watcher_user, scopes=[ApiKeyScope.SAFETY_READ.value])
        self.assertEqual(self.client.post(self.decline_url, **_bearer(raw)).status_code, 403)


class SafetyPartnerCheckinReadTests(_SafetyPartnerTestCase):
    """What an accepted partner can see, and what an unaccepted one cannot."""

    def test_accepted_partner_lists_the_checkin(self) -> None:
        """The list is the partner's equivalent of the owner's check-in list."""
        self._accept()
        response = self.client.get(self.partner_list_url, **_bearer(self.watcher_key))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        row = payload["results"][0]
        self.assertEqual(row["uuid"], str(self.checkin.uuid))
        self.assertEqual(row["owner_username"], "explorer")
        self.assertEqual(row["owner_profile_uuid"], str(self.owner.uuid))

    def test_partner_count_is_not_collapsed_by_the_filter(self) -> None:
        """Guards a real ORM trap in the queryset behind this list.

        ``partnered_with`` filters on the multi-valued ``partners`` relation. If
        the count annotations were applied *after* that filter, Django would
        reuse the same join and every row would report ``partner_count`` 1 - the
        caller's own row - no matter how many watchers the check-in actually
        has. A client rendering "1 partner" for a three-partner check-in is
        wrong in a way nobody would think to question.
        """
        self._accept()
        second = Profile.objects.get(user=baker.make(User, username="second-watcher"))
        SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=second, invited_by=self.owner, status=SafetyCheckinPartnerStatus.ACCEPTED)

        row = self.client.get(self.partner_list_url, **_bearer(self.watcher_key)).json()["results"][0]
        self.assertEqual(row["partner_count"], 2)
        self.assertEqual(row["contact_count"], 1)

    def test_invited_partner_lists_nothing(self) -> None:
        """An unanswered invitation grants no read whatsoever."""
        self.assertEqual(self.client.get(self.partner_list_url, **_bearer(self.watcher_key)).json()["count"], 0)

    def test_own_checkins_are_not_partnered_checkins(self) -> None:
        """The owner's own check-in never appears on the partner surface.

        Keeping the two disjoint is what stops an owner reaching the
        partner-only mark-safe write on their own check-in - an action whose
        entire audit meaning is "somebody else confirmed they are alright".
        """
        self.assertEqual(self.client.get(self.partner_list_url, **_bearer(self.owner_key)).json()["count"], 0)
        self.assertEqual(self.client.get(self.partner_detail_url, **_bearer(self.owner_key)).status_code, 404)

    def test_detail_gives_the_partner_the_full_document(self) -> None:
        """Matching the website, which hands an accepted partner the owner's own page.

        A partner's whole purpose is seeing the plan *before* something goes
        wrong; serving a thinner payload here would show the mobile client less
        than the website already does.
        """
        self._accept()
        payload = self.client.get(self.partner_detail_url, **_bearer(self.watcher_key)).json()

        self.assertEqual(payload["uuid"], str(self.checkin.uuid))
        self.assertEqual(payload["plan_details"], "North rim, back by dark")
        self.assertEqual(payload["owner_username"], "explorer")
        self.assertEqual([contact["display_name"] for contact in payload["contacts"]], ["Friend"])

    def test_detail_never_exposes_a_contact_portal_token(self) -> None:
        """The token is a credential that would let its holder act as the contact."""
        self._accept()
        body = self.client.get(self.partner_detail_url, **_bearer(self.watcher_key)).content.decode()
        self.assertNotIn(str(self.checkin.contacts.first().token), body)

    def test_invited_partner_detail_is_404_not_403(self) -> None:
        """A 403 would confirm the uuid names a real check-in belonging to someone."""
        response = self.client.get(self.partner_detail_url, **_bearer(self.watcher_key))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "No such check-in."})

    def test_stranger_detail_is_404(self) -> None:
        """Someone with no relationship to the check-in resolves nothing."""
        raw = self._issue_key(baker.make(User, username="stranger"))
        self.assertEqual(self.client.get(self.partner_detail_url, **_bearer(raw)).status_code, 404)

    def test_removed_partner_loses_the_detail_immediately(self) -> None:
        """Revocation is not grandfathered."""
        self._accept()
        self.assertEqual(self.client.get(self.partner_detail_url, **_bearer(self.watcher_key)).status_code, 200)
        SafetyCheckinPartner.objects.filter(pk=self.partner.pk).delete()
        self.assertEqual(self.client.get(self.partner_detail_url, **_bearer(self.watcher_key)).status_code, 404)

    def test_partner_reads_require_safety_read(self) -> None:
        """Both the list and the detail are gated on the read scope."""
        self._accept()
        raw = self._issue_key(self.watcher_user, scopes=[ApiKeyScope.PINS_READ.value])
        self.assertEqual(self.client.get(self.partner_list_url, **_bearer(raw)).status_code, 403)
        self.assertEqual(self.client.get(self.partner_detail_url, **_bearer(raw)).status_code, 403)


class SafetyPartnerMarkSafeTests(_SafetyPartnerTestCase):
    """A partner concluding the check-in on the owner's behalf."""

    def test_accepted_partner_marks_the_owner_safe(self) -> None:
        """The write this surface exists for - it stops the escalation beat."""
        self._accept()
        response = self.client.post(self.mark_safe_url, **_bearer(self.watcher_key))

        self.assertEqual(response.status_code, 200)
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.status, SafetyCheckinStatus.FOUND_SAFE)
        self.assertIsNotNone(self.checkin.resolved_at)
        self.assertEqual(self.checkin.resolved_by_label, "watcher")
        self.assertTrue(response.json()["is_resolved"])

    def test_owner_is_notified_that_they_were_found(self) -> None:
        """Concluding someone else's check-in has to tell them it happened."""
        self._accept()
        self.client.post(self.mark_safe_url, **_bearer(self.watcher_key))

        self.assertTrue(NotificationLog.objects.filter(profile=self.owner, notification_type=NotificationType.SAFETY_CHECKIN_RESOLVED).exists())

    def test_invited_partner_cannot_mark_safe(self) -> None:
        """The ACCEPTED clause is the whole check.

        Dropping it would let someone who was only ever *offered* a watching
        role stand down a real escalation - silencing the alarm for a person who
        may genuinely be in trouble.
        """
        response = self.client.post(self.mark_safe_url, **_bearer(self.watcher_key))

        self.assertEqual(response.status_code, 404)
        self.checkin.refresh_from_db()
        self.assertFalse(self.checkin.is_resolved)

    def test_stranger_cannot_mark_safe(self) -> None:
        """No relationship, no authority, and no confirmation the check-in exists."""
        raw = self._issue_key(baker.make(User, username="stranger"))
        self.assertEqual(self.client.post(self.mark_safe_url, **_bearer(raw)).status_code, 404)
        self.checkin.refresh_from_db()
        self.assertFalse(self.checkin.is_resolved)

    def test_already_resolved_is_409(self) -> None:
        """A state conflict, not a malformed request.

        A client must be able to tell "someone else already found them" from
        "you may not do this", so it can show the resolution instead of an error.
        """
        self._accept()
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(status=SafetyCheckinStatus.CHECKED_IN, resolved_at=timezone.now())

        response = self.client.post(self.mark_safe_url, **_bearer(self.watcher_key))
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())

    def test_repeat_mark_safe_does_not_re_resolve(self) -> None:
        """The second call sees an already-resolved check-in and conflicts.

        Pinned because the resolution path notifies every contact: a mark-safe
        that ran twice would email real emergency contacts twice about the same
        incident.
        """
        self._accept()
        self.assertEqual(self.client.post(self.mark_safe_url, **_bearer(self.watcher_key)).status_code, 200)
        self.checkin.refresh_from_db()
        first_resolved_at = self.checkin.resolved_at

        self.assertEqual(self.client.post(self.mark_safe_url, **_bearer(self.watcher_key)).status_code, 409)
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.resolved_at, first_resolved_at)

    def test_mark_safe_requires_safety_write(self) -> None:
        """A read-only key cannot conclude someone's check-in."""
        self._accept()
        raw = self._issue_key(self.watcher_user, scopes=[ApiKeyScope.SAFETY_READ.value])
        self.assertEqual(self.client.post(self.mark_safe_url, **_bearer(raw)).status_code, 403)
