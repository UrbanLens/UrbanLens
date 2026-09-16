"""Searching photos via `label:` must not read label rows belonging to a photo you cannot see.

``test_search_does_not_read_another_accounts_photo_labels.py`` already covers
``PhotoSearchProvider``'s bare-term path (``"labels__name"`` in ``apply_text``'s field list,
``icontains``). That file's sibling for Pin left one thing unchecked: "Whether Pin's and Photo's own
`label:` operator... has the same gap was not checked." This file is that check, for Photo.

``PhotoSearchProvider.search`` also calls ``apply_label_clause`` for the `label:`/`tag:` operator,
which filters through the same ``_semijoin(Image, "labels", ...)`` shape as the bare-term path -
``Exists(Image._base_manager.filter(condition, pk=OuterRef("pk")))``, unscoped to photos the viewer
can see. The one difference: `label:` matches with ``iexact``, not ``icontains``. P123's fix (a GIN
trigram index on ``Label.name``, migration 0049) targets ``icontains``'s compiled form specifically;
measured here rather than assumed, it says nothing about an ``iexact`` plan - so unlike the
bare-term path's matching variant (partially fixed), both variants of this operator stay open, the
same result already found for Wiki's and Pin's identical call to ``apply_label_clause``.
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
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PhotoSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The exact name of the viewer's own label - `label:` matches on the whole string (iexact).
MINE = "quokka-mine"

#: A name that matches nothing anywhere, for the true-negative variant.
NOWHERE = "wombat-nowhere"

#: Labels the stranger's photo holds before and after the growth step. Never equal to MINE or NOWHERE.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's labels were read.
TOLERANCE = 20

_REASON = (
    "Same semijoin as P123, over apply_label_clause's iexact match instead of icontains: the whole "
    "label table is read regardless of a stranger's growing, unrelated labels, because the semijoin "
    "runs against Image._base_manager with no access scoping at all. Fixing it needs the join reordered "
    "to drive from the photo, not the label, same as the bare-term path."
)


class _PhotoLabelOperatorCase(TestCase):
    """A viewer with one photo (one exactly-named label), and a stranger's photo whose unrelated
    labels share the table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.labelled = 0

        self.mine = self._image(self.viewer)
        self.mine.labels.add(baker.make(Label, profile=self.viewer, kind=KIND_TAG, name=MINE))
        self.their_image = self._image(self.stranger)
        self.seed_strangers_labels(FIRST_BATCH)

    def _image(self, profile: Profile) -> Image:
        return Image.objects.create(
            image=SimpleUploadedFile(f"p{profile.pk}.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
            profile=profile,
            caption="",
        )

    def seed_strangers_labels(self, count: int) -> None:
        """Give the stranger's photo *count* more labels, none of them equal to MINE or NOWHERE, then re-analyse."""
        labels = []
        for _ in range(count):
            self.labelled += 1
            labels.append(
                baker.make(Label, profile=self.stranger, kind=KIND_TAG, name=f"stranger-label-{self.labelled}")
            )
        self.their_image.labels.add(*labels)
        with connection.cursor() as cursor:
            cursor.execute(
                f"ANALYZE {Label._meta.db_table}, {Image._meta.db_table}, {Image.labels.through._meta.db_table}",  # noqa: S608 - table names from the ORM, not from input
            )

    def search(self, term: str) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run `label:"term"` as the viewer, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = PhotoSearchProvider().search(self.viewer, parse_query(f'label:"{term}"'), 20)
        return list(results), captured

    def label_reading_statements(self, term: str) -> list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]:
        """The statements of a `label:"term"` search that read the label table at all."""
        _, captured = self.search(term)
        table = Label._meta.db_table
        return [(sql, params) for sql, params in captured if table in sql]

    def rows_read_searching(self, term: str) -> int:
        """Rows a `label:"term"` search reads, across every statement touching the label table."""
        statements = self.label_reading_statements(term)
        self.assertTrue(statements, "no statement of the search mentioned the label table, so nothing was measured")
        return sum(rows_examined(sql, params) for sql, params in statements)

    def assertDoesNotGrow(self, term: str) -> None:
        """Assert a `label:"term"` search reads no more rows after the stranger's photo gains more unrelated labels."""
        before = self.rows_read_searching(term)

        self.seed_strangers_labels(SECOND_BATCH)

        after = self.rows_read_searching(term)
        if after > before + TOLERANCE:
            per_relation = [relations_read(sql, params) for sql, params in self.label_reading_statements(term)]
            self.fail(
                f"the viewer's label:\"{term}\" search read {before} rows, then {after} after a stranger's photo "
                f"gained {SECOND_BATCH} unrelated labels - {after - before} more rows for a photo the viewer has "
                f"never earned access to. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPhotoLabelOperatorTests(_PhotoLabelOperatorCase):
    """One account's labels must not be read to answer a `label:` search for a different account's photo."""

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_labels_when_the_term_matches_elsewhere(self) -> None:
        """A `label:` search that DOES match the viewer's own photo must not also scan the stranger's."""
        self.assertDoesNotGrow(MINE)

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_labels_when_the_term_matches_nothing(self) -> None:
        """A `label:` search that matches nothing anywhere is the true negative: nothing to short-circuit on."""
        self.assertDoesNotGrow(NOWHERE)


class TheMeasurementIsRealTests(_PhotoLabelOperatorCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_photo(self) -> None:
        """Proves `label:"MINE"` matched through the label rather than returning early or matching nothing."""
        results, _ = self.search(MINE)

        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's label search did not return the photo whose label matches, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_term_that_matches_nothing_really_matches_nothing(self) -> None:
        """The premise of the non-matching variant: NOWHERE must not equal any seeded label, anywhere."""
        self.seed_strangers_labels(SECOND_BATCH)

        self.assertEqual(
            Label.objects.filter(name__iexact=NOWHERE).count(),
            0,
            "NOWHERE matched a real label, so the non-matching variant is measuring something else",
        )

    def test_the_strangers_labels_never_collide_with_mine_or_nowhere(self) -> None:
        """The premise of the growth step: the stranger's added rows are unrelated filler, not incidental matches."""
        self.seed_strangers_labels(SECOND_BATCH)

        colliding = Label.objects.filter(profile=self.stranger).filter(name__iexact=MINE) | Label.objects.filter(
            profile=self.stranger
        ).filter(
            name__iexact=NOWHERE,
        )

        self.assertEqual(colliding.count(), 0, "a stranger's seeded label collided with MINE or NOWHERE by name")

    def test_the_stranger_is_not_visible_to_the_viewer(self) -> None:
        """The premise of the whole file: these labels are none of the viewer's business."""
        visible = Label.objects.visible_to(self.viewer).filter(profile=self.stranger).count()

        self.assertEqual(visible, 0, "the stranger's labels are visible to the viewer, so reading them is correct")

    def test_the_measured_statement_reads_the_label_table(self) -> None:
        """Proves the plan being summed actually touches labels, rather than being some other statement."""
        statements = self.label_reading_statements(MINE)
        self.assertTrue(statements, "the search issued no statement mentioning the label table")

        touched: set[str] = set()
        for sql, params in statements:
            touched.update(relations_read(sql, params))

        self.assertIn(
            Label._meta.db_table,
            touched,
            f"no plan read the label table; relations read were {sorted(touched)}",
        )
