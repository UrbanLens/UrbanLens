"""The Organize page's label rows, a page at a time (P66).

Pages are keyed on the last row's sort key rather than an offset, so a label created or deleted between two
page loads - REData's sync adds global tags in the background - neither repeats a row nor skips one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils.http import urlencode

from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.services.core.numbers import DB_INTEGER_MAX, DB_INTEGER_MIN, safe_int_or_none

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

#: Request header in which the client says how many rows it has loaded (a count, or ``all``), so a write's
#: re-render keeps them.
ROWS_LOADED_HEADER = "X-Org-Rows-Loaded"

_BIGINT_MAX = 2**63 - 1

#: Kinds whose cards show subtree pin totals, and so need priming.
_STATS_KINDS = frozenset({KIND_TAG, KIND_CATEGORY, KIND_STATUS})


def organize_page_size() -> int:
    """Cards in one page of an Organize tab."""
    return settings.ORGANIZE_ROWS_PAGE_SIZE


def organize_rows_queryset(kind: str, profile: Profile) -> QuerySet[Label]:
    """Every label of *kind* the Organize page lists for *profile*, in display order.

    The order ends on the pk because ``(order, name)`` is not unique: an own tag can share a global tag's name.

    Args:
        kind: A label kind.
        profile: The viewer.

    Returns:
        The labels, with what their cards render prefetched.

    Raises:
        ValueError: *kind* is not one the Organize page lists.
    """
    if kind == KIND_TAG:
        labels = Label.objects.tags().visible_to(profile).with_customizations_for(profile).with_pin_counts()
    elif kind == KIND_CATEGORY:
        labels = Label.objects.categories().for_profile(profile).with_pin_counts()
    elif kind == KIND_STATUS:
        labels = Label.objects.statuses().for_profile(profile).with_pin_counts()
    elif kind == KIND_USER:
        labels = Label.objects.user_labels().visible_to(profile).with_hierarchy()
    elif kind == KIND_MEDIA:
        labels = Label.objects.media().visible_to(profile).with_customizations_for(profile).with_hierarchy()
    else:
        msg = f"Unsupported label kind: {kind}"
        raise ValueError(msg)
    return labels.order_by("-order", "name", "pk")


@dataclass(frozen=True)
class RowsCursor:
    """The sort key of the last row a page showed; the next page starts after it.

    Attributes:
        order: Its ``Label.order``.
        name: Its name.
        pk: Its primary key.
    """

    order: int
    name: str
    pk: int

    @classmethod
    def of(cls, label: Label) -> RowsCursor:
        """The cursor that continues after *label*."""
        return cls(order=label.order, name=label.name, pk=label.pk)

    @classmethod
    def from_query(cls, params: Mapping[str, str]) -> RowsCursor | None:
        """Read a cursor from request parameters.

        Args:
            params: The query parameters.

        Returns:
            The cursor, or None when the request names none.

        Raises:
            ValueError: Some cursor parameters are present but they do not make a cursor.
        """
        order, name, pk = params.get("after_order"), params.get("after_name"), params.get("after_id")
        if order is None and name is None and pk is None:
            return None
        order_value, pk_value = safe_int_or_none(order), safe_int_or_none(pk)
        if order_value is None or pk_value is None or name is None:
            raise ValueError("Incomplete cursor.")
        if not DB_INTEGER_MIN <= order_value <= DB_INTEGER_MAX or not 0 <= pk_value <= _BIGINT_MAX:
            raise ValueError("Cursor out of range.")
        return cls(order=order_value, name=name, pk=pk_value)

    def as_query(self) -> dict[str, str]:
        """The cursor as request parameters, the inverse of :meth:`from_query`."""
        return {"after_order": str(self.order), "after_name": self.name, "after_id": str(self.pk)}

    def following(self) -> Q:
        """Rows after this one in :func:`organize_rows_queryset`'s order."""
        return Q(order__lt=self.order) | Q(order=self.order, name__gt=self.name) | Q(order=self.order, name=self.name, pk__gt=self.pk)


@dataclass(frozen=True)
class OrganizeRowsPage:
    """One page of an Organize tab.

    Attributes:
        labels: The labels on it, primed for their cards.
        next_cursor: Where the next page starts, or None when this one ends the tab.
        remaining: How many labels follow this page.
    """

    labels: list[Label]
    next_cursor: RowsCursor | None
    remaining: int


@dataclass(frozen=True)
class OrganizeRowsMore:
    """What the sentinel row at the end of a page needs.

    Attributes:
        next_url: The next page.
        rest_url: Every remaining row in one response.
        remaining: How many rows that is.
    """

    next_url: str
    rest_url: str
    remaining: int


def organize_rows_page(kind: str, profile: Profile, *, after: RowsCursor | None = None, limit: int | None = None) -> OrganizeRowsPage:
    """One page of an Organize tab.

    Args:
        kind: A label kind.
        profile: The viewer.
        after: Start after this row; None starts at the top.
        limit: At most this many rows; None for every remaining row.

    Returns:
        The page.

    Raises:
        ValueError: *limit* is not positive.
    """
    if limit is not None and limit < 1:
        raise ValueError("A page holds at least one row.")
    rows = organize_rows_queryset(kind, profile)
    if after is not None:
        rows = rows.filter(after.following())
    labels = list(rows if limit is None else rows[: limit + 1])
    more = False
    if limit is not None and len(labels) > limit:
        more = True
        del labels[limit:]
    # Primed on the list the template renders: the memo lives on these instances.
    if kind in _STATS_KINDS:
        Label.prime_total_pin_counts(labels)
    return OrganizeRowsPage(
        labels=labels,
        next_cursor=RowsCursor.of(labels[-1]) if more else None,
        remaining=rows.count() - len(labels) if more else 0,
    )


def organize_rows_more(page: OrganizeRowsPage, url_kind: str) -> OrganizeRowsMore | None:
    """The sentinel for the end of *page*, or None when nothing follows it.

    Args:
        page: The page just rendered.
        url_kind: The kind as the label routes spell it.

    Returns:
        The sentinel's URLs and count.
    """
    if page.next_cursor is None:
        return None
    query = page.next_cursor.as_query()
    rows_url = reverse("label.rows", kwargs={"label_kind": url_kind})
    return OrganizeRowsMore(
        next_url=f"{rows_url}?{urlencode(query)}",
        rest_url=f"{rows_url}?{urlencode({**query, 'all': '1'})}",
        remaining=page.remaining,
    )
