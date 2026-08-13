"""Paging through a list with tied sort keys must not repeat or drop rows.

``PaginatedListMixin.paginated_response`` documents its own precondition - "Must
have a deterministic ordering, or pages will overlap and drop rows" - and then
does nothing to establish it. Thirty call sites pass it a queryset; whether each
one's ordering is deterministic is left to whoever wrote it.

Most orderings end in a timestamp, which is unique enough in practice. Several end
in a plain ``CharField``: ``WikiOwner.name``, ``PinAlias.name``, ``Album.name``.
Postgres is free to return tied rows in any order, and it does not have to pick
the same order for the ``LIMIT/OFFSET`` behind page 1 as for page 2 - so a row can
appear on both pages while another appears on neither. Nothing errors; the caller
just silently never sees that row.

Rather than auditing thirty call sites and hoping the thirty-first remembers, the
tie-break is appended inside ``paginated_response``. These tests pin the property
at that boundary, which is where the guarantee now lives.
"""

from __future__ import annotations

from model_bakery import baker
from rest_framework.test import APIRequestFactory

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.property_owner.model import WikiOwner

_PAGE_SIZE = 5
_ROWS = 23


class _Harness(PaginatedListMixin):
    """Bare consumer of the mixin - no auth, no serializer machinery."""


class PaginationStabilityTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        # Every row shares one name, so `ordering = ["name"]` ties on all of them.
        for _ in range(_ROWS):
            baker.make(WikiOwner, name="Same Name LLC").locations.add(self.location)
        self.factory = APIRequestFactory()

    def _page_pks(self, page: int) -> list[int]:
        from rest_framework.request import Request

        request = Request(self.factory.get("/", {"page": page, "page_size": _PAGE_SIZE}))
        paginator = _Harness.pagination_class()
        queryset = WikiOwner.objects.for_location(self.location)
        from urbanlens.dashboard.external_api.pagination import stable_ordering

        return [row.pk for row in paginator.paginate_queryset(stable_ordering(queryset), request, view=None)]

    def test_the_fixture_actually_ties(self) -> None:
        """Guards the checks below from passing on accidentally-distinct rows."""
        names = set(WikiOwner.objects.for_location(self.location).values_list("name", flat=True))

        self.assertEqual(len(names), 1, "the fixture must tie on the sort key or this proves nothing")
        self.assertEqual(WikiOwner.objects.for_location(self.location).count(), _ROWS)

    def test_paging_through_sees_every_row_exactly_once(self) -> None:
        seen: list[int] = []
        for page in range(1, (_ROWS // _PAGE_SIZE) + 2):
            seen.extend(self._page_pks(page))

        self.assertEqual(len(seen), len(set(seen)), "a row appeared on more than one page")
        self.assertEqual(set(seen), set(WikiOwner.objects.for_location(self.location).values_list("pk", flat=True)))

    def test_the_same_page_is_the_same_twice(self) -> None:
        """Postgres may reorder tied rows between identical queries."""
        self.assertEqual(self._page_pks(2), self._page_pks(2))


class StableOrderingTests(TestCase):
    """The helper must add determinism without changing what a query means."""

    def test_it_appends_a_tiebreak_to_a_tied_ordering(self) -> None:
        from urbanlens.dashboard.external_api.pagination import stable_ordering

        ordered = stable_ordering(WikiOwner.objects.all())

        self.assertEqual(list(ordered.query.order_by)[-1], "pk")

    def test_it_preserves_the_original_ordering_ahead_of_the_tiebreak(self) -> None:
        from urbanlens.dashboard.external_api.pagination import stable_ordering

        ordered = stable_ordering(WikiOwner.objects.order_by("-name"))

        self.assertEqual(list(ordered.query.order_by), ["-name", "pk"])

    def test_it_leaves_an_already_unique_ordering_alone(self) -> None:
        from urbanlens.dashboard.external_api.pagination import stable_ordering

        ordered = stable_ordering(WikiOwner.objects.order_by("name", "pk"))

        self.assertEqual(list(ordered.query.order_by), ["name", "pk"])

    def test_it_does_not_touch_a_distinct_query(self) -> None:
        """``SELECT DISTINCT`` requires every ORDER BY term in the select list -
        appending pk would turn a working endpoint into a database error."""
        from urbanlens.dashboard.external_api.pagination import stable_ordering

        queryset = WikiOwner.objects.values("name").distinct()

        self.assertEqual(list(stable_ordering(queryset).query.order_by), list(queryset.query.order_by))

    def test_it_does_not_touch_an_aggregate_query(self) -> None:
        """Django folds ORDER BY terms into GROUP BY, so a tie-break would change
        the grouping and therefore the numbers."""
        from django.db.models import Count

        from urbanlens.dashboard.external_api.pagination import stable_ordering

        queryset = WikiOwner.objects.values("name").annotate(n=Count("pk")).order_by("name")

        self.assertEqual(list(stable_ordering(queryset).query.order_by), ["name"])

    def test_a_sliced_queryset_is_left_alone(self) -> None:
        """Django refuses to reorder after a slice; reordering here would 500."""
        from urbanlens.dashboard.external_api.pagination import stable_ordering

        sliced = WikiOwner.objects.all()[:10]

        self.assertEqual(list(stable_ordering(sliced).query.order_by), list(sliced.query.order_by))

    def test_a_combined_queryset_is_left_alone(self) -> None:
        """``ORDER BY`` on a union may only name columns in the combined select."""
        from urbanlens.dashboard.external_api.pagination import stable_ordering

        combined = WikiOwner.objects.filter(name="a").union(WikiOwner.objects.filter(name="b"))

        self.assertEqual(list(stable_ordering(combined).query.order_by), list(combined.query.order_by))

    def test_a_list_passes_through_untouched(self) -> None:
        """Several call sites paginate a pre-built list, not a queryset."""
        from urbanlens.dashboard.external_api.pagination import stable_ordering

        rows = [{"id": 3}, {"id": 1}]

        self.assertEqual(stable_ordering(rows), rows)
