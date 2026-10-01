"""Authentication backend allowing login by username or email address."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.backends import ModelBackend

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import HttpRequest


class EmailOrUsernameModelBackend(ModelBackend):
    """Same as Django's ModelBackend, but first resolves any spelling of a username or of one of the account's addresses."""

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> User | None:
        if username:
            from urbanlens.dashboard.services.auth.identity import find_user_by_identifier

            matched = find_user_by_identifier(username)
            if matched is not None:
                username = matched.get_username()
        return super().authenticate(request, username=username, password=password, **kwargs)
