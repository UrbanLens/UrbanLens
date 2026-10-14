"""The data sources the home and credits pages list, grouped under the category each one is in."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.points.credits import DataSourceCategory, DataSourceCredit


@dataclass(frozen=True, slots=True)
class CreditedCategory:
    """
    A category with at least one source to list.

    Attributes:
        category: The category as its winning contribution describes it.
        credits: Its sources, in the order the plugins contributed them.
    """

    category: DataSourceCategory
    credits: tuple[DataSourceCredit, ...]


def _categories() -> dict[str, DataSourceCategory]:
    """
    The winning category for each key, in ``order``.

    ``Registry.as_dict()`` logs every overridden key, which would be on every page view, so each key is read on its own.
    """
    from urbanlens.dashboard.plugins.points import DATA_SOURCE_CATEGORIES

    keys = dict.fromkeys(contribution.value[0] for contribution in DATA_SOURCE_CATEGORIES.contributions())
    categories = {key: category for key in keys if (category := DATA_SOURCE_CATEGORIES.get(key)) is not None}
    return dict(sorted(categories.items(), key=lambda item: item[1].order))


def credited_categories() -> list[CreditedCategory]:
    """
    The categories with at least one credit, in ``order``. Every credit is included here, map cards too.

    Returns:
        One entry per category that has sources; empty when no plugin credits any.
    """
    from urbanlens.dashboard.plugins.points import DATA_SOURCE_CREDITS

    by_key: dict[str, list[DataSourceCredit]] = {}
    for credit in DATA_SOURCE_CREDITS:
        by_key.setdefault(credit.category, []).append(credit)
    return [CreditedCategory(category=category, credits=tuple(by_key[key])) for key, category in _categories().items() if key in by_key]


def credits_page_categories() -> list[CreditedCategory]:
    """The categories the credits page lists by name, with map cards left out. A category left empty is dropped."""
    groups = []
    for group in credited_categories():
        listed = tuple(credit for credit in group.credits if credit.attribution is None)
        if listed:
            groups.append(CreditedCategory(category=group.category, credits=listed))
    return groups


def map_cards() -> list[DataSourceCredit]:
    """The sources the credits page shows as map cards: each one that carries an attribution."""
    from urbanlens.dashboard.plugins.points import DATA_SOURCE_CREDITS

    return [credit for credit in DATA_SOURCE_CREDITS if credit.attribution is not None]
