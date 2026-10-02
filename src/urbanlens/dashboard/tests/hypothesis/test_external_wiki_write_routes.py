"""External API community wiki writes: fields, names, links, article, comments and stat votes (P29).

The owner fixture has pinned the place, so it may see and edit its wiki; the stranger has not, and is answered the
same 404 as for a place with no wiki. Each asserts the viewer's write lands, the stranger changes nothing, anonymous
and a key without the write scope are refused, and a malformed body is a 4xx rather than a 500 or a silent wrong
write.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.links.model import WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote
from urbanlens.dashboard.services.wiki.articles import get_article, save_article_checked
from urbanlens.dashboard.tests.hypothesis.external_api_helpers import ExternalApiRouteCase


class _WikiFixture(ExternalApiRouteCase):
    """A wiki the owner has pinned, and a second wiki they have pinned too."""

    scopes = (ApiKeyScope.WIKI_READ, ApiKeyScope.WIKI_WRITE)
    read_scopes = (ApiKeyScope.WIKI_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, name="Old Mill")
        baker.make(Pin, profile=self.owner, location=self.location)
        self.slug = self.location.ensure_slug()
        self.other_location = baker.make(Location)
        self.other_wiki = baker.make(Wiki, location=self.other_location, name="Gasworks")
        baker.make(Pin, profile=self.owner, location=self.other_location)

    def _wiki(self) -> Wiki:
        return Wiki.objects.get(pk=self.wiki.pk)


class ExternalWikiDetailRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:wikis.detail", args=[self.slug])

    def test_a_viewer_edits_fields_and_the_edit_is_recorded(self) -> None:
        response = self.send(
            "patch", self.url, {"description": "Brick mill, 1890.", "security": {"fences": "everywhere"}}
        )

        self.assertEqual(response.status_code, 200, response.content)
        wiki = self._wiki()
        self.assertEqual((wiki.description, wiki.fences), ("Brick mill, 1890.", "everywhere"))
        self.assertEqual(WikiEdit.objects.get(wiki=self.wiki).editor_id, self.owner.pk)

    def test_someone_who_cannot_see_the_wiki_gets_404_and_changes_nothing(self) -> None:
        response = self.send("patch", self.url, {"description": "Defaced"}, auth=self.stranger_auth)

        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(self._wiki().description, "Defaced")
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("patch", self.url, {"description": "x"})
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_a_name_with_nothing_left_once_sanitized_is_400_and_keeps_the_name(self) -> None:
        for name in ("\U0001f525\U0001f525", "<>"):
            with self.subTest(name=name):
                self.assertEqual(self.send("patch", self.url, {"name": name}).status_code, 400)
        self.assertEqual(self._wiki().name, "Old Mill")
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_the_dashboard_edit_refuses_that_name_too(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(reverse("location.wiki.edit", args=[self.slug]), {"name": "\U0001f525"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._wiki().name, "Old Mill")

    def test_a_malformed_or_unknown_edit_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("patch", self.url)
        for body in (
            {},
            {"decription": "typo"},
            {"security": {"fences": "electrified"}},
            {"date_abandoned": "last year"},
            {"name": ""},
            {"name": "x" * 256},
            {"security": ["high"]},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.send("patch", self.url, body).status_code, 400)
        self.assertEqual(self._wiki().name, "Old Mill")
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())


class ExternalWikiAliasesRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:wikis.aliases", args=[self.slug])
        self.before = self._names()

    def _names(self) -> set[str]:
        return set(WikiAlias.objects.filter(wiki=self.wiki).values_list("name", flat=True))

    def test_a_viewer_adds_a_name_and_the_edit_is_recorded(self) -> None:
        response = self.send("post", self.url, {"name": "The Mill", "kind": "nickname"})

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self._names() - self.before, {"The Mill"})
        alias = WikiAlias.objects.get(wiki=self.wiki, name="The Mill")
        self.assertEqual((alias.kind, alias.created_by_id), ("nickname", self.owner.pk))
        self.assertTrue(
            WikiEdit.objects.filter(wiki=self.wiki, editor=self.owner, changes__has_key="alias_added").exists()
        )

    def test_a_case_variant_of_an_existing_name_is_409_not_a_500(self) -> None:
        self.send("post", self.url, {"name": "The Mill"})

        self.assertEqual(self.send("post", self.url, {"name": "THE MILL"}).status_code, 409)
        self.assertEqual(self._names() - self.before, {"The Mill"})

    def test_someone_who_cannot_see_the_wiki_gets_404_and_adds_nothing(self) -> None:
        self.assertEqual(self.send("post", self.url, {"name": "Pwned"}, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._names(), self.before)

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url, {"name": "x"})
        self.assertEqual(self._names(), self.before)

    def test_a_name_with_nothing_left_once_sanitized_is_400_not_a_blank_alias(self) -> None:
        for name in ("\U0001f525\U0001f525", "<>"):
            with self.subTest(name=name):
                self.assertEqual(self.send("post", self.url, {"name": name}).status_code, 400)
        self.assertEqual(self._names(), self.before)

    def test_an_unknown_kind_is_400_not_stored(self) -> None:
        for kind in ("bogus", "x" * 11, "Nickname"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    self.send("post", self.url, {"name": f"Mill {len(kind)}", "kind": kind}).status_code, 400
                )
        self.assertEqual(self._names(), self.before)

    def test_a_malformed_body_is_4xx(self) -> None:
        self.assert_malformed_bodies_are_4xx("post", self.url)
        for body in ({}, {"name": ["x"]}, {"name": "x" * 256}):
            with self.subTest(body=body):
                self.assertEqual(self.send("post", self.url, body).status_code, 400)
        self.assertEqual(self._names(), self.before)


class ExternalWikiAliasUseRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.alias = WikiAlias.objects.create(wiki=self.wiki, name="The Mill")
        self.url = reverse("external_api:wikis.aliases.use", args=[self.slug, self.alias.pk])

    def test_a_viewer_renames_the_wiki_and_the_rename_is_in_its_history(self) -> None:
        response = self.send("post", self.url)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._wiki().name, "The Mill")
        self.assertTrue(WikiEdit.objects.filter(wiki=self.wiki, editor=self.owner).exists())

    def test_a_name_of_another_wiki_is_404_and_renames_nothing(self) -> None:
        foreign = WikiAlias.objects.create(wiki=self.other_wiki, name="Gas Plant")

        url = reverse("external_api:wikis.aliases.use", args=[self.slug, foreign.pk])
        self.assertEqual(self.send("post", url).status_code, 404)
        self.assertEqual(self._wiki().name, "Old Mill")

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.assertEqual(self.send("post", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._wiki().name, "Old Mill")

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url)
        self.assertEqual(self._wiki().name, "Old Mill")

    def test_a_garbled_body_is_not_a_500(self) -> None:
        self.assertLess(self.send("post", self.url, "[1,").status_code, 500)

    def test_a_blank_alias_left_by_the_old_create_path_cannot_blank_the_wiki(self) -> None:
        blank = WikiAlias.objects.create(wiki=self.wiki, name="")
        self.client.force_login(self.owner_user)

        api = self.send("post", reverse("external_api:wikis.aliases.use", args=[self.slug, blank.pk]))
        dashboard = self.client.post(reverse("location.wiki.alias.use", args=[self.slug, blank.pk]))

        self.assertEqual((api.status_code, dashboard.status_code), (400, 400))
        self.assertEqual(self._wiki().name, "Old Mill")


class ExternalWikiLinksRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:wikis.links", args=[self.slug])

    def _urls(self) -> list[str]:
        return list(WikiLink.objects.filter(wiki=self.wiki).values_list("url", flat=True))

    def test_a_viewer_adds_a_link(self) -> None:
        response = self.send("post", self.url, {"name": "History", "url": "https://example.com/mill"})

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self._urls(), ["https://example.com/mill"])
        self.assertEqual(WikiLink.objects.get(wiki=self.wiki).created_by_id, self.owner.pk)

    def test_the_same_url_twice_is_400_in_the_error_envelope(self) -> None:
        self.send("post", self.url, {"url": "https://example.com/mill"})

        response = self.send("post", self.url, {"url": "https://example.com/mill"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertEqual(len(self._urls()), 1)

    def test_someone_who_cannot_see_the_wiki_gets_404_and_adds_nothing(self) -> None:
        response = self.send("post", self.url, {"url": "https://evil.example"}, auth=self.stranger_auth)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._urls(), [])

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url, {"url": "https://example.com"})
        self.assertEqual(self._urls(), [])

    def test_a_script_url_or_malformed_body_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("post", self.url)
        for body in ({"url": "javascript:alert(1)"}, {"url": ""}, {"url": ["https://example.com"]}, {"name": "x"}):
            with self.subTest(body=body):
                self.assertEqual(self.send("post", self.url, body).status_code, 400)
        self.assertEqual(self._urls(), [])


class ExternalWikiArticleRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:wikis.article", args=[self.slug])

    def _content(self) -> str | None:
        article = get_article(wiki=self.wiki)
        return article.content if article is not None else None

    def test_a_viewer_writes_then_edits_on_the_current_revision(self) -> None:
        first = self.send("put", self.url, {"content": "Built 1890.", "base_revision_id": None})
        second = self.send(
            "put", self.url, {"content": "Built 1891.", "base_revision_id": first.json()["base_revision_id"]}
        )

        self.assertEqual((first.status_code, second.status_code), (200, 200), second.content)
        self.assertEqual(self._content(), "Built 1891.")
        self.assertEqual(ArticleRevision.objects.filter(article__wiki=self.wiki, editor=self.owner).count(), 2)

    def test_an_edit_on_a_stale_revision_is_409_and_keeps_the_newer_text(self) -> None:
        first = self.send("put", self.url, {"content": "Built 1890.", "base_revision_id": None})
        self.send("put", self.url, {"content": "Built 1891.", "base_revision_id": first.json()["base_revision_id"]})

        stale = self.send(
            "put", self.url, {"content": "Built 1850.", "base_revision_id": first.json()["base_revision_id"]}
        )

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self._content(), "Built 1891.")

    def test_someone_who_cannot_see_the_wiki_gets_404_and_writes_nothing(self) -> None:
        body = {"content": "Pwned", "base_revision_id": None}

        self.assertEqual(self.send("put", self.url, body, auth=self.stranger_auth).status_code, 404)
        self.assertIsNone(self._content())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("put", self.url, {"content": "x", "base_revision_id": None})
        self.assertIsNone(self._content())

    def test_a_malformed_save_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("put", self.url)
        for body in (
            {"content": "x"},
            {"content": None, "base_revision_id": None},
            {"base_revision_id": "one"},
        ):
            with self.subTest(body=body):
                self.assertIn(self.send("put", self.url, body).status_code, range(400, 500))
        self.assertIsNone(self._content())


class ExternalWikiArticleRevisionDetailRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.other_editor = baker.make("auth.User").profile
        _article, self.theirs = save_article_checked(
            editor=self.other_editor, content="Built 1890.", base_revision_id=None, wiki=self.wiki
        )
        _article, self.mine = save_article_checked(
            editor=self.owner, content="Built 1891.", base_revision_id=self.theirs.pk, wiki=self.wiki
        )
        self.url = reverse("external_api:wikis.article.revisions.detail", args=[self.slug, self.mine.pk])

    def _exists(self, revision: ArticleRevision) -> bool:
        return ArticleRevision.objects.filter(pk=revision.pk).exists()

    def test_a_viewer_deletes_their_own_revision_and_the_article_text_stays(self) -> None:
        self.assertEqual(self.send("delete", self.url).status_code, 204)

        self.assertFalse(self._exists(self.mine))
        self.assertEqual(get_article(wiki=self.wiki).content, "Built 1891.")

    def test_someone_elses_revision_is_404_and_survives(self) -> None:
        url = reverse("external_api:wikis.article.revisions.detail", args=[self.slug, self.theirs.pk])

        self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertTrue(self._exists(self.theirs))

    def test_a_revision_of_another_wikis_article_is_404(self) -> None:
        _article, elsewhere = save_article_checked(
            editor=self.owner, content="Gas.", base_revision_id=None, wiki=self.other_wiki
        )
        url = reverse("external_api:wikis.article.revisions.detail", args=[self.slug, elsewhere.pk])

        self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertTrue(self._exists(elsewhere))

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.assertEqual(self.send("delete", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertTrue(self._exists(self.mine))

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("delete", self.url)
        self.assertTrue(self._exists(self.mine))


class ExternalWikiCommentsRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:wikis.comments", args=[self.slug])

    def _texts(self) -> list[str]:
        return list(Comment.objects.filter(wiki=self.wiki).values_list("text", flat=True))

    def test_a_viewer_comments_and_replies(self) -> None:
        first = self.send("post", self.url, {"text": "Roof is gone"})
        reply = self.send("post", self.url, {"text": "Still gone", "parent_id": first.json()["id"]})

        self.assertEqual((first.status_code, reply.status_code), (201, 201), reply.content)
        self.assertEqual(Comment.objects.get(pk=reply.json()["id"]).parent_id, first.json()["id"])

    def test_a_reply_to_a_comment_on_another_wiki_is_404(self) -> None:
        elsewhere = baker.make(Comment, wiki=self.other_wiki, profile=self.owner, text="Elsewhere")

        self.assertEqual(self.send("post", self.url, {"text": "Hi", "parent_id": elsewhere.pk}).status_code, 404)
        self.assertEqual(self._texts(), [])

    def test_someone_who_cannot_see_the_wiki_gets_404_and_comments_nothing(self) -> None:
        self.assertEqual(self.send("post", self.url, {"text": "Hi"}, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._texts(), [])

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url, {"text": "Hi"})
        self.assertEqual(self._texts(), [])

    def test_a_blank_overlong_or_malformed_comment_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("post", self.url)
        for body in (
            {"text": ""},
            {"text": "x" * 100_000},
            {"text": "Hi", "parent_id": "first"},
            {"text": ["Hi"]},
            {"text": "Hi", "parent_id": 10**30},
        ):
            with self.subTest(body=body):
                self.assertIn(self.send("post", self.url, body).status_code, range(400, 500))
        self.assertEqual(self._texts(), [])


class ExternalWikiCommentDetailRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.comment = baker.make(Comment, wiki=self.wiki, profile=self.owner, text="Roof is gone")
        self.url = reverse("external_api:wikis.comments.detail", args=[self.slug, self.comment.pk])

    def test_a_viewer_deletes_their_own_comment(self) -> None:
        self.assertEqual(self.send("delete", self.url).status_code, 204)
        self.assertFalse(Comment.objects.filter(pk=self.comment.pk).exists())

    def test_someone_elses_comment_is_404_and_survives(self) -> None:
        theirs = baker.make(Comment, wiki=self.wiki, profile=baker.make("auth.User").profile, text="Theirs")

        url = reverse("external_api:wikis.comments.detail", args=[self.slug, theirs.pk])
        self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertTrue(Comment.objects.filter(pk=theirs.pk).exists())

    def test_a_comment_of_another_wiki_is_404_through_this_one(self) -> None:
        elsewhere = baker.make(Comment, wiki=self.other_wiki, profile=self.owner, text="Elsewhere")

        url = reverse("external_api:wikis.comments.detail", args=[self.slug, elsewhere.pk])
        self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertTrue(Comment.objects.filter(pk=elsewhere.pk).exists())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.assertEqual(self.send("delete", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertTrue(Comment.objects.filter(pk=self.comment.pk).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("delete", self.url)
        self.assertTrue(Comment.objects.filter(pk=self.comment.pk).exists())


class ExternalWikiStatVoteRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:wikis.votes", args=[self.slug, "danger"])

    def _vote(self) -> int | None:
        return (
            WikiStatVote.objects.filter(wiki=self.wiki, profile=self.owner, field="danger")
            .values_list("value", flat=True)
            .first()
        )

    def test_a_viewer_votes_revotes_and_withdraws(self) -> None:
        self.assertEqual(self.send("put", self.url, {"value": 4}).status_code, 200)
        self.assertEqual(self.send("put", self.url, {"value": 2}).status_code, 200)
        self.assertEqual(self._vote(), 2)

        self.assertEqual(self.send("delete", self.url).status_code, 200)
        self.assertIsNone(self._vote())

    def test_an_unknown_field_is_404(self) -> None:
        url = reverse("external_api:wikis.votes", args=[self.slug, "beauty"])

        self.assertEqual(self.send("put", url, {"value": 3}).status_code, 404)
        self.assertFalse(WikiStatVote.objects.filter(wiki=self.wiki).exists())

    def test_someone_who_cannot_see_the_wiki_gets_404_and_votes_nothing(self) -> None:
        self.assertEqual(self.send("put", self.url, {"value": 5}, auth=self.stranger_auth).status_code, 404)
        self.assertFalse(WikiStatVote.objects.filter(wiki=self.wiki).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("put", self.url, {"value": 3})
        self.assertIsNone(self._vote())

    def test_an_out_of_range_or_malformed_vote_is_400_and_keeps_the_vote(self) -> None:
        self.send("put", self.url, {"value": 3})

        self.assert_malformed_bodies_are_4xx("put", self.url)
        for body in ({"value": 0}, {"value": 6}, {"value": "high"}, {"value": None}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.send("put", self.url, body).status_code, 400)
        self.assertEqual(self._vote(), 3)
