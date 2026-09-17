"""Searching your own photos must not read label rows belonging to accounts you cannot see.

Same mechanism as P123 (``test_search_does_not_read_another_accounts_labels.py``), a different
provider: ``PhotoSearchProvider.search`` also calls ``apply_label_clause``, which filters through
``_semijoin(Image, "labels", ...)`` - an ``Exists(Image._base_manager.filter(...))`` subquery built
from the *unfiltered* manager, so it is not scoped to photos the viewer can see. A stranger's image
never needs to be visible to the viewer for its labels to sit in the same ``dashboard_labels`` table
the semijoin's inner query scans.

P123's fix (a GIN trigram index on ``Label.name`` matching ``icontains``'s compiled form, migration
0049) lives on the ``Label`` model itself, not on ``PinSearchProvider``, so it should in principle
help every provider that reaches labels through this same clause - and does, for the matching
variant. The non-matching variant needed the same bounded-semi-join rewrite as P123's own file.

**Fixed 2026-09-17.** ``_semijoin`` now bounds ``labels``' filter by the outer queryset's own
candidate primary keys, materialised as a concrete list first rather than left as a nested subquery -
see ``test_search_does_not_read_another_accounts_labels.py``'s module docstring for the measured
mechanism and ``docs/archive/PROBLEMS-ARCHIVE.md`` (formerly P123) for the full writeup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PhotoSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term.
TERM = "quokka"

#: A word the viewer never searches for, for labels that must be scanned to be rejected.
OTHER = "wombat"

#: Labels the stranger holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's labels were read.
TOLERANCE = 20


class _PhotoSearchCase(TestCase):
    """A viewer with one matching photo, and a stranger whose photo labels share the table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.labelled = 0

        self.mine = self._image(self.viewer)
        self.mine.labels.add(baker.make(Label, profile=self.viewer, kind=KIND_TAG, name=f"{TERM} mine"))
        self.their_image = self._image(self.stranger)
        self.seed_strangers_labels(FIRST_BATCH)

    def _image(self, profile: Profile) -> Image:
        return Image.objects.create(
            image=SimpleUploadedFile(f"p{profile.pk}.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
            profile=profile,
            caption="",
        )

    def seed_strangers_labels(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger *count* more photo labels, then re-analyse.

        Attached to the stranger's own image rather than left loose, for the same reason as P123: a
        label reachable from nothing the semi-join can pk-match would prove less about the defect.
        """
        word = TERM if matching else OTHER
        labels = []
        for _ in range(count):
            self.labelled += 1
            labels.append(
                baker.make(Label, profile=self.stranger, kind=KIND_TAG, name=f"{word} theirs {self.labelled}"),
            )
        self.their_image.labels.add(*labels)
        with connection.cursor() as cursor:
            cursor.execute(
                f"ANALYZE {Label._meta.db_table}, {Image._meta.db_table}, {Image.labels.through._meta.db_table}",  # noqa: S608 - table names from the ORM, not from input
            )

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = PhotoSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def label_reading_statements(
        self,
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the label table at all.

        Returning both, rather than just the filtered statements, lets a caller pass the
        filtered statements' original positions to ``session_preamble`` - a statement's plan
        depends on session GUCs active when it ran (see ``_semijoin``'s ``enable_seqscan``
        bracket), not just its SQL text, and that bracket's position is only meaningful against
        the one capture it came from.
        """
        _, captured = self.search()
        table = Label._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        """Rows the viewer's search reads, across every statement touching the label table."""
        captured, statements = self.label_reading_statements()
        self.assertTrue(statements, "no statement of the search mentioned the label table, so nothing was measured")
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        """Assert the viewer's search reads no more rows after the stranger gains *count* photo labels."""
        before = self.rows_read_searching()

        self.seed_strangers_labels(count, matching=matching)

        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            captured, statements = self.label_reading_statements()
            per_relation = [
                relations_read(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements
            ]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's photo search read {before} rows, then {after} after a stranger added {count} "
                f"{kind} labels of their own - {after - before} more rows for data the viewer cannot see. "
                f"Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPhotoLabelsTests(_PhotoSearchCase):
    """One account's photo labels must not be read to answer another account's photo search."""

    def test_it_does_not_read_a_strangers_matching_labels(self) -> None:
        """Rows the plan reads and then discards at the photo join, because the photo is not the viewer's."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_labels(self) -> None:
        """Rows the plan reads only to reject on the name. Fixed 2026-09-17 by the bounded semi-join."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_PhotoSearchCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_photo(self) -> None:
        """Proves the search matched through a label rather than returning early or matching nothing."""
        results, _ = self.search()

        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the photo whose label matches the term, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_strangers_matching_labels_match_the_term(self) -> None:
        """Proves the seeded rows are rows a scan would have to look at, not inert filler."""
        self.seed_strangers_labels(SECOND_BATCH)

        matching = Label.objects.filter(profile=self.stranger, name__icontains=TERM).count()

        self.assertEqual(
            matching,
            FIRST_BATCH + SECOND_BATCH,
            "the stranger's seeded labels do not match the search term, so the matching-labels test is "
            "measuring the non-matching case twice",
        )

    def test_the_strangers_unrelated_labels_do_not_match_the_term(self) -> None:
        """The premise of the non-matching variant: these rows can only be read to be rejected."""
        self.seed_strangers_labels(SECOND_BATCH, matching=False)

        matching = Label.objects.filter(profile=self.stranger, name__icontains=TERM).count()

        self.assertEqual(
            matching,
            FIRST_BATCH,
            "the non-matching seed matched the search term after all, so the two variants measure the same "
            "thing and neither can tell a partial fix from a whole one",
        )

    def test_the_stranger_is_not_visible_to_the_viewer(self) -> None:
        """The premise of the whole file: these labels are none of the viewer's business."""
        visible = Label.objects.visible_to(self.viewer).filter(profile=self.stranger).count()

        self.assertEqual(visible, 0, "the stranger's labels are visible to the viewer, so reading them is correct")

    def test_the_measured_statement_reads_the_label_table(self) -> None:
        """Proves the plan being summed actually touches labels, rather than being some other statement."""
        captured, statements = self.label_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the label table")

        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))

        self.assertIn(
            Label._meta.db_table,
            touched,
            f"no plan read the label table; relations read were {sorted(touched)}",
        )
