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
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.direct_messages import can_direct_message, conversations_for, create_direct_message
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
    thread_messages = DirectMessage.objects.between(profile, partner).select_related("sender", "sender__user", "recipient", "recipient__user")
    return {
        "partner": partner,
        "thread_messages": thread_messages,
        "can_message_partner": can_direct_message(profile, partner),
        "max_message_length": MAX_DIRECT_MESSAGE_LENGTH,
    }


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
        try:
            create_direct_message(profile, partner, request.POST.get("body", ""))
        except ValueError as exc:
            # create_direct_message only raises ValueError with a fixed, developer-authored
            # message (blank/too-long body) - never a stack trace or sensitive data.
            return HttpResponseBadRequest(str(exc))  # lgtm[py/stack-trace-exposure]
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
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
