"""Reacting to a DM must respect a block, the same way sending a new one does.

Profile.are_blocked is documented as "an absolute veto on contact regardless
of who blocked whom" - but toggle_reaction only checked that the acting
profile was the message's sender or recipient, not whether the pair had
blocked each other. Since a reaction is a live, real-time broadcast to both
participants' open sockets (see _broadcast_reaction), two profiles that have
blocked each other could still exchange interactions on their prior message
history after the block. Covers the service function directly and both HTTP
surfaces that call it (the internal panel and the external API).
"""

from __future__ import annotations

from datetime import timedelta
import os

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.messaging.direct_messages import BlockedParticipantError, toggle_reaction

READ_WRITE = f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}"


def _profile() -> Profile:
    return baker.make(User).profile


def _open_dms(*profiles: Profile) -> None:
    Profile.objects.filter(pk__in=[p.pk for p in profiles]).update(direct_message_visibility=VisibilityChoice.ANYONE)
    for profile in profiles:
        profile.refresh_from_db()


def _block(blocker: Profile, blocked: Profile) -> None:
    Friendship.objects.create(from_profile=blocker, to_profile=blocked, status=FriendshipStatus.BLOCKED)


class ToggleReactionRespectsBlockTests(TestCase):
    """services.messaging.direct_messages.toggle_reaction, called directly."""

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.partner = _profile()
        _open_dms(self.me, self.partner)
        self.message = DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")

    def test_reacting_normally_still_works(self) -> None:
        action = toggle_reaction(self.me, self.message, "🔥")
        self.assertEqual(action, "added")

    def test_reacting_after_i_blocked_them_is_refused(self) -> None:
        _block(self.me, self.partner)
        with self.assertRaises(BlockedParticipantError):
            toggle_reaction(self.me, self.message, "🔥")

    def test_reacting_after_they_blocked_me_is_refused(self) -> None:
        """Blocking is a mutual veto regardless of who initiated it."""
        _block(self.partner, self.me)
        with self.assertRaises(BlockedParticipantError):
            toggle_reaction(self.me, self.message, "🔥")


class InternalReactionEndpointRespectsBlockTests(TestCase):
    """POST /messages/<slug>/react/<id>/ - the HTMX panel's own reaction endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.partner = _profile()
        _open_dms(self.me, self.partner)
        self.me.ensure_slug()
        self.partner.ensure_slug()
        self.client.force_login(self.me.user)
        self.message = DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")

    def _url(self) -> str:
        return reverse("messages.react", kwargs={"profile_slug": self.partner.slug, "message_id": self.message.pk})

    def test_reacting_after_a_block_is_refused(self) -> None:
        _block(self.me, self.partner)

        response = self.client.post(self._url(), {"emoji": "🔥"})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.message.reactions.filter(emoji="🔥").exists())


class ExternalApiReactionEndpointRespectsBlockTests(TestCase):
    """POST /dashboard/api/external/v1/messages/<slug>/react/<id>/ - the same gap, over the API."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.me = _profile()
        self.partner = _profile()
        _open_dms(self.me, self.partner)
        self.message = DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")

        from oauth2_provider.models import get_access_token_model

        token = get_access_token_model().objects.create(
            user=self.me.user,
            application=first_party_application(),
            token=f"tok-{os.urandom(8).hex()}",
            expires=timezone.now() + timedelta(hours=1),
            scope=READ_WRITE,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}

    def _url(self) -> str:
        return reverse(
            "external_api:messages.react",
            kwargs={"peer_slug": self.partner.ensure_slug(), "message_id": self.message.pk},
        )

    def test_reacting_after_a_block_is_refused(self) -> None:
        _block(self.me, self.partner)

        response = self.client.post(self._url(), {"emoji": "🔥"}, **self.auth)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.message.reactions.filter(emoji="🔥").exists())
