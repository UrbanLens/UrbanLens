"""Authentication backend allowing login by username or email address."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.backends import ModelBackend

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest


class EmailOrUsernameModelBackend(ModelBackend):
    """Same as Django's ModelBackend, but resolves an email-shaped username first.

    If the submitted "username" looks like an email address, it's resolved to
    the matching account's real username (via primary or verified secondary
    email, normalized) before delegating to the standard username/password
    check. Plain usernames are handled exactly as ModelBackend would.
    """

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> User | None:
        if username and "@" in username:
            from urbanlens.dashboard.services.email_normalization import find_user_by_email

            matched = find_user_by_email(username)
            if matched is not None:
                username = matched.get_username()
        return super().authenticate(request, username=username, password=password, **kwargs)
