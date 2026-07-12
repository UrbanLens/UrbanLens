"""Django password validators for complexity and breach checking."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


class ComplexityValidator:
    """Require uppercase, lowercase, and either a digit or a symbol."""

    def validate(self, password: str, user: AbstractBaseUser | None = None) -> None:
        """Reject passwords that lack required character classes.

        Args:
            password: Candidate password.
            user: Unused; present for Django validator compatibility.

        Raises:
            ValidationError: When the password fails complexity rules.
        """
        del user
        has_upper = any(c.isupper() for c in password)
        has_lower = any(c.islower() for c in password)
        has_digit = any(c.isdigit() for c in password)
        has_symbol = any(not c.isalnum() for c in password)

        missing: list[str] = []
        if not has_upper:
            missing.append(_("an uppercase letter"))
        if not has_lower:
            missing.append(_("a lowercase letter"))
        if not (has_digit or has_symbol):
            missing.append(_("a digit or a symbol"))

        if missing:
            if len(missing) == 1:
                detail = missing[0]
            elif len(missing) == 2:
                detail = _("%(first)s and %(second)s") % {"first": missing[0], "second": missing[1]}
            else:
                detail = _("%(leading)s, and %(last)s") % {
                    "leading": ", ".join(missing[:-1]),
                    "last": missing[-1],
                }
            raise ValidationError(
                _("This password must include %(detail)s.") % {"detail": detail},
                code="password_too_simple",
            )

    def get_help_text(self) -> str:
        """Return user-facing guidance for this validator."""
        return _("Your password must include uppercase and lowercase letters, plus a digit or a symbol.")


class HaveIBeenPwnedValidator:
    """Reject passwords that appear in Have I Been Pwned breach data.

    Uses the k-anonymity range API so the full password never leaves the server.
    If the API is unreachable the check is skipped (fail-open) so signup/reset
    is not blocked by a third-party outage; a warning is logged instead.
    """

    def validate(self, password: str, user: AbstractBaseUser | None = None) -> None:
        """Reject passwords found in known breaches.

        Args:
            password: Candidate password.
            user: Unused; present for Django validator compatibility.

        Raises:
            ValidationError: When the password is known-compromised.
        """
        del user
        from urbanlens.dashboard.services.apis.security.hibp import HaveIBeenPwnedGateway

        result = HaveIBeenPwnedGateway().is_password_pwned(password)
        if result is True:
            raise ValidationError(
                _("This password appears in a known data breach. Please choose a different one."),
                code="password_pwned",
            )

    def get_help_text(self) -> str:
        """Return user-facing guidance for this validator."""
        return _("Your password cannot be one that has appeared in a known data breach.")
