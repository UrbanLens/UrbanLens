"""Native push delivery: device registration and UnifiedPush dispatch.
Only the UnifiedPush transport dispatches today (an app-chosen push server - ntfy et al. - receives a plain POST of the JSON payload at the registered endpoint URL, per the UnifiedPush application-server contract)."""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.db.models import F
from django.utils import timezone
import requests

from urbanlens.dashboard.models.push_device import PushDevice, PushTransport
from urbanlens.dashboard.services.security.url_safety import is_blocked_address

if TYPE_CHECKING:
    from uuid import UUID

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Consecutive delivery failures after which a device is auto-revoked.
MAX_CONSECUTIVE_FAILURES = 10

#: Seconds allowed for one push POST - deliveries run in a Celery task, but a
#: hung push server still shouldn't monopolize a worker.
DISPATCH_TIMEOUT_SECONDS = 5


class PushRegistrationError(ValueError):
    """Raised when a submitted device registration can't be accepted."""


class MissingAddressError(PushRegistrationError):
    """No device address was submitted at all."""


class InvalidEndpointUrlError(PushRegistrationError):
    """The UnifiedPush endpoint isn't a well-formed http(s) URL."""


class EndpointCredentialsError(PushRegistrationError):
    """The UnifiedPush endpoint URL embeds a username/password."""


class EndpointResolutionError(PushRegistrationError):
    """The UnifiedPush endpoint's hostname couldn't be resolved."""


class EndpointUnreachableError(PushRegistrationError):
    """The UnifiedPush endpoint resolves to a private/loopback/link-local/CGNAT address.
    Distinct from :class:`EndpointResolutionError` so callers can tell "we don't know where this points" apart from "we know exactly where this points, and it's the SSRF guard's job to refuse it"."""


def _validate_unifiedpush_endpoint(address: str) -> None:
    """Reject UnifiedPush endpoint URLs the server should never POST to.

    Args:
        address: The submitted endpoint URL.

    Raises:
        InvalidEndpointUrlError: The URL is malformed or uses a non-HTTP scheme.
        EndpointCredentialsError: The URL carries a username/password.
        EndpointResolutionError: The hostname doesn't resolve.
        EndpointUnreachableError: The hostname resolves to a private/loopback/ link-local/CGNAT address."""
    parts = urlsplit(address)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise InvalidEndpointUrlError(f"UnifiedPush endpoint scheme/hostname invalid: scheme={parts.scheme!r} hostname={parts.hostname!r}")
    if parts.username or parts.password:
        raise EndpointCredentialsError(f"UnifiedPush endpoint embeds credentials: host={parts.hostname!r}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise EndpointResolutionError(f"UnifiedPush endpoint hostname failed to resolve: host={parts.hostname!r}") from exc
    # Python's ipaddress does not classify CGNAT as private, and cloud providers route internal-only
    # infrastructure through it - so a divergent copy of this check is a divergent SSRF guard.
    for info in infos:
        if is_blocked_address(ipaddress.ip_address(info[4][0])):
            raise EndpointUnreachableError(f"UnifiedPush endpoint host={parts.hostname!r} resolved to blocked address {info[4][0]}")


def register_device(profile: Profile, *, transport: str, address: str, name: str = "") -> PushDevice:
    """Register (or re-activate) a push destination for a profile.

    Args:
        profile: The owning profile.
        transport: A :class:`PushTransport` value.
        address: UnifiedPush endpoint URL or FCM registration token.
        name: Optional user-facing device label.

    Returns:
        The active device row.

    Raises:
        MissingAddressError: ``address`` is blank.
        InvalidEndpointUrlError: A UnifiedPush endpoint isn't a well-formed http(s) URL.
        EndpointCredentialsError: A UnifiedPush endpoint URL embeds credentials.
        EndpointResolutionError: A UnifiedPush endpoint's hostname doesn't resolve.
        EndpointUnreachableError: A UnifiedPush endpoint resolves to a blocked address."""
    address = (address or "").strip()
    if not address:
        raise MissingAddressError("register_device called with an empty device address.")
    if transport == PushTransport.UNIFIEDPUSH:
        _validate_unifiedpush_endpoint(address)

    device, _created = PushDevice.objects.update_or_create(
        profile=profile,
        address=address,
        defaults={
            "transport": transport,
            "name": (name or "").strip()[:100],
            "revoked_at": None,
            "failure_count": 0,
        },
    )
    return device


def unregister_device(profile: Profile, device_uuid: UUID | str) -> bool:
    """Revoke one of the profile's devices, if it exists.

    Args:
        profile: The owning profile - another profile's device uuid is indistinguishable from a nonexistent one.
        device_uuid: The device's public uuid.

    Returns:
        True when a device was revoked; False when nothing matched (already revoked devices count as matched, keeping the call idempotent)."""
    return PushDevice.objects.for_profile(profile).filter(uuid=device_uuid).update(revoked_at=timezone.now()) > 0


def send_push_to_profile(profile_id: int, payload: dict) -> int:
    """Deliver a notification payload to every active device of a profile.
    Failures are per-device and never raise: one dead endpoint must not stop delivery to the user's other devices, and delivery as a whole is best-effort on top of the always-written ``NotificationLog`` row.

    Args:
        profile_id: Primary key of the recipient profile.
        payload: JSON-serializable notification payload (see ``models.notifications.signals.as_push_payload``).

    Returns:
        Number of devices successfully delivered to."""
    delivered = 0
    for device in PushDevice.objects.filter(profile_id=profile_id).active():
        if device.transport != PushTransport.UNIFIEDPUSH:
            logger.debug("Skipping push device %s: transport %s not dispatched yet", device.pk, device.transport)
            continue
        if _dispatch_unifiedpush(device, payload):
            delivered += 1
    return delivered


def _dispatch_unifiedpush(device: PushDevice, payload: dict) -> bool:
    """POST one payload to one UnifiedPush endpoint, updating delivery bookkeeping.

    Args:
        device: The destination device.
        payload: JSON-serializable notification payload.

    Returns:
        True on a 2xx response.
    """
    try:
        response = requests.post(device.address, json=payload, timeout=DISPATCH_TIMEOUT_SECONDS)
        ok = 200 <= response.status_code < 300
    except requests.RequestException:
        logger.info("Push delivery to device %s failed", device.pk, exc_info=True)
        ok = False

    if ok:
        PushDevice.objects.filter(pk=device.pk).update(failure_count=0, last_success_at=timezone.now())
        return True

    # F() keeps the increment race-free across concurrent dispatches; the
    # revocation sweep below then reads the committed value.
    PushDevice.objects.filter(pk=device.pk).update(failure_count=F("failure_count") + 1)
    PushDevice.objects.filter(pk=device.pk, failure_count__gte=MAX_CONSECUTIVE_FAILURES, revoked_at__isnull=True).update(revoked_at=timezone.now())
    return False
