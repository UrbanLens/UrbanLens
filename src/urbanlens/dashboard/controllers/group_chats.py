"""Group chat controllers - creation, thread view, membership management, shares.

Sits beside ``controllers.direct_messages`` on the messages page: group
threads swap into the same ``#dm-thread-pane``, live delivery rides the same
per-profile WebSocket (see ``services.group_chats``), and the send endpoint
here is the no-JS / socket-down fallback.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.controllers.direct_messages import _get_profile
from urbanlens.dashboard.models.group_chats.model import MAX_GROUP_NAME_LENGTH, GroupChat, GroupMessage
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.direct_messages import can_direct_message
from urbanlens.dashboard.services.group_chats import (
    create_group_chat,
    create_group_message,
    delete_group_message,
    group_e2ee_ready,
    group_thread_page,
    mark_group_thread_open,
    remove_group_member,
    rename_group_chat,
    share_pin_in_group_message,
)
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

    from urbanlens.dashboard.models.group_chats.model import GroupChatMembership

logger = logging.getLogger(__name__)

#: How many profiles the add-member search returns.
MEMBER_SEARCH_LIMIT = 8


def _get_group(profile: Profile, group_uuid) -> tuple[GroupChat, GroupChatMembership]:
    """Resolve a group the profile is an active member of, or 404.

    Args:
        profile: The requesting profile.
        group_uuid: UUID of the group chat.

    Returns:
        ``(group, membership)``.

    Raises:
        Http404: When the group doesn't exist or the profile isn't an active
            member - the two cases are indistinguishable by design.
    """
    group = GroupChat.objects.filter(uuid=group_uuid).first()
    if group is None:
        raise Http404
    membership = group.membership_for(profile)
    if membership is None:
        raise Http404
    return group, membership


def _trigger_msg_label_refresh(response: HttpResponse) -> HttpResponse:
    """Attach HTMX triggers so the navbar label and sidebar list refresh.

    Args:
        response: The response to annotate.

    Returns:
        The same response with an ``HX-Trigger`` header added.
    """
    response["HX-Trigger"] = json.dumps({"msgCountRefresh": {"target": "body"}, "dmListRefresh": {"target": "body"}})
    return response


def _group_thread_context(profile: Profile, group: GroupChat, membership: GroupChatMembership) -> dict:
    """Build the template context for one group thread.

    Marks the thread read - rendering it is the act of reading it.

    Args:
        profile: The viewing profile.
        group: The group chat.
        membership: The viewer's active membership.

    Returns:
        Context dict for ``_group_thread.html``.
    """
    from urbanlens.dashboard.services.identity_visibility import resolve_visible_identities

    GroupMessage.objects.mark_read(membership)
    mark_group_thread_open(profile.pk, group.pk)
    thread_messages, has_more_older = group_thread_page(membership)
    members = [row.profile for row in group.active_memberships().select_related("profile", "profile__user").order_by("created")]

    # A group can include people who aren't friends with everyone else in it,
    # whose privacy settings may not permit some viewers to see their name/
    # avatar - the message content itself still shows (this is "who sent it",
    # not "what they said"), same as any other shared space. Resolved once
    # per distinct sender (not per message) and attached for the template.
    distinct_senders = {message.sender_id: message.sender for message in thread_messages if message.sender_id}
    sender_identities = resolve_visible_identities(profile, list(distinct_senders.values()))
    for message in thread_messages:
        if message.sender_id:
            message.sender_identity = sender_identities.get(message.sender_id)

    return {
        "group": group,
        "membership": membership,
        "thread_messages": thread_messages,
        "members": members,
        "member_count": len(members),
        "is_manager": group.is_manager(profile),
        "max_message_length": MAX_DIRECT_MESSAGE_LENGTH,
        "max_group_name_length": MAX_GROUP_NAME_LENGTH,
        "my_slug": profile.slug or "",
        "viewer_id": profile.pk,
        "group_e2ee_ready": group_e2ee_ready(group),
        "has_more_older": has_more_older,
        "oldest_message_id": thread_messages[0].pk if thread_messages else None,
        "is_muted": membership.muted,
    }


class GroupCreateView(LoginRequiredMixin, View):
    """POST /messages/groups/create/ - create a group chat from the sidebar dialog."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Create a group with the caller plus the picked members.

        Args:
            request: The incoming request. Reads ``name`` and repeated
                ``member_slugs`` values.

        Returns:
            JSON ``{uuid, url}`` on success; 400/403 with a plain-text error
            the page JS shows as a toast.
        """
        profile = _get_profile(request)
        name = request.POST.get("name", "")
        slugs = [slug for slug in request.POST.getlist("member_slugs") if slug]
        members = list(Profile.objects.select_related("user").filter(slug__in=slugs))
        try:
            group = create_group_chat(profile, name, members)
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        return JsonResponse({"uuid": str(group.uuid), "url": reverse("messages.group", kwargs={"group_uuid": group.uuid})}, status=201)


class GroupConversationView(LoginRequiredMixin, View):
    """GET /messages/g/<uuid>/ - one group thread, as a full page or an HTMX pane swap."""

    def get(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Render the group conversation.

        Args:
            request: The incoming request.
            group_uuid: UUID of the group chat.

        Returns:
            Thread partial for HTMX requests; the whole messages page with
            this group active otherwise.
        """
        from urbanlens.dashboard.services.direct_messages import all_conversations_for

        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)

        if request.headers.get("HX-Request"):
            response = render(request, "dashboard/partials/messages/_group_thread.html", _group_thread_context(profile, group, membership))
            return _trigger_msg_label_refresh(response)

        context = {
            **_group_thread_context(profile, group, membership),
            # all_conversations_for (not the 1:1-only conversations_for): the
            # sidebar on a directly-loaded group-thread URL must show every
            # conversation, groups included - not just 1:1 threads.
            "conversations": all_conversations_for(profile),
            "active_partner": None,
            "active_slug": "",
            "active_group_uuid": str(group.uuid),
            "profile": profile,
        }
        return render(request, "dashboard/pages/messages/index.html", context)


class GroupSendView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/send/ - fallback send when the WebSocket is unavailable."""

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Create a message and return the refreshed thread partial.

        Args:
            request: The incoming request. Reads ``body`` (or the encrypted
                ``ciphertext``/``nonce``/``key_version`` triple).
            group_uuid: UUID of the group chat.

        Returns:
            The thread partial on success; a plain-text 400/403 the page JS
            surfaces as a toast.
        """
        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)
        key_version_raw = request.POST.get("key_version", "")
        try:
            create_group_message(
                profile,
                group,
                request.POST.get("body", ""),
                ciphertext=request.POST.get("ciphertext", ""),
                nonce=request.POST.get("nonce", ""),
                key_version=int(key_version_raw) if key_version_raw.isdigit() else 0,
            )
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))  # lgtm[py/stack-trace-exposure]
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        response = render(request, "dashboard/partials/messages/_group_thread.html", _group_thread_context(profile, group, membership))
        return _trigger_msg_label_refresh(response)


class GroupOlderMessagesView(LoginRequiredMixin, View):
    """GET /messages/g/<uuid>/older/?before=<id> - one older page of a group thread."""

    def get(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Return the page of messages immediately older than ``before``.

        Args:
            request: The incoming request. Reads ``before`` (a message pk).
            group_uuid: UUID of the group chat.

        Returns:
            The message-items partial for that page, or 400 for a bad cursor.
        """
        from urbanlens.dashboard.services.identity_visibility import resolve_visible_identities

        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)
        before_raw = request.GET.get("before", "")
        if not before_raw.isdigit():
            return HttpResponseBadRequest("A valid message id is required.")
        messages, has_more_older = group_thread_page(membership, before_id=int(before_raw))

        # See _group_thread_context's identical comment - masks sender name/
        # avatar per the sender's own privacy settings toward this viewer.
        distinct_senders = {message.sender_id: message.sender for message in messages if message.sender_id}
        sender_identities = resolve_visible_identities(profile, list(distinct_senders.values()))
        for message in messages:
            if message.sender_id:
                message.sender_identity = sender_identities.get(message.sender_id)

        return render(
            request,
            "dashboard/partials/messages/_group_thread_messages_page.html",
            {
                "group": group,
                "thread_messages": messages,
                "viewer_id": profile.pk,
                "my_slug": profile.slug or "",
                "has_more_older": has_more_older,
                "oldest_message_id": messages[0].pk if messages else None,
            },
        )


class GroupReadView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/read/ - mark the group's messages as read."""

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Advance the caller's read mark for this group.

        Args:
            request: The incoming request.
            group_uuid: UUID of the group chat.

        Returns:
            An empty 204 with a label-refresh trigger.
        """
        from django.http import HttpResponse as DjangoHttpResponse

        profile = _get_profile(request)
        _group, membership = _get_group(profile, group_uuid)
        GroupMessage.objects.mark_read(membership)
        return _trigger_msg_label_refresh(DjangoHttpResponse(status=204))


class GroupRenameView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/rename/ - rename the group (any active member)."""

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Rename the group and return the refreshed thread.

        Args:
            request: The incoming request. Reads ``name``.
            group_uuid: UUID of the group chat.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)
        try:
            rename_group_chat(group, profile, request.POST.get("name", ""))
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        response = render(request, "dashboard/partials/messages/_group_thread.html", _group_thread_context(profile, group, membership))
        return _trigger_msg_label_refresh(response)


class GroupMuteToggleView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/mute/ - toggle notification muting for this group."""

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Flip the caller's mute flag and return the refreshed thread.

        Args:
            request: The incoming request.
            group_uuid: UUID of the group chat.

        Returns:
            The re-rendered thread partial.
        """
        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)
        membership.muted = not membership.muted
        membership.save(update_fields=["muted", "updated"])
        response = render(request, "dashboard/partials/messages/_group_thread.html", _group_thread_context(profile, group, membership))
        response["HX-Trigger"] = json.dumps({"dmListRefresh": {"target": "body"}})
        return response


class GroupMembersDialogView(LoginRequiredMixin, View):
    """GET /messages/g/<uuid>/members/ - the members-management dialog partial."""

    def get(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Render the members dialog.

        Args:
            request: The incoming request.
            group_uuid: UUID of the group chat.

        Returns:
            The rendered dialog partial.
        """
        from urbanlens.dashboard.services.identity_visibility import resolve_visible_identities

        profile = _get_profile(request)
        group, _membership = _get_group(profile, group_uuid)
        memberships = list(group.active_memberships().select_related("profile", "profile__user").order_by("created"))
        # Resolves each member's display name/avatar per their own privacy
        # settings toward the viewer (a member added by someone else may not
        # be friends with everyone here) and gives every member - masked or
        # not - a distinct fallback-avatar color, so two members sharing the
        # same default color/placeholder aren't indistinguishable apart from
        # an initial letter.
        identities = resolve_visible_identities(profile, [m.profile for m in memberships])
        return render(
            request,
            "dashboard/partials/messages/_group_members_dialog.html",
            {
                "group": group,
                "memberships": memberships,
                "identities": identities,
                "is_manager": group.is_manager(profile),
                "viewer_id": profile.pk,
            },
        )


class GroupAddMembersView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/members/add/ - add members (creator only)."""

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Add the posted members and return the refreshed thread.

        Args:
            request: The incoming request. Reads repeated ``member_slugs``.
            group_uuid: UUID of the group chat.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        from urbanlens.dashboard.services.group_chats import add_group_members

        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)
        slugs = [slug for slug in request.POST.getlist("member_slugs") if slug]
        members = list(Profile.objects.select_related("user").filter(slug__in=slugs))
        if not members:
            return HttpResponseBadRequest("Pick at least one person to add.")
        try:
            add_group_members(group, profile, members)
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        response = render(request, "dashboard/partials/messages/_group_thread.html", _group_thread_context(profile, group, membership))
        return _trigger_msg_label_refresh(response)


class GroupRemoveMemberView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/members/remove/ - remove a member (creator only)."""

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Remove the posted member and return the refreshed thread.

        Args:
            request: The incoming request. Reads ``profile_id``.
            group_uuid: UUID of the group chat.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)
        # Looked up by numeric id, not slug: the member being removed may have
        # a masked identity toward the requester (see resolve_visible_identities
        # in GroupMembersDialogView.get), and a profile's slug is derived from
        # their username (see Profile._slugify_base) - putting it in the DOM/
        # request body would leak the very identity the mask is hiding.
        profile_id_raw = request.POST.get("profile_id", "")
        if not profile_id_raw.isdigit():
            return HttpResponseBadRequest("A valid profile_id is required.")
        target = get_object_or_404(Profile.objects.select_related("user"), pk=profile_id_raw)
        try:
            remove_group_member(group, profile, target)
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        response = render(request, "dashboard/partials/messages/_group_thread.html", _group_thread_context(profile, group, membership))
        return _trigger_msg_label_refresh(response)


class GroupLeaveView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/leave/ - leave the group (any member, including the creator)."""

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """End the caller's membership.

        Args:
            request: The incoming request.
            group_uuid: UUID of the group chat.

        Returns:
            An empty 200 with an ``HX-Redirect`` back to the messages page.
        """
        from django.http import HttpResponse as DjangoHttpResponse

        profile = _get_profile(request)
        group, _membership = _get_group(profile, group_uuid)
        remove_group_member(group, profile, profile)
        response = DjangoHttpResponse(status=200)
        response["HX-Redirect"] = reverse("messages.view")
        return response


class GroupMessageDeleteView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/delete/<message_id>/ - sender deletes a message for everyone."""

    def post(self, request: HttpRequest, group_uuid, message_id: int) -> HttpResponse:
        """Tombstone the message and return the refreshed thread.

        Args:
            request: The incoming request.
            group_uuid: UUID of the group chat.
            message_id: PK of the message to delete.

        Returns:
            The re-rendered thread partial, or 403 on failure.
        """
        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)
        message = get_object_or_404(GroupMessage, pk=message_id, group=group)
        try:
            delete_group_message(message, profile)
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        response = render(request, "dashboard/partials/messages/_group_thread.html", _group_thread_context(profile, group, membership))
        return _trigger_msg_label_refresh(response)


class GroupSharePinView(LoginRequiredMixin, View):
    """GET/POST /messages/g/<uuid>/share/pin/ - share one of the sender's own pins with the group."""

    def get(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Render the pin-picker dialog body (reuses the 1:1 dialog partial).

        Args:
            request: The incoming request.
            group_uuid: UUID of the group chat.

        Returns:
            The rendered dialog partial.
        """
        profile = _get_profile(request)
        group, _membership = _get_group(profile, group_uuid)
        pins = Pin.objects.filter(profile=profile, parent_pin__isnull=True).select_related("location").order_by("name")[:200]
        return render(request, "dashboard/partials/messages/_share_pin_dialog.html", {"group": group, "pins": pins})

    def post(self, request: HttpRequest, group_uuid) -> HttpResponse:
        """Create the per-member PinShares + chat message and return the refreshed thread.

        Args:
            request: The incoming request. Reads ``pin_slug`` and ``body``.
            group_uuid: UUID of the group chat.

        Returns:
            The re-rendered thread partial, or 400/403 on failure.
        """
        profile = _get_profile(request)
        group, membership = _get_group(profile, group_uuid)
        pin = get_object_or_404(Pin, slug=request.POST.get("pin_slug"), profile=profile)
        body = request.POST.get("body", "").strip() or f"Check out {pin.display_label}!"
        try:
            share_pin_in_group_message(profile, group, pin, body)
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        response = render(request, "dashboard/partials/messages/_group_thread.html", _group_thread_context(profile, group, membership))
        return _trigger_msg_label_refresh(response)


class GroupSharePinRespondView(LoginRequiredMixin, View):
    """POST /messages/g/<uuid>/share/pin/<message_id>/respond/ - accept/reject the caller's own copy."""

    def post(self, request: HttpRequest, group_uuid, message_id: int) -> HttpResponse:
        """Apply the accept/reject decision and return the refreshed share card.

        Args:
            request: The incoming request. Reads ``action`` (``accept``/``reject``).
            group_uuid: UUID of the group chat.
            message_id: PK of the message carrying the pin share.

        Returns:
            The re-rendered ``_group_share_card.html`` fragment with a toast
            trigger, or 400/404 on failure.
        """
        from urbanlens.dashboard.controllers.pin_sharing import apply_pin_share_response
        from urbanlens.dashboard.models.pin_share.meta import PinShareStatus

        profile = _get_profile(request)
        group, _membership = _get_group(profile, group_uuid)
        message = get_object_or_404(GroupMessage.objects.filter(group=group), pk=message_id)
        share = message.shares.select_related("pin_share__pin__location").filter(recipient=profile).first()
        if share is None or share.pin_share is None:
            raise Http404

        context = {"message": message, "viewer_share": share, "viewer_id": profile.pk, "group": group}
        if share.pin_share.status != PinShareStatus.PENDING:
            response = render(request, "dashboard/partials/messages/_group_share_card.html", context)
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "info", "message": "This shared pin has already been handled."}})
            return response

        action = request.POST.get("action")
        if action not in ("accept", "reject"):
            return HttpResponseBadRequest("Unknown action.")
        _target_pin, status_message = apply_pin_share_response(share.pin_share, action)
        response = render(request, "dashboard/partials/messages/_group_share_card.html", context)
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "success" if action == "accept" else "info", "message": status_message}})
        return response


class GroupMemberSearchView(LoginRequiredMixin, View):
    """GET /messages/groups/member-search/?q=... - pickable profiles for group dialogs.

    Only profiles whose privacy settings permit a message from the requester
    are offered (the same rule ``create_group_chat``/``add_group_members``
    enforce server-side).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Return matching, addable profiles for the member picker.

        Args:
            request: The incoming request. Reads ``q``.

        Returns:
            The member search-results partial.
        """
        from django.db.models import Q

        from urbanlens.dashboard.services.avatar_colors import assign_avatar_colors

        profile = _get_profile(request)
        query = request.GET.get("q", "").strip()
        results: list[Profile] = []
        if len(query) >= 2:
            candidates = Profile.objects.select_related("user").filter(Q(user__username__icontains=query) | Q(slug__icontains=query)).exclude(pk=profile.pk).order_by("user__username")[: MEMBER_SEARCH_LIMIT * 4]
            # can_view_profile mirrors RecipientSearchView: the results partial
            # renders each candidate's real slug/username/avatar, so a profile
            # hidden from the requester must not be enumerable through this
            # picker either.
            results = [candidate for candidate in candidates if can_direct_message(profile, candidate) and candidate.can_view_profile(profile)][:MEMBER_SEARCH_LIMIT]
            for candidate in results:
                candidate.ensure_slug()
            # Distinct-per-list fallback avatar colors - see GroupMembersDialogView.get.
            assign_avatar_colors(results, identity=lambda p: p.slug or str(p.pk))
        return render(request, "dashboard/partials/messages/_group_member_results.html", {"results": results, "query": query})
