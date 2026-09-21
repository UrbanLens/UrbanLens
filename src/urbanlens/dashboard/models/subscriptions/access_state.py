"""What a viewer is allowed to see, answered once per account rather than once per page.

Two of the navbar's values - the feature set and the dev-toolbar verdict - come from the same
question, "is this account a site admin, and what does its subscription grant". Both are read on
every authenticated page, and both change when an admin acts rather than when a user browses.
Asking them per page cost four statements and about 13 ms of the app's own CPU on every
navigation; at 1,000 concurrent users the app container was CPU-throttled 31.6% of the time while
the database sat at a quarter of its cores, so that work is the scarce thing, not the SQL.

The answer is cached across requests and retired by the acts that change it, listed in
:func:`connect_invalidation`. The permission half is why that list has to be complete: an account
stripped of site admin must stop being treated as one on its next page, not when a timeout
expires.

What this is *not* is the gate on anything. The admin views enforce access through
``PermissionRequiredMixin`` and ``permission_required = "dashboard.view_site_admin"``, which calls
``has_perm`` itself and reads nothing from here. What a stale answer here can do is show or hide
chrome - a nav item, the dev toolbar, a feature flag the view will check again anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.contrib.auth.models import AnonymousUser, User

from urbanlens.core.versioned_cache import VersionedCache

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser

#: The permission that makes an account a site admin, which grants every feature.
SITE_ADMIN_PERMISSION = "dashboard.view_site_admin"

#: Long enough that a browsing user pays for it once, short enough to bound a bump lost to an
#: outage. Access is not re-read within a request either way: the request memo still covers that.
_SECONDS = 600

_cache = VersionedCache("access-state", seconds=_SECONDS)


@dataclass(frozen=True)
class AccessState:
    """One account's standing.

    Attributes:
        admin: Whether the account holds :data:`SITE_ADMIN_PERMISSION`, which grants everything.
        features: The ``SiteFeature`` values the site default and the account's roles grant. Empty
            for an admin, whose features are not enumerated from rows.
    """

    admin: bool
    features: frozenset[str]


def _compute(user: User) -> AccessState:
    """Read the account's standing from the database."""
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.models.subscriptions.model import active_subscription_roles

    if user.has_perm(SITE_ADMIN_PERMISSION):
        return AccessState(admin=True, features=frozenset())
    granted = frozenset(SiteSettings.get_current().feature_set).union(*(role.feature_set for role in active_subscription_roles(user)))
    return AccessState(admin=False, features=granted)


def access_state(user: AbstractBaseUser | AnonymousUser) -> AccessState:
    """The account's standing, from the shared cache when it is there.

    Args:
        user: The account to look up. Anonymous and unauthenticated users hold nothing.

    Returns:
        Its :class:`AccessState`.
    """
    if not isinstance(user, User) or not user.is_authenticated or user.pk is None:
        return AccessState(admin=False, features=frozenset())

    from urbanlens.dashboard.models.site_settings import request_cache

    remembered = request_cache.get_access(user.pk)
    if remembered is not None:
        return remembered

    if request_cache.shared_access_is_distrusted():
        state = _compute(user)
        request_cache.set_access(user.pk, state)
        return state

    name = str(user.pk)
    generation, found = _cache.read(name)
    stored = found.get(name)
    if isinstance(stored, tuple) and len(stored) == 2:
        admin, features = stored
        state = AccessState(admin=bool(admin), features=frozenset(features))
    else:
        state = _compute(user)
        _cache.write(generation, {name: (state.admin, sorted(state.features))})

    request_cache.set_access(user.pk, state)
    return state


def forget(**_kwargs: object) -> None:
    """Retire every account's cached standing.

    One counter covers every account because the acts that change access are administrative and
    rare, and because most of them - a site default changing, a role's features changing - alter
    what *every* account may see rather than one.

    The bump waits for the commit. ``post_save`` fires inside the writer's transaction, and a
    reader in the window between the two would see the new generation, read the *pre-commit* rows,
    and store that answer stamped as current - stale until the timeout rather than until the next
    page. Deferring the bump means such a reader stamps the old generation instead, which the
    commit then retires.

    That deferral is why dropping the in-request memo is not enough for the writer's own scope:
    with the counter still at its old value, its next read would fall through to a shared entry
    written before the change and find it current. So the scope is marked as well, and reads the
    database for the rest of its life rather than the cache.

    Args:
        **_kwargs: Whatever the signal sent, ignored.
    """
    from django.db import transaction

    from urbanlens.dashboard.models.site_settings import request_cache

    request_cache.invalidate()
    request_cache.distrust_shared_access()
    transaction.on_commit(_cache.bump)


class AccessBearingQuerySet:
    """Mixed in ahead of the concrete queryset for rows that decide what an account may see.

    The receivers in :func:`connect_invalidation` only hear about saves and deletes, and the site
    admin's own actions are neither - revoking a subscription and editing a role's features both go
    through ``Model.objects.filter(pk=...).update(...)`` in ``controllers/site_admin.py``. Without
    this a revoked subscription would keep granting its features in the chrome until the timeout.

    It carries no base of its own, so it composes with whichever
    :class:`~urbanlens.dashboard.models.abstract.queryset.DashboardQuerySet` a model already uses;
    that class is where the hook is called, and its docstring says what it covers.
    """

    def after_bulk_write(self) -> None:
        """Retire every account's cached standing, since these rows decide it."""
        forget()


#: ``User`` fields that change what :meth:`~django.contrib.auth.models.User.has_perm` answers. A
#: save naming only other fields must not retire anything - ``last_login`` is written on every
#: sign-in, and a bump per sign-in would empty the namespace exactly when it is most needed.
_ACCESS_FIELDS = frozenset({"is_active", "is_superuser"})


def forget_for_user(update_fields: frozenset[str] | None = None, **_kwargs: object) -> None:
    """Retire cached standing when a ``User`` save could have changed one.

    Args:
        update_fields: The fields the save named, or None when it wrote the whole row.
        **_kwargs: Whatever else the signal sent, ignored.
    """
    if update_fields is not None and not (_ACCESS_FIELDS & set(update_fields)):
        return
    forget()


def connect_invalidation() -> None:
    """Connect the receivers to every act that can change what an account may see.

    An act missing from here is an account that keeps an answer it should have lost, so the list
    is deliberately wider than the paths the product uses today: the permission and group senders
    cover a site admin being made or unmade by any route, including the Django admin and a shell.
    """
    from django.contrib.auth.models import Group, Permission
    from django.db.models.signals import m2m_changed, post_delete, post_save

    from urbanlens.dashboard.models.billing import RoleSubscription
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole, UserSubscription

    rows = (SiteSettings, UserSubscription, RoleSubscription, SubscriptionRole, Group, Permission)
    for sender in rows:
        post_save.connect(forget, sender=sender, dispatch_uid=f"access_state_forget_{sender.__name__}_save")
        post_delete.connect(forget, sender=sender, dispatch_uid=f"access_state_forget_{sender.__name__}_delete")

    post_save.connect(forget_for_user, sender=User, dispatch_uid="access_state_forget_User_save")
    post_delete.connect(forget, sender=User, dispatch_uid="access_state_forget_User_delete")

    memberships = (User.groups.through, User.user_permissions.through, Group.permissions.through)
    for through in memberships:
        m2m_changed.connect(forget, sender=through, dispatch_uid=f"access_state_forget_{through.__name__}")
