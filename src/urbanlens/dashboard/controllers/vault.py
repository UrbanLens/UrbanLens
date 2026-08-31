"""Vault landing page - counts, storage usage, and a way into Photos/Documents."""

from __future__ import annotations

from itertools import chain
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.storage import get_quota_bytes, get_storage_totals

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

_RECENT_LIMIT = 12


class VaultHomeView(LoginRequiredMixin, View):
    """The Vault landing page.

    GET /vault/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the Vault home page.

        Args:
            request: The HTTP request.

        Returns:
            The rendered Vault home page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        gallery = Image.objects.uploaded_by(profile)
        photo_count = gallery.photos().count()
        document_count = gallery.documents().count()
        album_count = Album.objects.for_profile(profile).count()

        used_bytes, exempt_bytes = get_storage_totals(profile)
        quota_bytes = get_quota_bytes(profile)
        remaining_bytes = None if quota_bytes is None else max(quota_bytes - used_bytes, 0)
        percent_used = min(round(used_bytes * 100 / quota_bytes), 100) if quota_bytes else 0

        # A shared "most recent upload" strip across both media types - the two
        # querysets are each already small (only the most recent slice), so
        # merging and re-sorting in Python beats a UNION query for this size.
        recent = sorted(
            chain(gallery.photos().order_by("-created")[:_RECENT_LIMIT], gallery.documents().order_by("-created")[:_RECENT_LIMIT]),
            key=lambda image: image.created,
            reverse=True,
        )[:_RECENT_LIMIT]

        return render(
            request,
            "dashboard/pages/vault/index.html",
            {
                "page_name": "vault",
                "photo_count": photo_count,
                "document_count": document_count,
                "album_count": album_count,
                "recent_uploads": recent,
                "storage_used_bytes": used_bytes,
                "storage_quota_bytes": quota_bytes,
                "storage_exempt_bytes": exempt_bytes,
                "storage_remaining_bytes": remaining_bytes,
                "storage_percent_used": percent_used,
            },
        )
