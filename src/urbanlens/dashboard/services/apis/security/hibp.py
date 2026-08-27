"""Have I Been Pwned Pwned Passwords API gateway.

Uses the k-anonymity range endpoint so the full password (and full SHA-1 hash)
never leaves the application. See https://haveibeenpwned.com/API/v3#PwnedPasswords.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from typing import ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway

logger = logging.getLogger(__name__)

_API_URL = "https://api.pwnedpasswords.com"
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; hello@urbanlens.org)"


@dataclass(slots=True, kw_only=True)
class HaveIBeenPwnedGateway(Gateway):
    """Check whether a password appears in known breach corpora via HIBP.

    Only the first five characters of the SHA-1 hash are sent to the API.
    """

    service_key: ClassVar[str] = "hibp"
    paid_service: ClassVar[bool] = False

    base_url: str = _API_URL

    def __post_init__(self) -> None:
        """Attach required headers for the Pwned Passwords range API."""
        Gateway.__post_init__(self)
        self.session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                # Troy Hunt recommends padding so response size does not leak prefix popularity.
                "Add-Padding": "true",
            }
        )

    @staticmethod
    def endpoint_for_log(url: str) -> str:
        """Record the range endpoint, never the prefix that identifies the password.

        The default implementation logs the full URL into an ``ApiCallLog`` row
        retained for over a year. Here the last path segment is the first five
        hex characters of the user's password SHA-1 - twenty bits of it,
        timestamped, for every password anyone has ever set. ``ApiCallLog``
        exists to track volume and cost per service, and the endpoint without
        its prefix answers that completely.

        Args:
            url: The range URL about to be requested.

        Returns:
            The URL truncated at the ``/range/`` segment.
        """
        marker = "/range/"
        prefix, sep, _rest = url.partition(marker)
        return f"{prefix}{marker}" if sep else url

    def is_password_pwned(self, password: str) -> bool | None:
        """Return whether ``password`` appears in HIBP's breach list.

        Args:
            password: The plaintext password to check. Never logged or transmitted in full.

        Returns:
            ``True`` if the password is known-compromised, ``False`` if it is not found,
            or ``None`` if the API could not be reached (caller should decide fail-open/closed).
        """
        digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
        prefix, suffix = digest[:5], digest[5:]
        try:
            response = self.session.get(f"{self.base_url}/range/{prefix}", timeout=5)
            response.raise_for_status()
        except Exception:
            # The prefix is twenty bits of the user's password hash - it does
            # not belong in a log file either (it was landing in one).
            logger.warning("HIBP range lookup failed; treating as unavailable", exc_info=True)
            return None

        for line in response.text.splitlines():
            parts = line.split(":")
            if len(parts) < 2:
                continue
            if parts[0].strip().upper() == suffix:
                return True
        return False
