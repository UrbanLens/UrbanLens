"""
Who may see a result a browser posted back.

Nothing a browser posts can be checked, so a submitter always sees their own, and anyone else sees it only once
enough distinct accounts have submitted the same thing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Distinct accounts that must have submitted a result before anyone but its submitters is shown it.
CORROBORATION = 2


def readable_by(submitters: Iterable[int], viewer_id: int | None) -> bool:
    """
    Whether a viewer may see a user-provided result.

    Args:
        submitters: The ids of the accounts that submitted it.
        viewer_id: The viewer's user pk, or None for a listing that is not for one viewer.

    Returns:
        True for a submitter, or when at least :data:`CORROBORATION` distinct accounts submitted it.
    """
    accounts = set(submitters)
    return viewer_id in accounts or len(accounts) >= CORROBORATION
