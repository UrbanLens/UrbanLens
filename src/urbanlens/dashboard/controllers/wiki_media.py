"""Wiki Media gallery - the community-page counterpart of the pin detail Media section.

The pin detail page renders a combined Media gallery (external archival/media
providers plus the owner's own uploads) with per-user relevance marking (see
``controllers.pin.PinController.media_provider`` / ``media_relevance``). These
views expose the same external media on a Location's community **wiki**, with
two deliberate differences:

* **External media is automatic.** It's about the place, not a private upload,
  so it appears on the wiki straight from the shared per-Location
  ``LocationCache`` the pin detail page already warms. User-uploaded photos, by
  contrast, only appear once intentionally shared to the wiki (``Image.wiki``).
* **Thumbs are community votes.** A thumbs-up/down is stored in the same
  Location-scoped :class:`MediaRelevance` model, but the wiki reads the
  *aggregate* across every contributing profile as a net score (up - down) and
  sorts items highest-first. Because ``MediaRelevance`` is keyed by Location,
  a relevance mark made on any user's pin detail page already counts here - no
  materialization and no schema change (see
  ``MediaRelevanceQuerySet.vote_scores``).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.external_data import GalleryMediaSource

logger = logging.getLogger(__name__)

# How many shared wiki photos to render as votable Media tiles. The full,
# unlimited management surface (upload, delete, cover photo, Flickr import)
# stays behind the section's "Manage" tab (WikiGalleryView), exactly as the
# pin page's "Mine" tab manages the owner's own photos.
_WIKI_PHOTOS_PREVIEW_LIMIT = 60


class WikiMediaProviderView(LoginRequiredMixin, View):
    """One media provider's tiles for a wiki's location, vote-annotated.

    GET /location/<slug>/wiki/media/<source>/  → ``pin_media_items.html`` fragment
    """

    @staticmethod
    def _poll_attempt(request: HttpRequest) -> int:
        """Which poll cycle this request is (0 for the initial load)."""
        try:
            return max(int(request.GET.get("attempt", "0")), 0)
        except (TypeError, ValueError):
            return 0

    def get(self, request: HttpRequest, location_slug: str, source: str) -> HttpResponse:
        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        if source == "photos":
            return self._photos(request, location, wiki, profile)

        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
        from urbanlens.dashboard.services.external_data import GalleryMediaSource, get_panel_source

        panel = get_panel_source(source)
        if not isinstance(panel, GalleryMediaSource):
            return HttpResponse(status=404)

        cached = LocationCache.get_fresh(location, panel.cache_source)
        if cached is None:
            return self._pending(request, location, profile, source, panel)
        items = panel.media_items(cached.data or {})

        scores = MediaRelevance.objects.vote_scores(location, source)
        my_marks = dict(MediaRelevance.objects.for_gallery(profile, location, source).values_list("item_key", "is_relevant"))
        rendered_items = []
        for item in items:
            key = media_item_key(item.url)
            rendered_items.append({"item": item, "key": key, "is_relevant": my_marks.get(key), "vote_score": scores.get(key, 0)})

        return render(request, "dashboard/partials/pins/pin_media_items.html", {"rendered_items": rendered_items, "source_key": source, "wiki_mode": True})

    def _photos(self, request: HttpRequest, location: Location, wiki: Wiki, profile: Profile) -> HttpResponse:
        """Render photos intentionally shared to this wiki as votable Media tiles."""
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        images = Image.objects.filter(wiki=wiki).select_related("profile").visible_to(profile).exclude(image="").order_by("-created")[:_WIKI_PHOTOS_PREVIEW_LIMIT]

        scores = MediaRelevance.objects.vote_scores(location, "photos")
        my_marks = dict(MediaRelevance.objects.for_gallery(profile, location, "photos").values_list("item_key", "is_relevant"))
        rendered_items = []
        for img in images:
            url = img.image.url
            key = media_item_key(url)
            rendered_items.append(
                {
                    "item": MediaItem(url=url, thumb_url=url, caption=img.caption or "", source="Photos", page_url=url),
                    "key": key,
                    "is_relevant": my_marks.get(key),
                    "vote_score": scores.get(key, 0),
                    "image_id": img.pk,
                    "lat": img.latitude,
                    "lng": img.longitude,
                },
            )
        if not rendered_items:
            return HttpResponse(status=204)

        return render(request, "dashboard/partials/pins/pin_media_items.html", {"rendered_items": rendered_items, "source_key": "photos", "wiki_mode": True})

    def _pending(self, request: HttpRequest, location: Location, profile: Profile, source: str, panel: GalleryMediaSource) -> HttpResponse:
        """Warm the provider's cache from the viewer's own pin, or give up quietly.

        External media is fetched by a Celery task driven by a Pin (it supplies
        the search name and coordinates). A wiki viewer reaches the page because
        they have a pin at (or near) this location; we use *their* pin so the
        fetch is gated by their ``external_apis_enabled`` and counts against
        their quota. A boundary-mate viewer with no pin at this exact location
        just sees whatever the pin detail page has already cached (204).
        """
        from urbanlens.dashboard.services.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, schedule_panel_fetch

        driver_pin = location.pins.filter(profile=profile).select_related("location").first()
        if driver_pin is None or not panel.gate(driver_pin):
            return HttpResponse(status=204)

        attempt = self._poll_attempt(request)
        if attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(source, driver_pin):
            return HttpResponse(status=204)

        response = render(
            request,
            "dashboard/partials/pins/wiki_media_loader_pending.html",
            {"source": source, "poll_url": request.path, "next_attempt": attempt + 1, "poll_interval": POLL_INTERVAL_SECONDS},
        )
        response["UL-Panel-Pending"] = "1"
        response["HX-Retarget"] = f"#wiki-media-loader-{source}"
        response["HX-Reswap"] = "outerHTML"
        return response


class WikiMediaVoteView(LoginRequiredMixin, View):
    """Cast, flip, or clear the viewer's community vote on one wiki Media item.

    POST /location/<slug>/wiki/media/vote/  → ``{"my_vote": bool|null, "vote_score": int}``

    Unlike the pin detail page's relevance endpoint, a wiki vote does **not**
    materialize the item into a durable ``Image`` row - the wiki shows external
    media straight from the shared cache, and a community up-vote shouldn't
    silently spend the voter's storage quota. It only records the vote and
    returns the item's new net score so the grid can re-sort.
    """

    def post(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key

        location, _wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            data = json.loads(request.body or b"{}")
            source = str(data["source"])[:30]
            url = str(data.get("url") or "")
            is_relevant = data.get("is_relevant")
        except (KeyError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

        item_key = data.get("item_key") or media_item_key(url)
        if not item_key:
            return JsonResponse({"error": "Missing item identity."}, status=400)

        if is_relevant is None:
            MediaRelevance.objects.for_gallery(profile, location, source).filter(item_key=item_key).delete()
        else:
            MediaRelevance.objects.update_or_create(
                profile=profile,
                location=location,
                source=source,
                item_key=item_key,
                defaults={"is_relevant": bool(is_relevant)},
            )

        score = MediaRelevance.objects.vote_scores(location, source).get(item_key, 0)
        my_vote = None if is_relevant is None else bool(is_relevant)
        return JsonResponse({"my_vote": my_vote, "vote_score": score})
