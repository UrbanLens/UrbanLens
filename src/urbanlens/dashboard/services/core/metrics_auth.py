"""Who is allowed to read a metrics endpoint.

Two transports serve metrics in this deployment - the Django view on the web
process, and a bare ``http.server`` in the Celery event exporter, which has no
Django request to work with. They must not drift apart: a second, subtly
different copy of "is this scraper authorized" is how one endpoint ends up open
while the other is guarded, and nothing says so.

So the decision lives here, over primitives (an ``Authorization`` header value
and a client address) rather than over a request object, and both transports
call it.
"""

from __future__ import annotations

import hmac
import logging

from django.conf import settings

from urbanlens.dashboard.services.security.client_ip import address_in_networks, parse_networks

logger = logging.getLogger(__name__)


def token_ok(authorization_header: str) -> bool:
    """Check a presented bearer token against the configured one.

    Args:
        authorization_header: Raw ``Authorization`` header value, or ``""``.

    Returns:
        ``True`` when no token is configured (the gate is off) or the presented
        token matches. ``compare_digest`` keeps the comparison constant-time, so
        a wrong token leaks nothing about how much of it was right.
    """
    expected = settings.UL_METRICS_TOKEN
    if not expected:
        return True
    scheme, _, presented = authorization_header.partition(" ")
    # RFC 7235 makes the scheme token case-insensitive; a scraper sending
    # "bearer" is conformant and must not be locked out.
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip(), expected)


def network_ok(address: str) -> bool:
    """Check a client address against the configured CIDR allowlist.

    Args:
        address: The client address, already resolved through the trusted-proxy
            hop count where one applies.

    Returns:
        ``True`` when no allowlist is configured (the gate is off) or the
        address falls inside one of its networks. An address that does not parse
        is never in any network, so a malformed one fails closed.
    """
    raw = settings.UL_METRICS_ALLOWED_CIDRS
    if not raw:
        return True
    return address_in_networks(address, parse_networks(raw))


def gates_configured() -> bool:
    """Report whether any gate is actually configured.

    Returns:
        ``True`` when a token or an allowlist is set. Used by the startup check
        and by the standalone exporter, which refuses to bind an unguarded port
        on a deployed environment rather than silently serving one.
    """
    return bool(settings.UL_METRICS_TOKEN or settings.UL_METRICS_ALLOWED_CIDRS)
