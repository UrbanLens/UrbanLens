"""Native push delivery: device registration and UnifiedPush dispatch.

The delivery counterpart of the browser's Channels WebSocket push
(``models.notifications.signals``): every ``NotificationLog`` insert enqueues
``tasks.dispatch_native_push``, which calls :func:`send_push_to_profile` here
to POST the notification payload to each of the recipient's active
:class:`~urbanlens.dashboard.models.push_device.model.PushDevice` rows.

Only the UnifiedPush transport dispatches today (an app-chosen push server -
ntfy et al. - receives a plain POST of the JSON payload at the registered
endpoint URL, per the UnifiedPush application-server contract). FCM rows are
accepted at registration for the future Play-flavor client but are skipped at
dispatch until that flavor exists.

Registering an arbitrary URL the server will later POST to is an SSRF vector,
so :func:`register_device` validates UnifiedPush endpoints: https-or-http
scheme only, no embedded credentials, and the hostname must not resolve to a
private/loopback/link-local address. DNS-rebinding after registration remains
theoretically possible (the check is at registration time, not per-send);
accepted as a residual risk for v1 since the payload is a notification body
and responses are never surfaced to the caller.
"""

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
    """The submitted device registration is invalid.

    The message is safe to surface directly to the caller.
    """


def _validate_unifiedpush_endpoint(address: str) -> None:
    """Reject UnifiedPush endpoint URLs the server should never POST to.

    Args:
        address: The submitted endpoint URL.

    Raises:
        PushRegistrationError: The URL is malformed, carries credentials, uses
            a non-HTTP scheme, or resolves to a private/loopback address.
    """
    parts = urlsplit(address)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise PushRegistrationError("UnifiedPush endpoint must be an http(s) URL.")
    if parts.username or parts.password:
        raise PushRegistrationError("UnifiedPush endpoint must not embed credentials.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise PushRegistrationError("UnifiedPush endpoint hostname does not resolve.") from exc
    for info in infos:
        candidate = ipaddress.ip_address(info[4][0])
        if candidate.is_private or candidate.is_loopback or candidate.is_link_local or candidate.is_reserved or candidate.is_multicast:
            raise PushRegistrationError("UnifiedPush endpoint must be publicly reachable.")


def register_device(profile: Profile, *, transport: str, address: str, name: str = "") -> PushDevice:
    """Register (or re-activate) a push destination for a profile.

    Idempotent on ``(profile, address)``: re-registering an address the
    profile already has updates its transport/name, clears any revocation,
    and resets the failure count - an app re-registering after reinstall or
    endpoint rotation must never be told "already exists".

    Args:
        profile: The owning profile.
        transport: A :class:`PushTransport` value.
        address: UnifiedPush endpoint URL or FCM registration token.
        name: Optional user-facing device label.

    Returns:
        The active device row.

    Raises:
        PushRegistrationError: The address fails transport-specific validation.
    """
    address = (address or "").strip()
    if not address:
        raise PushRegistrationError("A device address is required.")
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
        profile: The owning profile - another profile's device uuid is
            indistinguishable from a nonexistent one.
        device_uuid: The device's public uuid.

    Returns:
        True when a device was revoked; False when nothing matched (already
        revoked devices count as matched, keeping the call idempotent).
    """
    return PushDevice.objects.for_profile(profile).filter(uuid=device_uuid).update(revoked_at=timezone.now()) > 0


def send_push_to_profile(profile_id: int, payload: dict) -> int:
    """Deliver a notification payload to every active device of a profile.

    Failures are per-device and never raise: one dead endpoint must not stop
    delivery to the user's other devices, and delivery as a whole is
    best-effort on top of the always-written ``NotificationLog`` row.

    Args:
        profile_id: Primary key of the recipient profile.
        payload: JSON-serializable notification payload (see
            ``models.notifications.signals.as_push_payload``).

    Returns:
        Number of devices successfully delivered to.
    """
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
