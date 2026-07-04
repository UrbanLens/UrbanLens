"""Site admin bootstrap and redirect helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.models import Group

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

SITE_ADMIN_GROUP_NAME = "site_admin"


def ensure_site_admin_group_permissions(group: Group) -> None:
    """Attach site-admin panel permissions to ``group`` if missing.

    ``view_site_admin`` is not declared on a model ``Meta.permissions`` and
    is created here so callers (migrations and runtime promotion) stay aligned.

    Args:
        group: The site admin auth group to configure.
    """
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    badge_ct = ContentType.objects.get(app_label="dashboard", model="badge")
    sitesettings_ct = ContentType.objects.get(app_label="dashboard", model="sitesettings")

    edit_global_badge, _ = Permission.objects.get_or_create(
        codename="edit_global_badge",
        content_type=badge_ct,
        defaults={"name": "Can edit global badges"},
    )
    view_site_admin, _ = Permission.objects.get_or_create(
        codename="view_site_admin",
        content_type=sitesettings_ct,
        defaults={"name": "Can access site admin panel"},
    )
    group.permissions.add(edit_global_badge, view_site_admin)


def add_user_to_site_admin_group(user: User) -> None:
    """Add ``user`` to the site_admin auth group.

    Args:
        user: The user to promote.
    """
    group, _ = Group.objects.get_or_create(name=SITE_ADMIN_GROUP_NAME)
    ensure_site_admin_group_permissions(group)
    user.groups.add(group)


def promote_first_user_if_needed(user: User) -> bool:
    """Grant site admin to the first user created on a fresh site.

    Uses ``SiteSettings.bootstrap_admin_user`` as the authoritative record so
    concurrent sign-ups cannot both claim the role.

    Args:
        user: A newly created user.

    Returns:
        True when ``user`` was promoted to site admin.
    """
    from django.contrib.auth.models import User as UserModel
    from django.db import transaction

    from urbanlens.dashboard.models.site_settings import SiteSettings

    with transaction.atomic():
        settings = SiteSettings.objects.select_for_update().get_or_create(pk=1)[0]
        if settings.bootstrap_admin_user_id is not None:
            return False
        if UserModel.objects.exclude(pk=user.pk).exists():
            return False

        settings.bootstrap_admin_user = user
        settings.bootstrap_admin_onboarding_complete = False
        settings.save(
            update_fields=[
                "bootstrap_admin_user",
                "bootstrap_admin_onboarding_complete",
            ],
        )
        add_user_to_site_admin_group(user)
        logger.info("Promoted first user %s to site admin", user.username)
        return True


def should_redirect_to_site_admin(user: User) -> bool:
    """Return True when ``user`` should be sent to the site admin page after login.

    Args:
        user: The authenticated user.

    Returns:
        True when this user is the bootstrap admin who has not yet opened site admin.
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    settings = SiteSettings.get_current()
    return settings.bootstrap_admin_user_id == user.pk and not settings.bootstrap_admin_onboarding_complete


def complete_site_admin_onboarding(user: User) -> None:
    """Mark the bootstrap admin's first-visit redirect as complete.

    Args:
        user: The user viewing the site admin page.
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    settings = SiteSettings.get_current()
    if settings.bootstrap_admin_user_id != user.pk or settings.bootstrap_admin_onboarding_complete:
        return

    settings.bootstrap_admin_onboarding_complete = True
    settings.save(update_fields=["bootstrap_admin_onboarding_complete"])
    logger.info("Completed site admin onboarding for %s", user.username)
