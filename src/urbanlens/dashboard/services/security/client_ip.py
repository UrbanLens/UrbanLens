"""Resolving the address a request actually came from.

One implementation, because every caller is making a security decision with the
answer - per-IP rate limiting on login and passphrase suggestions, and the
network allowlist on ``/metrics``. A second copy that counted proxy hops
differently would be a hole in whichever caller got it wrong.
"""

from __future__ import annotations

from functools import lru_cache
import ipaddress
import logging
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from django.http import HttpRequest

logger = logging.getLogger(__name__)


def client_ip(request: HttpRequest) -> str:
    """Return the client address, trusting only the proxies we put in front.

    Counted from the right of ``X-Forwarded-For``, one place per proxy in
    ``TRUSTED_PROXY_COUNT``. The leftmost entries arrive from the client and are
    forgeable, so keying on them lets an attacker mint a fresh counter per
    request and spray passwords through a throttle untouched; only the entries
    our own proxies appended mean anything. A chain shorter than the configured
    hop count means the request did not come through them, so it falls back to
    the socket address.

    Args:
        request: The incoming HTTP request.

    Returns:
        A string address, or ``"unknown"`` when the socket address is missing.
        Suitable for use as a cache-key fragment; parse with
        :func:`parse_ip` before comparing it to a network.
    """
    remote_addr = request.META.get("REMOTE_ADDR") or "unknown"
    hops = settings.TRUSTED_PROXY_COUNT
    if hops <= 0:
        return remote_addr
    chain = [entry.strip() for entry in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if entry.strip()]
    if len(chain) < hops:
        return remote_addr
    return chain[-hops]


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an address that :func:`client_ip` returned.

    Args:
        value: The address string to parse.

    Returns:
        The parsed address, or ``None`` when it is not one - ``"unknown"``, or
        an ``X-Forwarded-For`` entry a client filled with arbitrary text. A
        caller comparing against an allowlist must treat ``None`` as "not
        allowed" rather than as an error to report.
    """
    # A port suffix is not part of the XFF grammar but appears in the wild from
    # proxies that append one; IPv6 arrives bracketed when it does.
    candidate = value.strip()
    if candidate.startswith("["):
        candidate = candidate.partition("]")[0].removeprefix("[")
    elif candidate.count(":") == 1:
        candidate = candidate.partition(":")[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


@lru_cache(maxsize=8)
def parse_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a comma-separated CIDR list into networks.

    Cached on the raw string, because callers re-derive this from a setting on
    every request and the result only changes when the setting does. That also
    keeps a typo to one log line rather than one per request. The cache is
    keyed by value, so a test that overrides the setting gets its own entry
    rather than a stale one; ``maxsize`` is small because the number of distinct
    allowlists a process ever sees is the number of settings that hold one.

    Args:
        raw: Comma-separated CIDRs, e.g. ``"10.2.0.0/24, 127.0.0.1/32"``.
            Blank entries are skipped so a trailing comma is harmless.

    Returns:
        The networks that parsed, as a tuple - immutable because it is shared
        between every caller that passes the same string. An unparseable entry
        is logged and dropped rather than raising: this list gates access, so a
        typo must narrow what is reachable, never widen it or take the process
        down at import time.
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            logger.warning("Ignoring unparseable CIDR %r in an address allowlist", candidate)
    return tuple(networks)


def address_in_networks(address: str, networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] | Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    """Report whether an address falls inside any of the given networks.

    Args:
        address: An address string as returned by :func:`client_ip`.
        networks: Networks to test against.

    Returns:
        ``True`` only when the address parses *and* falls inside one of the
        networks. An address that does not parse is not in any network.
    """
    parsed = parse_ip(address)
    if parsed is None:
        return False
    return any(parsed in network for network in networks)
