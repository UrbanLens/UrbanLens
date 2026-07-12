from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.controllers.notifications import _trigger_badge_refresh
from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.connections import get_connections

logger = logging.getLogger(__name__)


def _friend_list_ctx(viewer: Profile | None, profile: Profile) -> dict:
    """Build context dict for friend list partials and pages.

    Determines:
    - friends: accepted friendship records for this profile
    - incoming_requests: pending requests TO this profile (only if viewer == profile)
    - viewer_friendship_status: status of the friendship between viewer and this profile
    - viewer_can_request: whether the viewer can send a friend request to this profile
    """
    friend_profiles = get_connections(profile)

    incoming_requests: list[Friendship] = []
    outgoing_requests: list[Friendship] = []
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
            outgoing_requests = list(
                Friendship.objects.filter(
                    from_profile=profile,
                    status=FriendshipStatus.REQUESTED,
                ).select_related("to_profile__user"),
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

    return {
        "friends": friend_profiles,
        "mutual_friends": mutual_friends,
        "incoming_requests": incoming_requests,
        "outgoing_requests": outgoing_requests,
        "viewer_friendship": viewer_friendship,
        "viewer_can_request": viewer_can_request,
        "is_own_profile": viewer is not None and viewer.pk == profile.pk,
        "viewer": viewer,
        "friend_list_profile": profile,
    }


def notify_friend_request(from_profile: Profile, to_profile: Profile) -> None:
    """Create an in-app notification when a friend request is sent.

    Args:
        from_profile: Profile sending the request.
        to_profile: Profile receiving the request.
    """
    try:
        pref = to_profile.notification_preferences.friend_request
    except AttributeError:
        pref = DeliveryPreference.SITE

    if pref == DeliveryPreference.NONE:
        return

    NotificationLog.objects.create(
        profile=to_profile,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=NotificationType.FRIEND_REQUEST,
        title="New friend request",
        message=f"{from_profile.username} wants to be your friend.",
        url=reverse("profile.view_user", kwargs={"profile_slug": from_profile.slug or str(from_profile.uuid)}),
        source_profile=from_profile,
    )


def request_or_accept_friendship(from_profile: Profile, to_profile: Profile) -> Friendship | None:
    """Send a friend request, auto-accepting instead if one is already pending in reverse.

    If `to_profile` already sent `from_profile` a pending request, the two profiles
    clearly want to be friends - accept that request instead of creating a redundant
    crossed request and a duplicate "new friend request" notification.

    Args:
        from_profile: Profile initiating this request.
        to_profile: Profile being requested.

    Returns:
        The resulting Friendship (pending or newly accepted), or None if the request
        could not be created.
    """
    existing = Friendship.objects.all().between(from_profile, to_profile)
    if existing and existing.status == FriendshipStatus.REQUESTED and existing.from_profile_id == to_profile.pk:
        if not existing.accept():
            return None
        NotificationLog.objects.create(
            profile=to_profile,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.FRIEND_ACCEPTED,
            title="Friend request accepted",
            message=f"{from_profile.username} accepted your friend request.",
            url=reverse("profile.view_user", kwargs={"profile_slug": from_profile.slug or str(from_profile.uuid)}),
            source_profile=from_profile,
        )
        # Mark from_profile's own pending friend_request notification (from to_profile) as read
        NotificationLog.objects.filter(
            profile=from_profile,
            notification_type=NotificationType.FRIEND_REQUEST,
            source_profile_id=to_profile.pk,
        ).update(status=Status.READ)
        return existing

    friendship = Friendship.request(from_profile=from_profile, to_profile=to_profile.pk)
    if friendship:
        notify_friend_request(from_profile, to_profile)
    return friendship


def _mark_friend_request_notifications_read(viewer_profile: Profile, source_profile_id: int) -> None:
    """Mark the viewer's pending "new friend request" notification(s) from a source as read.

    Accepting/declining/ignoring a request on the profile page (rather than via the
    notification dropdown's own accept/decline buttons) previously left the originating
    notification unread indefinitely, inflating the bell badge count forever.

    Args:
        viewer_profile: Profile who just acted on the request.
        source_profile_id: pk of the profile that sent the request.
    """
    NotificationLog.objects.filter(
        profile=viewer_profile,
        notification_type=NotificationType.FRIEND_REQUEST,
        source_profile_id=source_profile_id,
    ).update(status=Status.READ)


def _mark_incoming_request_notifications_read(viewer_profile: Profile, incoming_requests: list[Friendship]) -> None:
    """Mark "new friend request" notifications read once the owner views the pending requests.

    UL-240: previously only accepting/declining/ignoring a request marked its notification
    read - simply seeing the pending request listed on your own profile page left the
    notification (and bell badge count) unread indefinitely.

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
    return _trigger_badge_refresh(response)


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

        friendship = request_or_accept_friendship(requesting, to_profile)
        if not friendship:
            return HttpResponse("Could not request friend.", status=400)

        if request.headers.get("HX-Request"):
            return render(
                request,
                "dashboard/partials/profile/friend_list_partial.html",
                _friend_list_ctx(requesting, to_profile),
            )
        return _redirect_to_profile(profile_id)

    def accept_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        if not friendship.accept():
            return HttpResponse("Enable Community in Settings to accept friend requests.", status=403)

        # Notify the original requester that their request was accepted.
        requester = friendship.from_profile if friendship.to_profile == request.user.profile else friendship.to_profile
        NotificationLog.objects.create(
            profile=requester,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.FRIEND_ACCEPTED,
            title="Friend request accepted",
            message=f"{request.user.profile.username} accepted your friend request.",
            url=reverse("profile.view_user", kwargs={"profile_slug": request.user.profile.slug or str(request.user.profile.uuid)}),
        )
        _mark_friend_request_notifications_read(request.user.profile, profile_id)

        if request.headers.get("HX-Request"):
            return _own_friend_widget_response(request)
        return _redirect_to_profile(profile_id)

    def reject_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.decline()
        _mark_friend_request_notifications_read(request.user.profile, profile_id)
        if request.headers.get("HX-Request"):
            return _own_friend_widget_response(request)
        return _redirect_to_profile(profile_id)

    def ignore_friend(self, request: HttpRequest, profile_id: int):
        """Ignore a friend request - no notification sent, button stays unavailable."""
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.ignore()
        _mark_friend_request_notifications_read(request.user.profile, profile_id)
        if request.headers.get("HX-Request"):
            return _own_friend_widget_response(request)
        return _redirect_to_profile(profile_id)

    def block_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            friendship = None

        if friendship:
            friendship.status = FriendshipStatus.BLOCKED
            friendship.save()
        else:
            other = Profile.objects.filter(pk=profile_id).first()
            if not other:
                return HttpResponse("Profile not found.", status=404)
            Friendship.objects.create(
                from_profile=request.user.profile,
                to_profile=other,
                status=FriendshipStatus.BLOCKED,
            )
        if request.headers.get("HX-Request"):
            return HttpResponse("Profile blocked.")
        return _redirect_to_profile(profile_id)

    def mute_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.status = FriendshipStatus.MUTED
        friendship.save()
        if request.headers.get("HX-Request"):
            return HttpResponse("Muted.")
        return _redirect_to_profile(profile_id)

    def remove_friend(self, request: HttpRequest, profile_id: int):
        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)
        try:
            friendship = Friendship.objects.all().between(profile_id, request.user.profile)
        except Friendship.DoesNotExist:
            return HttpResponse("Friend request not found.", status=404)
        if not friendship:
            return HttpResponse("Friend request not found.", status=404)

        friendship.remove()
        if request.headers.get("HX-Request"):
            return _own_friend_widget_response(request)
        return _redirect_to_profile(profile_id)

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
            response = _trigger_badge_refresh(response)
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
        notifications = NotificationLog.objects.for_profile(viewer_profile).select_related("source_profile").order_by("-created")[:20]
        unread_count = NotificationLog.objects.for_profile(viewer_profile).unread().count()
        response = render(
            request,
            "dashboard/partials/notifications/notification_dropdown.html",
            {
                "notifications": notifications,
                "unread_count": unread_count,
            },
        )
        return _trigger_badge_refresh(response)

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
        import smtplib

        from django.contrib.auth.models import User
        from django.core.exceptions import ValidationError
        from django.core.mail import EmailMultiAlternatives
        from django.core.validators import validate_email

        from urbanlens.dashboard.models.email_log import EmailType
        from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
        from urbanlens.dashboard.services.email_normalization import find_user_by_email, normalize_email
        from urbanlens.dashboard.services.email_safety import email_rate_limit_error, has_sent_join_email, record_email_sent

        if not isinstance(request.user, User):
            return HttpResponse("Authentication required.", status=401)

        email = request.POST.get("email", "").strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            return HttpResponse("Please enter a valid email address.", status=400)

        inviter = request.user.profile
        if normalize_email(email) == normalize_email(inviter.email):
            return HttpResponse("That's your own email address.", status=400)

        # Rate limiting must be checked before we know whether the address is
        # registered - erroring only on the actually-sends-an-email path would
        # let a capped caller distinguish member from non-member addresses.
        rate_limit_error = email_rate_limit_error(inviter)
        if rate_limit_error:
            return HttpResponse(rate_limit_error, status=429)

        subscription_role_slug = request.POST.get("subscription_role", "").strip()
        subscription_duration = request.POST.get("subscription_duration", "")
        subscription_role = None
        if subscription_role_slug and request.user.has_perm("dashboard.view_site_admin"):
            from urbanlens.dashboard.models.subscriptions import SubscriptionRole

            SubscriptionRole.ensure_defaults()
            subscription_role = SubscriptionRole.objects.filter(slug=subscription_role_slug).first()

        friendship_changed = False
        existing_user = find_user_by_email(email)
        if existing_user:
            to_profile = existing_user.profile
            # Respect visibility settings silently - no error, no distinguishable response.
            if to_profile != inviter and to_profile.friend_request_visibility != VisibilityChoice.NO_ONE:
                friendship = request_or_accept_friendship(inviter, to_profile)
                friendship_changed = bool(friendship)
                if friendship and subscription_role is not None:
                    from urbanlens.dashboard.controllers.site_admin import _parse_duration_months
                    from urbanlens.dashboard.models.subscriptions import grant_subscription

                    grant_subscription(existing_user, subscription_role, request.user, _parse_duration_months(subscription_duration))
        else:
            # No registered account - create an invitation token and send email.
            # Avoid duplicate pending invitations from the same inviter.
            FriendInvitation.objects.filter(
                inviter=inviter,
                email=email,
                accepted_at__isnull=True,
            ).delete()

            invitation = FriendInvitation(inviter=inviter, email=email)
            invitation.save()
            if subscription_role is not None:
                from urbanlens.dashboard.models.subscriptions import PendingSubscriptionGrant

                PendingSubscriptionGrant.objects.create(
                    invitation=invitation,
                    role=subscription_role,
                    granted_by=request.user,
                    duration_months="" if subscription_duration == "indefinite" else subscription_duration,
                )

            # A given user only ever sends one join-the-site email to a given
            # address - the invitation row above still enables auto-friending
            # on sign-up, but the mailbox is not contacted again.
            if not has_sent_join_email(inviter, email):
                signup_url = request.build_absolute_uri(
                    f"/signup/?invite={invitation.token}",
                )
                context = {
                    "inviter": inviter,
                    "signup_url": signup_url,
                }
                subject = f"{inviter.username} invited you to join UrbanLens"
                text_body = f"Hi,\n\n{inviter.username} invited you to join UrbanLens - a private mapping platform for urban explorers and photographers.\n\nAccept the invitation:\n{signup_url}\n\n- UrbanLens"
                html_body = render_to_string("dashboard/email/friend_invite.html", context)

                try:
                    msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[email])
                    msg.attach_alternative(html_body, "text/html")
                    msg.send()
                except (smtplib.SMTPException, OSError):
                    logger.exception("Failed to send friend invitation to %s", email)
                else:
                    record_email_sent(inviter, email, EmailType.JOIN_INVITE)

        response = render(request, "dashboard/partials/profile/invite_result.html", {"result": "sent"})
        if friendship_changed:
            # A new outgoing request (or crossed-request auto-accept) doesn't
            # otherwise show up anywhere without a reload - OOB-refresh whichever
            # friend-list widget is present on the current page (the compact one
            # on the profile page, or the full one on /friends/; htmx silently
            # skips whichever id isn't in the DOM).
            ctx = {**_friend_list_ctx(inviter, inviter), "oob": True}
            widget_html = render_to_string(request=request, template_name="dashboard/partials/profile/friend_list_partial.html", context=ctx)
            page_html = render_to_string(request=request, template_name="dashboard/partials/profile/friends_page_content.html", context=ctx)
            response.content += (widget_html + page_html).encode()
        return response
