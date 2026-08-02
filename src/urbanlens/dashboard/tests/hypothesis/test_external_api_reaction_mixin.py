"""Guards on ``_ReactionMixin``, the shared reaction toggle for the external API.

Wiki comments, pin comments and group messages all expose
``PUT/DELETE .../reactions/{emoji}/`` with identical semantics over one
polymorphic ``Reaction`` table. The mixin exists so those semantics are written
once; these tests pin the parts that a per-domain reimplementation gets wrong
in ways nobody notices until a user complains:

1. **Declarative, not toggling.** A retried PUT over a flaky mobile link must
   not undo the reaction the first attempt applied, and a repeated DELETE must
   stay a no-op.
2. **PUT and DELETE agree about the emoji vocabulary.** Before the extraction,
   validation only ran on the branch that actually called the service, so
   ``DELETE`` of an unsupported emoji answered 200 and told a buggy client its
   emoji were fine.
3. **The emoji check cannot be used to probe ids.** It runs before the target
   is resolved, so a junk emoji answers 400 whether or not the comment exists.
4. **The 404 discipline holds.** A comment id belonging to another wiki - or
   to a wiki the caller cannot see - is not found, never forbidden.
5. **The declarative wiring is actually declarative.** The service functions
   must be ``staticmethod``-wrapped; a bare function in the class body would be
   bound and silently receive the *view* as its first argument.

The wiki reaction endpoint is the concrete subject because it is the one route
already wired; every assertion here is about mixin-owned behaviour, so the
upcoming pin-comment and group-message endpoints inherit these guarantees.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.external_api.mixins import _ReactionMixin
from urbanlens.dashboard.external_api.views_wiki import WikiCommentReactionView
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.comments.comments import ALLOWED_EMOJIS, aggregate_reactions, toggle_reaction
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import grant_wiki_scopes

BASE = "/dashboard/api/external/v1/wikis"

#: Percent-encoded 👍 and 💀 - the first is in ``ALLOWED_EMOJIS``, the second
#: deliberately is not. Encoded because they travel in a URL path segment.
THUMBS_UP = "%F0%9F%91%8D"
SKULL = "%F0%9F%92%80"


class ReactionMixinWiringTests(SimpleTestCase):
    """The declarative configuration must be wired the way the mixin expects."""

    def test_service_hooks_are_staticmethods_not_bound_methods(self) -> None:
        """A bare function in the class body would receive the view as ``profile``.

        Functions are descriptors, so ``reaction_toggle = toggle_reaction``
        (without ``staticmethod``) type-checks at the call site and only fails
        deep inside the service with a nonsensical argument. Asserting identity
        against the underlying service function is what catches that.
        """
        self.assertIs(WikiCommentReactionView.reaction_toggle, toggle_reaction)
        self.assertIs(WikiCommentReactionView.reaction_summarizer, aggregate_reactions)
        view = WikiCommentReactionView()
        self.assertIs(view.reaction_toggle, toggle_reaction)
        self.assertIs(view.reaction_summarizer, aggregate_reactions)

    def test_target_field_names_a_real_reaction_foreign_key(self) -> None:
        """``reaction_target_field`` is passed straight into a queryset filter.

        A typo would raise ``FieldError`` at request time rather than at import
        time, so the endpoint would look fine until someone reacted to
        something.
        """
        reaction_fields = {field.name for field in Reaction._meta.get_fields()}
        self.assertIn(WikiCommentReactionView.reaction_target_field, reaction_fields)

    def test_allowed_emojis_are_declared(self) -> None:
        """Declaring the vocabulary is what makes PUT and DELETE agree."""
        self.assertEqual(set(WikiCommentReactionView.reaction_allowed_emojis or ()), set(ALLOWED_EMOJIS))

    def test_unimplemented_target_resolution_is_loud(self) -> None:
        """A subclass that forgets the one required hook fails immediately."""

        class _Unconfigured(_ReactionMixin):
            """A subclass that configures nothing."""

        with self.assertRaises(NotImplementedError):
            _Unconfigured().resolve_reaction_target(None)  # type: ignore[arg-type]


class ReactionMixinBehaviourTests(TestCase):
    """End-to-end behaviour through the one reaction route already wired."""

    def setUp(self) -> None:
        """Create a wiki the caller has pinned, plus a wiki-scoped key."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="reactor")
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Reaction client")
        grant_wiki_scopes(self.user)

        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        self.comment = baker.make("dashboard.Comment", wiki=self.wiki, profile=self.profile, text="React to me")

    def _headers(self, raw_key: str | None = None) -> dict:
        """Bearer-header kwargs for the fixture key, or an explicitly given one.

        Args:
            raw_key: A raw key to use instead of the fixture's.

        Returns:
            Request kwargs carrying the Authorization header.
        """
        return {"HTTP_AUTHORIZATION": f"Bearer {raw_key or self.raw_key}"}

    def _url(self, emoji: str, comment_id: int | None = None) -> str:
        """The reaction URL for one emoji on one comment.

        Args:
            emoji: Percent-encoded emoji for the URL's last segment.
            comment_id: Comment to address; defaults to the fixture comment.

        Returns:
            The fully-built reaction URL.
        """
        pk = self.comment.pk if comment_id is None else comment_id
        return f"{BASE}/{self.location.ensure_slug()}/comments/{pk}/reactions/{emoji}/"

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

    def test_put_is_idempotent(self) -> None:
        """Two PUTs leave exactly one reaction, not zero."""
        url = self._url(THUMBS_UP)
        first = self.client.put(url, **self._headers())
        second = self.client.put(url, **self._headers())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["reactions"]["👍"]["count"], 1)
        self.assertEqual(Reaction.objects.filter(comment=self.comment, profile=self.profile).count(), 1)

    def test_delete_is_idempotent(self) -> None:
        """DELETE on an absent reaction stays absent rather than creating one."""
        url = self._url(THUMBS_UP)
        first = self.client.delete(url, **self._headers())
        second = self.client.delete(url, **self._headers())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["reactions"], {})
        self.assertFalse(Reaction.objects.filter(comment=self.comment).exists())

    def test_response_reports_the_viewers_own_reaction(self) -> None:
        """``reacted`` is the flag the client renders its highlighted state from."""
        response = self.client.put(self._url(THUMBS_UP), **self._headers())
        self.assertTrue(response.json()["reactions"]["👍"]["reacted"])

    def test_summary_is_fresh_after_the_write(self) -> None:
        """The response must reflect the toggle, not the pre-request state.

        The summary is read off the target's ``reactions`` relation, so a stale
        prefetch cache would make a successful PUT look like it did nothing and
        send the client into a retry loop.
        """
        other = Profile.objects.get(user=baker.make(User))
        Reaction.objects.create(profile=other, comment=self.comment, emoji="👍")

        response = self.client.put(self._url(THUMBS_UP), **self._headers())
        self.assertEqual(response.json()["reactions"]["👍"]["count"], 2)

    def test_unsupported_emoji_is_rejected_on_put(self) -> None:
        """An emoji outside the declared vocabulary never reaches the service."""
        response = self.client.put(self._url(SKULL), **self._headers())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Reaction.objects.filter(comment=self.comment).exists())

    def test_unsupported_emoji_is_rejected_on_delete_too(self) -> None:
        """DELETE must not answer 200 to an emoji PUT would have refused.

        Before the mixin, validation only happened on the branch that called
        the service, so a client sending a junk emoji got a 400 from PUT and a
        cheerful 200 from DELETE - which reads as "that emoji is fine".
        """
        response = self.client.delete(self._url(SKULL), **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_unsupported_emoji_on_a_nonexistent_comment_is_still_400(self) -> None:
        """The emoji check cannot be used to probe which comment ids exist.

        Validating the emoji before resolving the target keeps the response
        identical for a real id and an invented one; resolving first would
        answer 404 for ids the caller cannot reach and 400 for ones they can,
        turning a junk emoji into an existence oracle.
        """
        response = self.client.put(self._url(SKULL, comment_id=self.comment.pk + 9999), **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_comment_from_another_wiki_is_not_found(self) -> None:
        """A globally-valid comment id is still scoped to the addressed wiki."""
        other_location = baker.make("dashboard.Location")
        other_wiki = baker.make("dashboard.Wiki", location=other_location, name="Elsewhere")
        baker.make("dashboard.Pin", profile=self.profile, location=other_location)
        foreign_comment = baker.make("dashboard.Comment", wiki=other_wiki, profile=self.profile, text="Elsewhere")

        response = self.client.put(self._url(THUMBS_UP, comment_id=foreign_comment.pk), **self._headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})
        self.assertFalse(Reaction.objects.filter(comment=foreign_comment).exists())

    def test_wiki_the_caller_has_not_pinned_is_not_found(self) -> None:
        """The wiki visibility gate applies to reactions like every other handler."""
        unpinned_location = baker.make("dashboard.Location")
        unpinned_wiki = baker.make("dashboard.Wiki", location=unpinned_location, name="Unknown")
        hidden_comment = baker.make("dashboard.Comment", wiki=unpinned_wiki, profile=self.profile, text="Hidden")
        url = f"{BASE}/{unpinned_location.ensure_slug()}/comments/{hidden_comment.pk}/reactions/{THUMBS_UP}/"

        response = self.client.put(url, **self._headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})

    def test_reacting_to_someone_elses_comment_is_allowed(self) -> None:
        """Reactions are not owner-scoped - a wiki thread is shared community content.

        Worth pinning explicitly: the neighbouring DELETE-comment endpoint *is*
        author-scoped, and a mixin that copied that scoping would break the
        feature outright while looking more secure.

        The author is given ``comment_visibility = ANYONE`` deliberately. The
        default is ``ANYTHING_IN_COMMON``, under which a brand-new profile's
        comment is invisible to this caller - so a fixture at the default was
        asserting that a comment the *list* endpoint hides can still be reacted
        to by id, which is the enumeration hole ``comment_is_visible`` closes,
        not the not-owner-scoped behavior this test is for. ANYONE isolates the
        property under test.
        """
        other = Profile.objects.get(user=baker.make(User))
        other.comment_visibility = VisibilityChoice.ANYONE
        other.save(update_fields=["comment_visibility"])
        theirs = baker.make("dashboard.Comment", wiki=self.wiki, profile=other, text="Theirs")

        response = self.client.put(self._url(THUMBS_UP, comment_id=theirs.pk), **self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Reaction.objects.filter(comment=theirs, profile=self.profile).exists())

    def test_reacting_to_a_comment_hidden_from_the_caller_is_not_found(self) -> None:
        """A comment the thread listing drops cannot be reached by guessing its id.

        The counterpart to the test above: wiki scope alone left every comment
        on a visible wiki reactable by sequential id, including those
        ``visible_comment_tree`` withholds. Reacting returned a summary (so the
        id was confirmed) and notified an author whose comments this caller is
        not permitted to read.
        """
        other = Profile.objects.get(user=baker.make(User))
        other.comment_visibility = VisibilityChoice.FRIENDS
        other.save(update_fields=["comment_visibility"])
        hidden = baker.make("dashboard.Comment", wiki=self.wiki, profile=other, text="Hidden")

        response = self.client.put(self._url(THUMBS_UP, comment_id=hidden.pk), **self._headers())
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Reaction.objects.filter(comment=hidden, profile=self.profile).exists())

    def test_read_only_scope_cannot_react(self) -> None:
        """Reacting is a write; ``wiki:read`` alone must not reach it."""
        raw = self._key_with_scopes([ApiKeyScope.WIKI_READ.value])
        response = self.client.put(self._url(THUMBS_UP), **self._headers(raw))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Reaction.objects.filter(comment=self.comment).exists())

    def test_unrelated_scope_cannot_react(self) -> None:
        """A key scoped for some other domain reaches nothing here."""
        raw = self._key_with_scopes([ApiKeyScope.TRIPS_WRITE.value])
        response = self.client.delete(self._url(THUMBS_UP), **self._headers(raw))
        self.assertEqual(response.status_code, 403)

    def test_another_users_reaction_is_untouched_by_delete(self) -> None:
        """DELETE removes only the caller's own row, never the whole emoji."""
        other = Profile.objects.get(user=baker.make(User))
        Reaction.objects.create(profile=other, comment=self.comment, emoji="👍")
        self.client.put(self._url(THUMBS_UP), **self._headers())

        response = self.client.delete(self._url(THUMBS_UP), **self._headers())
        self.assertEqual(response.json()["reactions"]["👍"]["count"], 1)
        self.assertFalse(response.json()["reactions"]["👍"]["reacted"])
        self.assertTrue(Reaction.objects.filter(comment=self.comment, profile=other).exists())
