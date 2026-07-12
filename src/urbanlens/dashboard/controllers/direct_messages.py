"""Direct messages controllers - inbox page, conversation thread, navbar dropdown.

Live delivery is a WebSocket to ``DirectMessageConsumer`` (see
``dashboard/consumers.py``); the send endpoint here is the no-JS / socket-down
fallback, mirroring how the safety check-in chat splits responsibilities
between its consumer and ``SafetyCheckinMessageView``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.direct_messages import (
    REACTION_PICKER_EMOJIS,
    can_direct_message,
    clear_email_debounce,
    conversations_for,
    create_direct_message,
    delete_message_for_everyone,
    delete_message_for_self,
    display_identity_for,
    is_profile_online,
    is_safe_reaction_emoji,
    mark_thread_open,
    reaction_summary,
    toggle_reaction,
)
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

#: How many conversations the navbar dropdown shows.
DROPDOWN_CONVERSATION_LIMIT = 8

#: How many profiles the new-message recipient search returns.
RECIPIENT_SEARCH_LIMIT = 8


def _get_profile(request: HttpRequest) -> Profile:
    """Return (creating if needed) the requesting user's profile.

    Args:
        request: The authenticated request.

    Returns:
        The user's Profile.
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return profile


def _get_partner(profile: Profile, profile_slug: str) -> Profile:
    """Resolve a conversation partner by slug, hiding profiles that don't exist.

    Args:
        profile: The requesting profile (excluded - no self-conversations).
        profile_slug: The partner's URL slug.

    Returns:
        The partner Profile.

    Raises:
        Http404: When no such profile exists or it is the requester's own.
    """
    partner = get_object_or_404(Profile.objects.select_related("user"), slug=profile_slug)
    if partner.pk == profile.pk:
        raise Http404
    return partner


def _trigger_msg_badge_refresh(response: HttpResponse) -> HttpResponse:
    """Attach HTMX triggers so the navbar messages badge and sidebar conversation list refresh.

    Used both when opening a thread (marks it read - the sidebar's unread pill and
    last-message preview need to catch up) and on the plain-POST send fallback
    (the sidebar's last-message preview needs to catch up there too).

    Args:
        response: The response to annotate.

    Returns:
        The same response with an ``HX-Trigger`` header added.
    """
    response["HX-Trigger"] = json.dumps({"msgCountRefresh": {"target": "body"}, "dmListRefresh": {"target": "body"}})
    return response


def _thread_context(profile: Profile, partner: Profile) -> dict:
    """Build the template context for one conversation thread.

    Marks the partner's unread messages as read - rendering the thread is the
    act of reading it.

    Args:
        profile: The viewing profile.
        partner: The conversation partner.

    Returns:
        Context dict for ``_thread.html``.
    """
    DirectMessage.objects.between(profile, partner).filter(recipient=profile).mark_read()
    clear_email_debounce(partner.pk, profile.pk)
    mark_thread_open(profile.pk, partner.pk)
    thread_messages = (
        DirectMessage.objects.between(profile, partner)
        .select_related(
            "sender",
            "sender__user",
            "recipient",
            "recipient__user",
            "reply_to",
            "reply_to__sender",
            "share",
            "share__pin_share",
            "share__pin_share__pin",
            "share__trip",
            "share__trip_membership",
            "share__recommended_profile",
        )
        .prefetch_related("images", "reactions__profile")
    )
    identity = display_identity_for(profile, partner)
    partner_online = False
    if not identity["is_anonymized"] and Profile.visibility_permits(partner.online_status_visibility, partner, profile):
        partner_online = is_profile_online(partner)
    return {
        "partner": partner,
        "thread_messages": thread_messages,
        "can_message_partner": can_direct_message(profile, partner),
        "max_message_length": MAX_DIRECT_MESSAGE_LENGTH,
        "my_slug": profile.slug or "",
        "viewer_id": profile.pk,
        "reaction_picker_emojis": REACTION_PICKER_EMOJIS,
        "partner_online": partner_online,
        "image_permission_status": _image_permission_status(profile, partner),
        "partner_e2ee_enrolled": _e2ee_enrolled(partner),
        **identity,
    }


def _e2ee_enrolled(profile: Profile) -> bool:
    """Return True when `profile` has a direct-message encryption key bundle.

    Drives whether the composer encrypts: a message is only encrypted when
    both participants have published a key bundle.

    Args:
        profile: The profile to check.

    Returns:
        True when a ``MessagingKeyBundle`` exists for this profile.
    """
    from urbanlens.dashboard.models.e2ee import MessagingKeyBundle

    return MessagingKeyBundle.objects.filter(profile=profile).exists()


def _image_permission_status(viewer: Profile, sender: Profile) -> str:
    """Return `viewer`'s standing decision on images from `sender` ("pending" if none yet).

    Args:
        viewer: The profile who would be viewing the images.
        sender: The profile who sent them.

    Returns:
        An ``ImagePermissionStatus`` value.
    """
    from urbanlens.dashboard.models.direct_messages.image_permission import DirectMessageImagePermission
    from urbanlens.dashboard.models.direct_messages.meta import ImagePermissionStatus

    permission = DirectMessageImagePermission.objects.filter(viewer=viewer, sender=sender).first()
    return permission.status if permission else ImagePermissionStatus.PENDING


class MessagesPageView(LoginRequiredMixin, View):
    """GET /messages/ - the full messages page (conversation list, empty thread pane)."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the messages page.

        Args:
            request: The incoming request.

        Returns:
            The rendered messages page.
        """
        profile = _get_profile(request)
        return render(
            request,
            "dashboard/pages/messages/index.html",
            {
                "conversations": conversations_for(profile),
                "active_partner": None,
                "active_slug": "",
                "profile": profile,
            },
        )


class ConversationView(LoginRequiredMixin, View):
    """GET /messages/<profile_slug>/ - one conversation, as a full page or an HTMX pane swap."""

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Render the conversation with ``profile_slug``.

        HTMX requests get just the thread partial (the page swaps it into the
        right-hand pane); plain navigation gets the whole page with this
        conversation active.

        Args:
            request: The incoming request.
            profile_slug: Slug of the conversation partner.

        Returns:
            Thread partial or full page, with a badge-refresh trigger since
            opening a thread marks it read.

        Raises:
            Http404: When there is no existing conversation and the partner's
                privacy settings would reject a message - so conversation URLs
                don't confirm the existence of profiles that are hidden from
                this user.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        if not DirectMessage.objects.between(profile, partner).exists() and not can_direct_message(profile, partner):
            raise Http404

        if request.headers.get("HX-Request"):
            response = render(request, "dashboard/partials/messages/_thread.html", _thread_context(profile, partner))
            return _trigger_msg_badge_refresh(response)

        context = {
            **_thread_context(profile, partner),
            "conversations": conversations_for(profile),
            "active_partner": partner,
            "active_slug": partner.slug or "",
            "profile": profile,
        }
        return render(request, "dashboard/pages/messages/index.html", context)


class ConversationSendView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/send/ - fallback send when the WebSocket is unavailable."""

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Create a message and return the refreshed thread partial.

        Args:
            request: The incoming request. Reads ``body``.
            profile_slug: Slug of the recipient.

        Returns:
            The thread partial on success; a plain-text 400/403 on rejection -
            the page JS surfaces that text verbatim as a toast, mirroring the
            safety chat fallback contract.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        image_ids = [int(v) for v in request.POST.getlist("image_ids") if v.isdigit()]
        reply_to_raw = request.POST.get("reply_to", "")
        key_version_raw = request.POST.get("key_version", "")
        try:
            create_direct_message(
                profile,
                partner,
                request.POST.get("body", ""),
                ciphertext=request.POST.get("ciphertext", ""),
                nonce=request.POST.get("nonce", ""),
                key_version=int(key_version_raw) if key_version_raw.isdigit() else 0,
                image_ids=image_ids,
                markup_map_uuid=request.POST.get("markup_map_uuid") or None,
                reply_to_id=int(reply_to_raw) if reply_to_raw.isdigit() else None,
            )
        except ValueError as exc:
            # create_direct_message only raises ValueError with a fixed, developer-authored
            # message (blank/too-long body) - never a stack trace or sensitive data.
            return HttpResponseBadRequest(str(exc))  # lgtm[py/stack-trace-exposure]
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        response = render(request, "dashboard/partials/messages/_thread.html", _thread_context(profile, partner))
        return _trigger_msg_badge_refresh(response)


class DirectMessageImageUploadView(LoginRequiredMixin, View):
    """POST /messages/upload-image/ - upload one photo attachment ahead of sending.

    Mirrors ``PhotoUploadView``: creates an unattached ``Image`` (no
    ``direct_message`` yet) so the client can upload as soon as a file is
    picked. ``create_direct_message`` attaches it by id once the message is
    actually sent - an upload with no matching send just leaves a harmless
    unattached row, the same tradeoff other upload-then-attach flows make.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        """Create an unattached Image owned by the caller and queue metadata ingestion.

        Args:
            request: The HTTP request carrying an ``image`` file.

        Returns:
            JSON with the new image's ``id`` and ``url``, or a 400/413 error.
        """
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.services.images import compute_checksum
        from urbanlens.dashboard.services.storage import quota_error_for_upload

        profile = _get_profile(request)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)
        if not (image_file.content_type or "").startswith("image/"):
            return JsonResponse({"error": "That file is not an image."}, status=400)

        quota_error = quota_error_for_upload(profile, image_file.size)
        if quota_error:
            return JsonResponse({"error": quota_error}, status=413)

        checksum = compute_checksum(image_file)
        image = Image.objects.create(image=image_file, profile=profile, checksum=checksum, file_size=image_file.size)

        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, image.pk)
        return JsonResponse({"id": image.pk, "url": image.image.url}, status=201)


class MessageReactionToggleView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/react/<message_id>/ - toggle an emoji reaction.

    Args come from the POST body (``emoji``). Broadcasts the message's updated
    reaction summary to both participants over the WebSocket (see
    ``services.direct_messages.toggle_reaction``); the response itself is the
    re-rendered reaction-bar partial, used to update the acting client's own
    UI immediately without waiting on the WS round-trip.
    """

    def post(self, request: HttpRequest, profile_slug: str, message_id: int) -> HttpResponse:
        """Toggle the caller's reaction and return the refreshed reaction bar.

        Args:
            request: The incoming request. Reads ``emoji``.
            profile_slug: Slug of the conversation partner (scopes the lookup).
            message_id: PK of the message being reacted to.

        Returns:
            The rendered reaction-bar partial, or 400/403/404 on failure.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        message = get_object_or_404(DirectMessage.objects.between(profile, partner), pk=message_id)

        emoji = request.POST.get("emoji", "").strip()[:10]
        if not emoji:
            return HttpResponseBadRequest("An emoji is required.")
        if not is_safe_reaction_emoji(emoji):
            return HttpResponseBadRequest("That is not a valid reaction.")

        try:
            toggle_reaction(profile, message, emoji)
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))

        return render(
            request,
            "dashboard/partials/messages/_message_reactions.html",
            {
                "message": message,
                "reactions": reaction_summary(message),
                "my_slug": profile.slug or "",
                "partner": partner,
                "reaction_picker_emojis": REACTION_PICKER_EMOJIS,
            },
        )


class MessageDeleteView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/delete/<message_id>/ - delete or hide one message.

    ``scope=everyone`` (only valid for the message's sender) tombstones it for
    the recipient - the sender always keeps their own copy. ``scope=self``
    (only valid for the message's recipient) hides it from that recipient's
    own view only, leaving the sender's copy and any pending `@`-mention
    share completely untouched.
    """

    def post(self, request: HttpRequest, profile_slug: str, message_id: int) -> HttpResponse:
        """Apply the requested delete scope and return the refreshed thread.

        Args:
            request: The incoming request. Reads ``scope`` (``everyone`` or ``self``).
            profile_slug: Slug of the conversation partner.
            message_id: PK of the message to delete.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        message = get_object_or_404(DirectMessage.objects.between(profile, partner), pk=message_id)

        scope = request.POST.get("scope", "")
        try:
            if scope == "everyone":
                delete_message_for_everyone(message, profile)
            elif scope == "self":
                delete_message_for_self(message, profile)
            else:
                return HttpResponseBadRequest("Unknown delete scope.")
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))

        response = render(request, "dashboard/partials/messages/_thread.html", _thread_context(profile, partner))
        return _trigger_msg_badge_refresh(response)


class MessageImagePermissionView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/image-permission/ - respond to the image-consent prompt.

    ``decision=allow`` and ``decision=reject`` set a standing decision for
    every future image from that sender. ``decision=allow_once`` (with
    ``message_id``) reveals just that one message's images without changing
    the standing decision.
    """

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Apply the recipient's image-consent decision and return the refreshed thread.

        Args:
            request: The incoming request. Reads ``decision`` and, for
                ``allow_once``, ``message_id``.
            profile_slug: Slug of the conversation partner (the image sender).

        Returns:
            The re-rendered thread partial, or 400 for an unknown decision.
        """
        from urbanlens.dashboard.models.direct_messages.image_permission import DirectMessageImagePermission
        from urbanlens.dashboard.models.direct_messages.meta import ImagePermissionStatus

        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        decision = request.POST.get("decision", "")

        if decision == "allow":
            DirectMessageImagePermission.objects.update_or_create(viewer=profile, sender=partner, defaults={"status": ImagePermissionStatus.ALLOWED})
        elif decision == "reject":
            DirectMessageImagePermission.objects.update_or_create(viewer=profile, sender=partner, defaults={"status": ImagePermissionStatus.REJECTED})
        elif decision == "allow_once":
            message_id = request.POST.get("message_id", "")
            if message_id.isdigit():
                DirectMessage.objects.filter(pk=message_id, sender=partner, recipient=profile).update(images_revealed=True)
        else:
            return HttpResponseBadRequest("Unknown decision.")

        response = render(request, "dashboard/partials/messages/_thread.html", _thread_context(profile, partner))
        return _trigger_msg_badge_refresh(response)


class ConversationReadView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/read/ - mark the partner's messages as read.

    Called by the messages page when a live message arrives on the thread the
    user is already looking at, so the unread badge doesn't claim a message
    the user has plainly seen.
    """

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Mark all messages from the partner as read.

        Args:
            request: The incoming request.
            profile_slug: Slug of the conversation partner.

        Returns:
            An empty 204 with a badge-refresh trigger.
        """
        from django.http import HttpResponse as DjangoHttpResponse

        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        DirectMessage.objects.between(profile, partner).filter(recipient=profile).mark_read()
        clear_email_debounce(partner.pk, profile.pk)
        return _trigger_msg_badge_refresh(DjangoHttpResponse(status=204))


class ConversationListView(LoginRequiredMixin, View):
    """GET /messages/list/ - renders the conversation-list partial (HTMX pane refresh)."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the conversation list for the sidebar.

        Args:
            request: The incoming request.

        Returns:
            The conversation-list partial. ``active_slug`` comes from the
            query string so the refreshed list keeps highlighting the open
            conversation.
        """
        profile = _get_profile(request)
        return render(
            request,
            "dashboard/partials/messages/_conversation_list.html",
            {
                "conversations": conversations_for(profile),
                "active_slug": request.GET.get("active", ""),
            },
        )


class MessagesDropdownView(LoginRequiredMixin, View):
    """GET /messages/dropdown/ - renders the navbar messages dropdown partial."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the dropdown with the most recent conversations.

        Args:
            request: The incoming request.

        Returns:
            The dropdown partial.
        """
        profile = _get_profile(request)
        conversations = conversations_for(profile)[:DROPDOWN_CONVERSATION_LIMIT]
        return render(
            request,
            "dashboard/partials/messages/_dropdown.html",
            {"conversations": conversations},
        )


class MessagesUnreadCountView(LoginRequiredMixin, View):
    """GET /messages/unread-count/ - returns the navbar unread badge partial."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the unread badge.

        Args:
            request: The incoming request.

        Returns:
            The badge partial with the count of unread messages.
        """
        profile = _get_profile(request)
        count = DirectMessage.objects.unread_for(profile).count()
        return render(request, "dashboard/partials/messages/_badge.html", {"unread_count": count})


class RecipientSearchView(LoginRequiredMixin, View):
    """GET /messages/recipients/?q=... - search for profiles the user can message."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Return matching, messageable profiles for the new-message picker.

        Only profiles whose privacy settings permit a message from the
        requester are returned - the picker never offers someone who would
        reject the send.

        Args:
            request: The incoming request. Reads ``q``.

        Returns:
            The recipient search-results partial.
        """
        profile = _get_profile(request)
        query = request.GET.get("q", "").strip()
        results: list[Profile] = []
        if len(query) >= 2:
            candidates = (
                Profile.objects.select_related("user")
                .filter(Q(user__username__icontains=query) | Q(slug__icontains=query))
                .exclude(pk=profile.pk)
                .order_by("user__username")[: RECIPIENT_SEARCH_LIMIT * 4]
            )
            results = [candidate for candidate in candidates if can_direct_message(profile, candidate)][:RECIPIENT_SEARCH_LIMIT]
            for candidate in results:
                candidate.ensure_slug()
        return render(
            request,
            "dashboard/partials/messages/_recipient_results.html",
            {"results": results, "query": query},
        )
