"""External API community wiki writes: alias nickname toggle, comment reactions, history revert (P29).

Each asserts what a viewer of the wiki may do, that a caller who cannot see the wiki or the row is refused with the
same 404 and nothing changed, that anonymous and a read-only key are refused, and that a malformed request is a 4xx.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.aliases.model import AliasType, WikiAlias
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_WRITE = [ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value]


class _WikiApiFixture(TestCase):
    """A wiki the viewer has pinned, another wiki they have pinned, and a stranger who pinned neither."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer = baker.make(User)
        self.profile = self.viewer.profile
        self.stranger = baker.make(User)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, name="Old Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.slug = self.location.ensure_slug()
        self.other_location = baker.make(Location)
        self.other_wiki = baker.make(Wiki, location=self.other_location, name="Gasworks")
        baker.make(Pin, profile=self.profile, location=self.other_location)
        self.auth = self._key(self.viewer, _WRITE)
        self.read_only = self._key(self.viewer, [ApiKeyScope.WIKI_READ.value])
        self.stranger_auth = self._key(self.stranger, _WRITE)

    def _key(self, user: User, scopes: list[str]) -> dict:
        key, raw = generate_api_key(user, f"client {len(scopes)}")
        ApiKey.objects.filter(pk=key.pk).update(scopes=scopes)
        return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


class ExternalWikiAliasToggleNicknameRouteTests(_WikiApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.alias = baker.make(WikiAlias, wiki=self.wiki, name="The Mill", kind=AliasType.ALTERNATE)
        self.url = reverse("external_api:wikis.aliases.toggle_nickname", args=[self.slug, self.alias.pk])

    def _kind(self, alias: WikiAlias | None = None) -> str:
        return WikiAlias.objects.values_list("kind", flat=True).get(pk=(alias or self.alias).pk)

    def test_a_viewer_flips_it_both_ways(self) -> None:
        response = self.client.post(self.url, **self.auth)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._kind(), AliasType.NICKNAME)
        self.client.post(self.url, **self.auth)
        self.assertEqual(self._kind(), AliasType.ALTERNATE)

    def test_an_alias_of_another_wiki_is_404_and_untouched(self) -> None:
        foreign = baker.make(WikiAlias, wiki=self.other_wiki, name="Gas Plant", kind=AliasType.ALTERNATE)

        url = reverse("external_api:wikis.aliases.toggle_nickname", args=[self.slug, foreign.pk])
        self.assertEqual(self.client.post(url, **self.auth).status_code, 404)
        self.assertEqual(self._kind(foreign), AliasType.ALTERNATE)

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.assertEqual(self.client.post(self.url, **self.stranger_auth).status_code, 404)
        self.assertEqual(self._kind(), AliasType.ALTERNATE)

    def test_a_read_only_key_and_anonymous_are_refused(self) -> None:
        self.assertEqual(self.client.post(self.url, **self.read_only).status_code, 403)
        self.assertIn(self.client.post(self.url).status_code, (401, 403))
        self.assertEqual(self._kind(), AliasType.ALTERNATE)

    def test_a_garbled_body_is_ignored_not_a_500(self) -> None:
        response = self.client.post(self.url, "[1,", content_type="application/json", **self.auth)

        self.assertLess(response.status_code, 500)


class ExternalWikiCommentReactionRouteTests(_WikiApiFixture):
    emoji = "🔥"

    def setUp(self) -> None:
        super().setUp()
        self.author = self._commenter()
        self.comment = baker.make(Comment, wiki=self.wiki, profile=self.author, text="Roof is gone")
        self.url = self._url(self.comment)

    def _commenter(self):
        profile = baker.make(User).profile
        profile.comment_visibility = VisibilityChoice.ANYONE
        profile.save(update_fields=["comment_visibility"])
        return profile

    def _url(self, comment: Comment, emoji: str | None = None) -> str:
        return reverse("external_api:wikis.comments.reactions", args=[self.slug, comment.pk, emoji or self.emoji])

    def _reactions(self, comment: Comment | None = None) -> int:
        return Reaction.objects.filter(comment=comment or self.comment).count()

    def test_put_adds_once_and_delete_removes(self) -> None:
        first = self.client.put(self.url, **self.auth)
        second = self.client.put(self.url, **self.auth)

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(self._reactions(), 1)
        self.assertEqual(first.json()["reactions"][self.emoji], {"count": 1, "reacted": True})

        self.assertEqual(self.client.delete(self.url, **self.auth).status_code, 200)
        self.assertEqual(self._reactions(), 0)

    def test_an_unsupported_emoji_is_400(self) -> None:
        response = self.client.put(self._url(self.comment, "x"), **self.auth)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._reactions(), 0)

    def test_a_comment_of_another_wiki_is_404(self) -> None:
        foreign = baker.make(Comment, wiki=self.other_wiki, profile=self.author, text="Elsewhere")

        self.assertEqual(self.client.put(self._url(foreign), **self.auth).status_code, 404)
        self.assertEqual(self._reactions(foreign), 0)

    def test_a_comment_its_author_hides_from_the_viewer_is_404(self) -> None:
        self.author.comment_visibility = VisibilityChoice.NO_ONE
        self.author.save(update_fields=["comment_visibility"])

        self.assertEqual(self.client.put(self.url, **self.auth).status_code, 404)
        self.assertEqual(self._reactions(), 0)

    def test_a_reply_under_a_visible_comment_takes_a_reaction(self) -> None:
        reply = baker.make(Comment, wiki=self.wiki, profile=self._commenter(), parent=self.comment, text="Still there")

        self.assertEqual(self.client.put(self._url(reply), **self.auth).status_code, 200)
        self.assertEqual(self._reactions(reply), 1)

    def test_a_reply_under_a_comment_hidden_from_the_viewer_is_404(self) -> None:
        """The thread drops a hidden comment with its replies, so a reaction would confirm a reply the viewer never saw."""
        self.author.comment_visibility = VisibilityChoice.NO_ONE
        self.author.save(update_fields=["comment_visibility"])
        replier = self._commenter()
        reply = baker.make(Comment, wiki=self.wiki, profile=replier, parent=self.comment, text="Still standing")

        self.assertEqual(self.client.put(self._url(reply), **self.auth).status_code, 404)
        self.assertEqual(self._reactions(reply), 0)

    def test_the_dashboard_reaction_route_refuses_that_reply_too(self) -> None:
        self.author.comment_visibility = VisibilityChoice.NO_ONE
        self.author.save(update_fields=["comment_visibility"])
        reply = baker.make(Comment, wiki=self.wiki, profile=self._commenter(), parent=self.comment, text="Standing")
        self.client.force_login(self.viewer)

        response = self.client.post(reverse("comment.react", args=[reply.pk]), {"emoji": self.emoji})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._reactions(reply), 0)

    def test_the_viewers_own_reply_under_a_hidden_comment_still_takes_their_reaction(self) -> None:
        self.author.comment_visibility = VisibilityChoice.NO_ONE
        self.author.save(update_fields=["comment_visibility"])
        own_reply = baker.make(Comment, wiki=self.wiki, profile=self.profile, parent=self.comment, text="Mine")

        self.assertEqual(self.client.put(self._url(own_reply), **self.auth).status_code, 200)
        self.assertEqual(self._reactions(own_reply), 1)

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.assertEqual(self.client.put(self.url, **self.stranger_auth).status_code, 404)
        self.assertEqual(self._reactions(), 0)

    def test_a_read_only_key_and_anonymous_are_refused(self) -> None:
        self.assertEqual(self.client.put(self.url, **self.read_only).status_code, 403)
        self.assertIn(self.client.put(self.url).status_code, (401, 403))
        self.assertEqual(self._reactions(), 0)


class ExternalWikiHistoryRevertRouteTests(_WikiApiFixture):
    def setUp(self) -> None:
        super().setUp()
        Wiki.objects.filter(pk=self.wiki.pk).update(name="Old Mill Lofts")
        self.edit = baker.make(
            WikiEdit,
            wiki=self.wiki,
            editor=baker.make(User).profile,
            changes={"name": {"from": "Old Mill", "to": "Old Mill Lofts"}},
            reverted=False,
        )
        self.url = reverse("external_api:wikis.history.revert", args=[self.slug, self.edit.pk])

    def _name(self) -> str:
        return Wiki.objects.values_list("name", flat=True).get(pk=self.wiki.pk)

    def _reverted(self) -> bool:
        return WikiEdit.objects.values_list("reverted", flat=True).get(pk=self.edit.pk)

    def test_a_viewer_reverts_an_edit_and_the_revert_is_recorded(self) -> None:
        response = self.client.post(self.url, **self.auth)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._name(), "Old Mill")
        self.assertTrue(self._reverted())
        revert = WikiEdit.objects.get(wiki=self.wiki, is_revert=True)
        self.assertEqual(revert.editor_id, self.profile.pk)

    def test_reverting_it_twice_is_400(self) -> None:
        self.client.post(self.url, **self.auth)

        self.assertEqual(self.client.post(self.url, **self.auth).status_code, 400)
        self.assertEqual(WikiEdit.objects.filter(wiki=self.wiki, is_revert=True).count(), 1)

    def test_an_edit_overtaken_since_is_409_and_changes_nothing(self) -> None:
        Wiki.objects.filter(pk=self.wiki.pk).update(name="Mill House")

        self.assertEqual(self.client.post(self.url, **self.auth).status_code, 409)
        self.assertEqual(self._name(), "Mill House")
        self.assertFalse(self._reverted())

    def test_an_edit_of_another_wiki_is_404(self) -> None:
        url = reverse("external_api:wikis.history.revert", args=[self.other_location.ensure_slug(), self.edit.pk])

        self.assertEqual(self.client.post(url, **self.auth).status_code, 404)
        self.assertFalse(self._reverted())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.assertEqual(self.client.post(self.url, **self.stranger_auth).status_code, 404)
        self.assertEqual(self._name(), "Old Mill Lofts")
        self.assertFalse(self._reverted())

    def test_a_read_only_key_and_anonymous_are_refused(self) -> None:
        self.assertEqual(self.client.post(self.url, **self.read_only).status_code, 403)
        self.assertIn(self.client.post(self.url).status_code, (401, 403))
        self.assertEqual(self._name(), "Old Mill Lofts")
        self.assertFalse(self._reverted())
