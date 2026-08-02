"""Emoji reactions on a pin's own comment thread, plus the notification they emit.

Two things are under test here, and they fail in different ways.

**The endpoint.** ``PinCommentReactionView`` is pure configuration over
``_ReactionMixin`` - ``test_external_api_reaction_mixin`` already pins the
mixin's own semantics through the wiki route - so what is worth asserting is
the part that is *not* shared: which row this endpoint addresses, who may reach
it, and under which scope. A pin comment is private owner content, so the scope
is ``pins:write`` and a comment on someone else's pin must be indistinguishable
from one that does not exist.

**The notification.** Reacting to somebody's comment notifies them on the web
and, until this change, did not over the API: the internal HTMX panel wrote the
``NotificationLog`` row itself, while both API reaction endpoints went through
``services.comments.comments.toggle_reaction``, which did not. Moving the notification
into the service is what makes the two surfaces agree, and the tests below fail
against the previous implementation.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.comments.comments import toggle_reaction
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

BASE = "/dashboard/api/external/v1/pins"

#: Percent-encoded 👍 (in ``ALLOWED_EMOJIS``) and 💀 (deliberately not).
THUMBS_UP = "%F0%9F%91%8D"
SKULL = "%F0%9F%92%80"


class PinCommentReactionApiTests(TestCase):
    """PUT/DELETE one emoji reaction on a comment on the caller's own pin."""

    def setUp(self) -> None:
        """Create the key owner, their pin, a comment on it, and a bystander."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Reaction client")
        self.other_profile = Profile.objects.get(user=baker.make(User, username="bystander"))

        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin
        self.comment = baker.make("dashboard.Comment", pin=self.pin, profile=self.profile, text="Note to self")

    def _headers(self, raw_key: str | None = None) -> dict:
        """Bearer-header kwargs for the fixture key, or an explicitly given one.

        Args:
            raw_key: A raw key to use instead of the fixture's.

        Returns:
            Request kwargs carrying the Authorization header.
        """
        return {"HTTP_AUTHORIZATION": f"Bearer {raw_key or self.raw_key}"}

    def _url(self, emoji: str, *, comment_id: int | None = None, pin_slug: str | None = None) -> str:
        """The reaction URL for one emoji on one comment.

        Args:
            emoji: Percent-encoded emoji for the URL's last segment.
            comment_id: Comment to address; defaults to the fixture comment.
            pin_slug: Pin to address; defaults to the fixture pin.

        Returns:
            The fully-built reaction URL.
        """
        slug = pin_slug or self.pin.slug or str(self.pin.uuid)
        pk = self.comment.pk if comment_id is None else comment_id
        return f"{BASE}/{slug}/comments/{pk}/reactions/{emoji}/"

    def _key_with_scopes(self, scopes: list[str]) -> str:
        """Issue a second key carrying exactly *scopes*.

        Args:
            scopes: Raw scope values to store on the row.

        Returns:
            The raw key value.
        """
        api_key, raw = generate_api_key(self.user, "Scoped")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def test_put_adds_the_reaction_and_returns_the_summary(self) -> None:
        """The happy path: one PUT, one row, and the fresh summary back."""
        response = self.client.put(self._url(THUMBS_UP), **self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reactions"]["👍"], {"count": 1, "reacted": True})
        self.assertEqual(Reaction.objects.filter(comment=self.comment, profile=self.profile).count(), 1)

    def test_delete_removes_it_again(self) -> None:
        """DELETE is the declarative inverse, not a second toggle."""
        self.client.put(self._url(THUMBS_UP), **self._headers())
        response = self.client.delete(self._url(THUMBS_UP), **self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reactions"], {})
        self.assertFalse(Reaction.objects.filter(comment=self.comment).exists())

    def test_unsupported_emoji_is_rejected(self) -> None:
        """The emoji vocabulary is enforced before anything is written."""
        response = self.client.put(self._url(SKULL), **self._headers())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "That is not a supported reaction."})
        self.assertFalse(Reaction.objects.filter(comment=self.comment).exists())

    def test_comment_on_another_users_pin_is_not_found(self) -> None:
        """The pin gate is a lookup, so someone else's pin reads as nonexistent.

        Not merely a read leak: without ``pin=`` scoping the caller could write
        a reaction row onto a comment on a stranger's private pin.
        """
        their_pin = create_pin_for_profile(self.other_profile, name="Theirs", latitude=1.0, longitude=1.0).pin
        their_comment = baker.make("dashboard.Comment", pin=their_pin, profile=self.other_profile, text="Private")

        response = self.client.put(
            self._url(THUMBS_UP, comment_id=their_comment.pk, pin_slug=their_pin.slug or str(their_pin.uuid)),
            **self._headers(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})
        self.assertFalse(Reaction.objects.filter(comment=their_comment).exists())

    def test_comment_from_another_pin_of_the_callers_own_is_not_found(self) -> None:
        """``pin=`` in the lookup, not just ownership: ids stay scoped to the host."""
        second_pin = create_pin_for_profile(self.profile, name="Second", latitude=43.0, longitude=-74.0).pin
        foreign_comment = baker.make("dashboard.Comment", pin=second_pin, profile=self.profile, text="Elsewhere")

        response = self.client.put(self._url(THUMBS_UP, comment_id=foreign_comment.pk), **self._headers())

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Reaction.objects.filter(comment=foreign_comment).exists())

    def test_unsupported_emoji_on_an_invisible_comment_is_still_400(self) -> None:
        """The emoji check runs first, so it cannot be used as an existence oracle."""
        response = self.client.put(self._url(SKULL, comment_id=self.comment.pk + 9999), **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_wiki_scopes_alone_cannot_react(self) -> None:
        """A pin's thread is private owner content - ``wiki:write`` must not reach it."""
        raw = self._key_with_scopes([ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value])

        response = self.client.put(self._url(THUMBS_UP), **self._headers(raw))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Reaction.objects.filter(comment=self.comment).exists())

    def test_read_only_pin_scope_cannot_react(self) -> None:
        """Reacting is a write; ``pins:read`` alone is not enough for either verb."""
        raw = self._key_with_scopes([ApiKeyScope.PINS_READ.value])

        self.assertEqual(self.client.put(self._url(THUMBS_UP), **self._headers(raw)).status_code, 403)
        self.assertEqual(self.client.delete(self._url(THUMBS_UP), **self._headers(raw)).status_code, 403)


class ReactionNotificationTests(TestCase):
    """Reacting to someone's comment must notify them, whichever surface did it.

    The regression these guard: the notification lived in the internal HTMX
    view, so every API reaction was silent. A user reacting from the mobile app
    produced no notification at all, while the same reaction from the website
    produced one - a difference invisible to everyone except the person who
    never heard about it.
    """

    def setUp(self) -> None:
        """Create an author, a reactor, and a comment on the author's own pin."""
        baker.make(User)
        self.author = Profile.objects.get(user=baker.make(User, username="author"))
        self.reactor_user = baker.make(User, username="reactor")
        self.reactor = Profile.objects.get(user=self.reactor_user)

        self.pin = create_pin_for_profile(self.author, name="Old Mill", latitude=42.5, longitude=-73.5).pin
        self.comment = baker.make("dashboard.Comment", pin=self.pin, profile=self.author, text="Worth a look")

    def _liked_notifications(self) -> int:
        """How many "reacted to your comment" rows the author has.

        Returns:
            The row count.
        """
        return NotificationLog.objects.filter(profile=self.author, notification_type=NotificationType.COMMENT_LIKED).count()

    def test_adding_a_reaction_notifies_the_comment_author(self) -> None:
        """The service - not the view - is what makes every surface notify."""
        toggle_reaction(self.reactor, self.comment, "👍")

        self.assertEqual(self._liked_notifications(), 1)

    def test_removing_a_reaction_does_not_notify(self) -> None:
        """Un-reacting is not an event; notifying would make the toggle a spam vector."""
        toggle_reaction(self.reactor, self.comment, "👍")
        toggle_reaction(self.reactor, self.comment, "👍")

        self.assertEqual(self._liked_notifications(), 1)

    def test_reacting_to_your_own_comment_notifies_nobody(self) -> None:
        """Self-notification is suppressed in the one shared implementation."""
        toggle_reaction(self.author, self.comment, "👍")

        self.assertEqual(self._liked_notifications(), 0)

    def test_the_authors_delivery_preference_is_respected(self) -> None:
        """A user who turned "comment liked" off stays off, service path included.

        The preference row is created explicitly rather than fetched: it is
        created lazily in production, and the notification helper treats a
        missing row as "site delivery", so the off case only exists once a row
        says so.
        """
        NotificationPreference.objects.update_or_create(profile=self.author, defaults={"comment_liked": DeliveryPreference.NONE})

        toggle_reaction(self.reactor, self.comment, "👍")

        self.assertEqual(self._liked_notifications(), 0)

    def test_reacting_through_the_api_notifies_too(self) -> None:
        """End-to-end: the endpoint inherits the notification from the service.

        Reacting to another profile's comment on a pin needs the reactor to own
        the pin, so the roles here are inverted relative to the rest of this
        class - the reactor is the pin owner and the author is a guest
        commenter.

        The guest's ``comment_visibility`` is widened to ANYONE because owning
        the pin is not the same as being allowed to read every comment on it:
        under the ``ANYTHING_IN_COMMON`` default this guest's comment is hidden
        from the owner, and reacting to a comment the thread would not show is
        now a 404. That gate is the subject of its own test; here it would only
        obscure the notification behaviour being checked.
        """
        owner_user = baker.make(User, username="pinowner")
        owner = Profile.objects.get(user=owner_user)
        _key, raw_key = generate_api_key(owner_user, "Reaction client")
        pin = create_pin_for_profile(owner, name="Guest thread", latitude=44.1, longitude=-75.2).pin
        self.author.comment_visibility = VisibilityChoice.ANYONE
        self.author.save(update_fields=["comment_visibility"])
        guest_comment = baker.make("dashboard.Comment", pin=pin, profile=self.author, text="Been here")

        url = f"{BASE}/{pin.slug or pin.uuid}/comments/{guest_comment.pk}/reactions/{THUMBS_UP}/"
        response = self.client.put(url, HTTP_AUTHORIZATION=f"Bearer {raw_key}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._liked_notifications(), 1)
