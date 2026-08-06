from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.controllers.notifications import _trigger_label_refresh
from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.core.text_limits import MAX_FRIEND_REQUEST_MESSAGE_LENGTH, text_length_error
from urbanlens.dashboard.services.social.connections import get_connections
from urbanlens.dashboard.services.social.friendship import (
    FriendshipActionError,
    FriendshipNotFoundError,
    InviteRateLimitedError,
    InviteValidationError,
    _mark_friend_request_notifications_read,
    accept_friend_request,
    block_profile,
    ignore_friend_request,
    invite_by_email as invite_by_email_service,
    mute_profile,
    notify_friend_request,
    reject_friend_request,
    remove_friend as remove_friend_service,
    request_or_accept_friendship,
    unblock_profile,
    unmute_profile,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

logger = logging.getLogger(__name__)

# Re-exported for callers that imported these from this module before the
# transitions moved to ``services.social.friendship`` (the notification signal handlers
# and several tests do). New code should import from the service directly.
__all__ = [
    "FriendController",
    "_mark_friend_request_notifications_read",
    "notify_friend_request",
    "request_or_accept_friendship",
]


def _pending_cancel_token(profile_id: int, kind: str, pk: int) -> str:
    """Opaque per-item token for cancelling a pending outgoing request.

    A pending outgoing Friendship (email matched a registered account, or a
    direct profile-click request) and a pending FriendInvitation (unmatched
    email) must be completely indistinguishable to the sender until accepted
    - see ``_friend_list_ctx``. That rules out exposing either row's real
    id or a type-specific cancel URL in the widget markup: differing URL
    shapes (or a numeric target-profile id) would tell the sender whether
    their invited email belongs to an account. This HMAC is what the cancel
    button carries instead: fixed-length hex, identical shape for both kinds,
    and not reversible or forgeable without ``SECRET_KEY``. The server
    resolves it by recomputing tokens over the sender's own (small) pending
    set - see ``FriendController.cancel_pending``.

    Args:
        profile_id: The SENDER's profile pk - scopes tokens per user.
        kind: ``"friendship"`` or ``"invitation"``.
        pk: The pending row's pk within that kind's table.

    Returns:
        Hex HMAC token, safe to embed in a URL.
    """
    from django.utils.crypto import salted_hmac

    return salted_hmac("dashboard.friendship.pending_cancel", f"{profile_id}:{kind}:{pk}").hexdigest()


def _friend_list_ctx(viewer: Profile | None, profile: Profile) -> dict:
    """Build context dict for friend list partials and pages.

    Determines:
    - friends: accepted friendship records for this profile
    - incoming_requests: pending requests TO this profile (only if viewer == profile)
    - outgoing_pending: this profile's own pending sent requests (only if
      viewer == profile) - Friendship and FriendInvitation rows merged into
      one list of ``{"cancel_token", "created"}`` dicts, sorted by created.
      Templates must render these WITHOUT the target's identity (name/avatar/
      username/profile link). Until a request is accepted, the sender must not
      be able to learn who they reached, nor even whether an invited email
      belongs to a registered account at all - showing full identity only for
      Friendship rows (which always resolve to a real account) while
      FriendInvitation rows (unmatched emails) were invisible turned "does a
      pending card exist" into an account-enumeration side channel. The merge
      goes further than rendering both identically: per-kind loops (or
      kind-specific cancel URLs/ids) would leak the same bit through markup
      ordering or the DOM, so both kinds share one loop, one opaque
      cancel-token URL shape, and one chronological ordering.
    - viewer_friendship_status: status of the friendship between viewer and this profile
    - viewer_can_request: whether the viewer can send a friend request to this profile
    """
    from django.utils import timezone

    from urbanlens.dashboard.models.friendship.invitation import FriendInvitation

    friend_profiles = get_connections(profile)

    incoming_requests: list[Friendship] = []
    outgoing_requests: list[Friendship] = []
    outgoing_email_invitations: list[FriendInvitation] = []
    viewer_friendship: Friendship | None = None
    viewer_can_request = False
    mutual_friends: list[Profile] = []

    if viewer:
        # Incoming/outgoing requests only shown to the profile owner
        if viewer.pk == profile.pk:
            incoming_requests = list(
                Friendship.objects.filter(
                    to_profile=profile,
                    status=FriendshipStatus.REQUESTED,
                ).select_related("from_profile__user"),
            )
            # No select_related here on purpose: the target's identity must
            # never be rendered for a pending outgoing request (see docstring),
            # so nothing ever touches to_profile.
            outgoing_requests = list(
                Friendship.objects.filter(
                    from_profile=profile,
                    status=FriendshipStatus.REQUESTED,
                ),
            )
            outgoing_email_invitations = list(
                FriendInvitation.objects.filter(
                    inviter=profile,
                    accepted_at__isnull=True,
                    expires_at__gt=timezone.now(),
                ),
            )

        # Determine viewer's relationship with this profile
        if viewer.pk != profile.pk:
            try:
                viewer_friendship = Friendship.objects.all().between(viewer, profile)
            except Friendship.DoesNotExist:
                viewer_friendship = None

            status = viewer_friendship.status if viewer_friendship else None
            viewer_can_request = status is None or FriendshipStatus.can_request(status)

            # Compute mutual friends (profile's friends that viewer is also friends with)
            profile_friend_ids = {fp.pk for fp in friend_profiles}
            viewer_friendships = Friendship.objects.all().profile(viewer.pk).is_friend().values_list("from_profile_id", "to_profile_id")
            viewer_friend_ids: set[int] = set()
            viewer_friend_ids.update(to_id if from_id == viewer.pk else from_id for from_id, to_id in viewer_friendships)

            mutual_ids = profile_friend_ids & viewer_friend_ids
            mutual_friends = [fp for fp in friend_profiles if fp.pk in mutual_ids]

    pending_entries: list[tuple[datetime, str]] = [(req.created, _pending_cancel_token(profile.pk, "friendship", req.pk)) for req in outgoing_requests] + [
        (inv.created, _pending_cancel_token(profile.pk, "invitation", inv.pk)) for inv in outgoing_email_invitations
    ]
    outgoing_pending = [{"cancel_token": token, "created": created} for created, token in sorted(pending_entries)]

    return {
        "friends": friend_profiles,
        "mutual_friends": mutual_friends,
        "incoming_requests": incoming_requests,
        "outgoing_pending": outgoing_pending,
        "outgoing_pending_count": len(outgoing_pending),
        "viewer_friendship": viewer_friendship,
        "viewer_can_request": viewer_can_request,
        "is_own_profile": viewer is not None and viewer.pk == profile.pk,
        "viewer": viewer,
        "friend_list_profile": profile,
    }


def _mark_incoming_request_notifications_read(viewer_profile: Profile, incoming_requests: list[Friendship]) -> None:
    """Mark "new friend request" notifications read once the owner views the pending requests.

    UL-240: previously only accepting/declining/ignoring a request marked its notification
    read - simply seeing the pending request listed on your own profile page left the
    notification (and bell label count) unread indefinitely.

    Args:
        viewer_profile: Profile who is viewing their own pending requests.
        incoming_requests: Pending Friendship rows currently shown to the viewer.
    """
    source_ids = [req.from_profile_id for req in incoming_requests]
    if not source_ids:
        return
    NotificationLog.objects.filter(
        profile=viewer_profile,
        notification_type=NotificationType.FRIEND_REQUEST,
        source_profile_id__in=source_ids,
    ).update(status=Status.READ)


def _own_friend_widget_response(request: HttpRequest) -> HttpResponse:
    """Re-render whichever own-profile friend widget triggered this HTMX request.

    Accept/reject/ignore/remove actions always mutate the current user's own
    friendships, so the refreshed context is always built for `request.user.profile`
    - never for the other profile named in the URL. The compact widget on the
    profile page and the full friends page share this data but use different
    markup, so dispatch on HX-Target (the id of the element htmx is swapping).
    """
    viewer_profile, _ = Profile.objects.get_or_create(user=request.user)
    ctx = _friend_list_ctx(viewer_profile, viewer_profile)
    if request.headers.get("HX-Target") == "friends_page_list":
        response = render(request, "dashboard/partials/profile/friends_page_content.html", ctx)
    else:
        response = render(request, "dashboard/partials/profile/friend_list_partial.html", ctx)
    return _trigger_label_refresh(response)


def _redirect_to_profile(profile_id: int, fallback_view_name: str = "profile.view") -> HttpResponse:
    """Redirect back to a profile page after a plain (non-HTMX) form submission."""
    other_profile = Profile.objects.filter(pk=profile_id).first()
    if other_profile:
        return redirect("profile.view_user", profile_slug=other_profile.slug or str(other_profile.uuid))
    return redirect(fallback_view_name)


class FriendController(LoginRequiredMixin, GenericViewSet):
    def request_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)

        to_profile = Profile.objects.filter(pk=profile_id).first()
        if not to_profile:
            return HttpResponse("User not found.", status=404)

        requesting = request.user.profile
        visibility = to_profile.friend_request_visibility

        if visibility == VisibilityChoice.NO_ONE:
            return HttpResponse("This user is not accepting friend requests.", status=403)

        # Shared evaluator: friends always qualify, ANYTHING_IN_COMMON accepts
        # any of pin/friend/trip overlap.
        if not Profile.visibility_permits(visibility, to_profile, requesting):
            rejection_messages = {
                VisibilityChoice.FRIENDS: "This user is not accepting friend requests.",
                VisibilityChoice.COMMON_PIN: "This user only accepts requests from people who share a pinned location.",
                VisibilityChoice.COMMON_FRIEND: "This user only accepts requests from friends of friends.",
                VisibilityChoice.COMMON_TRIP: "This user only accepts requests from people on a shared trip.",
                VisibilityChoice.ANYTHING_IN_COMMON: "This user only accepts requests from people with a pin, friend, or trip in common.",
            }
            return HttpResponse(rejection_messages.get(visibility, "This user is not accepting friend requests."), status=403)

        message = request.POST.get("message", "").strip()
        length_error = text_length_error(message, MAX_FRIEND_REQUEST_MESSAGE_LENGTH, "Message")
        if length_error:
            return HttpResponse(length_error, status=400)

        friendship = request_or_accept_friendship(requesting, to_profile, message or None)
        if not friendship:
            return HttpResponse("Could not request friend.", status=400)

        if request.headers.get("HX-Request"):
            return render(
                request,
                "dashboard/partials/profile/friend_list_partial.html",
                _friend_list_ctx(requesting, to_profile),
            )
        return _redirect_to_profile(profile_id)

    def _friend_action(
        self,
        request: HttpRequest,
        profile_id: int,
        action: Callable[[Profile, Profile], Friendship],
        *,
        htmx_response: Callable[[HttpRequest], HttpResponse],
        missing_message: str = "Friend request not found.",
    ) -> HttpResponse:
        """Run one ``services.social.friendship`` transition and render this controller's reply.

        The transitions themselves (and their error taxonomy) live in the
        service so the external API shares them; this only maps those errors
        onto the status codes the profile-page buttons have always returned.

        Args:
            request: The incoming request.
            profile_id: pk of the other profile in the relationship.
            action: The service function to apply, as ``(actor, target)``.
            htmx_response: Builds the partial returned to an HTMX caller.
            missing_message: Body used when the target profile does not exist.

        Returns:
            The rendered partial (HTMX) or a redirect back to the profile.
        """
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)

        target = Profile.objects.filter(pk=profile_id).first()
        if target is None:
            return HttpResponse(missing_message, status=404)

        try:
            action(request.user.profile, target)
        except FriendshipNotFoundError as exc:
            return HttpResponse(exc.safe_message, status=404)
        except FriendshipActionError as exc:
            # Covers FriendLimitExceededError and the community-disabled case,
            # both of which this surface has always answered with 403.
            return HttpResponse(exc.safe_message, status=403)

        if request.headers.get("HX-Request"):
            return htmx_response(request)
        return _redirect_to_profile(profile_id)

    def accept_friend(self, request: HttpRequest, profile_id: int):
        """Accept a pending friend request from ``profile_id``."""
        return self._friend_action(request, profile_id, accept_friend_request, htmx_response=_own_friend_widget_response)

    def reject_friend(self, request: HttpRequest, profile_id: int):
        """Decline a pending friend request from ``profile_id``."""
        return self._friend_action(request, profile_id, reject_friend_request, htmx_response=_own_friend_widget_response)

    def ignore_friend(self, request: HttpRequest, profile_id: int):
        """Ignore a friend request - no notification sent, button stays unavailable."""
        return self._friend_action(request, profile_id, ignore_friend_request, htmx_response=_own_friend_widget_response)

    def block_friend(self, request: HttpRequest, profile_id: int):
        """Block ``profile_id``, creating the relationship row if there isn't one."""
        return self._friend_action(
            request,
            profile_id,
            block_profile,
            htmx_response=lambda _request: HttpResponse("Profile blocked."),
            missing_message="Profile not found.",
        )

    def unblock_friend(self, request: HttpRequest, profile_id: int):
        """Lift a block this user placed on ``profile_id``.

        The profile page's Unblock button used to post to ``friend.remove``,
        which reached ``remove_friend`` - a direction-agnostic transition that
        would just as happily clear a block placed *on* the caller by someone
        else. That is now refused at the service, so the button needs the real
        inverse; ``unblock_profile`` also answers 404 rather than 403 when the
        block is not the caller's, so nothing here confirms one exists.
        """
        return self._friend_action(
            request,
            profile_id,
            unblock_profile,
            htmx_response=lambda _request: HttpResponse("Unblocked."),
            missing_message="Profile not found.",
        )

    def mute_friend(self, request: HttpRequest, profile_id: int):
        """Mute an existing relationship with ``profile_id``.

        Sets the ``muted`` flag only - the relationship itself (and so every
        visibility gate that reads ``Profile.are_friends``) is untouched. See
        ``services.social.friendship.mute_profile``.
        """
        return self._friend_action(
            request,
            profile_id,
            mute_profile,
            htmx_response=lambda _request: HttpResponse("Muted."),
        )

    def unmute_friend(self, request: HttpRequest, profile_id: int):
        """Un-mute an existing relationship with ``profile_id``.

        The counterpart the profile page never had. Its Unmute button used to
        post to ``friend.request``, which ``FriendshipStatus.can_request``
        refuses for a ``Muted`` row, so unmuting always answered 400 and the
        relationship could not be recovered from the UI at all.
        """
        return self._friend_action(
            request,
            profile_id,
            unmute_profile,
            htmx_response=lambda _request: HttpResponse("Unmuted."),
        )

    def remove_friend(self, request: HttpRequest, profile_id: int):
        """End an existing friendship with ``profile_id``."""
        return self._friend_action(request, profile_id, remove_friend_service, htmx_response=_own_friend_widget_response)

    def cancel_pending(self, request: HttpRequest, token: str):
        """Cancel one of the current user's own pending outgoing requests, by opaque token.

        One endpoint for BOTH pending kinds - an outgoing Friendship request
        (email matched a registered account, or a direct profile-click
        request) and an outgoing FriendInvitation (unmatched email). The
        token (see ``_pending_cancel_token``) is resolved by recomputing the
        HMAC over the caller's own pending rows, so the URL shape, the
        response, and the 404 behavior are byte-identical regardless of which
        kind (or neither) matched - a sender can't use this endpoint, or the
        markup pointing at it, to learn whether an invited email belongs to a
        registered account.
        """
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)

        from django.utils import timezone
        from django.utils.crypto import constant_time_compare

        from urbanlens.dashboard.models.friendship.invitation import FriendInvitation

        profile = request.user.profile
        cancelled = False
        for friendship in Friendship.objects.filter(from_profile=profile, status=FriendshipStatus.REQUESTED):
            if constant_time_compare(token, _pending_cancel_token(profile.pk, "friendship", friendship.pk)):
                friendship.remove()
                cancelled = True
                break
        if not cancelled:
            for invitation in FriendInvitation.objects.filter(inviter=profile, accepted_at__isnull=True, expires_at__gt=timezone.now()):
                if constant_time_compare(token, _pending_cancel_token(profile.pk, "invitation", invitation.pk)):
                    invitation.delete()
                    cancelled = True
                    break
        if not cancelled:
            return HttpResponse("Pending request not found.", status=404)

        if request.headers.get("HX-Request"):
            return _own_friend_widget_response(request)
        return redirect("profile.view_user", profile_slug=request.user.profile.slug or str(request.user.profile.uuid))

    def friend_list(self, request: HttpRequest, profile_id: int):
        """HTMX partial: friend list shown on the profile page."""
        profile = Profile.objects.filter(pk=profile_id).first()
        if not profile:
            return HttpResponse("")
        viewer = request.user.profile if request.user.is_authenticated else None
        ctx = _friend_list_ctx(viewer, profile)
        response = render(request, "dashboard/partials/profile/friend_list_partial.html", ctx)
        if viewer and viewer.pk == profile.pk and ctx["incoming_requests"]:
            _mark_incoming_request_notifications_read(viewer, ctx["incoming_requests"])
            response = _trigger_label_refresh(response)
        return response

    def friends_page(self, request: HttpRequest, profile_id: int):
        """Full friends list page - only accessible to the profile owner."""
        from django.http import Http404

        profile = Profile.objects.filter(pk=profile_id).first()
        if not profile:
            raise Http404
        viewer = request.user.profile if request.user.is_authenticated else None
        if viewer is None or viewer.pk != profile.pk:
            return redirect("profile.view_user", profile_slug=profile.slug or str(profile.uuid))
        ctx = _friend_list_ctx(viewer, profile)
        if ctx["incoming_requests"]:
            _mark_incoming_request_notifications_read(viewer, ctx["incoming_requests"])
        return render(request, "dashboard/pages/profile/friends.html", {**ctx, "profile": profile})

    def friends_page_widget(self, request: HttpRequest, profile_id: int):
        """HTMX partial: just the /friends/ page's list content, for live refresh."""
        from django.http import Http404

        profile = Profile.objects.filter(pk=profile_id).first()
        if not profile:
            raise Http404
        viewer = request.user.profile if request.user.is_authenticated else None
        if viewer is None or viewer.pk != profile.pk:
            raise Http404
        ctx = _friend_list_ctx(viewer, profile)
        return render(request, "dashboard/partials/profile/friends_page_content.html", ctx)

    def friend_request_respond(self, request: HttpRequest, from_profile_id: int):
        """Accept or decline a friend request from the notification dropdown.

        Returns the refreshed notification dropdown so the HTMX swap replaces
        #notif-dropdown-wrap's innerHTML inline without navigating away.
        """
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)

        action = request.POST.get("action", "accept")
        viewer_profile = request.user.profile

        try:
            friendship = Friendship.objects.all().between(from_profile_id, viewer_profile)
        except Friendship.DoesNotExist:
            friendship = None

        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        if action == "accept":
            friendship.accept()
            from_profile = Profile.objects.filter(pk=from_profile_id).first()
            if from_profile:
                NotificationLog.objects.create(
                    profile=from_profile,
                    status=Status.UNREAD,
                    importance=Importance.MEDIUM,
                    notification_type=NotificationType.FRIEND_ACCEPTED,
                    title="Friend request accepted",
                    message=f"{viewer_profile.username} accepted your friend request.",
                    url=reverse("profile.view_user", kwargs={"profile_slug": viewer_profile.slug or str(viewer_profile.uuid)}),
                    source_profile=viewer_profile,
                )
        else:
            friendship.decline()

        # Mark any pending friend_request notifications from this source as read
        NotificationLog.objects.filter(
            profile=viewer_profile,
            notification_type=NotificationType.FRIEND_REQUEST,
            source_profile_id=from_profile_id,
        ).update(status=Status.READ)

        # Return refreshed notification dropdown
        notifications = NotificationLog.objects.for_profile(viewer_profile).for_display().order_by("-created")[:20]
        unread_count = NotificationLog.objects.for_profile(viewer_profile).unread().count()
        response = render(
            request,
            "dashboard/partials/notifications/notification_dropdown.html",
            {
                "notifications": notifications,
                "unread_count": unread_count,
            },
        )
        return _trigger_label_refresh(response)

    def invite_by_email(self, request: HttpRequest):
        """Invite a friend by email address.

        If the email belongs to an existing account (primary or verified
        secondary email), send that account a friend request. Otherwise
        create a FriendInvitation and email the address with a join link; on
        sign-up the pending request is auto-accepted.

        The response is identical in every case - it never reveals the
        target's username or whether the email belongs to a registered
        account, since that would let a caller enumerate site membership by
        trying addresses one at a time.
        """
        from django.contrib.auth.models import User

        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)

        inviter = request.user.profile

        subscription_role_slug = request.POST.get("subscription_role", "").strip()
        subscription_duration = request.POST.get("subscription_duration", "")
        subscription_role = None
        if subscription_role_slug and request.user.has_perm("dashboard.view_site_admin"):
            from urbanlens.dashboard.models.subscriptions import SubscriptionRole

            subscription_role = SubscriptionRole.objects.get_by_slug(subscription_role_slug)

        try:
            invite_by_email_service(
                inviter,
                request.POST.get("email", ""),
                request.POST.get("message", ""),
                signup_url_builder=lambda token: request.build_absolute_uri(f"/signup/?invite={token}"),
                subscription_role=subscription_role,
                subscription_duration=subscription_duration,
            )
        except InviteValidationError as exc:
            return HttpResponse(exc.safe_message, status=400)
        except InviteRateLimitedError as exc:
            return HttpResponse(exc.safe_message, status=429)

        # The response body must be byte-identical no matter what happened above -
        # embedding a friend-list refresh directly here would let a caller tell a
        # registered target from an unregistered one (or a closed-visibility target
        # from an open one) just by diffing response content. Any live refresh of
        # the friend-list widgets instead happens via a separate, decoupled request
        # triggered by the header below.
        response = render(request, "dashboard/partials/profile/invite_result.html", {"result": "sent"})
        response["HX-Trigger"] = json.dumps({"friendListChanged": True})
        return response
