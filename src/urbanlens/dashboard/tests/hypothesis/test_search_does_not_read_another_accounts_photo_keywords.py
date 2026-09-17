"""Searching your own photos must not read keyword rows belonging to a photo you cannot see.

``test_search_does_not_read_another_accounts_photo_labels.py`` already covers
``PhotoSearchProvider``'s ``labels__name`` path. Its field list (``apply_text``'s call in
``PhotoSearchProvider.search``) also feeds ``keywords__keyword`` - Image's reverse foreign key to
``ImageKeyword`` (``related_name="keywords"``, populated by photo-keyword plugins: embedded XMP/IPTC
tags, AI vision descriptions, content classifiers). Same shape as labels, a different relation: the
unscoped ``Exists(Image._base_manager.filter(keywords__keyword__icontains=..., pk=OuterRef("pk")))``
semijoin reads another account's photo-keyword rows regardless of whether the viewer can see that
photo. No index covers ``ImageKeyword.keyword`` for this ``icontains`` path, so both the matching and
non-matching variant are expected to reproduce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from model_bakery import baker
import pytest

from urbanlens.core.tests.explain import relations_read, rows_examined
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.keyword import ImageKeyword
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PhotoSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through the viewer's own photo's keyword.
TERM = "quokka"

#: A word the viewer never searches for, for keywords that must be scanned to be rejected.
OTHER = "wombat"

#: Keywords the stranger's photo holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's keywords were read.
TOLERANCE = 20

_REASON = (
    "Same mechanism as P123, over PhotoSearchProvider's keywords__keyword instead of labels__name: the "
    "keyword semi-join reads the whole dashboard_image_keywords table, because it runs against "
    "Image._base_manager with no access scoping. Fixing it needs the join reordered to drive from the "
    "photo, not the keyword."
)


class _PhotoKeywordCase(TestCase):
    """A viewer with one photo matching through its own keyword, and a stranger's photo with unrelated keywords."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.keyworded = 0

        self.mine = self._image(self.viewer)
        baker.make(ImageKeyword, image=self.mine, source="test", keyword=f"{TERM} mine")
        self.their_image = self._image(self.stranger)
        self.seed_strangers_keywords(FIRST_BATCH)

    def _image(self, profile: Profile) -> Image:
        return Image.objects.create(
            image=SimpleUploadedFile(f"p{profile.pk}.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
            profile=profile,
            caption="",
        )

    def seed_strangers_keywords(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's photo *count* more keywords, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.keyworded += 1
            baker.make(ImageKeyword, image=self.their_image, source="test", keyword=f"{word} theirs {self.keyworded}")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {ImageKeyword._meta.db_table}, {Image._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's bare-term search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = PhotoSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def keyword_reading_statements(self) -> list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]:
        _, captured = self.search()
        table = ImageKeyword._meta.db_table
        return [(sql, params) for sql, params in captured if table in sql]

    def rows_read_searching(self) -> int:
        statements = self.keyword_reading_statements()
        self.assertTrue(
            statements, "no statement of the search mentioned the image-keyword table, so nothing was measured"
        )
        return sum(rows_examined(sql, params) for sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.seed_strangers_keywords(count, matching=matching)
        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            per_relation = [relations_read(sql, params) for sql, params in self.keyword_reading_statements()]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's photo search read {before} rows, then {after} after a stranger added {count} "
                f"{kind} keywords of their own - {after - before} more rows for a photo the viewer has "
                f"never earned access to. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPhotoKeywordsTests(_PhotoKeywordCase):
    """One account's photo keywords must not be read to answer a different account's photo search."""

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_matching_keywords(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_unrelated_keywords(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_PhotoKeywordCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_photo(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the photo whose keyword matches the term, so the "
            "measured statement is not the one the defect is about",
        )

    def test_the_strangers_matching_keywords_match_the_term(self) -> None:
        self.seed_strangers_keywords(SECOND_BATCH)
        matching = ImageKeyword.objects.filter(image=self.their_image, keyword__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded keywords do not match the search term"
        )

    def test_the_strangers_unrelated_keywords_do_not_match_the_term(self) -> None:
        self.seed_strangers_keywords(SECOND_BATCH, matching=False)
        matching = ImageKeyword.objects.filter(image=self.their_image, keyword__icontains=TERM).count()
        self.assertEqual(matching, FIRST_BATCH, "the non-matching seed matched the search term after all")

    def test_the_strangers_photo_is_not_visible_to_the_viewer(self) -> None:
        visible = Image.objects.visible_to(self.viewer).filter(pk=self.their_image.pk).count()
        self.assertEqual(
            visible, 0, "the stranger's photo is visible to the viewer, so reading its keywords is correct"
        )

    def test_the_measured_statement_reads_the_image_keyword_table(self) -> None:
        statements = self.keyword_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the image-keyword table")
        touched: set[str] = set()
        for sql, params in statements:
            touched.update(relations_read(sql, params))
        self.assertIn(
            ImageKeyword._meta.db_table,
            touched,
            f"no plan read the image-keyword table; relations read were {sorted(touched)}",
        )
