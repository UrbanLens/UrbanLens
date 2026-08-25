"""Image gallery controller - upload, list, reposition, and delete photos."""

from __future__ import annotations

from decimal import Decimal
import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.images.attachment import ImageAttachment
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.pagination import get_page
from urbanlens.dashboard.services.media.images import delete_stored_file, detach_image_from_wiki, image_to_gallery_json, parse_reposition_payload
from urbanlens.dashboard.services.photos.uploads import UploadRejection, upload_photo_for_owner
from urbanlens.dashboard.services.wiki.concealment import visible_rows
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

_GALLERY_PAGE_SIZE = 12


def _wiki_for_location(location: Location | None) -> Wiki | None:
    """Return the community Wiki for a Location, or None when it has no wiki yet."""
    return Wiki.objects.get_for_location(location)


def create_uploaded_photo(request: HttpRequest, owner: Pin | Wiki, profile: Profile) -> tuple[Image | None, JsonResponse]:
    """Store the request's uploaded photo against *owner*.

    Shared by the pin gallery, the wiki gallery, and an album's upload button so
    all three answer with the same body and the same status codes - the client
    code that renders an uploaded tile is shared too, and would otherwise have
    to special-case whichever surface drifted.

    Args:
        request: The upload request; reads the ``image`` file and ``caption``.
        owner: The Pin or Wiki to attach the photo to.
        profile: The uploading profile.

    Returns:
        Tuple of (the created Image or None, the response to return). Callers
        that need the row - to file it somewhere else - take the first element
        rather than re-parsing the response body.
    """
    image_file = request.FILES.get("image")
    if not image_file:
        return None, JsonResponse({"error": "No image provided."}, status=400)

    result = upload_photo_for_owner(owner, profile, image_file, request.POST.get("caption", ""))
    if isinstance(result, UploadRejection):
        return None, JsonResponse({"error": result.message}, status=result.status)

    # Imported here rather than at module scope: dashboard.tasks pulls in the
    # service layer, which imports this module back.
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import process_image_upload

    safely_enqueue_task(process_image_upload, result.pk)
    return result, JsonResponse(image_to_gallery_json(result, request, profile), status=201)


def store_uploaded_photo(request: HttpRequest, owner: Pin | Wiki, profile: Profile) -> JsonResponse:
    """Store the request's uploaded photo against *owner* and return the gallery JSON.

    Args:
        request: The upload request; reads the ``image`` file and ``caption``.
        owner: The Pin or Wiki to attach the photo to.
        profile: The uploading profile.

    Returns:
        201 with ``image_to_gallery_json`` on success, or the rejection's own
        status with an ``error`` message.
    """
    _image, response = create_uploaded_photo(request, owner, profile)
    return response


# -- Pin gallery --------------------------------------------------------------


def _pin_gallery_images(request: HttpRequest, pin: Pin, profile: Profile):
    """Images for a pin's gallery, optionally including child-pin photos.

    With ``?children=1`` (the pin page's "show child pin details" toggle) photos
    uploaded to any descendant child pin are included too, so the parent's
    gallery shows the whole place.

    Args:
        request: Current request (read for the ``children`` flag).
        pin: The pin whose gallery is being rendered.
        profile: The requesting profile (visibility filtering).

    Returns:
        Tuple of (queryset, include_children flag).
    """
    # Child expansion is owner-only: the "show child pin details" toggle exists
    # on the owner's own pin page, and another user's child pins are theirs.
    include_children = request.GET.get("children") == "1" and pin.profile_id == profile.pk
    if include_children:
        subtree = Pin.objects.filter(pk=pin.pk).with_descendants()
        images = Image.objects.filter(pin__in=subtree).select_related("profile", "pin", "pin__location", "pin__location__wiki")
    else:
        images = Image.objects.filter(pin=pin).select_related("profile")
    return images.visible_to(profile), include_children


class PinGalleryView(LoginRequiredMixin, View):
    """HTML gallery panel for the pin detail page (loaded via HTMX)."""

    def _get_context(self, request: HttpRequest, pin_slug: str) -> dict:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images, include_children = _pin_gallery_images(request, pin, profile)
        page_obj = get_page(request, images.order_by("-created"), _GALLERY_PAGE_SIZE)
        return {
            "pin": pin,
            "images": page_obj.object_list,
            "page_obj": page_obj,
            "profile": profile,
            "context_type": "pin",
            "include_children": include_children,
            "extra_query": "children=1" if include_children else "",
            "photo_bulk_actions": [
                {"action": "delete", "icon": "delete", "label": "Delete"},
                {"action": "wiki", "icon": "public", "label": "Send to wiki"},
            ],
        }

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        ctx = self._get_context(request, pin_slug)
        return render(request, "dashboard/partials/pins/_photo_gallery.html", ctx)

    def post(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        """Upload an image to a pin. Rejects a file the uploader already has on this pin."""
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return store_uploaded_photo(request, pin, profile)


class PinGalleryJsonView(LoginRequiredMixin, View):
    """JSON endpoint for the pin photo map layer."""

    def get(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        images, include_children = _pin_gallery_images(request, pin, profile)
        data = []
        for img in images.with_coords():
            entry = image_to_gallery_json(img, request, profile)
            if include_children and img.pin_id is not None and img.pin_id != pin.pk and img.pin is not None:
                # Child-pin photos render read-only on the parent's map layer;
                # they are repositioned from their own pin's page.
                entry["child_pin_name"] = img.pin.effective_name
            data.append(entry)
        return JsonResponse({"images": data})


class PinGalleryBulkView(LoginRequiredMixin, View):
    """Bulk actions over a pin's own gallery photos: delete, or send to wiki.

    Backs the Photo gallery's multi-select floating toolbar. Only the
    profile's own uploads on this pin are eligible - selecting someone
    else's (child-pin, in the ``?children=1`` view) photo id is silently
    ignored rather than erroring, since the toolbar only ever offers these
    actions on the viewer's own tiles.
    """

    def post(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            data = json.loads(request.body)
            action = data["action"]
            image_ids = [int(i) for i in data.get("image_ids", [])]
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

        images = Image.objects.filter(pk__in=image_ids, pin=pin, profile=profile)

        if action == "delete":
            # Collect the stored file paths first, then delete the underlying
            # storage files (Django has no bulk API for that) followed by a
            # single bulk DB delete, instead of one DELETE per row.
            # Same reference rule as delete_stored_file: a shared photo's file
            # backs several rows, and the whole batch is going, so rows inside it
            # must not count as references.
            batch = list(images)
            batch_pks = [image.pk for image in batch]
            # A row also linked to a wiki (send_to_wiki below repoints rather than
            # copies) must be unlinked from the pin, not destroyed - see
            # detach_image_from_pin.
            to_destroy = [image for image in batch if image.wiki_id is None]
            to_unlink_ids = [image.pk for image in batch if image.wiki_id is not None]
            for image in to_destroy:
                delete_stored_file(image, also_deleting=batch_pks)
            Image.objects.filter(pk__in=[image.pk for image in to_destroy]).delete()
            if to_unlink_ids:
                Image.objects.filter(pk__in=to_unlink_ids).update(pin=None)
            # Row count, not file count - a row with no stored file (e.g. still
            # processing) still gets deleted and must still be counted, or the
            # response silently undercounts what the client asked it to delete.
            return JsonResponse({"deleted": len(to_destroy), "unlinked": len(to_unlink_ids)})

        if action == "send_to_wiki":
            wiki = _wiki_for_location(pin.location)
            if wiki is None:
                return JsonResponse({"error": "Create a community wiki for this location first."}, status=400)
            # The deliberate act. Uploading to a pin no longer puts a photo on the
            # location's wiki, so this is the only way one gets there - and it is
            # recorded as an attachment as well as an FK, because the attachment is
            # what says a person chose to contribute this.
            from urbanlens.dashboard.services.photos.attachment import attach_to_wiki

            sending = list(images.exclude(wiki=wiki))
            for image in sending:
                attach_to_wiki(image, wiki, added_by=profile)
            count = images.filter(pk__in=[image.pk for image in sending]).update(wiki=wiki)
            return JsonResponse({"updated": count})

        return JsonResponse({"error": "Unknown action."}, status=400)


class PinCoverPhotoView(LoginRequiredMixin, View):
    """Set or clear a pin's hero-banner cover photo."""

    def post(self, request: HttpRequest, pin_slug: str) -> JsonResponse:
        """Set (or, given a null ``image_id``, clear) the pin's cover photo.

        Args:
            request: Incoming HTTP request. Reads JSON body ``image_id`` -
                an int to set the cover photo, or null/absent to clear it.
            pin_slug: Slug of the pin to update; must belong to the requester.

        Returns:
            JSON ``{"cover_photo": null}`` when cleared, or
            ``{"cover_photo": <url>}`` with the new cover's image URL (the
            lightbox uses this to update the page live without a reload).

        Raises:
            Http404: The pin doesn't exist/belong to the requester, or the
                given image isn't eligible (not tied to this pin or its
                Location).
        """
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            data = json.loads(request.body)
            image_id = data.get("image_id")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid request data."}, status=400)

        if image_id is None:
            pin.cover_photo = None
            pin.save(update_fields=["cover_photo", "updated"])
            return JsonResponse({"cover_photo": None})

        # Any image tied to this pin's own gallery, or already associated
        # with its Location (e.g. a Media-gallery item materialized via
        # "send to wiki" or a prior cover-photo pick), is eligible - but only
        # among photos this viewer may see, since every pin upload stamps
        # Image.location and two users pinning the same place therefore share
        # a location id. `visible_to` is applied to the single-row queryset so
        # its per-uploader visibility pass stays scoped to that one uploader.
        image = get_object_or_404(Image.objects.filter(pk=image_id).visible_to(profile))
        if image.pin_id != pin.pk and image.location_id != pin.location_id:
            raise Http404
        pin.cover_photo = image
        pin.save(update_fields=["cover_photo", "updated"])
        return JsonResponse({"cover_photo": image.image.url if image.image else image.source_url})


class PinImageView(LoginRequiredMixin, View):
    """Reposition or delete a single image on a pin."""

    def _get_image(self, image_id: int, pin_slug: str) -> Image:
        return get_object_or_404(Image, pk=image_id, pin__slug=pin_slug)

    def post(self, request: HttpRequest, pin_slug: str, image_id: int) -> JsonResponse:
        """Update lat/lng when the user drags the photo marker on the map."""
        img = self._get_image(image_id, pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if img.profile != profile:
            raise Http404
        try:
            img.latitude, img.longitude = parse_reposition_payload(request.body)
        except ValueError as exc:
            logger.warning("Failed to update image %s on pin %s: %s", image_id, pin_slug, exc)
            return JsonResponse({"error": "Invalid request data."}, status=400)
        img.save(update_fields=["latitude", "longitude", "updated"])
        return JsonResponse({"latitude": float(img.latitude), "longitude": float(img.longitude)})

    def delete(self, request: HttpRequest, pin_slug: str, image_id: int) -> HttpResponse:
        """Take a photo off this pin, and off the wiki only if asked.

        Contributing a photo to a community wiki is a deliberate act, so undoing
        it has to be one too. This screen never mentions the wiki, and it used to
        drop the ``Image`` row outright - which withdrew the contribution
        silently. Now the wiki keeps the photo unless the owner says otherwise;
        silence means no.

        ``?from_wiki=1`` is that explicit answer, and it is honoured only for a
        photo the owner uploaded. One fetched from a URL was a public resource
        online before this app saw it, so there is no consent here to withdraw -
        removing it is something you do on the wiki itself.

        Args:
            request: Incoming request; ``from_wiki=1`` also withdraws it.
            pin_slug: Slug of the pin the photo is being removed from.
            image_id: Primary key of the photo.

        Returns:
            204, whichever branch ran.

        Raises:
            Http404: The photo does not belong to the requester.
        """
        img = self._get_image(image_id, pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if img.profile != profile:
            raise Http404
        withdrawing = request.GET.get("from_wiki") == "1" and img.source == ImageSource.UPLOAD
        if img.wiki_id is not None and not withdrawing:
            Image.objects.filter(pk=img.pk).update(pin=None)
            ImageAttachment.objects.filter(image=img, pin__slug=pin_slug).delete()
            return HttpResponse(status=204)

        delete_stored_file(img)
        img.delete()
        return HttpResponse(status=204)


# -- Location wiki gallery -----------------------------------------------------


class WikiGalleryView(LoginRequiredMixin, View):
    """HTML gallery panel for the wiki page."""

    def _get_context(self, request: HttpRequest, location_slug: str) -> dict:
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows, conceal_wiki, concealment_active

        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        images = Image.objects.filter(wiki=wiki).select_related("profile").visible_to(profile).order_by("-created")
        # A third gate, on top of the container and settings gates visible_to
        # already applies. Provider media stays - a fresh wiki carries it - and
        # uploads survive only from the viewer or a friend. Paginated *after*
        # filtering, or the page count reports the photos it is standing in
        # front of.
        if concealment_active(wiki, profile):
            images = conceal_rows(images, profile)
        page_obj = get_page(request, images, _GALLERY_PAGE_SIZE)
        return {
            "location": location,
            "wiki": conceal_wiki(wiki, profile),
            "images": page_obj.object_list,
            "page_obj": page_obj,
            "profile": profile,
            "context_type": "wiki",
        }

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        ctx = self._get_context(request, location_slug)
        return render(request, "dashboard/partials/pins/_photo_gallery.html", ctx)

    def post(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        """Upload an image to a location wiki. Rejects a file the uploader already has on this wiki."""
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        return store_uploaded_photo(request, wiki, profile)


class WikiGalleryJsonView(LoginRequiredMixin, View):
    """JSON endpoint for the wiki photo map layer."""

    def get(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows, concealment_active

        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        images = Image.objects.filter(wiki=wiki).select_related("profile").visible_to(profile).with_coords()
        # This layer plots photos at their capture coordinates, so an unfiltered
        # payload does not merely list other people's contributions - it maps
        # where they stood.
        if concealment_active(wiki, profile):
            images = conceal_rows(images, profile)
        data = [image_to_gallery_json(img, request, profile) for img in images]
        return JsonResponse({"images": data})


class WikiCoverPhotoView(LoginRequiredMixin, View):
    """Set or clear a wiki's hero-banner cover photo.

    Any profile with a pin at the wiki's location may set it - the wiki is
    community content editable by everyone who can see it (comments, markup,
    aliases share the same access rule; see ``resolve_visible_wiki``). Each
    viewer's own ``show_wiki_cover_photos`` preference (see the pin detail
    template) independently controls whether they see it.
    """

    def post(self, request: HttpRequest, location_slug: str) -> JsonResponse:
        from urbanlens.dashboard.services.wiki.concealment import writable_wiki

        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        try:
            data = json.loads(request.body)
            image_id = data.get("image_id")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid request data."}, status=400)

        # The row that gets saved must be the real one, not a concealed
        # projection of it - see concealment.writable_wiki.
        target = writable_wiki(wiki)

        if image_id is None:
            target.cover_photo = None
            target.save(update_fields=["cover_photo", "updated"])
            return JsonResponse({"cover_photo": None})

        # Scoped to the one row before `visible_to`, whose per-uploader pass
        # would otherwise walk every uploader on the site to answer about one image.
        image = get_object_or_404(Image.objects.filter(pk=image_id).visible_to(profile))
        # On the wiki, not merely at the place. Accepting any photo whose location
        # matched let one person publish another's: a pin photo carries the
        # location but no wiki, and being able to *see* somebody's photo - which a
        # neighbour with a pin at the same place generally can - is not permission
        # to put it on the front of a page everyone reads. The wiki cover is
        # rendered with no visibility gate of its own, so this is the gate.
        #
        # The pin twin (PinCoverPhotoView) keeps the wider rule deliberately: its
        # consequence is confined to the setter's own page.
        on_this_wiki = image.wiki_id == wiki.pk or ImageAttachment.objects.filter(image=image, wiki=wiki).exists()
        if not on_this_wiki:
            raise Http404
        target.cover_photo = image
        target.save(update_fields=["cover_photo", "updated"])
        return JsonResponse({"cover_photo": image.image.url if image.image else image.source_url})


class WikiImageView(LoginRequiredMixin, View):
    """Reposition or delete a single image on a wiki."""

    def _get_image(self, request: HttpRequest, image_id: int, location_slug: str) -> tuple[Image, Profile]:
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        return get_object_or_404(visible_rows(Image.objects.filter(wiki=wiki), wiki, profile), pk=image_id), profile

    def post(self, request: HttpRequest, location_slug: str, image_id: int) -> JsonResponse:
        img, profile = self._get_image(request, image_id, location_slug)
        if img.profile != profile:
            raise Http404
        try:
            img.latitude, img.longitude = parse_reposition_payload(request.body)
        except ValueError as exc:
            logger.warning("Failed to update image %s on location %s: %s", image_id, location_slug, exc)
            return JsonResponse({"error": "Invalid request data."}, status=400)
        img.save(update_fields=["latitude", "longitude", "updated"])
        return JsonResponse({"latitude": float(img.latitude), "longitude": float(img.longitude)})

    def delete(self, request: HttpRequest, location_slug: str, image_id: int) -> HttpResponse:
        img, profile = self._get_image(request, image_id, location_slug)
        if img.profile != profile:
            raise Http404
        detach_image_from_wiki(img)
        return HttpResponse(status=204)
