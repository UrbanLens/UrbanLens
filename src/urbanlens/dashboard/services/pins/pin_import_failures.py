"""Review-queue lifecycle for PinImportFailure - pins whose Google Maps CID couldn't be resolved to a location during import."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure, PinImportFailureReason, PinImportFailureStatus
from urbanlens.dashboard.services.apis.locations.legacy_cid_coordinate_fix import repair_legacy_pin_coordinates
from urbanlens.dashboard.services.locations.geocoding import get_pin_by_address
from urbanlens.dashboard.services.pins.pin_creation import AddressResolutionError, NoLocationProvidedError, PinCreationForbiddenError, create_pin_for_profile

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile


def record_pin_import_failure(profile: Profile, cid: int, *, name: str, description: str, reason: PinImportFailureReason, maps_url: str = "") -> None:
    """Record a pin's cid that couldn't be placed automatically, unless one already exists.

    Args:
        profile: Owner the failure belongs to.
        cid: The Google Maps CID that couldn't be resolved.
        name: Best-known name for the place, captured at the moment it was deferred.
        description: Best-known description, captured the same way.
        reason: Why the automatic lookup failed.
        maps_url: The Google Maps URL the cid came from, when the import had one."""
    PinImportFailure.objects.get_or_create(
        profile=profile,
        cid=cid,
        defaults={
            "name": name,
            "description": description,
            "reason": reason,
            "maps_url": maps_url or "",
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
    This mirrors ``tasks._create_pin_from_confirmed``'s own use of the same repair for the automatic resolution path - manual resolution must not skip it, or a legacy pin the automatic path failed to fix stays stranded forever.

    Args:
        failure: The pending failure being resolved.
        profile: The owning profile (must match ``failure.profile``).
        address: Free-text address to geocode, when coordinates weren't given.
        latitude: Marker latitude, when an address wasn't given.
        longitude: Marker longitude, when an address wasn't given.

    Returns:
        The pin that was moved (legacy repair) or newly placed.

    Raises:
        services.pins.pin_creation.NoLocationProvidedError: Neither a usable address nor coordinates were given.
        services.pins.pin_creation.AddressResolutionError: The address couldn't be geocoded.
        services.pins.pin_creation.PinCreationForbiddenError: An address needed geocoding but external lookups are turned off for this profile."""
    if latitude is None or longitude is None:
        if not address:
            raise NoLocationProvidedError("Neither coordinates nor an address were given.")
        if not profile.external_apis_enabled:
            raise PinCreationForbiddenError("external_apis_enabled is False for this profile.")
        latitude, longitude = get_pin_by_address(address)
        if latitude is None or longitude is None:
            raise AddressResolutionError("Geocoding the given address returned no coordinates.")

    # TEMPORARY (legacy CID coordinate repair)
    repaired = repair_legacy_pin_coordinates(
        profile=profile,
        cid=int(failure.cid),
        name=failure.name,
        latitude=latitude,
        longitude=longitude,
    )
    # end TEMPORARY

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
    Safe to call unconditionally on every successful pin placement in ``tasks.resolve_deferred_pin_locations`` - including for a cid that never had a failure entry, in which case this is a no-op.

    Args:
        profile: Owner the failure (if any) belongs to.
        cid: The Google Maps CID that just resolved.
        pin: The pin created or found for that cid."""
    PinImportFailure.objects.filter(profile=profile, cid=cid, status=PinImportFailureStatus.PENDING).update(
        status=PinImportFailureStatus.RESOLVED,
        pin=pin,
        updated=timezone.now(),
    )
