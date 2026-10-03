"""Vault → Photos / Documents: each media kind's gallery page, its windowed grid's JSON pages, and its uploads.

Every view here takes the ``kind`` URL kwarg and reads what differs from that kind's ``MediaKindSpec``.
Deleting an item goes through ``vault_photos.PhotoActionView``, whose ``delete`` checks only ownership.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.controllers.vault_photos import organize_context
from urbanlens.dashboard.models.images.kinds import MediaKindSpec, media_kind_spec
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.sort import GALLERY_SORT_SPECS, GallerySort, gallery_sort_spec
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.images import image_to_gallery_json

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

_GALLERY_PAGE_SIZE = 24
_MAX_ITEMS_PER_FETCH = 100


def _sorted_gallery(spec: MediaKindSpec, profile: Profile, request: HttpRequest) -> QuerySet[Image]:
    """The profile's uploads of *spec*'s kind, ordered by the request's ``sort``.

    Args:
        spec: Which gallery.
        profile: Whose uploads to list.
        request: The current request, read for ``sort`` and, where the kind offers it, ``show``.

    Returns:
        The gallery queryset, ordered.
    """
    # select_related: image_to_gallery_json names the uploader through Profile.username -> user.username, two queries per row otherwise.
    gallery = Image.objects.uploaded_by(profile).of_kind(spec.kind).select_related(*spec.select_related)
    if spec.offers_from_others and request.GET.get("show") == "from_others":
        gallery = gallery.copied_from_others()
    return gallery_sort_spec(request.GET.get("sort") or GallerySort.RECENT).apply(gallery)


class VaultMediaView(LoginRequiredMixin, View):
    """A media kind's gallery page - upload zone and the grid's first page.

    GET /vault/<plural>/
    """

    def get(self, request: HttpRequest, kind: str) -> HttpResponse:
        """Render the gallery page.

        Args:
            request: The HTTP request.
            kind: The ``MediaKind`` this route serves.

        Returns:
            The rendered page.
        """
        from urbanlens.dashboard.services.media.storage import get_quota_bytes, get_storage_totals, max_upload_file_size_bytes

        spec = media_kind_spec(kind)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        gallery = _sorted_gallery(spec, profile, request)
        used_bytes, exempt_bytes = get_storage_totals(profile)
        context: dict[str, Any] = {
            "page_name": "vault",
            "media_kind": spec,
            "items": list(gallery[:_GALLERY_PAGE_SIZE]),
            "item_count": gallery.count(),
            "profile": profile,
            "storage_used_bytes": used_bytes,
            "storage_quota_bytes": get_quota_bytes(profile),
            "storage_exempt_bytes": exempt_bytes,
            "max_upload_file_size_bytes": max_upload_file_size_bytes(),
            "grid_page_size": _GALLERY_PAGE_SIZE,
            "sort": request.GET.get("sort") or GallerySort.RECENT,
            "gallery_sort_specs": list(GALLERY_SORT_SPECS.values()),
        }
        if spec.offers_from_others:
            context["show"] = request.GET.get("show") or "mine"
            context["from_others_count"] = Image.objects.uploaded_by(profile).of_kind(spec.kind).copied_from_others().count()
        if spec.organizes_visits:
            context.update(organize_context(profile))
        return render(request, spec.template, context)


class VaultMediaItemsView(LoginRequiredMixin, View):
    """One page of a media kind's gallery as JSON, for the windowed grid.

    GET /vault/<plural>/items/?offset=&limit=&sort=

    Same ``{items, total, offset, limit}`` shape as the album grid's ``AlbumItemsView``, so every grid shares
    frontend/ts/shared/photo-virtual-grid.ts.
    """

    def get(self, request: HttpRequest, kind: str) -> JsonResponse:
        """Return one page of the profile's gallery, in the requested sort.

        Args:
            request: The HTTP request, with ``offset``/``limit``/``sort`` query params.
            kind: The ``MediaKind`` this route serves.

        Returns:
            JSON ``{items, total, offset, limit}``.
        """
        spec = media_kind_spec(kind)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            offset = max(0, int(request.GET.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(request.GET.get("limit") or _GALLERY_PAGE_SIZE)
        except (TypeError, ValueError):
            limit = _GALLERY_PAGE_SIZE
        limit = min(max(1, limit), _MAX_ITEMS_PER_FETCH)

        gallery = _sorted_gallery(spec, profile, request)
        return JsonResponse(
            {
                "items": [image_to_gallery_json(image, request, profile) for image in gallery[offset : offset + limit]],
                "total": gallery.count(),
                "offset": offset,
                "limit": limit,
            },
        )


class VaultMediaUploadView(LoginRequiredMixin, View):
    """Upload one file to a media kind's gallery (called once per file by the page's uploader).

    POST /vault/<plural>/upload/

    Every kind goes through ``upload_photo``, which types the file by its content rather than by which
    endpoint received it, and enforces each kind's feature gate.
    """

    def post(self, request: HttpRequest, kind: str) -> JsonResponse:
        """Create an unfiled Image and queue its processing.

        Args:
            request: The HTTP request, carrying the file under the kind's upload field.
            kind: The ``MediaKind`` this route serves.

        Returns:
            The new item serialized for the gallery grid, or an error.
        """
        from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError, upload_photo
        from urbanlens.dashboard.services.photos.uploads import record_photo_upload_failure

        spec = media_kind_spec(kind)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        upload = request.FILES.get(spec.upload_field)
        if not upload:
            return JsonResponse({"error": spec.missing_file_error}, status=400)

        try:
            image = upload_photo(profile, upload, caption=(upload.name or "") if spec.caption_from_filename else None)
        except PhotoUploadError as exc:
            # Recorded, not just returned: Vault > Photos renders a "Couldn't upload" panel with a retry.
            logger.info("%s upload rejected for profile %s: %s", spec.kind, profile.pk, exc.message)
            record_photo_upload_failure(profile, upload.name or spec.kind, exc.generic_message)
            return JsonResponse({"error": exc.generic_message}, status=exc.status, headers=exc.headers)

        return JsonResponse(image_to_gallery_json(image, request, profile), status=201)
