"""Review-queue lifecycle for PinImportFailure - pins whose Google Maps CID couldn't
be resolved to a location during import.

Called from two places:

- ``tasks.resolve_deferred_pin_locations``: records a failure
  (:func:`record_pin_import_failure`) the moment a cid is confirmed unresolvable or a
  retry loop gives up, and clears one (:func:`auto_resolve_pin_import_failure_for_cid`)
  the moment a cid it previously failed on resolves successfully on a later retry.
- ``controllers.pin_import_failures``: the review queue's own actions, letting the
  owner manually place (:func:`resolve_pin_import_failure`) or clear
  (:func:`dismiss_pin_import_failure`) an entry.

None of this reflects a defect in the imported pin or a mistake by the importer -
Google's own place data genuinely doesn't cover everywhere, and the live cid lookup
itself can time out - see ``PinImportFailure``'s own docstring. Every user-facing
string produced from this module (and by callers rendering its rows) must keep that
framing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure, PinImportFailureReason, PinImportFailureStatus
from urbanlens.dashboard.services.apis.locations.legacy_cid_coordinate_fix import repair_legacy_pin_coordinates
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.pins.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile


def record_pin_import_failure(profile: Profile, cid: int, *, name: str, description: str, reason: PinImportFailureReason) -> None:
    """Record a pin's cid that couldn't be placed automatically, unless one already exists.

    Called from ``tasks.resolve_deferred_pin_locations`` for a cid that resolution
    just confirmed unresolvable, or that's still unresolved when a retry loop gives
    up after too many stalled/failed rounds - see that task's docstring for the
    three cases. Idempotent per ``(profile, cid)`` via
    ``PinImportFailure.objects.get_or_create`` (backed by the
    ``uq_pin_import_failure_profile_cid`` constraint): since the same import can
    legitimately be re-run over the same source file, this must never create a
    second row, or touch ``name``/``description``/``reason`` on one that already
    exists - even if a later attempt captured slightly different values, or the
    existing row isn't pending anymore. An owner who already resolved or dismissed
    a failure must never see it silently resurrected by a re-import.

    Args:
        profile: Owner the failure belongs to.
        cid: The Google Maps CID that couldn't be resolved.
        name: Best-known name for the place, captured at the moment it was deferred.
        description: Best-known description, captured the same way.
        reason: Why the automatic lookup failed.
    """
    PinImportFailure.objects.get_or_create(
        profile=profile,
        cid=cid,
        defaults={
            "name": name,
            "description": description,
            "reason": reason,
        },
    )


def resolve_pin_import_failure(
    failure: PinImportFailure,
    profile: Profile,
    *,
    address: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> Pin:
    """Place a pin for a failure entry using an address or coordinates the owner supplied.

    TEMPORARY: before creating a new pin, this first tries
    :func:`services.apis.locations.legacy_cid_coordinate_fix.repair_legacy_pin_coordinates`
    - this failure's cid may belong to one of the profile's own pre-2026-07-25
    pins that the old CID lookup mis-placed, in which case that pin is moved
    onto the coordinates the owner just supplied (keeping its name, labels,
    and other data) instead of a second, duplicate pin being created. This
    mirrors ``tasks._create_pin_from_confirmed``'s own use of the same repair
    for the automatic resolution path - manual resolution must not skip it,
    or a legacy pin the automatic path failed to fix stays stranded forever.
    Remove this paragraph (and the call below) together with the rest of
    ``legacy_cid_coordinate_fix``.

    Falls back to the same generic :func:`services.pins.pin_creation.create_pin_for_profile`
    path as a manual "add pin" when no legacy pin matched - this is a one-off
    manual recovery action, not a replay of the import pipeline, so it
    deliberately does not re-run ``failure.name``/``failure.description``
    through the importer's own ``strip_html``/``extract_image_urls``/
    ``extract_link_urls`` handling the way ``tasks._create_pin_from_confirmed``
    would; those already-plain strings are passed through as-is.

    Args:
        failure: The pending failure being resolved.
        profile: The owning profile (must match ``failure.profile``).
        address: Free-text address to geocode, when coordinates weren't given.
        latitude: Marker latitude, when an address wasn't given.
        longitude: Marker longitude, when an address wasn't given.

    Returns:
        The pin that was moved (legacy repair) or newly placed.

    Raises:
        services.pins.pin_creation.PinCreationError: Neither a usable address nor
            coordinates were given, or the address couldn't be geocoded.
        services.pins.pin_creation.PinCreationForbiddenError: An address needed
            geocoding but external lookups are turned off for this profile.
    """
    if latitude is None or longitude is None:
        if not address:
            raise PinCreationError("No address or lat/lon provided.")
        if not profile.external_apis_enabled:
            raise PinCreationForbiddenError("External lookups are turned off in your settings - drop a pin on the map instead.")
        latitude, longitude = get_pin_by_address(address)
        if latitude is None or longitude is None:
            raise PinCreationError("Unable to convert address to lat/lng.")

    # --- TEMPORARY (legacy CID coordinate repair) -------------------------
    repaired = repair_legacy_pin_coordinates(
        profile=profile,
        cid=int(failure.cid),
        name=failure.name,
        latitude=latitude,
        longitude=longitude,
    )
    # --- end TEMPORARY ------------------------------------------------------

    if repaired is not None:
        pin = repaired
    else:
        result = create_pin_for_profile(
            profile,
            name=failure.name or None,
            description=failure.description or None,
            latitude=latitude,
            longitude=longitude,
            name_is_user_provided=False,
        )
        pin = result.pin

    failure.status = PinImportFailureStatus.RESOLVED
    failure.pin = pin
    failure.save(update_fields=["status", "pin", "updated"])
    return pin


def dismiss_pin_import_failure(failure: PinImportFailure) -> None:
    """Dismiss a pending failure entry without placing a pin for it.

    Args:
        failure: The pending failure being dismissed.
    """
    failure.status = PinImportFailureStatus.DISMISSED
    failure.save(update_fields=["status", "updated"])


def auto_resolve_pin_import_failure_for_cid(profile: Profile, cid: int, pin: Pin) -> None:
    """Clear a pending failure the moment its cid resolves on its own.

    Safe to call unconditionally on every successful pin placement in
    ``tasks.resolve_deferred_pin_locations`` - including for a cid that never had
    a failure entry, in which case this is a no-op. At most one pending row can
    ever match a given ``(profile, cid)``, per the ``uq_pin_import_failure_profile_cid``
    constraint.

    Args:
        profile: Owner the failure (if any) belongs to.
        cid: The Google Maps CID that just resolved.
        pin: The pin created or found for that cid.
    """
    PinImportFailure.objects.filter(profile=profile, cid=cid, status=PinImportFailureStatus.PENDING).update(
        status=PinImportFailureStatus.RESOLVED,
        pin=pin,
        updated=timezone.now(),
    )
