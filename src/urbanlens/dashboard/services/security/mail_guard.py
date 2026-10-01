"""The outbound-mail backend: refuses addresses nobody can receive at, and hands the rest to the configured backend."""

from __future__ import annotations

from email.utils import parseaddr
import logging
import re
import smtplib
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

from urbanlens.dashboard.services.auth.email_normalization import is_gmail_address, normalize_email

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.core.mail import EmailMessage

logger = logging.getLogger(__name__)

#: RFC 2606/6761 reserved top-level domains, which no mailbox can exist under.
RESERVED_TLDS = frozenset({"invalid", "test", "example", "localhost"})
#: RFC 2606 reserved second-level domains, and every subdomain of them.
RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})

# Gmail only issues letters, digits and dots; dots are dropped by normalization, so anything else left in the
# mailbox name is one Gmail cannot hold.
_GMAIL_MAILBOX_RE = re.compile(r"^[a-z0-9]+$")

#: SMTP's "mailbox unavailable" reply, used for every refused recipient.
_REFUSED = (550, b"5.1.1 Refused before the relay: no mailbox can exist at this address")


def is_undeliverable_address(address: str) -> bool:
    """Whether no one can receive mail at ``address``, so handing it to the relay can only bounce.

    True for a reserved domain, and for a Gmail address whose mailbox name holds a character Gmail never
    issues (anything but letters, digits and dots, e.g. ``someone-else@gmail.com``). The integration suite
    registers addresses of the second kind, which is what lets it exercise Gmail's dot and ``+tag`` rules
    without addressing a mailbox a real person could own.

    Args:
        address: A bare address, or a ``Name <address>`` header value.

    Returns:
        True when the address must not reach the relay.
    """
    bare = parseaddr(address)[1].strip().lower().rstrip(".")
    local, _, domain = bare.rpartition("@")
    if not local or not domain:
        return True
    if domain.rpartition(".")[2] in RESERVED_TLDS or any(domain == reserved or domain.endswith(f".{reserved}") for reserved in RESERVED_DOMAINS):
        return True
    return is_impossible_gmail_address(bare)


def is_impossible_gmail_address(address: str) -> bool:
    """Whether ``address`` is a Gmail address whose mailbox name holds a character Gmail never issues."""
    bare = address.strip().lower()
    if not is_gmail_address(bare):
        return False
    return not _GMAIL_MAILBOX_RE.match(normalize_email(bare).rpartition("@")[0])


class RecipientGuardEmailBackend(BaseEmailBackend):
    """Drops undeliverable recipients from every message, then delivers the rest through ``EMAIL_DELIVERY_BACKEND``.

    A message left with no recipient is not sent. Like an SMTP relay refusing every recipient, that raises
    ``SMTPRecipientsRefused`` unless ``fail_silently``, so callers that already handle a refused address
    behave as they would with a real relay.
    """

    def __init__(self, fail_silently: bool = False, **kwargs: Any) -> None:
        super().__init__(fail_silently=fail_silently)
        delivery_backend = getattr(settings, "EMAIL_DELIVERY_BACKEND", "")
        if not delivery_backend or delivery_backend == f"{type(self).__module__}.{type(self).__qualname__}":
            raise ImproperlyConfigured("EMAIL_DELIVERY_BACKEND must name the backend that actually delivers mail.")
        self.delivery = get_connection(delivery_backend, fail_silently=fail_silently, **kwargs)

    def open(self) -> bool | None:
        return self.delivery.open()

    def close(self) -> None:
        self.delivery.close()

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        """Send what can be delivered.

        Args:
            email_messages: The messages; undeliverable recipients are removed from each in place.

        Returns:
            How many messages the delivery backend sent.

        Raises:
            SMTPRecipientsRefused: Every recipient of every message was refused, and ``fail_silently`` is off.
        """
        refused: dict[str, tuple[int, bytes]] = {}
        deliverable = [message for message in email_messages if self._strip_undeliverable(message, refused)]
        if refused:
            logger.info("Refused %d undeliverable recipient(s) before the relay; %d of %d message(s) still sent.", len(refused), len(deliverable), len(email_messages))
        if not deliverable:
            if refused and not self.fail_silently:
                raise smtplib.SMTPRecipientsRefused(refused)
            return 0
        return self.delivery.send_messages(deliverable) or 0

    @staticmethod
    def _strip_undeliverable(message: EmailMessage, refused: dict[str, tuple[int, bytes]]) -> bool:
        """Remove undeliverable recipients from ``message``; True when anyone is left to send it to."""
        for field in ("to", "cc", "bcc"):
            kept = []
            for address in getattr(message, field):
                if is_undeliverable_address(address):
                    refused[address] = _REFUSED
                else:
                    kept.append(address)
            setattr(message, field, kept)
        return bool(message.recipients())
