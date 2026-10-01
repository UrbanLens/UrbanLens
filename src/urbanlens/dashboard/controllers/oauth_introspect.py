"""OAuth2 token introspection that agrees with the validator about a deactivated account."""

from __future__ import annotations

import hashlib

from django.http import JsonResponse
from oauth2_provider.models import get_access_token_model
from oauth2_provider.views import IntrospectTokenView


class ActiveOwnerIntrospectTokenView(IntrospectTokenView):
    """``oauth2_provider``'s introspection, reporting a deactivated account's token, or one with no account, inactive.

    The toolkit answers from the token row alone, so without this a resource server would accept a token
    ``ActiveOwnerOAuth2Validator`` refuses everywhere else.
    """

    @staticmethod
    def get_token_response(token_value: str | None = None) -> JsonResponse:
        """Describe a token per RFC 7662, as inactive unless an active account owns it.

        Args:
            token_value: The token asked about.

        Returns:
            The introspection response.
        """
        if token_value is not None:
            checksum = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
            token = get_access_token_model().objects.select_related("user").filter(token_checksum=checksum).first()
            if token is not None and (token.user is None or not token.user.is_active):
                return JsonResponse({"active": False})
        return IntrospectTokenView.get_token_response(token_value)
