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
  schema change needed for the score itself (see
  ``MediaRelevanceQuerySet.vote_scores``).
* **An up-vote also submits the item to the wiki.** Unlike the pin detail
  page's "mark relevant" (which materializes onto the *pin*, private by
  default), a wiki up-vote is cast on the wiki's own page, about an item the
  voter is already looking at *as* a candidate wiki photo - so it's treated
  as the same deliberate sharing action as the pin page's separate "Send to
  wiki" button, not merely an opinion. See ``WikiMediaVoteView.post`` for why
  this is the only thing that makes an externally-sourced photo eligible for
  ``services.spotguessr.photos`` (its ``wiki__isnull=False`` gate) at all.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

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
        from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, get_panel_source

        panel = get_panel_source(source)
        if not isinstance(panel, GalleryMediaSource):
            return HttpResponse(status=404)

        cached = LocationCache.get_fresh(location, panel.cache_source)
        # A row whose media half was never filled in is not an answer for this
        # gallery, even though it is one for the info panel sharing the row.
        if cached is None or not panel.media_is_ready(cached.data or {}):
            return self._pending(request, location, profile, source, panel)
        items = panel.media_items(cached.data or {})

        from urbanlens.dashboard.services.media.media_relevance import local_images_for_gallery_items
        from urbanlens.dashboard.services.media.previews import gallery_thumb_url

        scores = MediaRelevance.objects.vote_scores(location, source)
        my_marks = dict(MediaRelevance.objects.for_gallery(profile, location, source).values_list("item_key", "is_relevant"))
        # Prefer an already-materialized local copy over hot-linking the
        # provider - see the matching comment in controllers.pin. Voting is
        # wiki-side too, so this is the same lookup either flow benefits from.
        local_images = local_images_for_gallery_items(location, source, [item.url for item in items])
        rendered_items = []
        for item in items:
            key = media_item_key(item.url)
            local_image = local_images.get(item.url)
            rendered_items.append(
                {
                    "item": item,
                    "key": key,
                    "is_relevant": my_marks.get(key),
                    "vote_score": scores.get(key, 0),
                    "local_url": local_image.image.url if local_image else None,
                    # TIFFs, scanned PDFs and HEICs reach the gallery routinely
                    # and none of them render in an <img> - see
                    # services.media.previews.
                    "thumb_url": gallery_thumb_url(item.url, item.thumb_url, item.content_type),
                    # Only present once this item has a local copy - a vote on
                    # a still-transient item has no REData photo_id to
                    # attach to (see WikiMediaVoteView.post).
                    "image_id": local_image.pk if local_image else None,
                },
            )

        return render(request, "dashboard/partials/pins/pin_media_items.html", {"rendered_items": rendered_items, "source_key": source, "wiki_mode": True})

    def _photos(self, request: HttpRequest, location: Location, wiki: Wiki, profile: Profile) -> HttpResponse:
        """Render photos intentionally shared to this wiki as votable Media tiles."""
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        # Same rows the wiki gallery serves, so the same filter: `visible_to`
        # answers "may this account see this upload at all", which is a
        # different question from "would a brand-new wiki have carried it".
        # Without the second one this tab hands back the uploads the gallery
        # conceals, on the same page load.
        from urbanlens.dashboard.services.wiki.concealment import visible_rows

        images = visible_rows(Image.objects.filter(wiki=wiki), wiki, profile).select_related("profile").visible_to(profile).exclude(image="").order_by("-created")[:_WIKI_PHOTOS_PREVIEW_LIMIT]

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
                    "_redata_confidence": img.redata_confidence,
                    "_created": img.created,
                },
            )
        if not rendered_items:
            return HttpResponse(status=204)

        # Most relevant first: the community's own net vote score takes
        # priority; REData's cached confidence (services.photos.redata_relevance)
        # breaks ties among equally-voted photos - including the common case
        # of no votes at all, where every score is otherwise 0 - falling back
        # to upload recency for a photo REData hasn't scored yet.
        rendered_items.sort(key=lambda entry: (entry["vote_score"], entry["_redata_confidence"] if entry["_redata_confidence"] is not None else -1, entry["_created"]), reverse=True)

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
        from urbanlens.dashboard.services.pins.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, schedule_panel_fetch

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

    POST /location/<slug>/wiki/media/vote/  →
    ``{"my_vote": bool|null, "vote_score": int, "image_id"?: int, "image_url"?: str, "materialize_error"?: str}``

    An up-vote (``is_relevant: true``) on an externally-sourced item (any
    ``source`` other than ``"photos"``, which already lists real ``Image``
    rows attached to this wiki - see ``WikiMediaProviderView._photos``) also
    materializes it and attaches it to this wiki, exactly like the pin
    detail page's "Send to wiki" bulk action - see the module docstring's
    "An up-vote also submits the item to the wiki" bullet for why voting
    here is treated as that same deliberate sharing action. This costs the
    *voter's* storage quota (``materialize_media_item`` downloads and saves
    it), same as any other materialize call. A failed download still keeps
    the vote (the voter's opinion is worth keeping even if today's download
    attempt failed) but is reported back via ``materialize_error`` so the
    frontend can toast it. A down-vote or a cleared vote never materializes
    anything - only an existing, already-materialized ``image_id`` supplied
    by the client (re-scoped to this location) gets its REData signal
    reversed.
    """

    def post(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
        from urbanlens.dashboard.services.media.media_relevance import record_relevant_and_cache
        from urbanlens.dashboard.services.media.quota_rewards import refresh_community_quota_bonus
        from urbanlens.dashboard.services.photos.redata_relevance import queue_relevance_vote

        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            data = json.loads(request.body or b"{}")
            source = str(data["source"])[:30]
            url = str(data.get("url") or "")
            is_relevant = data.get("is_relevant")
            image_id = data.get("image_id")
            page_url = str(data.get("page_url") or "")
            caption = str(data.get("caption") or "")
        except (KeyError, ValueError, TypeError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

        item_key = data.get("item_key") or media_item_key(url)
        if not item_key:
            return JsonResponse({"error": "Missing item identity."}, status=400)

        response: dict = {}
        if is_relevant is None:
            MediaRelevance.objects.for_gallery(profile, location, source).filter(item_key=item_key).delete()
        elif is_relevant and source != "photos" and url:
            # An explicit click overrides any prior vote. The "photos" panel
            # lists photos already attached to this wiki, so it falls to the
            # branch below, which reuses the client's own image_id instead of
            # re-downloading a local file.
            result = record_relevant_and_cache(
                location=location,
                profile=profile,
                source=source,
                url=url,
                page_url=page_url,
                caption=caption,
                wiki=wiki,
                item_key=item_key,
            )
            if result.error:
                response["materialize_error"] = result.error
            elif result.image is not None:
                response["image_id"] = result.image.pk
                response["image_url"] = result.image.image.url
        else:
            MediaRelevance.objects.update_or_create(
                profile=profile,
                location=location,
                source=source,
                item_key=item_key,
                defaults={"is_relevant": bool(is_relevant)},
            )
            # image_id is only trusted after re-scoping to this location - a
            # client sending an id from elsewhere must not be able to attach
            # a vote to an unrelated photo.
            existing_image = Image.objects.filter(pk=image_id, location=location).first() if image_id else None
            if existing_image is not None:
                queue_relevance_vote(existing_image, profile, is_relevant=bool(is_relevant))
                if is_relevant:
                    refresh_community_quota_bonus(existing_image)

        score = MediaRelevance.objects.vote_scores(location, source).get(item_key, 0)
        my_vote = None if is_relevant is None else bool(is_relevant)
        response.update({"my_vote": my_vote, "vote_score": score})
        return JsonResponse(response)
