"""Wiki Media gallery - community counterpart of the pin Media section."""

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

# How many shared wiki photos to render as votable Media tiles.
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
        from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, get_panel_source, panel_visible_to

        panel = get_panel_source(source)
        if not isinstance(panel, GalleryMediaSource):
            return HttpResponse(status=404)

        # Same gate the pin page's own generic panel dispatch applies - a feature-gated source's photos must not
        # leak through this separate wiki-media route, and LocationCache is shared with the pin page so a gated
        # panel's row is just as reachable here without this check.
        if not panel_visible_to(profile.user, panel):
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
        # Prefer an already-materialized local copy over hot-linking the provider - see the matching comment in
        # controllers.pin. Voting is wiki-side too, so this is the same lookup either flow benefits from.
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
                    # TIFFs, scanned PDFs and HEICs reach the gallery routinely and none of them render in an
                    # <img> - see services.media.previews.
                    "thumb_url": gallery_thumb_url(item.url, item.thumb_url, item.content_type),
                    # Only present once this item has a local copy - a vote on a still-transient item has no
                    # REData photo_id to attach to (see WikiMediaVoteView.post).
                    "image_id": local_image.pk if local_image else None,
                    # Ownership isn't meaningful for an external-provider result (explicitly None, not just
                    # absent - see the "photos" branch below and pin_media_items.html's data-mine, which needs
                    # to tell "no data" apart from "definitely not mine").
                    "is_mine": None,
                },
            )

        return render(request, "dashboard/partials/pins/pin_media_items.html", {"rendered_items": rendered_items, "source_key": source, "wiki_mode": True})

    def _photos(self, request: HttpRequest, location: Location, wiki: Wiki, profile: Profile) -> HttpResponse:
        """Render photos intentionally shared to this wiki as votable Media tiles."""
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        # Same rows the wiki gallery serves, so the same filter: `visible_to` answers "may this account see this
        # upload at all", which is a different question from "would a brand-new wiki have carried it".
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
                    "item": MediaItem(url=url, thumb_url=url, caption=img.caption or "", source="Photos", page_url=url, author=img.author or ""),
                    "key": key,
                    "is_relevant": my_marks.get(key),
                    "vote_score": scores.get(key, 0),
                    "image_id": img.pk,
                    "lat": img.latitude,
                    "lng": img.longitude,
                    # Drives the lightbox's "Copy to my Private Pin" action (isMine === false) and its "Copied
                    # from..." line - see pin_media_items.html and shared/media-lightbox.ts.
                    "is_mine": img.profile_id == profile.pk,
                    "copied_from_label": img.copied_from_label or "",
                    "_redata_confidence": img.redata_confidence,
                    "_created": img.created,
                },
            )
        if not rendered_items:
            return HttpResponse(status=204)

        # Most relevant first: the community's own net vote score takes priority; REData's cached confidence
        # (services.photos.redata_relevance) breaks ties among equally-voted photos - including the common case
        # of no votes at all, where every score is otherwise 0 - falling back to upload recency for a photo
        # REData hasn't scored yet.
        rendered_items.sort(key=lambda entry: (entry["vote_score"], entry["_redata_confidence"] if entry["_redata_confidence"] is not None else -1, entry["_created"]), reverse=True)

        return render(request, "dashboard/partials/pins/pin_media_items.html", {"rendered_items": rendered_items, "source_key": "photos", "wiki_mode": True})

    def _pending(self, request: HttpRequest, location: Location, profile: Profile, source: str, panel: GalleryMediaSource) -> HttpResponse:
        """Warm the provider's cache from the viewer's own pin, or give up quietly.

        A wiki viewer reaches the page because they have a pin at (or near) this location; we use *their*
        pin so the fetch is gated by their ``external_apis_enabled`` and counts against their quota.
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

    POST /location/<slug>/wiki/media/vote/ →

    A down-vote or a cleared vote never materializes anything - only an existing, already-materialized
    ``image_id`` supplied by the client (re-scoped to this wiki's own attached media) gets its REData
    signal reversed.
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
            # An explicit click overrides any prior vote.
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
            # image_id is only trusted after re-scoping to this wiki's own attached media - scoping to the
            # location alone let a caller record a vote against a pin-owned (not wiki-attached) photo at the
            # same location, which they have no business voting on.
            existing_image = Image.objects.filter(pk=image_id, wiki=wiki).first() if image_id else None
            if existing_image is not None:
                queue_relevance_vote(existing_image, profile, is_relevant=bool(is_relevant))
                if is_relevant:
                    refresh_community_quota_bonus(existing_image)

        score = MediaRelevance.objects.vote_scores(location, source).get(item_key, 0)
        my_vote = None if is_relevant is None else bool(is_relevant)
        response.update({"my_vote": my_vote, "vote_score": score})
        return JsonResponse(response)


class CopyWikiPhotoView(LoginRequiredMixin, View):
    """Copy a wiki photo onto the viewer's own pin at this location.

    POST /location/<slug>/wiki/media/copy-to-pin/<int:image_id>/ →

    Deliberately not a ``PhotoActionView`` action (``controllers.vault_photos``): that view's shared
    image lookup only ever operates on images the requester already owns, which a wiki photo being
    copied is not.
    The image is instead scoped and gated exactly like every other wiki-photo lookup in this module -
    ``resolve_visible_wiki`` for wiki access, ``visible_rows``/``visible_to`` for concealment and the
    uploader's own visibility setting - so this can never expose a photo the wiki's own gallery views
    wouldn't already show this viewer.
    """

    def post(self, request: HttpRequest, location_slug: str, image_id: int) -> JsonResponse:
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.services.photos.wiki_copy import copy_wiki_photo_to_pin
        from urbanlens.dashboard.services.wiki.concealment import visible_rows

        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        image = visible_rows(Image.objects.filter(pk=image_id, wiki=wiki), wiki, profile).visible_to(profile).first()
        if image is None:
            return JsonResponse({"error": "That photo could not be found."}, status=404)

        target_pin = location.pins.filter(profile=profile).first()
        if target_pin is None:
            return JsonResponse({"error": "You don't have a pin here to copy it to."}, status=400)

        _copy, created = copy_wiki_photo_to_pin(image, target_pin, profile)
        return JsonResponse(
            {
                "copied": True,
                "already_copied": not created,
                "pin_slug": target_pin.slug,
                "pin_name": target_pin.name,
            }
        )
