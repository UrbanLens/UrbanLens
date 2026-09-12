"""Who may see property-owner identity and contact details.
That lookup is the product, so it is gated on :attr:`~urbanlens.dashboard.models.subscriptions.SiteFeature.PROPERTY_OWNERS`. * **User contributed** - a private ``PinOwner`` note, or a ``WikiOwner`` a community member typed in."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser

    from urbanlens.dashboard.models.property_owner.model import PinOwner, WikiOwner


def can_see_official_owners(user: AbstractBaseUser | AnonymousUser | None) -> bool:
    """Whether this user may see automatically-sourced owner identity/contact info.

    Args:
        user: The viewing user.

    Returns:
        True when the user holds ``SiteFeature.PROPERTY_OWNERS`` - which site admins and any role granting it do, per ``user_has_feature``."""
    from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature

    if user is None:
        return False
    return user_has_feature(user, SiteFeature.PROPERTY_OWNERS)


def viewer_of(pin) -> AbstractBaseUser | None:
    """The user whose entitlement governs what a pin's panels may show.
    The Private Pin page only ever renders a pin its own owner requested (``PinController.panel_info`` looks it up by ``profile__user``), so the pin's own profile is the viewer.

    Args:
        pin: The pin being rendered, or None.

    Returns:
        The viewing user, or None when there isn't one to resolve."""
    profile = getattr(pin, "profile", None)
    return getattr(profile, "user", None)


def visible_owners(owners, user: AbstractBaseUser | AnonymousUser) -> list[WikiOwner | PinOwner]:
    """Drop officially-sourced owners this user isn't entitled to see.
    ``PinOwner`` has no ``source`` field at all - private per-pin records are definitionally user-entered (see ``OwnerSource``'s own docstring) - so a queryset of those passes through untouched without needing a separate call site.

    Args:
        owners: An iterable of ``WikiOwner``/``PinOwner`` rows.
        user: The viewing user.

    Returns:
        The rows this user may see, as a list (the caller renders them and also needs a length, so evaluating once here avoids a second query)."""
    from urbanlens.dashboard.models.property_owner.meta import OwnerSource

    rows = list(owners)
    if can_see_official_owners(user):
        return rows
    return [owner for owner in rows if getattr(owner, "source", None) != OwnerSource.OFFICIAL]


def sale_rows(sales, user: AbstractBaseUser | AnonymousUser) -> list[dict]:
    """Sale records with their party names filtered for this viewer.

    Args:
        sales: Sale records, ideally with their party relations prefetched.
        user: The viewing user.

    Returns:
        One dict per sale: ``sale``, the visible ``previous_owners`` and ``new_owners``, and ``parties_withheld`` when any name was removed - so the template can say the parties are known but not shown, rather than rendering a misleading "Unknown"."""
    entitled = can_see_official_owners(user)
    rows = []
    for sale in sales:
        previous = list(sale.previous_owners.all())
        new = list(sale.new_owners.all())
        visible_previous = previous if entitled else visible_owners(previous, user)
        visible_new = new if entitled else visible_owners(new, user)
        rows.append(
            {
                "sale": sale,
                "previous_owners": visible_previous,
                "new_owners": visible_new,
                "parties_withheld": len(visible_previous) < len(previous) or len(visible_new) < len(new),
            },
        )
    return rows


def withheld_official_count(owners, user: AbstractBaseUser | AnonymousUser) -> int:
    """How many official owner records were withheld from this user.

    Args:
        owners: An iterable of ``WikiOwner``/``PinOwner`` rows.
        user: The viewing user.

    Returns:
        The number of hidden records; 0 when the user may see them all."""
    from urbanlens.dashboard.models.property_owner.meta import OwnerSource

    if can_see_official_owners(user):
        return 0
    return sum(1 for owner in owners if getattr(owner, "source", None) == OwnerSource.OFFICIAL)
