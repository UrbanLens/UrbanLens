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
