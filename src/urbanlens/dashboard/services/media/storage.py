"""Storage quota accounting and upload downscale policy. - Resolving a user's quota (site default vs. subscription-role overrides). - Summing how many bytes of uploads a profile has stored. - Deciding how (and whether) an upload should be downscaled..."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, ClassVar

from django.db import OperationalError, connection, transaction
from django.db.models import Q, Sum
from django.template.defaultfilters import filesizeformat

from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions.model import active_subscription_roles

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole

logger = logging.getLogger(__name__)

#: How long a request waits for the same profile's other uploads to finish saving.
UPLOAD_RESERVATION_WAIT_SECONDS = 20.0
#: Imports run in a worker with nobody waiting on a response, so they queue for longer.
BACKGROUND_RESERVATION_WAIT_SECONDS = 120.0
_LOCK_NOT_AVAILABLE = "55P03"

GIB = 1024**3

# Downscale caps (longest edge, px) offered to users in the storage settings.
# Ordered largest to smallest; labels describe the practical quality level.
DOWNSCALE_DIMENSION_CHOICES: list[tuple[int, str]] = [
    (3840, "3840 px — 4K"),
    (2560, "2560 px — Quad HD"),
    (1920, "1920 px — Full HD"),
    (1280, "1280 px — HD"),
    (800, "800 px — web thumbnail"),
]

# Downscale caps (height, px) offered to users for video uploads.
VIDEO_DOWNSCALE_HEIGHT_CHOICES: list[tuple[int, str]] = [
    (2160, "2160 px — 4K"),
    (1440, "1440 px — 2K"),
    (1080, "1080 px — Full HD"),
    (720, "720 px — HD"),
    (480, "480 px — SD"),
]

# Rough re-encoded output density, in bytes per pixel, used only for the
# "about N more photos" estimate. JPEG at quality ~85 lands around 0.35 B/px;
# WebP at the same visual quality around 0.22 B/px.
_JPEG_BYTES_PER_PIXEL = 0.35
_WEBP_BYTES_PER_PIXEL = 0.22
# Long edge assumed for photos stored at original size (a 12 MP phone camera).
_ORIGINAL_ASSUMED_DIMENSION = 4032
# Photos are rarely square; assume a 4:3 frame when converting the long edge
# to a pixel count.
_ASSUMED_ASPECT = 0.75


def get_quota_bytes(profile: Profile, *, roles: Sequence[SubscriptionRole] | None = None) -> int | None:
    """Resolve the storage quota for a profile, in bytes.

    Args:
        profile: The profile whose quota to resolve.
        roles: The profile's active subscription roles, when the caller has already resolved them.

    Returns:
        The quota in bytes, or None when the user's storage is unlimited."""
    settings = SiteSettings.get_current()
    quotas_gb = [settings.storage_quota_gb]
    for role in active_subscription_roles(profile.user) if roles is None else roles:
        if role.storage_quota_gb is not None:
            quotas_gb.append(role.storage_quota_gb)
    if any(quota == 0 for quota in quotas_gb):
        return None
    return max(quotas_gb) * GIB


def get_storage_used_bytes(profile: Profile) -> int:
    """Total bytes of stored uploads counted against a profile's quota.
    Rows carrying a ``quota_exempt_reason`` are skipped permanently - cached external media and community-rewarded contributions are storage the whole site benefits from, so no single user is charged for them (see ``services.media.quota_rewards``).

    Args:
        profile: The profile whose usage to sum.

    Returns:
        The number of bytes currently used."""
    from urbanlens.dashboard.models.images.model import Image

    total = Image.objects.filter(profile=profile, quota_exempt_reason="").aggregate(total=Sum("file_size"))["total"]
    return int(total or 0)


def get_exempt_bytes(profile: Profile) -> int:
    """Total bytes this profile stores that don't count against their quota.
    Surfaced in the storage settings so a user can see the benefit rather than just an unexplained gap between their file list and their usage bar.

    Args:
        profile: The profile whose exempt storage to sum.

    Returns:
        The number of exempt bytes."""
    from urbanlens.dashboard.models.images.model import Image

    total = Image.objects.filter(profile=profile).exclude(quota_exempt_reason="").aggregate(total=Sum("file_size"))["total"]
    return int(total or 0)


def get_storage_totals(profile: Profile) -> tuple[int, int]:
    """Both halves of a profile's storage accounting, in one query.

    Args:
        profile: The profile whose storage to total.

    Returns:
        ``(counted_bytes, exempt_bytes)``."""
    from urbanlens.dashboard.models.images.model import Image

    totals = Image.objects.filter(profile=profile).aggregate(
        counted=Sum("file_size", filter=Q(quota_exempt_reason="")),
        exempt=Sum("file_size", filter=~Q(quota_exempt_reason="")),
    )
    return int(totals["counted"] or 0), int(totals["exempt"] or 0)


def _quota_exceeded_message(used: int, quota: int) -> str:
    # "files", not "photos": documents share this quota and reach this same
    # message through the Vault's Documents dropzone.
    return f"This upload would exceed your storage quota ({filesizeformat(used)} of {filesizeformat(quota)} used). Delete some files, or lower your image size in Settings → Storage."


class UploadRefusedError(Exception):
    """An upload the reservation would not admit.

    Attributes:
        message: User-facing explanation, safe to return to the uploader.
        status: The HTTP status a view should answer with.
    """

    status: ClassVar[int] = 400

    def __init__(self, message: str) -> None:
        """Store the user-facing message.

        Args:
            message: Why the upload was refused.
        """
        super().__init__(message)
        self.message = message


class StorageQuotaExceededError(UploadRefusedError):
    """The upload does not fit in what is left of the profile's storage quota."""

    status = 413


class UploadReservationBusyError(UploadRefusedError):
    """Another upload by the same profile held the reservation for longer than the caller would wait."""

    status = 429


@dataclass(slots=True)
class UploadReservation:
    """A profile's storage, held exclusively until the surrounding transaction ends.

    Attributes:
        profile: The uploading profile.
        reserved: Bytes admitted through :meth:`reserve` so far in this reservation.
    """

    profile: Profile
    reserved: int = 0
    _quota: int | None = field(default=None, init=False)
    _used: int | None = field(default=None, init=False)
    _resolved: bool = field(default=False, init=False)

    def _resolve(self) -> tuple[int | None, int]:
        if not self._resolved:
            self._quota = get_quota_bytes(self.profile)
            self._used = get_storage_used_bytes(self.profile) if self._quota is not None else 0
            self._resolved = True
        return self._quota, self._used or 0

    def reserve(self, size: int) -> None:
        """Admit *size* more counted bytes, or refuse.

        Rows stored under this reservation are counted from the bytes reserved here, not re-read,
        so reserve before each counted row even when several share one reservation.

        Args:
            size: Bytes the next counted row will store.

        Raises:
            StorageQuotaExceededError: The quota has no room for *size* more bytes.
        """
        size = max(size, 0)
        quota, used = self._resolve()
        if quota is not None and used + self.reserved + size > quota:
            raise StorageQuotaExceededError(_quota_exceeded_message(used + self.reserved, quota))
        self.reserved += size


def lock_profile_uploads(profile: Profile, *, wait_seconds: float = UPLOAD_RESERVATION_WAIT_SECONDS) -> None:
    """Hold *profile*'s upload lock until the current transaction ends.

    Args:
        profile: Whose uploads to serialize.
        wait_seconds: How long to wait for another holder before giving up.

    Raises:
        UploadReservationBusyError: Another transaction held the lock for longer than *wait_seconds*.
        django.db.transaction.TransactionManagementError: Called outside a transaction, where the lock would be released at once.
    """
    if not connection.in_atomic_block:
        raise transaction.TransactionManagementError("lock_profile_uploads needs a transaction to hold the lock for.")
    timeout = f"{max(1, int(wait_seconds * 1000))}ms"
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout')")
            (previous,) = cursor.fetchone()
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [timeout])
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"upload-reservation:{profile.pk}"])
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        logger.info("Upload reservation for profile %s still held after %ss", profile.pk, wait_seconds)
        raise UploadReservationBusyError("Another upload to your account is still being saved. Try again in a moment.") from exc


@contextmanager
def reserve_upload(profile: Profile, size: int | None, *, wait_seconds: float = UPLOAD_RESERVATION_WAIT_SECONDS) -> Iterator[UploadReservation]:
    """Store uploads for *profile* one reservation at a time, inside one transaction.

    Everything that decides whether a row may be stored (the quota, a duplicate-checksum lookup, a
    rolling allowance) belongs inside the block, next to the insert, so that no other upload by the
    same profile can read the same state before this one commits.

    Args:
        profile: The uploading profile.
        size: Counted bytes to reserve on entry, or None to take the lock only and :meth:`UploadReservation.reserve` later (for a caller that may store a quota-exempt copy instead).
        wait_seconds: How long to wait for the profile's other uploads.

    Yields:
        The reservation.

    Raises:
        StorageQuotaExceededError: *size* does not fit.
        UploadReservationBusyError: The profile's other uploads held the lock for longer than *wait_seconds*.
    """
    with transaction.atomic():
        lock_profile_uploads(profile, wait_seconds=wait_seconds)
        reservation = UploadReservation(profile=profile)
        if size is not None:
            reservation.reserve(size)
        yield reservation


def ingress_body_limit_bytes() -> int:
    """The largest request body the ingress in front of this deployment will pass.

    Returns:
        The cap in bytes, or 0 when nothing in front of the app imposes one.
    """
    from django.conf import settings

    return max(0, int(getattr(settings, "MAX_REQUEST_BODY_BYTES", 0) or 0))


def cap_to_ingress(limit_bytes: int) -> int:
    """Lower *limit_bytes* to what the ingress will actually carry.

    Args:
        limit_bytes: The limit this deployment would otherwise apply.

    Returns:
        The smaller of *limit_bytes* and the ingress cap, or *limit_bytes* unchanged when no cap is configured."""
    ingress = ingress_body_limit_bytes()
    return min(limit_bytes, ingress) if ingress else limit_bytes


def max_upload_file_size_bytes() -> int:
    """Site-wide max size for a single photo/video/document upload, in bytes.

    Returns:
        The admin's configured limit, lowered to the ingress cap when there is one."""
    return cap_to_ingress(SiteSettings.get_current().max_upload_file_size_mb * 1_000_000)


def file_size_error_for_upload(upload_size: int | None) -> str | None:
    """Check a single upload against the site-wide max file size.

    Args:
        upload_size: Size of the incoming file in bytes.

    Returns:
        A user-facing error message when the file is too large, or None."""
    max_bytes = max_upload_file_size_bytes()
    size = upload_size or 0
    if size <= max_bytes:
        return None
    return f"That file is too large ({filesizeformat(size)}). The maximum upload size is {filesizeformat(max_bytes)}."


def get_entitled_policy(profile: Profile) -> tuple[int | None, bool]:
    """The site-imposed downscale policy for a profile, ignoring the user's own cap.

    Args:
        profile: The uploading profile.

    Returns:
        (max_dimension, convert_webp): the longest-edge cap in pixels (None when the site imposes none) and whether uploads are re-encoded as WebP."""
    settings = SiteSettings.get_current()
    exempt = not settings.image_downscale_vip and bool(active_subscription_roles(profile.user))
    max_dimension = settings.image_downscale_max_dimension if settings.image_downscale_enabled and not exempt else None
    convert_webp = settings.image_convert_webp and not exempt
    return max_dimension, convert_webp


def get_downscale_policy(profile: Profile) -> tuple[int | None, bool]:
    """The effective downscale policy for a profile's future uploads.
    Combines the site-imposed policy with the user's voluntary cap: the user can only tighten the cap (the smaller dimension wins), never loosen it.

    Args:
        profile: The uploading profile.

    Returns:
        (max_dimension, convert_webp) as in :func:`get_entitled_policy`."""
    entitled_dimension, convert_webp = get_entitled_policy(profile)
    dimensions = [d for d in (entitled_dimension, profile.image_downscale_max_dimension) if d]
    return (min(dimensions) if dimensions else None), convert_webp


def get_stored_photo_policy(image: Image, max_dimension_override: int | None = None) -> tuple[int | None, bool]:
    """The size and format a photo is stored in.

    Args:
        image: The photo's row.
        max_dimension_override: Longest-edge cap for a row with no profile, from the code that created it.

    Returns:
        (max_dimension, convert_webp) as in :func:`get_entitled_policy`.
    """
    if image.profile is not None:
        return get_downscale_policy(image.profile)
    # A profile-less row is a provider photo kept for a location's gallery, with no plan to read a policy from.
    from urbanlens.dashboard.services.photos.photo_enrichment import DEFAULT_ENRICHED_MAX_DIMENSION

    return (max_dimension_override if max_dimension_override is not None else DEFAULT_ENRICHED_MAX_DIMENSION), True


def get_entitled_video_policy(profile: Profile) -> int | None:
    """The site-imposed video downscale policy for a profile, ignoring the user's own cap.
    Mirrors :func:`get_entitled_policy` for photos, but videos have no WebP- equivalent format toggle - only a max resolution.

    Args:
        profile: The uploading profile.

    Returns:
        Max video height in pixels the site imposes, or None for no cap."""
    settings = SiteSettings.get_current()
    exempt = not settings.video_downscale_vip and bool(active_subscription_roles(profile.user))
    return settings.video_downscale_max_height if settings.video_downscale_enabled and not exempt else None


def get_video_downscale_policy(profile: Profile) -> int | None:
    """The effective video downscale policy for a profile's future uploads.
    Combines the site-imposed cap with the user's voluntary cap: the user can only tighten it (the smaller height wins), never loosen it.

    Args:
        profile: The uploading profile.

    Returns:
        Max video height in pixels, or None for no cap."""
    entitled = get_entitled_video_policy(profile)
    heights = [h for h in (entitled, profile.video_downscale_max_height) if h]
    return min(heights) if heights else None


def estimate_bytes_per_photo(max_dimension: int | None, convert_webp: bool) -> int:
    """Rough bytes one stored photo occupies at a given downscale setting.

    Args:
        max_dimension: Longest-edge cap in pixels; None means original size, for which a 12 MP phone photo is assumed.
        convert_webp: Whether uploads are re-encoded as WebP.

    Returns:
        Estimated stored bytes per photo (always at least 50 KB).
    """
    dimension = max_dimension or _ORIGINAL_ASSUMED_DIMENSION
    pixels = dimension * dimension * _ASSUMED_ASPECT
    bytes_per_pixel = _WEBP_BYTES_PER_PIXEL if convert_webp else _JPEG_BYTES_PER_PIXEL
    return max(int(pixels * bytes_per_pixel), 50_000)


def estimate_photos_remaining(remaining_bytes: int, max_dimension: int | None, convert_webp: bool) -> int:
    """Approximate number of additional photos that fit in ``remaining_bytes``.

    Args:
        remaining_bytes: Free quota, in bytes.
        max_dimension: Longest-edge cap the photos would be stored at.
        convert_webp: Whether uploads are re-encoded as WebP.

    Returns:
        The estimated photo count (never negative)."""
    return max(remaining_bytes, 0) // estimate_bytes_per_photo(max_dimension, convert_webp)


def get_storage_settings_context(profile: Profile) -> dict:
    """Everything the user settings "Storage" section needs to render.

    Args:
        profile: The profile viewing the settings page.

    Returns:
        Dict with usage totals, quota, percent used, the entitled policy, and the list of downscale options (each with its own photos-remaining estimate) for the preference select."""
    quota_bytes = get_quota_bytes(profile)
    used_bytes, exempt_bytes = get_storage_totals(profile)
    remaining_bytes = None if quota_bytes is None else max(quota_bytes - used_bytes, 0)
    percent_used = 0
    if quota_bytes:
        percent_used = min(round(used_bytes * 100 / quota_bytes), 100)

    entitled_dimension, convert_webp = get_entitled_policy(profile)
    # Estimates need a finite budget; for unlimited accounts show the options
    # without photo counts (the template hides the estimate line).
    estimate_budget = remaining_bytes if remaining_bytes is not None else 0

    if entitled_dimension is None:
        default_label = "Site default — original size (no downscaling)"
    else:
        default_label = f"Site default — {entitled_dimension} px"

    options = [
        {
            "value": "",
            "label": default_label,
            "estimated_photos": estimate_photos_remaining(estimate_budget, entitled_dimension, convert_webp),
            "selected": profile.image_downscale_max_dimension is None,
        }
    ]
    for dimension, label in DOWNSCALE_DIMENSION_CHOICES:
        # Only offer caps that actually tighten the entitled policy.
        if entitled_dimension is not None and dimension >= entitled_dimension:
            continue
        options.append(
            {
                "value": str(dimension),
                "label": label,
                "estimated_photos": estimate_photos_remaining(estimate_budget, dimension, convert_webp),
                "selected": profile.image_downscale_max_dimension == dimension,
            }
        )

    entitled_video_height = get_entitled_video_policy(profile)
    if entitled_video_height is None:
        default_video_label = "Site default — original resolution (no downscaling)"
    else:
        default_video_label = f"Site default — {entitled_video_height} px"

    video_options = [
        {
            "value": "",
            "label": default_video_label,
            "selected": profile.video_downscale_max_height is None,
        }
    ]
    for height, label in VIDEO_DOWNSCALE_HEIGHT_CHOICES:
        if entitled_video_height is not None and height >= entitled_video_height:
            continue
        video_options.append(
            {
                "value": str(height),
                "label": label,
                "selected": profile.video_downscale_max_height == height,
            }
        )

    return {
        "storage_quota_bytes": quota_bytes,
        "storage_used_bytes": used_bytes,
        "storage_exempt_bytes": exempt_bytes,
        "storage_remaining_bytes": remaining_bytes,
        "storage_percent_used": percent_used,
        "storage_entitled_dimension": entitled_dimension,
        "storage_convert_webp": convert_webp,
        "storage_downscale_options": options,
        "storage_entitled_video_height": entitled_video_height,
        "storage_video_downscale_options": video_options,
    }


def allowed_user_dimension_values(profile: Profile) -> set[int]:
    """The downscale caps a user may choose for themselves.

    Args:
        profile: The profile saving the preference.

    Returns:
        The set of permitted pixel values (the empty preference is always allowed).
    """
    entitled_dimension, _ = get_entitled_policy(profile)
    return {dimension for dimension, _label in DOWNSCALE_DIMENSION_CHOICES if entitled_dimension is None or dimension < entitled_dimension}


def allowed_user_video_height_values(profile: Profile) -> set[int]:
    """The video downscale caps a user may choose for themselves.

    Args:
        profile: The profile saving the preference.

    Returns:
        The set of permitted height values (the empty preference is always allowed).
    """
    entitled_height = get_entitled_video_policy(profile)
    return {height for height, _label in VIDEO_DOWNSCALE_HEIGHT_CHOICES if entitled_height is None or height < entitled_height}
