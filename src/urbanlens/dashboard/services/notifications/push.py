"""Native push delivery: device registration and UnifiedPush dispatch.
Only the UnifiedPush transport dispatches today (an app-chosen push server - ntfy et al. - receives a plain POST of the JSON payload at the registered endpoint URL, per the UnifiedPush application-server contract)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ipaddress
import logging
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.db.models import F
from django.utils import timezone
import requests

from urbanlens.dashboard.models.push_device import PushDevice, PushTransport
from urbanlens.dashboard.services.core.capacity import PUSH_DEVICES, reserve
from urbanlens.dashboard.services.core.celery import safely_enqueue_task
from urbanlens.dashboard.services.core.egress import require_egress
from urbanlens.dashboard.services.core.rate_limiter import EnvironmentRefusedError
from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, is_blocked_address, open_public_url

if TYPE_CHECKING:
    from uuid import UUID

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: The egress policy's key for UnifiedPush delivery (``messaging``): production's alone unless overridden.
PUSH_SERVICE = "unified_push"

#: Consecutive delivery failures after which a device is auto-revoked.
MAX_CONSECUTIVE_FAILURES = 10

#: Seconds allowed for one push POST - deliveries run in a Celery task, but a
#: hung push server still shouldn't monopolize a worker.
DISPATCH_TIMEOUT_SECONDS = 5

#: Wall-clock budget for one push POST as a whole; the per-phase timeout above bounds each read, not their sum.
DISPATCH_DEADLINE_SECONDS = 10

#: Devices one task delivers to; the rest go to further tasks, so no task outlives its time limit.
PUSH_BATCH_SIZE = 40

#: Deliveries in flight at once within a batch.
PUSH_CONCURRENCY = 8


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
        EndpointUnreachableError: A UnifiedPush endpoint resolves to a blocked address.
        CapacityExceededError: The profile already has ``max_push_devices_per_user`` other active devices."""
    address = (address or "").strip()
    if not address:
        raise MissingAddressError("register_device called with an empty device address.")
    if transport == PushTransport.UNIFIEDPUSH:
        _validate_unifiedpush_endpoint(address)

    # A re-registration of an address that is already active takes no new room.
    others = PushDevice.objects.for_profile(profile).active().exclude(address=address)
    with reserve(PUSH_DEVICES, profile.pk, in_use=others):
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
        Number of devices in the first batch successfully delivered to; later batches report from their own tasks."""
    from urbanlens.dashboard.tasks import dispatch_push_to_devices

    # Only UnifiedPush dispatches today.
    device_ids = list(PushDevice.objects.filter(profile_id=profile_id, transport=PushTransport.UNIFIEDPUSH).active().order_by("pk").values_list("pk", flat=True))
    for start in range(PUSH_BATCH_SIZE, len(device_ids), PUSH_BATCH_SIZE):
        safely_enqueue_task(dispatch_push_to_devices, device_ids[start : start + PUSH_BATCH_SIZE], payload)
    return send_push_to_devices(device_ids[:PUSH_BATCH_SIZE], payload)


def send_push_to_devices(device_ids: list[int], payload: dict) -> int:
    """Deliver a payload to one batch of devices, ``PUSH_CONCURRENCY`` at a time.

    Args:
        device_ids: At most ``PUSH_BATCH_SIZE`` device primary keys; revoked ones are skipped.
        payload: JSON-serializable notification payload.

    Returns:
        Number of devices successfully delivered to; 0, recording nothing against the devices, where this environment
        does not push (D26)."""
    try:
        require_egress(PUSH_SERVICE)
    except EnvironmentRefusedError:
        return 0
    devices = list(PushDevice.objects.filter(pk__in=device_ids, transport=PushTransport.UNIFIEDPUSH).active())
    if not devices:
        return 0
    with ThreadPoolExecutor(max_workers=min(PUSH_CONCURRENCY, len(devices))) as pool:
        outcomes = list(pool.map(lambda device: _post_unifiedpush(device, payload), devices))
    for device, ok in zip(devices, outcomes, strict=True):
        _record_delivery(device, ok=ok)
    return sum(outcomes)


def _post_unifiedpush(device: PushDevice, payload: dict) -> bool:
    """POST one payload to one UnifiedPush endpoint. Touches no database, so it can run on a pool thread.

    Args:
        device: The destination device.
        payload: JSON-serializable notification payload.

    Returns:
        True on a 2xx response.
    """
    # Re-checked here rather than trusted from registration: the endpoint's DNS is the user's to change.
    # No redirects - a 307 would repeat the POST, body and all, wherever it points.
    try:
        with open_public_url(
            "POST",
            device.address,
            json=payload,
            timeout=DISPATCH_TIMEOUT_SECONDS,
            total_deadline=DISPATCH_DEADLINE_SECONDS,
            max_redirects=0,
        ) as response:
            return 200 <= response.status_code < 300
    except (requests.RequestException, UnsafeUrlError):
        logger.info("Push delivery to device %s failed", device.pk, exc_info=True)
        return False


def _record_delivery(device: PushDevice, *, ok: bool) -> None:
    """Update a device's delivery bookkeeping, revoking it after too many consecutive failures.

    Args:
        device: The device delivered to.
        ok: Whether the delivery succeeded.
    """
    if ok:
        PushDevice.objects.filter(pk=device.pk).update(failure_count=0, last_success_at=timezone.now())
        return
    # F() keeps the increment race-free across concurrent dispatches; the
    # revocation sweep below then reads the committed value.
    PushDevice.objects.filter(pk=device.pk).update(failure_count=F("failure_count") + 1)
    PushDevice.objects.filter(pk=device.pk, failure_count__gte=MAX_CONSECUTIVE_FAILURES, revoked_at__isnull=True).update(revoked_at=timezone.now())
