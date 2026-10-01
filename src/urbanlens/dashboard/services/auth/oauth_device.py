"""The device-code grant's token step, which takes the account from the device grant rather than the validator."""

from __future__ import annotations

from typing import Any

from oauthlib.oauth2.rfc6749.errors import InvalidGrantError


def refuse_an_inactive_account(request: Any) -> None:
    """Refuse a device-code exchange once the account that approved the device is deactivated.

    Runs after ``oauth2_provider.utils.set_oauthlib_user_to_device_request_user``, which sets the account straight
    from the device grant's row, so ``ActiveOwnerOAuth2Validator`` never sees it. Imported by settings, so it
    imports nothing from Django.

    Args:
        request: The oauthlib request.

    Raises:
        InvalidGrantError: No account approved the device, or it is deactivated.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_active:
        raise InvalidGrantError(request=request)
