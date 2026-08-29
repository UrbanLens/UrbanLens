"""The article conflict check has to be serialised, not just present.

`save_article_checked` reads the latest revision id, compares it to the one the
editor started from, and then writes. Without a lock that sequence is a TOCTOU:
two editors who both loaded revision R both read `latest_id == R`, both pass the
check, and both append. One editor's save then silently stops being the current
article - the precise outcome the conflict check exists to prevent, and they were
told it succeeded.

Nothing else catches it. `ArticleRevision` carries no revision number and no
unique constraint, and "latest" is just `-created`, so there is no database-level
guard to fall back on. A *first* save is safe without a lock because
`Article.pin`/`.wiki` are `OneToOneField` - the second insert loses there.

The lock is asserted by inspecting the SQL actually issued, rather than by racing
two threads: a thread race is timing-dependent and would be flaky in CI, while
`FOR UPDATE` appearing in the statement is exactly the mechanism under test.
"""

from __future__ import annotations

from unittest import mock

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


class ConcealedViewerConflictCheckTests(TestCase):
    """A concealed viewer's conflict check is scoped to what they were shown, not the true latest.

    ``concealment_active`` is hardcoded False today (the reputation ledger it needs doesn't exist
    yet), so this branch is currently dead in production - mocked here so a regression is caught
    before the day it starts returning True, rather than after.
    """

    def setUp(self) -> None:
        super().setUp()
        self.author = Profile.objects.get(user=baker.make("auth.User"))
        self.stranger = Profile.objects.get(user=baker.make("auth.User"))
        self.viewer = Profile.objects.get(user=baker.make("auth.User"))
        self.wiki = baker.make("dashboard.Wiki")

    def test_a_revision_hidden_from_a_concealed_viewer_does_not_conflict_their_save(self) -> None:
        """The viewer's base was the last thing *they* could see - a stranger's edit past that isn't a conflict."""
        article, first = save_article_checked(
            editor=self.author,
            content="First draft.",
            edit_summary="",
            base_revision_id=None,
            wiki=self.wiki,
        )
        assert first is not None
        save_article_checked(
            editor=self.stranger,
            content="A stranger's edit, invisible to the viewer.",
            edit_summary="",
            base_revision_id=latest_revision_id(article),
            wiki=self.wiki,
        )

        with mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True):
            # Without the concealment carve-out, this would incorrectly conflict against
            # the stranger's revision, which this viewer was never shown.
            _article, revision = save_article_checked(
                editor=self.viewer,
                content="The viewer's edit, based on what they saw.",
                edit_summary="",
                base_revision_id=first.pk,
                viewer=self.viewer,
                wiki=self.wiki,
            )

        self.assertIsNotNone(revision)

    def test_a_visible_edit_still_conflicts_a_concealed_viewer(self) -> None:
        """The carve-out narrows what conflicts for a concealed viewer - it doesn't disable the check."""
        article, first = save_article_checked(
            editor=self.author,
            content="First draft.",
            edit_summary="",
            base_revision_id=None,
            wiki=self.wiki,
        )
        assert first is not None
        save_article_checked(
            editor=self.viewer,
            content="The viewer's own earlier edit.",
            edit_summary="",
            base_revision_id=latest_revision_id(article),
            viewer=self.viewer,
            wiki=self.wiki,
        )

        with (
            mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True),
            self.assertRaises(ArticleConflictError),
        ):
            save_article_checked(
                editor=self.viewer,
                content="Based on stale info.",
                edit_summary="",
                base_revision_id=first.pk,
                viewer=self.viewer,
                wiki=self.wiki,
            )
