"""Custom python-social-auth pipeline steps.

These are inserted into ``SOCIAL_AUTH_PIPELINE`` in ``settings/base.py`` and
run for every OAuth login (Google, Discord, ...).

Step contracts
--------------
- Return ``None`` or an empty dict to do nothing and pass through.
- Return a dict to merge extra data into the pipeline state.
- Raise ``StopPipeline`` to abort the login.

All steps must accept ``**kwargs`` because the pipeline may pass extra
keyword arguments that we do not care about.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from django.core.files.base import ContentFile

from urbanlens.dashboard.services.avatar import AvatarService
from urbanlens.dashboard.services.username import USERNAME_RE, UsernameGenerator, username_is_taken

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


# -- Pipeline steps ------------------------------------------------------------


def generate_sso_username(
    backend: Any,
    user: User | None,
    response: dict[str, Any],
    details: dict[str, Any],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Choose an initial username for new SSO users.

    Prefers the provider handle when it is valid and not already taken:
    Discord ``username``, or the local part of a Google account email.
    Otherwise falls back to a random ``{adjective}{animal}{number}`` name.

    Replaces the default ``social_core.pipeline.user.get_username`` step.
    Existing users (``user`` is not None) are left unchanged.

    Args:
        backend: The social-auth backend in use.
        user: The existing Django User if this is a returning account, else None.
        response: Raw response from the OAuth provider.
        details: Normalised details dict produced by ``social_details``.

    Returns:
        Dict with ``username`` key for new users, or None for returning users.
    """
    if user is not None:
        return {"username": user.username}

    preferred = _provider_username_preference(backend, response, details)
    if preferred and not username_is_taken(preferred):
        logger.debug("Using provider SSO username: %s", preferred)
        return {"username": preferred}

    return {"username": UsernameGenerator.generate()}


def suppress_last_name_for_new_users(
    backend: Any,
    user: User | None,
    response: dict[str, Any],
    is_new: bool = False,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Clear ``last_name`` on the Django User for brand-new SSO accounts.

    Runs after ``user_details`` (which copies the provider's given/family name
    into the User model).  Preserving the first name lets the UI greet the
    user naturally while stripping the last name limits personal data exposure.

    Existing users are not affected so that a user who manually added their
    last name on their profile settings doesn't lose it on every subsequent
    login.

    Args:
        backend: The social-auth backend in use.
        user: The Django User being logged in.
        response: Raw response from the OAuth provider.
        is_new: True only when the User row was just created in this pipeline run.
    """
    if not is_new or user is None:
        return
    if user.last_name:
        user.last_name = ""
        user.save(update_fields=["last_name"])
        logger.debug("Cleared last_name for new SSO user %s", user.username)


def fetch_and_save_avatar(
    backend: Any,
    user: User | None,
    response: dict[str, Any],
    is_new: bool = False,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Download the provider avatar (or Gravatar) and store it on the Profile.

    Only fetches when the profile has no existing avatar so that users who
    upload their own photo are not overwritten on subsequent logins.

    Args:
        backend: The social-auth backend in use (name is ``backend.name``).
        user: The Django User, or None if authentication failed earlier.
        response: Raw response from the OAuth provider.
        is_new: True when the User was just created in this pipeline run.
    """
    if user is None:
        return

    try:
        profile = user.profile
    except AttributeError:
        logger.warning("No profile found for user %s; skipping avatar fetch", user.pk)
        return

    if profile.avatar:
        return

    avatar_url = AvatarService.resolve_provider_url(backend, user, response)
    if not avatar_url:
        return

    image_bytes = AvatarService.download(avatar_url)
    if not image_bytes:
        return

    filename = f"sso_avatar_{user.pk}.jpg"
    profile.avatar.save(filename, ContentFile(image_bytes), save=True)
    logger.info("Saved SSO avatar for user %s from %s", user.username, backend.name)


def mark_new_user_onboarding(
    backend: Any,
    user: User | None,
    is_new: bool = False,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Set profile_setup_complete=False for brand-new SSO users.

    Causes PostLoginRedirectView to send them to /profile/edit/ so they can
    choose a username and avatar before landing on the map.  Existing users
    and email-registered users are not affected.

    Args:
        backend: The social-auth backend in use.
        user: The Django User, or None if authentication failed earlier.
        is_new: True when the User was just created in this pipeline run.
    """
    if not is_new or user is None:
        return
    try:
        profile = user.profile
        profile.profile_setup_complete = False
        profile.save(update_fields=["profile_setup_complete"])
        logger.debug("Marked onboarding incomplete for new SSO user %s", user.username)
    except AttributeError:
        logger.warning("Could not mark onboarding for new SSO user pk=%s", getattr(user, "pk", "?"))


def save_discord_social_link(
    backend: Any,
    user: User | None,
    response: dict[str, Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Store the Discord username as a SocialLink for Discord SSO users.

    Runs for every Discord login so that username changes on Discord are
    reflected in UrbanLens.  Only overwrites the stored handle; does not
    remove the link if the response is missing a username.

    Args:
        backend: The social-auth backend in use.
        user: The Django User, or None if authentication failed earlier.
        response: Raw OAuth response payload from Discord.
    """
    if user is None or getattr(backend, "name", "") != "discord":
        return

    username = response.get("username")
    if not username:
        return

    try:
        profile = user.profile
    except AttributeError:
        logger.warning("No profile found for user %s; skipping Discord social link", user.pk)
        return

    from urbanlens.dashboard.models.social_link.model import SocialLink

    SocialLink.objects.update_or_create(
        profile=profile,
        platform="discord",
        defaults={"handle": username},
    )
    logger.debug("Saved Discord social link for user %s: %s", user.username, username)


# -- Internal helpers ----------------------------------------------------------


def _sanitize_sso_username(raw: str) -> str | None:
    """Normalize a provider handle to UrbanLens username rules.

    Args:
        raw: Provider username or email address.

    Returns:
        A sanitized username, or None when the value cannot be normalized.
    """
    local_part = raw.strip().split("@", 1)[0]
    sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", local_part)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if len(sanitized) < 3:
        return None
    if len(sanitized) > 30:
        sanitized = sanitized[:30].rstrip("_")
        if len(sanitized) < 3:
            return None
    if not USERNAME_RE.match(sanitized):
        return None
    return sanitized


def _provider_username_preference(
    backend: Any,
    response: dict[str, Any],
    details: dict[str, Any],
) -> str | None:
    """Return a preferred username derived from the OAuth provider, if any.

    Args:
        backend: The social-auth backend in use.
        response: Raw response from the OAuth provider.
        details: Normalised details dict produced by ``social_details``.

    Returns:
        Sanitized username candidate, or None when no provider handle is usable.
    """
    name = getattr(backend, "name", "")
    if name == "discord":
        raw = response.get("username")
    elif name == "google-oauth2":
        raw = details.get("email") or response.get("email")
    else:
        return None
    if not raw:
        return None
    return _sanitize_sso_username(str(raw))
