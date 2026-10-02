"""External API writes on a pin's own notes, names, links, visits, comments, rating and article (P29).

Each asserts the owner's write lands, that another account's key is answered 404 and nothing changes, that a row of
another pin is not reachable through the owner's pin, that anonymous and a key without the write scope are refused,
and that a malformed body is a 4xx rather than a 500 or a silent wrong write.
"""

from __future__ import annotations

import datetime

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.wiki.articles import get_article, save_article_checked
from urbanlens.dashboard.tests.hypothesis.external_api_helpers import ExternalApiRouteCase


class _PinFixture(ExternalApiRouteCase):
    """The owner's pin, a second pin of theirs, and a pin of the stranger's."""

    scopes = (ApiKeyScope.PINS_READ, ApiKeyScope.PINS_WRITE, ApiKeyScope.VISITS_READ, ApiKeyScope.VISITS_WRITE)
    read_scopes = (ApiKeyScope.PINS_READ, ApiKeyScope.VISITS_READ)

    def setUp(self) -> None:
        super().setUp()
        self.pin = create_pin_for_profile(self.owner, name="Old Mill", latitude=42.5, longitude=-73.5).pin
        self.other_pin = create_pin_for_profile(self.owner, name="Gasworks", latitude=42.6, longitude=-73.6).pin
        self.foreign_pin = create_pin_for_profile(self.stranger, name="Their Pin", latitude=10.0, longitude=10.0).pin
        self.slug = self.pin.slug


class ExternalPinNotesRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:pins.notes", args=[self.slug])

    def _notes(self) -> list[str]:
        return list(PinNote.objects.filter(pin=self.pin).values_list("text", flat=True))

    def test_the_owner_adds_a_note(self) -> None:
        response = self.send("post", self.url, {"text": "  Catwalk is rotten  "})

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self._notes(), ["Catwalk is rotten"])

    def test_a_stranger_gets_404_and_adds_nothing(self) -> None:
        self.assertEqual(self.send("post", self.url, {"text": "graffiti"}, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._notes(), [])

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url, {"text": "x"})
        self.assertEqual(self._notes(), [])

    def test_a_blank_overlong_or_malformed_note_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("post", self.url)
        for body in ({"text": "   "}, {"text": "x" * 100_000}, {"text": None}, {"text": ["a"]}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.send("post", self.url, body).status_code, 400)
        self.assertEqual(self._notes(), [])


class ExternalPinNoteDetailRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.note = PinNote.objects.create(pin=self.pin, text="Catwalk is rotten")
        self.url = reverse("external_api:pins.notes.detail", args=[self.slug, self.note.pk])

    def test_the_owner_deletes_the_note(self) -> None:
        self.assertEqual(self.send("delete", self.url).status_code, 204)
        self.assertFalse(PinNote.objects.filter(pk=self.note.pk).exists())

    def test_a_note_of_another_pin_is_404_through_this_one(self) -> None:
        elsewhere = PinNote.objects.create(pin=self.other_pin, text="Gas leak")
        theirs = PinNote.objects.create(pin=self.foreign_pin, text="Theirs")

        for note in (elsewhere, theirs):
            url = reverse("external_api:pins.notes.detail", args=[self.slug, note.pk])
            self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertEqual(PinNote.objects.filter(pk__in=[elsewhere.pk, theirs.pk]).count(), 2)

    def test_a_stranger_gets_404_and_the_note_survives(self) -> None:
        self.assertEqual(self.send("delete", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertTrue(PinNote.objects.filter(pk=self.note.pk).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("delete", self.url)
        self.assertTrue(PinNote.objects.filter(pk=self.note.pk).exists())


class ExternalPinAliasesRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:pins.aliases", args=[self.slug])

    def _names(self) -> set[str]:
        return set(PinAlias.objects.filter(pin=self.pin).values_list("name", flat=True))

    def test_the_owner_adds_a_name(self) -> None:
        before = self._names()

        response = self.send("post", self.url, {"name": "The Mill", "kind": "nickname"})

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self._names() - before, {"The Mill"})
        self.assertEqual(PinAlias.objects.get(pin=self.pin, name="The Mill").kind, "nickname")

    def test_a_case_variant_of_an_existing_name_is_409(self) -> None:
        self.send("post", self.url, {"name": "The Mill"})
        before = self._names()

        self.assertEqual(self.send("post", self.url, {"name": "THE MILL"}).status_code, 409)
        self.assertEqual(self._names(), before)

    def test_a_stranger_gets_404_and_adds_nothing(self) -> None:
        before = self._names()

        self.assertEqual(self.send("post", self.url, {"name": "Pwned"}, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._names(), before)

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        before = self._names()

        self.assert_refused_without_credentials("post", self.url, {"name": "x"})
        self.assertEqual(self._names(), before)

    def test_a_name_with_nothing_left_once_sanitized_is_400_not_a_500(self) -> None:
        before = self._names()

        for name in ("\U0001f525\U0001f525", "<>", "   "):
            with self.subTest(name=name):
                self.assertEqual(self.send("post", self.url, {"name": name}).status_code, 400)
        self.assertEqual(self._names(), before)

    def test_a_malformed_body_or_unknown_kind_is_400(self) -> None:
        before = self._names()

        self.assert_malformed_bodies_are_4xx("post", self.url)
        for body in ({"name": "x", "kind": "official-ish"}, {"name": "x" * 256}, {"name": ["x"]}):
            with self.subTest(body=body):
                self.assertEqual(self.send("post", self.url, body).status_code, 400)
        self.assertEqual(self._names(), before)


class ExternalPinAliasDetailRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.alias = PinAlias.objects.create(pin=self.pin, name="The Mill")
        self.url = reverse("external_api:pins.aliases.detail", args=[self.slug, self.alias.pk])

    def test_the_owner_removes_a_name(self) -> None:
        self.assertEqual(self.send("delete", self.url).status_code, 204)
        self.assertFalse(PinAlias.objects.filter(pk=self.alias.pk).exists())

    def test_the_current_name_cannot_be_removed(self) -> None:
        current = PinAlias.objects.filter(pin=self.pin, name="Old Mill").first() or PinAlias.objects.create(
            pin=self.pin, name="Old Mill"
        )
        url = reverse("external_api:pins.aliases.detail", args=[self.slug, current.pk])

        self.assertEqual(self.send("delete", url).status_code, 400)
        self.assertTrue(PinAlias.objects.filter(pk=current.pk).exists())

    def test_a_name_of_another_pin_is_404_through_this_one(self) -> None:
        theirs = PinAlias.objects.create(pin=self.foreign_pin, name="Their Mill")

        url = reverse("external_api:pins.aliases.detail", args=[self.slug, theirs.pk])
        self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertTrue(PinAlias.objects.filter(pk=theirs.pk).exists())

    def test_a_stranger_gets_404_and_the_name_survives(self) -> None:
        self.assertEqual(self.send("delete", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertTrue(PinAlias.objects.filter(pk=self.alias.pk).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("delete", self.url)
        self.assertTrue(PinAlias.objects.filter(pk=self.alias.pk).exists())


class ExternalPinAliasUseRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.alias = PinAlias.objects.create(pin=self.pin, name="The Mill")
        self.url = reverse("external_api:pins.aliases.use", args=[self.slug, self.alias.pk])

    def _name(self, pin: Pin | None = None) -> str:
        return Pin.objects.get(pk=(pin or self.pin).pk).effective_name

    def test_the_owner_renames_the_pin_to_the_alias(self) -> None:
        response = self.send("post", self.url)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._name(), "The Mill")

    def test_a_name_of_another_pin_is_404_and_renames_nothing(self) -> None:
        theirs = PinAlias.objects.create(pin=self.foreign_pin, name="Hijacked")

        url = reverse("external_api:pins.aliases.use", args=[self.slug, theirs.pk])
        self.assertEqual(self.send("post", url).status_code, 404)
        self.assertEqual(self._name(), "Old Mill")
        self.assertEqual(self._name(self.foreign_pin), "Their Pin")

    def test_a_stranger_gets_404_and_renames_nothing(self) -> None:
        self.assertEqual(self.send("post", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._name(), "Old Mill")

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url)
        self.assertEqual(self._name(), "Old Mill")

    def test_a_garbled_body_is_not_a_500(self) -> None:
        self.assertLess(self.send("post", self.url, "[1,").status_code, 500)


class ExternalPinLinksRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:pins.links", args=[self.slug])

    def _urls(self) -> list[str]:
        return list(PinLink.objects.filter(pin=self.pin).values_list("url", flat=True))

    def test_the_owner_adds_a_link(self) -> None:
        response = self.send("post", self.url, {"name": "History", "url": "https://example.com/mill"})

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self._urls(), ["https://example.com/mill"])

    def test_the_same_url_twice_is_409(self) -> None:
        self.send("post", self.url, {"url": "https://example.com/mill"})

        self.assertEqual(self.send("post", self.url, {"url": "https://example.com/mill"}).status_code, 409)
        self.assertEqual(len(self._urls()), 1)

    def test_a_stranger_gets_404_and_adds_nothing(self) -> None:
        response = self.send("post", self.url, {"url": "https://evil.example"}, auth=self.stranger_auth)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._urls(), [])

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url, {"url": "https://example.com"})
        self.assertEqual(self._urls(), [])

    def test_a_script_url_or_malformed_body_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("post", self.url)
        for body in (
            {"url": "javascript:alert(1)"},
            {"url": "data:text/html,hi"},
            {"url": ""},
            {"url": ["https://example.com"]},
            {"name": "x"},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.send("post", self.url, body).status_code, 400)
        self.assertEqual(self._urls(), [])


class ExternalPinLinkDetailRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.link = PinLink.objects.create(pin=self.pin, name="History", url="https://example.com/mill")
        self.url = reverse("external_api:pins.links.detail", args=[self.slug, self.link.pk])

    def test_the_owner_removes_a_link(self) -> None:
        self.assertEqual(self.send("delete", self.url).status_code, 204)
        self.assertFalse(PinLink.objects.filter(pk=self.link.pk).exists())

    def test_a_link_of_another_pin_is_404_through_this_one(self) -> None:
        theirs = PinLink.objects.create(pin=self.foreign_pin, name="Theirs", url="https://example.com/theirs")

        url = reverse("external_api:pins.links.detail", args=[self.slug, theirs.pk])
        self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertTrue(PinLink.objects.filter(pk=theirs.pk).exists())

    def test_a_stranger_gets_404_and_the_link_survives(self) -> None:
        self.assertEqual(self.send("delete", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertTrue(PinLink.objects.filter(pk=self.link.pk).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("delete", self.url)
        self.assertTrue(PinLink.objects.filter(pk=self.link.pk).exists())


class ExternalPinVisitDetailRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.when = timezone.now() - datetime.timedelta(days=3)
        self.visit = baker.make(PinVisit, pin=self.pin, visited_at=self.when, notes="Muddy")
        self.url = reverse("external_api:pins.visits.detail", args=[self.slug, self.visit.pk])

    def _visit(self) -> tuple:
        return PinVisit.objects.values_list("visited_at", "notes").get(pk=self.visit.pk)

    def test_the_owner_edits_then_deletes_a_visit(self) -> None:
        response = self.send("patch", self.url, {"notes": "Dry"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._visit(), (self.when, "Dry"))
        self.assertEqual(self.send("delete", self.url).status_code, 204)
        self.assertFalse(PinVisit.objects.filter(pk=self.visit.pk).exists())

    def test_a_visit_of_another_pin_is_404_through_this_one(self) -> None:
        theirs = baker.make(PinVisit, pin=self.foreign_pin, visited_at=self.when, notes="Theirs")

        url = reverse("external_api:pins.visits.detail", args=[self.slug, theirs.pk])
        self.assertEqual(self.send("patch", url, {"notes": "mine now"}).status_code, 404)
        self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertEqual(PinVisit.objects.values_list("notes", flat=True).get(pk=theirs.pk), "Theirs")

    def test_a_stranger_gets_404_and_nothing_changes(self) -> None:
        self.assertEqual(self.send("patch", self.url, {"notes": "x"}, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self.send("delete", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._visit(), (self.when, "Muddy"))

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("patch", self.url, {"notes": "x"})
        self.assert_refused_without_credentials("delete", self.url)
        self.assertEqual(self._visit(), (self.when, "Muddy"))

    def test_a_future_garbled_or_malformed_edit_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("patch", self.url)
        future = (timezone.now() + datetime.timedelta(days=30)).isoformat()
        for body in ({"visited_at": future}, {"visited_at": "yesterday"}, {"notes": "x" * 100_000}, {"notes": ["x"]}):
            with self.subTest(body=body):
                self.assertEqual(self.send("patch", self.url, body).status_code, 400)
        self.assertEqual(self._visit(), (self.when, "Muddy"))


class ExternalPinCommentsRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:pins.comments", args=[self.slug])

    def _texts(self, pin: Pin | None = None) -> list[str]:
        return list(Comment.objects.filter(pin=pin or self.pin).values_list("text", flat=True))

    def test_the_owner_comments_and_replies(self) -> None:
        first = self.send("post", self.url, {"text": "Roof is gone"})
        reply = self.send("post", self.url, {"text": "Still gone", "parent_id": first.json()["id"]})

        self.assertEqual((first.status_code, reply.status_code), (201, 201), reply.content)
        self.assertEqual(Comment.objects.get(pk=reply.json()["id"]).parent_id, first.json()["id"])

    def test_a_reply_to_a_comment_on_another_pin_is_404(self) -> None:
        theirs = baker.make(Comment, pin=self.foreign_pin, profile=self.stranger, text="Theirs")

        self.assertEqual(self.send("post", self.url, {"text": "Hi", "parent_id": theirs.pk}).status_code, 404)
        self.assertEqual(self._texts(), [])

    def test_a_stranger_gets_404_and_comments_nothing(self) -> None:
        self.assertEqual(self.send("post", self.url, {"text": "Hi"}, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._texts(), [])

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url, {"text": "Hi"})
        self.assertEqual(self._texts(), [])

    def test_a_blank_overlong_or_malformed_comment_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("post", self.url)
        for body in ({"text": ""}, {"text": "x" * 100_000}, {"text": "Hi", "parent_id": "first"}, {"parent_id": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.send("post", self.url, body).status_code, 400)
        self.assertEqual(self._texts(), [])


class ExternalPinCommentDetailRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.comment = baker.make(Comment, pin=self.pin, profile=self.owner, text="Roof is gone")
        self.url = reverse("external_api:pins.comments.detail", args=[self.slug, self.comment.pk])

    def test_the_owner_deletes_their_comment(self) -> None:
        self.assertEqual(self.send("delete", self.url).status_code, 204)
        self.assertFalse(Comment.objects.filter(pk=self.comment.pk).exists())

    def test_a_comment_of_another_pin_is_404_through_this_one(self) -> None:
        theirs = baker.make(Comment, pin=self.foreign_pin, profile=self.stranger, text="Theirs")

        url = reverse("external_api:pins.comments.detail", args=[self.slug, theirs.pk])
        self.assertEqual(self.send("delete", url).status_code, 404)
        self.assertTrue(Comment.objects.filter(pk=theirs.pk).exists())

    def test_a_stranger_gets_404_and_the_comment_survives(self) -> None:
        self.assertEqual(self.send("delete", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertTrue(Comment.objects.filter(pk=self.comment.pk).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("delete", self.url)
        self.assertTrue(Comment.objects.filter(pk=self.comment.pk).exists())


class ExternalPinCommentReactionRouteTests(_PinFixture):
    emoji = "\U0001f525"

    def setUp(self) -> None:
        super().setUp()
        self.comment = baker.make(Comment, pin=self.pin, profile=self.owner, text="Roof is gone")
        self.url = reverse("external_api:pins.comments.reactions", args=[self.slug, self.comment.pk, self.emoji])

    def _reactions(self, comment: Comment | None = None) -> int:
        return Reaction.objects.filter(comment=comment or self.comment).count()

    def test_the_owner_reacts_once_and_removes_it(self) -> None:
        self.assertEqual(self.send("put", self.url).status_code, 200)
        self.assertEqual(self.send("put", self.url).status_code, 200)
        self.assertEqual(self._reactions(), 1)

        self.assertEqual(self.send("delete", self.url).status_code, 200)
        self.assertEqual(self._reactions(), 0)

    def test_a_comment_of_another_pin_is_404(self) -> None:
        theirs = baker.make(Comment, pin=self.foreign_pin, profile=self.stranger, text="Theirs")

        url = reverse("external_api:pins.comments.reactions", args=[self.slug, theirs.pk, self.emoji])
        self.assertEqual(self.send("put", url).status_code, 404)
        self.assertEqual(self._reactions(theirs), 0)

    def test_a_stranger_gets_404(self) -> None:
        self.assertEqual(self.send("put", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._reactions(), 0)

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("put", self.url)
        self.assertEqual(self._reactions(), 0)

    def test_an_unsupported_emoji_is_400(self) -> None:
        url = reverse("external_api:pins.comments.reactions", args=[self.slug, self.comment.pk, "x"])

        self.assertEqual(self.send("put", url).status_code, 400)
        self.assertEqual(self._reactions(), 0)


class ExternalPinReviewRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:pins.review", args=[self.slug])

    def _rating(self) -> int | None:
        return Review.objects.filter(pin=self.pin, profile=self.owner).values_list("rating", flat=True).first()

    def test_the_owner_rates_rerates_and_clears(self) -> None:
        self.assertEqual(self.send("put", self.url, {"rating": 4}).status_code, 201)
        self.assertEqual(self.send("put", self.url, {"rating": 2}).status_code, 200)
        self.assertEqual(self._rating(), 2)

        self.assertEqual(self.send("delete", self.url).status_code, 204)
        self.assertIsNone(self._rating())
        self.assertEqual(self.send("delete", self.url).status_code, 404)

    def test_a_stranger_gets_404_and_rates_nothing(self) -> None:
        self.assertEqual(self.send("put", self.url, {"rating": 1}, auth=self.stranger_auth).status_code, 404)
        self.assertFalse(Review.objects.filter(pin=self.pin).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("put", self.url, {"rating": 1})
        self.assertIsNone(self._rating())

    def test_an_out_of_range_or_malformed_rating_is_400(self) -> None:
        self.send("put", self.url, {"rating": 3})

        self.assert_malformed_bodies_are_4xx("put", self.url)
        for body in ({"rating": 6}, {"rating": -1}, {"rating": "five"}, {"rating": None}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.send("put", self.url, body).status_code, 400)
        self.assertEqual(self._rating(), 3)


class ExternalPinArticleRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:pins.article", args=[self.slug])

    def _content(self, pin: Pin | None = None) -> str | None:
        article = get_article(pin=pin or self.pin)
        return article.content if article is not None else None

    def test_the_owner_writes_then_edits_on_the_current_revision(self) -> None:
        first = self.send("put", self.url, {"content": "Built 1890.", "base_revision_id": None})
        second = self.send(
            "put", self.url, {"content": "Built 1891.", "base_revision_id": first.json()["base_revision_id"]}
        )

        self.assertEqual((first.status_code, second.status_code), (200, 200), second.content)
        self.assertEqual(self._content(), "Built 1891.")

    def test_an_edit_on_a_stale_revision_is_409_and_keeps_the_newer_text(self) -> None:
        first = self.send("put", self.url, {"content": "Built 1890.", "base_revision_id": None})
        self.send("put", self.url, {"content": "Built 1891.", "base_revision_id": first.json()["base_revision_id"]})

        stale = self.send(
            "put", self.url, {"content": "Built 1850.", "base_revision_id": first.json()["base_revision_id"]}
        )

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self._content(), "Built 1891.")

    def test_a_stranger_gets_404_and_writes_nothing(self) -> None:
        body = {"content": "Pwned", "base_revision_id": None}

        self.assertEqual(self.send("put", self.url, body, auth=self.stranger_auth).status_code, 404)
        self.assertIsNone(self._content())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("put", self.url, {"content": "x", "base_revision_id": None})
        self.assertIsNone(self._content())

    def test_a_malformed_save_is_400(self) -> None:
        self.assert_malformed_bodies_are_4xx("put", self.url)
        for body in ({"content": "x"}, {"content": ["x"], "base_revision_id": None}, {"base_revision_id": None}):
            with self.subTest(body=body):
                self.assertEqual(self.send("put", self.url, body).status_code, 400)
        self.assertIsNone(self._content())


class ExternalPinArticleRestoreRouteTests(_PinFixture):
    def setUp(self) -> None:
        super().setUp()
        _article, self.first = save_article_checked(
            editor=self.owner, content="Built 1890.", base_revision_id=None, pin=self.pin
        )
        save_article_checked(editor=self.owner, content="Built 1891.", base_revision_id=self.first.pk, pin=self.pin)
        self.url = reverse("external_api:pins.article.revisions.restore", args=[self.slug, self.first.pk])

    def _content(self) -> str:
        return get_article(pin=self.pin).content

    def test_the_owner_restores_an_older_revision(self) -> None:
        response = self.send("post", self.url)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._content(), "Built 1890.")
        self.assertTrue(ArticleRevision.objects.filter(restored_from=self.first).exists())

    def test_a_revision_of_another_pins_article_is_404(self) -> None:
        _theirs, revision = save_article_checked(
            editor=self.stranger, content="Secret", base_revision_id=None, pin=self.foreign_pin
        )

        url = reverse("external_api:pins.article.revisions.restore", args=[self.slug, revision.pk])
        self.assertEqual(self.send("post", url).status_code, 404)
        self.assertEqual(self._content(), "Built 1891.")

    def test_a_stranger_gets_404_and_restores_nothing(self) -> None:
        self.assertEqual(self.send("post", self.url, auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._content(), "Built 1891.")

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url)
        self.assertEqual(self._content(), "Built 1891.")

    def test_a_garbled_body_is_not_a_500(self) -> None:
        self.assertLess(self.send("post", self.url, "[1,").status_code, 500)
