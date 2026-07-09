"""Storage quota accounting and upload downscale policy.

Central place for everything the storage-quota feature needs:

- Resolving a user's quota (site default vs. subscription-role overrides).
- Summing how many bytes of uploads a profile has stored.
- Deciding how (and whether) an upload should be downscaled / converted,
  combining the site-wide policy with the user's own voluntary cap.
- Estimating how many more photos fit in the remaining quota at a given
  downscale setting, so the settings UI can show an intuitive number.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import Sum
from django.template.defaultfilters import filesizeformat

from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions.model import active_subscription_roles

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

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


def get_quota_bytes(profile: Profile) -> int | None:
    """Resolve the storage quota for a profile, in bytes.

    The site-wide default applies to everyone; active subscription roles with
    their own quota raise it (the largest applicable quota wins). A quota of
    0 GB anywhere means unlimited.

    Args:
        profile: The profile whose quota to resolve.

    Returns:
        The quota in bytes, or None when the user's storage is unlimited.
    """
    settings = SiteSettings.get_current()
    quotas_gb = [settings.storage_quota_gb]
    for role in active_subscription_roles(profile.user):
        if role.storage_quota_gb is not None:
            quotas_gb.append(role.storage_quota_gb)
    if any(quota == 0 for quota in quotas_gb):
        return None
    return max(quotas_gb) * GIB


def get_storage_used_bytes(profile: Profile) -> int:
    """Total bytes of stored uploads counted against a profile's quota.

    Rows predating the ``file_size`` field are skipped until their size is
    lazily backfilled by ``process_image_upload``.

    Args:
        profile: The profile whose usage to sum.

    Returns:
        The number of bytes currently used.
    """
    from urbanlens.dashboard.models.images.model import Image

    total = Image.objects.filter(profile=profile).aggregate(total=Sum("file_size"))["total"]
    return int(total or 0)


def quota_error_for_upload(profile: Profile, upload_size: int | None) -> str | None:
    """Check whether an upload of ``upload_size`` bytes fits in the profile's quota.

    Args:
        profile: The uploading profile.
        upload_size: Size of the incoming file in bytes (None counts as 0 -
            an UploadedFile of unknown size is still admitted, and its true
            size is recorded once stored).

    Returns:
        A user-facing error message when the upload would exceed the quota,
        or None when the upload is allowed.
    """
    quota = get_quota_bytes(profile)
    if quota is None:
        return None
    used = get_storage_used_bytes(profile)
    if used + max(upload_size or 0, 0) <= quota:
        return None
    return f"This upload would exceed your storage quota ({filesizeformat(used)} of {filesizeformat(quota)} used). Delete some photos or lower your image size in Settings → Storage."


def get_entitled_policy(profile: Profile) -> tuple[int | None, bool]:
    """The site-imposed downscale policy for a profile, ignoring the user's own cap.

    Users with an active subscription are exempt from site-imposed downscaling
    and WebP conversion unless the admin enabled "downscale subscriber uploads".

    Args:
        profile: The uploading profile.

    Returns:
        (max_dimension, convert_webp): the longest-edge cap in pixels (None
        when the site imposes none) and whether uploads are re-encoded as WebP.
    """
    settings = SiteSettings.get_current()
    exempt = not settings.image_downscale_vip and bool(active_subscription_roles(profile.user))
    max_dimension = settings.image_downscale_max_dimension if settings.image_downscale_enabled and not exempt else None
    convert_webp = settings.image_convert_webp and not exempt
    return max_dimension, convert_webp


def get_downscale_policy(profile: Profile) -> tuple[int | None, bool]:
    """The effective downscale policy for a profile's future uploads.

    Combines the site-imposed policy with the user's voluntary cap: the user
    can only tighten the cap (the smaller dimension wins), never loosen it.

    Args:
        profile: The uploading profile.

    Returns:
        (max_dimension, convert_webp) as in :func:`get_entitled_policy`.
    """
    entitled_dimension, convert_webp = get_entitled_policy(profile)
    dimensions = [d for d in (entitled_dimension, profile.image_downscale_max_dimension) if d]
    return (min(dimensions) if dimensions else None), convert_webp


def estimate_bytes_per_photo(max_dimension: int | None, convert_webp: bool) -> int:
    """Rough bytes one stored photo occupies at a given downscale setting.

    Args:
        max_dimension: Longest-edge cap in pixels; None means original size,
            for which a 12 MP phone photo is assumed.
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
        The estimated photo count (never negative).
    """
    return max(remaining_bytes, 0) // estimate_bytes_per_photo(max_dimension, convert_webp)


def get_storage_settings_context(profile: Profile) -> dict:
    """Everything the user settings "Storage" section needs to render.

    Args:
        profile: The profile viewing the settings page.

    Returns:
        Dict with usage totals, quota, percent used, the entitled policy, and
        the list of downscale options (each with its own photos-remaining
        estimate) for the preference select.
    """
    quota_bytes = get_quota_bytes(profile)
    used_bytes = get_storage_used_bytes(profile)
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

    return {
        "storage_quota_bytes": quota_bytes,
        "storage_used_bytes": used_bytes,
        "storage_remaining_bytes": remaining_bytes,
        "storage_percent_used": percent_used,
        "storage_entitled_dimension": entitled_dimension,
        "storage_convert_webp": convert_webp,
        "storage_downscale_options": options,
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
