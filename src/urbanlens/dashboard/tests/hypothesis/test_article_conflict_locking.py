"""The article conflict check has to be serialised, not just present."""

from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.wiki.articles import (
    ArticleConflictError,
    latest_revision_id,
    save_article_checked,
)
from urbanlens.dashboard.tests.hypothesis.viewer_projection_helpers import projecting


class ArticleConflictLockingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.editor = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make("dashboard.Pin", profile=self.editor)

    def _save(self, content: str, base_revision_id: int | None):
        return save_article_checked(
            editor=self.editor,
            content=content,
            edit_summary="",
            base_revision_id=base_revision_id,
            pin=self.pin,
            wiki=None,
        )

    def test_a_first_save_creates_the_article(self) -> None:
        article, revision = self._save("First draft.", None)

        self.assertIsNotNone(article)
        self.assertIsNotNone(revision)

    def test_a_matching_base_revision_saves(self) -> None:
        article, first = self._save("First draft.", None)

        _article, second = self._save("Second draft.", latest_revision_id(article))

        self.assertIsNotNone(second)
        self.assertNotEqual(second.pk, first.pk)

    def test_a_stale_base_revision_is_refused(self) -> None:
        article, first = self._save("First draft.", None)
        self._save("Someone else's edit.", latest_revision_id(article))

        with self.assertRaises(ArticleConflictError):
            self._save("My edit, based on the old text.", first.pk)

    def test_nothing_is_written_when_the_conflict_fires(self) -> None:
        """The docstring promises this; a partial write would be worse than a refusal."""
        from urbanlens.dashboard.models.article.model import ArticleRevision

        article, first = self._save("First draft.", None)
        self._save("Someone else's edit.", latest_revision_id(article))
        before = ArticleRevision.objects.count()

        with self.assertRaises(ArticleConflictError):
            self._save("My edit.", first.pk)

        self.assertEqual(ArticleRevision.objects.count(), before)

    def test_the_check_takes_a_row_lock(self) -> None:
        """The mechanism: without FOR UPDATE the check is a TOCTOU."""
        article, _first = self._save("First draft.", None)

        with CaptureQueriesContext(connection) as queries:
            self._save("Second draft.", latest_revision_id(article))

        locking = [q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"].upper()]
        self.assertTrue(locking, "the article row should be locked for the read-check-write")
        self.assertTrue(
            any("article" in sql.lower() for sql in locking),
            f"the lock should be on the article row, got: {locking[:2]}",
        )


class ProjectedViewerConflictCheckTests(TestCase):
    """A viewer whose projection narrows the revisions is checked against what they were shown, not the stored latest."""

    def setUp(self) -> None:
        super().setUp()
        self.author = Profile.objects.get(user=baker.make("auth.User"))
        self.stranger = Profile.objects.get(user=baker.make("auth.User"))
        self.viewer = Profile.objects.get(user=baker.make("auth.User"))
        self.wiki = baker.make("dashboard.Wiki")

    def _first_draft(self):
        article, first = save_article_checked(
            editor=self.author, content="First draft.", edit_summary="", base_revision_id=None, wiki=self.wiki
        )
        assert first is not None
        return article, first

    def test_a_revision_the_viewer_is_not_shown_does_not_conflict_their_save(self) -> None:
        article, first = self._first_draft()
        save_article_checked(
            editor=self.stranger,
            content="Not shown to the viewer.",
            edit_summary="",
            base_revision_id=latest_revision_id(article),
            wiki=self.wiki,
        )

        with projecting(hidden_profile_ids=[self.stranger.pk]):
            _article, revision = save_article_checked(
                editor=self.viewer,
                content="The viewer's edit, based on what they saw.",
                edit_summary="",
                base_revision_id=first.pk,
                viewer=self.viewer,
                wiki=self.wiki,
            )

        self.assertIsNotNone(revision)

    def test_a_revision_the_viewer_is_shown_still_conflicts(self) -> None:
        article, first = self._first_draft()
        save_article_checked(
            editor=self.viewer,
            content="The viewer's own earlier edit.",
            edit_summary="",
            base_revision_id=latest_revision_id(article),
            viewer=self.viewer,
            wiki=self.wiki,
        )

        with projecting(hidden_profile_ids=[self.stranger.pk]), self.assertRaises(ArticleConflictError):
            save_article_checked(
                editor=self.viewer,
                content="Based on stale info.",
                edit_summary="",
                base_revision_id=first.pk,
                viewer=self.viewer,
                wiki=self.wiki,
            )

    def test_with_no_projection_any_newer_revision_conflicts(self) -> None:
        article, first = self._first_draft()
        save_article_checked(
            editor=self.stranger,
            content="Newer.",
            edit_summary="",
            base_revision_id=latest_revision_id(article),
            wiki=self.wiki,
        )

        with self.assertRaises(ArticleConflictError):
            save_article_checked(
                editor=self.viewer,
                content="Stale.",
                edit_summary="",
                base_revision_id=first.pk,
                viewer=self.viewer,
                wiki=self.wiki,
            )
