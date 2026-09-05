"""Tests for the external API's owner/partner check-in chat.

Before this endpoint existed the only non-session chat surface was the
tokenized contact-portal WebSocket, which authenticates an emergency *contact* -
so a mobile client could read a check-in and never see the conversation
happening on it. These tests hold the lines that make the REST version safe to
be that second door:

* the transcript is readable by the owner and by an **ACCEPTED** partner, and by
  nobody else - a merely-invited partner, a stranger, and an emergency contact
  with an account all get an identical 404, never a 403;
* a REST-sent message goes through the same create-then-broadcast pair the web
  form uses, so it reaches everyone holding an open socket. This is asserted on
  the broadcast itself rather than on "the row was created", because the failure
  it guards is silent: the sender's own view looks completely correct while
  every other participant sees nothing until they reload;
* an archived check-in answers 409 rather than 400, so a client can tell "this
  conversation is over" from "your message was malformed";
* the contact-portal ``token`` never appears in a message payload, matching the
  rule the rest of the safety surface already holds.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.serializers_safety_chat import SafetyCheckinMessageCreateSerializer
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckinArchive,
    SafetyCheckinMessage,
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
)
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.visits.safety import MAX_CHAT_MESSAGE_LENGTH, create_checkin


def _bearer(raw_key: str) -> dict:
    """Build the auth header kwargs for a bearer-key request.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Extra kwargs for ``self.client``.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _SafetyChatTestCase(TestCase):
    """Shared setup: an owner, an accepted partner, and one live check-in."""

    def setUp(self) -> None:
        """Create the cast and issue each of them a safety-scoped key."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.owner_user = baker.make(User, username="explorer")
        self.owner = Profile.objects.get(user=self.owner_user)
        self.partner_user = baker.make(User, username="watcher")
        self.partner = Profile.objects.get(user=self.partner_user)

        self.owner_key = self._issue_key(self.owner_user)
        self.partner_key = self._issue_key(self.partner_user)

        self.checkin = create_checkin(
            profile=self.owner,
            title="Quarry trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=6),
            grace_period=datetime.timedelta(hours=1),
            plan_details="North rim, back by dark",
            contact_message="Please call me",
            contacts=[(None, "friend@example.com", "Friend")],
        )
        SafetyCheckinPartner.objects.create(
            checkin=self.checkin,
            profile=self.partner,
            invited_by=self.owner,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )
        self.url = reverse("external_api:safety.checkins.messages", kwargs={"checkin_slug": self.checkin.slug})

    def _issue_key(self, user: User, scopes: list[str] | None = None) -> str:
        """Issue an API key for *user* carrying the safety scopes.

        Args:
            user: The key's owner.
            scopes: Scope values to grant, defaulting to safety read + write.

        Returns:
            The plaintext key.
        """
        key, raw = generate_api_key(user, "Test")
        # scopes is editable=False, so it is set directly rather than through a
        # form. The default grant deliberately excludes safety:*.
        ApiKey.objects.filter(pk=key.pk).update(
            scopes=scopes or [ApiKeyScope.SAFETY_READ.value, ApiKeyScope.SAFETY_WRITE.value]
        )
        return raw

    def _post(self, raw_key: str, body: str):
        """POST a chat message.

        Args:
            raw_key: The plaintext API key to authenticate with.
            body: The message body to submit.

        Returns:
            The response.
        """
        return self.client.post(self.url, {"body": body}, content_type="application/json", **_bearer(raw_key))


class SafetyChatBodyValidationProperties(SimpleTestCase):
    """Property-based bounds on the submitted-message serializer.

    Deliberately DB-free and client-free: the property being checked is a pure
    function of the string, and driving it through ``self.client`` would make a
    few hundred examples take minutes for no extra coverage.

    The property that matters is the *agreement* between this serializer and
    ``services.visits.safety.create_chat_message``. The service strips and then rejects
    an empty result; if the serializer disagreed for any input, that input would
    pass validation and then fail deeper in with a differently-shaped error body
    for the same user mistake - so the two must accept and reject exactly the
    same strings.
    """

    #: Ordinary text plus every flavour of whitespace, and deliberately no NUL or
    #: surrogates: DRF's CharField rejects those through validators of its own,
    #: which is correct behaviour but not the property under test here - including
    #: them would only assert that DRF still bans them.
    SAFE_ALPHABET = st.sampled_from(list(" \t\n\r\x0bab z9!?,.-é中\U0001f600"))

    @settings(max_examples=250, deadline=None)
    @given(st.text(alphabet=SAFE_ALPHABET, max_size=60))
    def test_validity_matches_the_services_own_rule(self, body: str) -> None:
        """A body is valid here exactly when the service would accept it.

        Args:
            body: Arbitrary submitted text, weighted toward the blank and
                nearly-blank cases where the two rules could disagree.
        """
        service_would_accept = bool(body.strip())
        self.assertEqual(SafetyCheckinMessageCreateSerializer(data={"body": body}).is_valid(), service_would_accept)

    @settings(max_examples=50, deadline=None)
    @given(st.integers(min_value=1, max_value=MAX_CHAT_MESSAGE_LENGTH))
    def test_any_length_up_to_the_cap_is_accepted(self, length: int) -> None:
        """The cap is inclusive at every length below it, not just at 1.

        Args:
            length: How many characters the submitted body should have.
        """
        self.assertTrue(SafetyCheckinMessageCreateSerializer(data={"body": "x" * length}).is_valid())

    @settings(max_examples=50, deadline=None)
    @given(st.integers(min_value=MAX_CHAT_MESSAGE_LENGTH + 1, max_value=MAX_CHAT_MESSAGE_LENGTH + 500))
    def test_any_length_past_the_cap_is_rejected(self, length: int) -> None:
        """Nothing over the cap gets through, however slightly it overshoots.

        Args:
            length: How many characters the submitted body should have.
        """
        self.assertFalse(SafetyCheckinMessageCreateSerializer(data={"body": "x" * length}).is_valid())


class SafetyChatAccessTests(_SafetyChatTestCase):
    """Who may read and write a check-in's chat."""

    def test_owner_reads_the_transcript(self) -> None:
        """The owner is always a participant in their own check-in's chat."""
        SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_profile=self.owner, body="Setting off now")
        response = self.client.get(self.url, **_bearer(self.owner_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["body"], "Setting off now")

    def test_accepted_partner_reads_the_transcript(self) -> None:
        """A partner who accepted is exactly who this endpoint exists to serve."""
        SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_profile=self.owner, body="Setting off now")
        response = self.client.get(self.url, **_bearer(self.partner_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_merely_invited_partner_gets_404(self) -> None:
        """An unaccepted invite must not open someone's live conversation.

        The case a status-less ``partners.filter()`` would let through: the row
        exists from the moment the invite is sent, so a membership test that
        ignores status admits someone who never took on the responsibility.
        """
        invitee_user = baker.make(User, username="invitee")
        invitee = Profile.objects.get(user=invitee_user)
        SafetyCheckinPartner.objects.create(
            checkin=self.checkin, profile=invitee, invited_by=self.owner, status=SafetyCheckinPartnerStatus.INVITED
        )

        response = self.client.get(self.url, **_bearer(self._issue_key(invitee_user)))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "No such check-in."})

    def test_invited_partner_cannot_post(self) -> None:
        """The write side refuses the same people the read side does."""
        invitee_user = baker.make(User, username="invitee")
        invitee = Profile.objects.get(user=invitee_user)
        SafetyCheckinPartner.objects.create(
            checkin=self.checkin, profile=invitee, invited_by=self.owner, status=SafetyCheckinPartnerStatus.INVITED
        )

        self.assertEqual(self._post(self._issue_key(invitee_user), "let me in").status_code, 404)
        self.assertFalse(SafetyCheckinMessage.objects.filter(body="let me in").exists())

    def test_stranger_gets_404_not_403(self) -> None:
        """A 403 would confirm the slug names a real check-in belonging to someone."""
        stranger_user = baker.make(User, username="stranger")
        response = self.client.get(self.url, **_bearer(self._issue_key(stranger_user)))
        self.assertEqual(response.status_code, 404)

    def test_removed_partner_loses_the_transcript_immediately(self) -> None:
        """Revocation is not grandfathered - the next read is already refused."""
        self.assertEqual(self.client.get(self.url, **_bearer(self.partner_key)).status_code, 200)
        SafetyCheckinPartner.objects.filter(checkin=self.checkin, profile=self.partner).delete()
        self.assertEqual(self.client.get(self.url, **_bearer(self.partner_key)).status_code, 404)

    def test_unknown_slug_is_404(self) -> None:
        """A slug matching nothing is the same "nothing" as a refused one."""
        url = reverse("external_api:safety.checkins.messages", kwargs={"checkin_slug": "no-such-checkin"})
        self.assertEqual(self.client.get(url, **_bearer(self.owner_key)).status_code, 404)

    def test_read_requires_safety_read_scope(self) -> None:
        """Scopes are per method, and this one is not covered by the default grant."""
        raw = self._issue_key(baker.make(User, username="scopeless"), scopes=[ApiKeyScope.PINS_READ.value])
        self.assertEqual(self.client.get(self.url, **_bearer(raw)).status_code, 403)

    def test_post_requires_safety_write_scope(self) -> None:
        """A read-only safety key cannot put words in the owner's chat."""
        key, raw = generate_api_key(self.partner_user, "Read only")
        ApiKey.objects.filter(pk=key.pk).update(scopes=[ApiKeyScope.SAFETY_READ.value])
        self.assertEqual(self._post(raw, "hello").status_code, 403)

    def test_unauthenticated_request_is_rejected(self) -> None:
        """No credential, no chat."""
        self.assertIn(self.client.get(self.url).status_code, (401, 403))


class SafetyChatReadTests(_SafetyChatTestCase):
    """The shape and ordering of the transcript."""

    def test_rows_are_newest_first(self) -> None:
        """Ordering is explicit and total, so pages cannot overlap or drop rows."""
        first = SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_profile=self.owner, body="one")
        second = SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_profile=self.owner, body="two")

        results = self.client.get(self.url, **_bearer(self.owner_key)).json()["results"]
        self.assertEqual([row["id"] for row in results], [second.pk, first.pk])

    def test_is_mine_distinguishes_the_caller(self) -> None:
        """Each participant sees their own messages flagged, and only their own."""
        SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_profile=self.owner, body="from owner")
        SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_profile=self.partner, body="from partner")

        owner_view = {
            row["body"]: row["is_mine"]
            for row in self.client.get(self.url, **_bearer(self.owner_key)).json()["results"]
        }
        partner_view = {
            row["body"]: row["is_mine"]
            for row in self.client.get(self.url, **_bearer(self.partner_key)).json()["results"]
        }

        self.assertEqual(owner_view, {"from owner": True, "from partner": False})
        self.assertEqual(partner_view, {"from owner": False, "from partner": True})

    def test_profile_sender_carries_account_identity(self) -> None:
        """A message from an account exposes that account's username and uuid."""
        SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_profile=self.partner, body="on my way")
        row = self.client.get(self.url, **_bearer(self.owner_key)).json()["results"][0]

        self.assertEqual(row["sender_kind"], "profile")
        self.assertEqual(row["sender_name"], "watcher")
        self.assertEqual(row["sender_username"], "watcher")
        self.assertEqual(row["sender_profile_uuid"], str(self.partner.uuid))

    def test_contact_sender_has_a_name_but_no_account(self) -> None:
        """An email-only contact is a participant without being a user.

        ``sender_kind`` is the discriminator so a client branches on the kind
        rather than on which field happened to be null - a contact has no
        profile page to link to, and inventing one is worse than omitting it.
        """
        contact = self.checkin.contacts.first()
        SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_contact=contact, body="Are you okay?")

        row = self.client.get(self.url, **_bearer(self.owner_key)).json()["results"][0]
        self.assertEqual(row["sender_kind"], "contact")
        self.assertEqual(row["sender_name"], "Friend")
        self.assertIsNone(row["sender_username"])
        self.assertIsNone(row["sender_profile_uuid"])
        self.assertFalse(row["is_mine"])

    def test_orphaned_sender_still_renders(self) -> None:
        """Both sender FKs are SET_NULL, so a deleted account leaves its messages behind.

        The transcript has to survive that rather than 500 - a safety
        conversation losing its history because one participant closed their
        account would destroy the record of what was said during an incident.
        """
        SafetyCheckinMessage.objects.create(checkin=self.checkin, body="orphan")
        row = self.client.get(self.url, **_bearer(self.owner_key)).json()["results"][0]

        self.assertEqual(row["sender_kind"], "unknown")
        self.assertEqual(row["sender_name"], "Unknown")
        self.assertIsNone(row["sender_username"])

    def test_contact_portal_token_never_appears(self) -> None:
        """The portal token is a credential - anyone holding it can act as the contact."""
        contact = self.checkin.contacts.first()
        SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_contact=contact, body="checking in on you")

        body = self.client.get(self.url, **_bearer(self.owner_key)).content.decode()
        self.assertNotIn(str(contact.token), body)
        self.assertNotIn("token", body)

    def test_transcript_is_scoped_to_one_checkin(self) -> None:
        """Messages from another check-in never leak into this one's page."""
        other = create_checkin(
            profile=self.partner,
            title="Different trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=4),
            grace_period=datetime.timedelta(hours=1),
        )
        SafetyCheckinMessage.objects.create(checkin=other, sender_profile=self.partner, body="unrelated")
        SafetyCheckinMessage.objects.create(checkin=self.checkin, sender_profile=self.owner, body="mine")

        bodies = [row["body"] for row in self.client.get(self.url, **_bearer(self.owner_key)).json()["results"]]
        self.assertEqual(bodies, ["mine"])


class SafetyChatWriteTests(_SafetyChatTestCase):
    """Posting a message, and what has to happen when one is posted."""

    def test_owner_posts_a_message(self) -> None:
        """A successful send returns the created row, not a list."""
        response = self._post(self.owner_key, "Reached the north rim")

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["body"], "Reached the north rim")
        self.assertEqual(payload["sender_username"], "explorer")
        self.assertTrue(payload["is_mine"])
        self.assertTrue(
            SafetyCheckinMessage.objects.filter(checkin=self.checkin, body="Reached the north rim").exists()
        )

    def test_partner_posts_a_message_attributed_to_their_own_profile(self) -> None:
        """A partner's message is theirs, not the owner's.

        Worth pinning: ``resolve_message_sender`` is handed the request's user
        and no contact, and a lazy implementation that simply used
        ``checkin.profile`` would attribute every REST message to the explorer -
        making a watcher's "I can't reach you" look like the explorer saying it.
        """
        response = self._post(self.partner_key, "Can't reach you, calling now")

        self.assertEqual(response.status_code, 201)
        message = SafetyCheckinMessage.objects.get(body="Can't reach you, calling now")
        self.assertEqual(message.sender_profile_id, self.partner.pk)
        self.assertIsNone(message.sender_contact_id)

    def test_a_rest_message_is_broadcast_to_the_live_socket(self) -> None:
        """The whole point of reusing the service's send pair.

        A message that is only *saved* is invisible in real time to everyone
        holding an open socket until they happen to reload - and nothing errors,
        so the sender's own view looks entirely correct. That silence is why
        this asserts on the broadcast rather than on the row.
        """
        with mock.patch("urbanlens.dashboard.services.visits.safety.broadcast_chat_message") as broadcast:
            response = self._post(self.owner_key, "still going")

        self.assertEqual(response.status_code, 201)
        broadcast.assert_called_once()
        checkin_arg, message_arg = broadcast.call_args.args
        self.assertEqual(checkin_arg.pk, self.checkin.pk)
        self.assertEqual(message_arg.body, "still going")

    def test_blank_body_is_400(self) -> None:
        """An empty message is a malformed request, and nothing is created."""
        response = self._post(self.owner_key, "")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SafetyCheckinMessage.objects.filter(checkin=self.checkin).count(), 0)

    def test_whitespace_only_body_is_400(self) -> None:
        """Spaces are blank once stripped, so they must be refused up front.

        Otherwise the request passes validation and then fails inside the
        service with a differently-shaped error body for the same user mistake.
        """
        response = self._post(self.owner_key, "    ")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SafetyCheckinMessage.objects.filter(checkin=self.checkin).count(), 0)

    def test_missing_body_is_400(self) -> None:
        """A payload with no body at all is rejected like a blank one."""
        response = self.client.post(self.url, {}, content_type="application/json", **_bearer(self.owner_key))
        self.assertEqual(response.status_code, 400)

    def test_over_long_body_is_400(self) -> None:
        """The cap matches the service's, so the two cannot disagree."""
        response = self._post(self.owner_key, "x" * (MAX_CHAT_MESSAGE_LENGTH + 1))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SafetyCheckinMessage.objects.filter(checkin=self.checkin).count(), 0)

    def test_body_at_the_limit_is_accepted(self) -> None:
        """The bound is inclusive - an exactly-maximum message is legal."""
        response = self._post(self.owner_key, "x" * MAX_CHAT_MESSAGE_LENGTH)
        self.assertEqual(response.status_code, 201)

    def test_archived_checkin_is_409_not_400(self) -> None:
        """ "This conversation is over" is a different answer from "your message is bad".

        A client must be able to tell them apart: one means retire the thread,
        the other means ask the user to retype. Writing into an archived
        check-in would also restore plaintext onto a row whose PII has already
        been sealed away and scrubbed.
        """
        SafetyCheckinArchive.objects.create(
            checkin=self.checkin, ciphertext="x", nonce="y", sealed_key="z", key_bundle_version=1
        )

        response = self._post(self.owner_key, "one last thing")
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())
        self.assertFalse(SafetyCheckinMessage.objects.filter(body="one last thing").exists())

    def test_archived_checkin_still_reads(self) -> None:
        """Archival closes the chat to writes, not to reads.

        The transcript's bodies are scrubbed by archival itself; the endpoint
        must not additionally start 404ing, or a client would be unable to tell
        an archived check-in from one it may not see.
        """
        SafetyCheckinArchive.objects.create(
            checkin=self.checkin, ciphertext="x", nonce="y", sealed_key="z", key_bundle_version=1
        )
        self.assertEqual(self.client.get(self.url, **_bearer(self.owner_key)).status_code, 200)
