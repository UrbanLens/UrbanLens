"""Vault → Documents page: site-wide document gallery, mirroring Vault Photos'
grid/skeleton/pruning/sort infrastructure (see controllers.vault_photos) for
``MediaKind.DOCUMENT`` uploads instead of photos.

Deliberately simpler than Vault Photos: documents have no EXIF/GPS to derive
an organize queue or visit suggestions from, so this page is just an upload
zone plus the full gallery grid. Deleting a document reuses
``vault.photos.action``'s ``delete`` handler (``PhotoActionView.delete_photo``
is already media-type-agnostic - it only checks ownership).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.sort import GALLERY_SORT_SPECS, GallerySort, gallery_sort_spec
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.images import image_to_gallery_json

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest, HttpResponse

_GALLERY_PAGE_SIZE = 24


def _sorted_documents(profile: Profile, request: HttpRequest) -> QuerySet[Image]:
    """The profile's uploaded documents, ordered by the requested ``sort`` param.

    Args:
        profile: Whose documents to list.
        request: The current HttpRequest, read for ``sort``.

    Returns:
        The document queryset, ordered.
    """
    # profile__user: see the same note on vault_photos._sorted_gallery - the
    # uploader name behind image_to_gallery_json is two queries per row otherwise.
    gallery = Image.objects.uploaded_by(profile).documents().select_related("profile__user")
    sort = gallery_sort_spec(request.GET.get("sort") or GallerySort.RECENT)
    return sort.apply(gallery)


class VaultDocumentsView(LoginRequiredMixin, View):
    """The Vault Documents page - upload zone and full gallery.

    GET /vault/documents/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the Documents page.

        Args:
            request: The HTTP request.

        Returns:
            The rendered Documents page.
        """
        from urbanlens.dashboard.services.media.storage import get_quota_bytes, get_storage_totals, max_upload_file_size_bytes

        profile, _ = Profile.objects.get_or_create(user=request.user)
        gallery = _sorted_documents(profile, request)
        sort = request.GET.get("sort") or GallerySort.RECENT
        documents = list(gallery[:_GALLERY_PAGE_SIZE])
        used_bytes, exempt_bytes = get_storage_totals(profile)
        return render(
            request,
            "dashboard/pages/vault/documents.html",
            {
                "page_name": "vault",
                "documents": documents,
                "profile": profile,
                "document_count": gallery.count(),
                "storage_used_bytes": used_bytes,
                "storage_quota_bytes": get_quota_bytes(profile),
                "storage_exempt_bytes": exempt_bytes,
                "max_upload_file_size_bytes": max_upload_file_size_bytes(),
                "grid_page_size": _GALLERY_PAGE_SIZE,
                "sort": sort,
                "gallery_sort_specs": list(GALLERY_SORT_SPECS.values()),
            },
        )


class DocumentItemsView(LoginRequiredMixin, View):
    """One page of the full document gallery grid as JSON, for the windowed grid.

    GET /vault/documents/items/?offset=&limit=&sort=

    Same ``{items, total, offset, limit}`` shape as Vault Photos' own
    ``PhotoItemsView`` (see controllers.vault_photos), which is what lets
    both pages share ``photo-virtual-grid.ts``'s fetch/scroll/prune engine.
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        """Return one page of the profile's documents, in the requested sort.

        Args:
            request: The HTTP request, with ``offset``/``limit``/``sort`` query params.

        Returns:
            JSON ``{items, total, offset, limit}``.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            offset = max(0, int(request.GET.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(request.GET.get("limit") or _GALLERY_PAGE_SIZE)
        except (TypeError, ValueError):
            limit = _GALLERY_PAGE_SIZE
        limit = min(max(1, limit), 100)

        gallery = _sorted_documents(profile, request)
        total = gallery.count()
        documents = list(gallery[offset : offset + limit])
        return JsonResponse(
            {
                "items": [image_to_gallery_json(document, request, profile) for document in documents],
                "total": total,
                "offset": offset,
                "limit": limit,
            },
        )


class DocumentUploadView(LoginRequiredMixin, View):
    """Upload one document to the Vault gallery (called once per file by the page JS).

    POST /vault/documents/upload/

    Reuses ``services.photos.photo_upload.upload_photo`` - the same pipeline
    Vault Photos' own dropzone calls - which already classifies a document by
    content-type/extension and enforces the ``DOCUMENT_UPLOADS`` feature gate;
    nothing here is document-specific beyond the field name read from the
    request and the error message for a missing file.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Create an unfiled document Image and kick off background processing.

        Args:
            request: The HTTP request carrying a ``document`` file.

        Returns:
            The new document serialized for the gallery grid, or an error.
        """
        from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError, upload_photo
        from urbanlens.dashboard.services.photos.uploads import record_photo_upload_failure

        profile, _ = Profile.objects.get_or_create(user=request.user)
        doc_file = request.FILES.get("document")
        if not doc_file:
            return JsonResponse({"error": "No document provided."}, status=400)

        try:
            doc = upload_photo(profile, doc_file, caption=doc_file.name or "")
        except PhotoUploadError as exc:
            # See the same call in vault_photos.PhotoUploadView: a failure here
            # belongs in the retry panel on Vault > Photos, not only in a toast.
            record_photo_upload_failure(profile, doc_file.name or "document", exc.message)
            return JsonResponse({"error": exc.message}, status=exc.status)

        return JsonResponse(image_to_gallery_json(doc, request, profile), status=201)
