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
    all_conversations_for,
    build_thread_timeline,
    can_direct_message,
    clear_email_debounce,
    create_direct_message,
    delete_message_for_everyone,
    delete_message_for_self,
    display_identity_for,
    is_profile_online,
    is_safe_reaction_emoji,
    key_change_events_for,
    mark_thread_open,
    reaction_summary,
    search_direct_messages,
    thread_page,
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

#: Minimum characters before a message search runs at all, matching the
#: recipient search's threshold - short of that, results would be too broad
#: to be useful and would just add query load on every keystroke.
MESSAGE_SEARCH_MIN_QUERY_LENGTH = 2


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


def _trigger_msg_label_refresh(response: HttpResponse) -> HttpResponse:
    """Attach HTMX triggers so the navbar messages label and sidebar conversation list refresh.

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


def _visible_key_events(profile: Profile, partner: Profile, messages: list[DirectMessage]) -> list[dict]:
    """Restrict key-change notices to the time span actually covered by `messages`.

    ``key_change_events_for`` returns every key rotation across the whole
    conversation's history; a single paginated page only wants the ones that
    fall between its own oldest and newest loaded message, so a notice never
    renders detached from the messages it explains.

    Args:
        profile: One participant.
        partner: The other participant.
        messages: The currently loaded page of messages (any order).

    Returns:
        Key-change events whose timestamp falls within `messages`' span, or
        an empty list when `messages` is empty.
    """
    if not messages:
        return []
    created_times = [message.created for message in messages]
    start, end = min(created_times), max(created_times)
    return [event for event in key_change_events_for(profile, partner) if start <= event["created"] <= end]


def _thread_context(profile: Profile, partner: Profile) -> dict:
    """Build the template context for one conversation thread.

    Marks the partner's unread messages as read - rendering the thread is the
    act of reading it. Only the most recent page of history is loaded here;
    older messages are fetched on demand (see ``ConversationOlderMessagesView``).

    Args:
        profile: The viewing profile.
        partner: The conversation partner.

    Returns:
        Context dict for ``_thread.html``.
    """
    from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute

    clear_email_debounce(partner.pk, profile.pk)
    mark_thread_open(profile.pk, partner.pk)
    thread_messages, has_more_older = thread_page(profile, partner)
    # Mark read only AFTER loading the page: the loaded instances keep their
    # in-memory read_at=None, so a "delete as soon as read" message renders
    # its content on this first open instead of tombstoning the instant the
    # read mark lands (is_expired_for_recipient keys off read_at). The next
    # render sees the persisted read_at and tombstones it as intended.
    DirectMessage.objects.between(profile, partner).filter(recipient=profile).mark_read()
    timeline = build_thread_timeline(thread_messages, _visible_key_events(profile, partner, thread_messages))
    identity = display_identity_for(profile, partner)
    partner_online = False
    if not identity["is_anonymized"] and Profile.visibility_permits(partner.online_status_visibility, partner, profile):
        partner_online = is_profile_online(partner)
    return {
        "partner": partner,
        "thread_messages": thread_messages,
        "timeline": timeline,
        "can_message_partner": can_direct_message(profile, partner),
        "max_message_length": MAX_DIRECT_MESSAGE_LENGTH,
        "my_slug": profile.slug or "",
        "viewer_id": profile.pk,
        "reaction_picker_emojis": REACTION_PICKER_EMOJIS,
        "partner_online": partner_online,
        "image_permission_status": _image_permission_status(profile, partner),
        "partner_e2ee_enrolled": _e2ee_enrolled(partner),
        "has_more_older": has_more_older,
        "oldest_message_id": thread_messages[0].pk if thread_messages else None,
        "is_muted": DirectMessageMute.objects.for_pair(profile, partner).exists(),
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

    return MessagingKeyBundle.objects.for_profile(profile).exists()


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

    permission = DirectMessageImagePermission.objects.for_pair(viewer, sender).first()
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
                "conversations": all_conversations_for(profile),
                "active_partner": None,
                "active_slug": "",
                "profile": profile,
                "viewer_id": profile.pk,
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
            Thread partial or full page, with a label-refresh trigger since
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
            return _trigger_msg_label_refresh(response)

        context = {
            **_thread_context(profile, partner),
            "conversations": all_conversations_for(profile),
            "active_partner": partner,
            "active_slug": partner.slug or "",
            "profile": profile,
        }
        return render(request, "dashboard/pages/messages/index.html", context)


class ConversationMuteToggleView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/mute/ - toggle notification muting for one conversation.

    Only suppresses notifications (in-app + the delayed "new message" email) -
    the conversation, unread counts, and message delivery are unaffected.
    """

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute

        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        mute, created = DirectMessageMute.objects.get_or_create(viewer=profile, sender=partner)
        if not created:
            mute.delete()

        response = render(request, "dashboard/partials/messages/_thread.html", _thread_context(profile, partner))
        response["HX-Trigger"] = json.dumps({"dmListRefresh": {"target": "body"}})
        return response


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
        return _trigger_msg_label_refresh(response)


class ConversationOlderMessagesView(LoginRequiredMixin, View):
    """GET /messages/<profile_slug>/older/?before=<id> - one older page of a conversation.

    Powers infinite-scroll-up in the thread pane: the oldest bubble currently
    loaded carries a sentinel that HTMX fires once it's scrolled into view
    (see ``_thread.html`` / ``_thread_messages_page.html``), which lands here
    and returns the next page of history to prepend.
    """

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Return the page of messages immediately older than ``before``.

        Args:
            request: The incoming request. Reads ``before`` (a message pk).
            profile_slug: Slug of the conversation partner.

        Returns:
            The message-items partial for that page, or 400 if ``before`` is
            missing or not an integer.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        before_raw = request.GET.get("before", "")
        if not before_raw.isdigit():
            return HttpResponseBadRequest("A valid message id is required.")

        messages, has_more_older = thread_page(profile, partner, before_id=int(before_raw))
        timeline = build_thread_timeline(messages, _visible_key_events(profile, partner, messages))
        return render(
            request,
            "dashboard/partials/messages/_thread_messages_page.html",
            {
                "partner": partner,
                "timeline": timeline,
                "viewer_id": profile.pk,
                "my_slug": profile.slug or "",
                "reaction_picker_emojis": REACTION_PICKER_EMOJIS,
                "image_permission_status": _image_permission_status(profile, partner),
                "has_more_older": has_more_older,
                "oldest_message_id": messages[0].pk if messages else None,
            },
        )


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
        from urbanlens.dashboard.models.images.model import Image, MediaKind
        from urbanlens.dashboard.services.images import compute_checksum, image_upload_error
        from urbanlens.dashboard.services.storage import quota_error_for_upload

        profile = _get_profile(request)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)
        if not (image_file.content_type or "").startswith("image/"):
            return JsonResponse({"error": "That file is not an image."}, status=400)

        upload_error = image_upload_error(image_file, MediaKind.PHOTO)
        if upload_error:
            message, status = upload_error
            return JsonResponse({"error": message}, status=status)

        quota_error = quota_error_for_upload(profile, image_file.size)
        if quota_error:
            return JsonResponse({"error": quota_error}, status=413)

        checksum = compute_checksum(image_file)
        image = Image.objects.create(image=image_file, profile=profile, checksum=checksum, file_size=image_file.size)

        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, image.pk)
        return JsonResponse({"id": image.pk, "url": image.image.url}, status=201)


class DirectMessageMapPickerView(LoginRequiredMixin, View):
    """GET /messages/attach-map/picker/ - list the caller's own maps to attach.

    Companion to the draw-a-new-map composer flow (``#dm-attach-map-btn``):
    lets the sender attach one of their existing maps instead - the only way
    a previously-cloned ("Add to my maps") map can be forwarded on, since
    ``create_direct_message`` only accepts maps the sender already owns.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the picker list of the caller's own MarkupMaps.

        Args:
            request: The HTTP request, optionally carrying a ``q`` search term.

        Returns:
            Rendered HTML fragment listing matching maps.
        """
        from urbanlens.dashboard.models.markup.model import MarkupMap

        profile = _get_profile(request)
        query = (request.GET.get("q") or "").strip()
        candidates = MarkupMap.objects.for_profile(profile).select_related("shared_by__user").order_by("-updated")
        if query:
            candidates = candidates.filter(title__icontains=query)
        return render(request, "dashboard/partials/messages/_map_picker.html", {"candidates": candidates[:50], "query": query})


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
        return _trigger_msg_label_refresh(response)


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
        return _trigger_msg_label_refresh(response)


class ConversationReadView(LoginRequiredMixin, View):
    """POST /messages/<profile_slug>/read/ - mark the partner's messages as read.

    Called by the messages page when a live message arrives on the thread the
    user is already looking at, so the unread label doesn't claim a message
    the user has plainly seen.
    """

    def post(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Mark all messages from the partner as read.

        Args:
            request: The incoming request.
            profile_slug: Slug of the conversation partner.

        Returns:
            An empty 204 with a label-refresh trigger.
        """
        from django.http import HttpResponse as DjangoHttpResponse

        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        DirectMessage.objects.between(profile, partner).filter(recipient=profile).mark_read()
        clear_email_debounce(partner.pk, profile.pk)
        return _trigger_msg_label_refresh(DjangoHttpResponse(status=204))


class ConversationSearchView(LoginRequiredMixin, View):
    """GET /messages/<profile_slug>/search/?q=... - search within one conversation.

    Understands the same natural-language phrasing as global search (dates,
    "photos"/"maps"/"pins" keywords, "from <person>") via
    ``services.direct_messages.search_direct_messages`` - the same query
    parser and queryset builder the Ctrl+K dialog's message search uses, so
    behavior never drifts between the two surfaces.
    """

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        """Return message hits within this conversation matching ``q``.

        Args:
            request: The incoming request. Reads ``q``.
            profile_slug: Slug of the conversation partner.

        Returns:
            The message-search-results partial, scoped to this conversation.
        """
        profile = _get_profile(request)
        partner = _get_partner(profile, profile_slug)
        query = request.GET.get("q", "").strip()
        hits = search_direct_messages(profile, query, partner=partner) if len(query) >= MESSAGE_SEARCH_MIN_QUERY_LENGTH else []
        return render(
            request,
            "dashboard/partials/messages/_message_search_results.html",
            {"hits": hits, "query": query, "scope": "conversation"},
        )


class MessagesSearchView(LoginRequiredMixin, View):
    """GET /messages/search/?q=... - search across every conversation.

    Companion to :class:`ConversationSearchView`: same query parsing and
    underlying queryset, just unscoped to a single partner.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Return message hits across all of the profile's conversations matching ``q``.

        Args:
            request: The incoming request. Reads ``q``.

        Returns:
            The message-search-results partial, grouped by conversation.
        """
        profile = _get_profile(request)
        query = request.GET.get("q", "").strip()
        hits = search_direct_messages(profile, query) if len(query) >= MESSAGE_SEARCH_MIN_QUERY_LENGTH else []
        return render(
            request,
            "dashboard/partials/messages/_message_search_results.html",
            {"hits": hits, "query": query, "scope": "all"},
        )


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
                "conversations": all_conversations_for(profile),
                "active_slug": request.GET.get("active", ""),
                "active_group_uuid": request.GET.get("active_group", ""),
                "viewer_id": profile.pk,
            },
        )


class MessagesDropdownView(LoginRequiredMixin, View):
    """GET /messages/dropdown/ - renders the navbar messages dropdown partial.

    The dropdown behaves like a notification tray: it lists only conversations
    with unread messages. Once a thread is read its row disappears from here
    (the panel re-fetches on every open); the full history lives on the
    messages page.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the dropdown with the most recently active unread conversations.

        Args:
            request: The incoming request.

        Returns:
            The dropdown partial. ``has_conversations`` distinguishes the
            "all caught up" empty state (some DM history, nothing unread)
            from "no messages yet" (no DM history at all).
        """
        profile = _get_profile(request)
        conversations = all_conversations_for(profile)
        unread = [conv for conv in conversations if conv["unread_count"]][:DROPDOWN_CONVERSATION_LIMIT]
        return render(
            request,
            "dashboard/partials/messages/_dropdown.html",
            {"conversations": unread, "has_conversations": bool(conversations)},
        )


class MessagesUnreadCountView(LoginRequiredMixin, View):
    """GET /messages/unread-count/ - returns the navbar unread label partial."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the unread label.

        Args:
            request: The incoming request.

        Returns:
            The label partial with the count of conversations that have at
            least one unread message (not the total unread message count -
            one label per conversation needing attention).
        """
        from urbanlens.dashboard.services.group_chats import unread_group_conversation_count

        profile = _get_profile(request)
        count = DirectMessage.objects.unread_conversation_count(profile) + unread_group_conversation_count(profile)
        return render(request, "dashboard/partials/messages/_label.html", {"unread_count": count})


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
            candidates = Profile.objects.select_related("user").filter(Q(user__username__icontains=query) | Q(slug__icontains=query)).exclude(pk=profile.pk).order_by("user__username")[: RECIPIENT_SEARCH_LIMIT * 4]
            results = [candidate for candidate in candidates if can_direct_message(profile, candidate)][:RECIPIENT_SEARCH_LIMIT]
            for candidate in results:
                candidate.ensure_slug()
        return render(
            request,
            "dashboard/partials/messages/_recipient_results.html",
            {"results": results, "query": query},
        )
