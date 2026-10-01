"""OAuth2 token validation that ends with the owning account."""

from __future__ import annotations

from typing import Any

from oauth2_provider.oauth2_validators import OAuth2Validator

#: Grants that would mint a token without an account's own consent and second factor, or for no account at all.
REFUSED_GRANTS = frozenset({"password", "client_credentials"})


def _owner_is_active(request: Any) -> bool:
    user = getattr(request, "user", None)
    return user is not None and user.is_active


class ActiveOwnerOAuth2Validator(OAuth2Validator):
    """Refuses a deactivated account's access and refresh tokens, a token with no account, and :data:`REFUSED_GRANTS`.

    A deactivated account cannot sign in, but a token it issued earlier is a way into the same account, and
    django-oauth-toolkit only checks a token's expiry and scope. This covers every entry point that validates
    through ``OAUTH2_PROVIDER["OAUTH2_VALIDATOR_CLASS"]``: the external API, media authentication and the token
    endpoint. Sockets resolve tokens themselves (``websocket_auth``), introspection reads the token row
    (``controllers.oauth_introspect``), and the device grant takes its account from the device row
    (``oauth_device``).
    """

    def validate_grant_type(self, client_id: str, grant_type: str, client: Any, request: Any, *args: Any, **kwargs: Any) -> bool:
        """Refuse :data:`REFUSED_GRANTS`.

        Args:
            client_id: The client's id.
            grant_type: The grant asked for.
            client: The client asking.
            request: The oauthlib request.
            *args: Passed through.
            **kwargs: Passed through.

        Returns:
            True when the client may use this grant.
        """
        return grant_type not in REFUSED_GRANTS and super().validate_grant_type(client_id, grant_type, client, request, *args, **kwargs)

    def validate_code(self, client_id: str, code: str, client: Any, request: Any, *args: Any, **kwargs: Any) -> bool:
        """Exchange an authorization code only while the account that granted it is active.

        Args:
            client_id: The client's id.
            code: The authorization code.
            client: The client presenting it.
            request: The oauthlib request, given the grant's user on success.
            *args: Passed through.
            **kwargs: Passed through.

        Returns:
            True when the code is valid for this client and its owner active.
        """
        return super().validate_code(client_id, code, client, request, *args, **kwargs) and _owner_is_active(request)

    def validate_bearer_token(self, token: str, scopes: list[str], request: Any) -> bool:
        """Accept a bearer token only while its owner is active.

        Args:
            token: The bearer token.
            scopes: The scopes the resource requires.
            request: The oauthlib request, given the token's user on success.

        Returns:
            True when the token is valid and its owner active.
        """
        return super().validate_bearer_token(token, scopes, request) and _owner_is_active(request)

    def validate_refresh_token(self, refresh_token: str, client: Any, request: Any, *args: Any, **kwargs: Any) -> bool:
        """Exchange a refresh token only while its owner is active.

        Args:
            refresh_token: The refresh token.
            client: The client presenting it.
            request: The oauthlib request, given the token's user on success.
            *args: Passed through.
            **kwargs: Passed through.

        Returns:
            True when the token is valid for this client and its owner active.
        """
        return super().validate_refresh_token(refresh_token, client, request, *args, **kwargs) and _owner_is_active(request)
