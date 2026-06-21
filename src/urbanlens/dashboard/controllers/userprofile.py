"""Profile view and edit controllers."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from urbanlens.dashboard.forms.profile_form import (
    DiscordHandleForm,
    ProfileForm,
    validate_birth_date,
    validate_started_exploring,
)
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest


class ViewProfileView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, profile_uuid: UUID | None = None) -> HttpResponse:
        if profile_uuid is not None:
            profile = get_object_or_404(Profile, uuid=profile_uuid)
            if not self._can_view_profile(request, profile):
                raise Http404
        else:
            profile, _ = Profile.objects.get_or_create(user=request.user)

        from urbanlens.dashboard.services.social_links import get_profile_links

        context = {
            "profile": profile,
            "social_links": get_profile_links(profile),
        }
        self._add_common_context(request, profile, context)
        return render(request, "dashboard/pages/profile/index.html", context)

    def post(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Handle avatar upload from the profile hero card."""
        profile = Profile.objects.get(user=request.user)
        if "avatar" in request.FILES:
            profile.avatar = request.FILES["avatar"]
            profile.save(update_fields=["avatar"])
        return redirect("profile.view")

    def _can_view_profile(self, request: HttpRequest, profile: Profile) -> bool:
        """Return True if the requesting user is allowed to view this profile."""
        if request.user == profile.user:
            return True

        visibility = profile.profile_visibility

        if visibility == VisibilityChoice.ANYONE:
            return True

        if not request.user.is_authenticated:
            return False

        if visibility == VisibilityChoice.NO_ONE:
            return False

        try:
            my_profile = Profile.objects.get(user=request.user)
        except Profile.DoesNotExist:
            return False

        if visibility == VisibilityChoice.FRIENDS:
            from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

            try:
                friendship = Friendship.objects.between(my_profile, profile)
                return FriendshipStatus.is_friend(friendship.status)
            except Friendship.DoesNotExist:
                return False

        if visibility == VisibilityChoice.COMMON_PIN:
            my_loc_ids = set(
                Pin.objects.filter(profile=my_profile, location__isnull=False).values_list("location_id", flat=True),
            )
            their_loc_ids = set(
                Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", flat=True),
            )
            return bool(my_loc_ids & their_loc_ids)

        if visibility == VisibilityChoice.COMMON_FRIEND:
            from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

            my_friends = set(
                Friendship.objects.filter(from_profile=my_profile, status=FriendshipStatus.ACCEPTED).values_list(
                    "to_profile_id",
                    flat=True,
                ),
            ) | set(
                Friendship.objects.filter(to_profile=my_profile, status=FriendshipStatus.ACCEPTED).values_list(
                    "from_profile_id",
                    flat=True,
                ),
            )
            their_friends = set(
                Friendship.objects.filter(from_profile=profile, status=FriendshipStatus.ACCEPTED).values_list(
                    "to_profile_id",
                    flat=True,
                ),
            ) | set(
                Friendship.objects.filter(to_profile=profile, status=FriendshipStatus.ACCEPTED).values_list(
                    "from_profile_id",
                    flat=True,
                ),
            )
            return bool(my_friends & their_friends)

        if visibility == VisibilityChoice.COMMON_TRIP:
            from urbanlens.dashboard.models.trips.model import TripMembership

            my_trips = set(TripMembership.objects.filter(profile=my_profile).values_list("trip_id", flat=True))
            their_trips = set(TripMembership.objects.filter(profile=profile).values_list("trip_id", flat=True))
            return bool(my_trips & their_trips)

        return False

    def _add_common_context(self, request: HttpRequest, profile: Profile, context: dict) -> None:
        """Populate cross-user stats and friendship context when viewing another user's profile."""
        if not request.user.is_authenticated or profile.user == request.user:
            return

        try:
            my_profile = Profile.objects.get(user=request.user)
        except Profile.DoesNotExist:
            return

        # Location IDs pinned by this profile
        their_loc_ids = set(
            Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", flat=True),
        )
        # Location IDs pinned by the current user
        my_loc_ids = set(
            Pin.objects.filter(profile=my_profile, location__isnull=False).values_list("location_id", flat=True),
        )
        common_ids = their_loc_ids & my_loc_ids

        # Visited by both - has the protected "Visited" status badge, or has a last_visited date
        visited_filter = Q(statuses__name="Visited") | Q(last_visited__isnull=False)
        their_visited_ids = set(
            Pin.objects.filter(profile=profile, location__isnull=False)
            .filter(visited_filter)
            .values_list("location_id", flat=True),
        )
        my_visited_ids = set(
            Pin.objects.filter(profile=my_profile, location__isnull=False)
            .filter(visited_filter)
            .values_list("location_id", flat=True),
        )
        shared_visited_ids = their_visited_ids & my_visited_ids

        context["common_pin_count"] = len(common_ids)
        context["shared_visited"] = (
            Location.objects.filter(id__in=shared_visited_ids).order_by("name")
            if shared_visited_ids
            else Location.objects.none()
        )

        # Friendship relationship
        from urbanlens.dashboard.models.friendship.model import Friendship

        friendship = Friendship.objects.all().between(my_profile, profile)
        context["friendship"] = friendship
        context["friendship_status"] = friendship.status if friendship else None
        context["friends_since"] = (
            friendship.updated if friendship and friendship.status == FriendshipStatus.ACCEPTED else None
        )

        # Trips in common
        from urbanlens.dashboard.models.trips.model import TripMembership
        my_trip_ids = set(TripMembership.objects.filter(profile=my_profile).values_list("trip_id", flat=True))
        their_trip_ids = set(TripMembership.objects.filter(profile=profile).values_list("trip_id", flat=True))
        common_trip_ids = my_trip_ids & their_trip_ids
        if common_trip_ids:
            from urbanlens.dashboard.models.trips.model import Trip
            context["trips_in_common"] = Trip.objects.filter(id__in=common_trip_ids).order_by("name")
        else:
            from urbanlens.dashboard.models.trips.model import Trip
            context["trips_in_common"] = Trip.objects.none()

        # Private annotations (notes + user badges) - only when viewing someone else
        from urbanlens.dashboard.models.badges.model import Badge
        from urbanlens.dashboard.models.badges.profile_assignment import ProfileBadgeAssignment
        from urbanlens.dashboard.models.profile.note import ProfileNote

        context["viewer_notes"] = ProfileNote.objects.filter(author=my_profile, subject=profile)
        context["user_badges"] = Badge.objects.user_badges().visible_to(my_profile).ordered()
        context["assigned_badge_ids"] = set(
            ProfileBadgeAssignment.objects.filter(author=my_profile, subject=profile).values_list(
                "badge_id", flat=True,
            ),
        )
        context["my_profile"] = my_profile


class ProfileFieldUpdateView(LoginRequiredMixin, View):
    """Save a single profile field immediately (auto-save AJAX endpoint)."""

    _PROFILE_TEXT = frozenset({"bio", "area"})
    _PROFILE_DATES = frozenset({"birth_date", "started_exploring"})
    _USER_FIELDS = frozenset({"first_name", "last_name"})

    def post(self, request: HttpRequest) -> JsonResponse:
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)
        field = request.POST.get("field", "")

        if field in self._USER_FIELDS:
            value = request.POST.get("value", "").strip()
            setattr(request.user, field, value)
            request.user.save(update_fields=[field])
            return JsonResponse({"ok": True})

        profile, _ = Profile.objects.get_or_create(user=request.user)

        if field == "avatar":
            file = request.FILES.get("file_value")
            if not file:
                return JsonResponse({"error": "No file provided."}, status=400)
            profile.avatar = file
            profile.save(update_fields=["avatar"])
            return JsonResponse({"ok": True, "avatar_url": profile.avatar.url})

        if field in self._PROFILE_TEXT:
            value = request.POST.get("value", "").strip()
            setattr(profile, field, value or None)
            profile.save(update_fields=[field])
            return JsonResponse({"ok": True})

        if field in self._PROFILE_DATES:
            raw = request.POST.get("value", "").strip()
            if raw:
                try:
                    parsed = datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    return JsonResponse({"error": "Invalid date format."}, status=400)
                validator = validate_birth_date if field == "birth_date" else validate_started_exploring
                error = validator(parsed)
                if error:
                    return JsonResponse({"error": error})
                setattr(profile, field, parsed)
            else:
                setattr(profile, field, None)
            profile.save(update_fields=[field])
            return JsonResponse({"ok": True})

        return JsonResponse({"error": "Unknown field."}, status=400)


class EditProfileView(LoginRequiredMixin, View):
    def _build_context(
        self,
        profile: Profile,
        form: ProfileForm,
        discord_form: DiscordHandleForm,
        link_error: str = "",
    ) -> dict:
        from urbanlens.dashboard.services.social_links import get_profile_links

        discord_link = profile.social_links.filter(platform="discord").first()
        if not discord_form.is_bound:
            discord_form = DiscordHandleForm(initial={"discord": discord_link.handle if discord_link else ""})
        return {
            "form": form,
            "discord_form": discord_form,
            "social_links": get_profile_links(profile),
            "link_error": link_error,
        }

    def get(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        context = self._build_context(profile, ProfileForm(instance=profile), DiscordHandleForm())
        return render(request, "dashboard/pages/profile/edit.html", context)

    def post(self, request: HttpRequest) -> HttpResponse:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        action = request.POST.get("action", "save_profile")

        if action == "save_profile":
            return self._save_profile(request, profile)
        if action == "save_discord":
            return self._save_discord(request, profile)
        if action == "add_link":
            return self._add_link(request, profile)
        if action == "remove_link":
            return self._remove_link(request, profile)
        return redirect("profile.edit")

    def _save_profile(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect("login")
        form = ProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            request.user.first_name = request.POST.get("first_name", "").strip()
            request.user.last_name = request.POST.get("last_name", "").strip()
            request.user.save(update_fields=["first_name", "last_name"])
            return redirect("profile.edit")
        context = self._build_context(profile, form, DiscordHandleForm())
        return render(request, "dashboard/pages/profile/edit.html", context)

    def _save_discord(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        from urbanlens.dashboard.models.social_link.model import SocialLink

        discord_form = DiscordHandleForm(request.POST)
        if discord_form.is_valid():
            handle = discord_form.cleaned_data.get("discord", "").strip()
            if handle:
                SocialLink.objects.update_or_create(
                    profile=profile,
                    platform="discord",
                    defaults={"handle": handle},
                )
            else:
                profile.social_links.filter(platform="discord").delete()
            return redirect("profile.edit")
        context = self._build_context(profile, ProfileForm(instance=profile), discord_form)
        return render(request, "dashboard/pages/profile/edit.html", context)

    def _add_link(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        from urbanlens.dashboard.models.social_link.model import SocialLink
        from urbanlens.dashboard.services.social_links import parse_social_link

        raw = request.POST.get("link_input", "").strip()
        result = parse_social_link(raw) if raw else None
        if not result:
            context = self._build_context(
                profile,
                ProfileForm(instance=profile),
                DiscordHandleForm(),
                link_error="Couldn't recognise that URL. Try pasting the full profile link.",
            )
            return render(request, "dashboard/pages/profile/edit.html", context)
        platform, handle = result
        SocialLink.objects.update_or_create(
            profile=profile,
            platform=platform,
            defaults={"handle": handle},
        )
        return redirect("profile.edit")

    def _remove_link(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        from urbanlens.dashboard.services.social_links import KNOWN_PLATFORMS

        platform = request.POST.get("remove_platform", "")
        if platform in KNOWN_PLATFORMS:
            profile.social_links.filter(platform=platform).delete()
        return redirect("profile.edit")


class ProfileNoteView(LoginRequiredMixin, View):
    """Create a new private note about another profile (HTMX)."""

    def post(self, request: HttpRequest, profile_uuid: UUID) -> HttpResponse:
        from urbanlens.dashboard.models.profile.note import ProfileNote

        subject = get_object_or_404(Profile, uuid=profile_uuid)
        author = request.user.profile
        if author == subject:
            return HttpResponse("Cannot annotate your own profile.", status=400)

        content = request.POST.get("content", "").strip()
        if content:
            ProfileNote.objects.create(author=author, subject=subject, content=content)

        return _render_profile_annotation_partial(request, author, subject)


class ProfileNoteDeleteView(LoginRequiredMixin, View):
    """Delete one of the viewer's private notes about another profile (HTMX)."""

    def post(self, request: HttpRequest, profile_uuid: UUID, note_id: int) -> HttpResponse:
        from urbanlens.dashboard.models.profile.note import ProfileNote

        subject = get_object_or_404(Profile, uuid=profile_uuid)
        author = request.user.profile
        ProfileNote.objects.filter(pk=note_id, author=author, subject=subject).delete()
        return _render_profile_annotation_partial(request, author, subject)


class ProfileNoteEditView(LoginRequiredMixin, View):
    """Edit (PATCH) the content of an existing private note (HTMX)."""

    def post(self, request: HttpRequest, profile_uuid: UUID, note_id: int) -> HttpResponse:
        from urbanlens.dashboard.models.profile.note import ProfileNote

        subject = get_object_or_404(Profile, uuid=profile_uuid)
        author = request.user.profile
        content = request.POST.get("content", "").strip()
        ProfileNote.objects.filter(pk=note_id, author=author, subject=subject).update(content=content)
        return _render_profile_annotation_partial(request, author, subject)


class ProfileBadgeToggleView(LoginRequiredMixin, View):
    """Toggle a user-type badge on another profile (HTMX - re-renders the badge chips)."""

    def post(self, request: HttpRequest, profile_uuid: UUID, badge_id: int) -> HttpResponse:
        from urbanlens.dashboard.models.badges.model import KIND_USER, Badge
        from urbanlens.dashboard.models.badges.profile_assignment import ProfileBadgeAssignment

        subject = get_object_or_404(Profile, uuid=profile_uuid)
        author = request.user.profile
        if author == subject:
            return HttpResponse("Cannot annotate your own profile.", status=400)

        badge = get_object_or_404(Badge, pk=badge_id, kind=KIND_USER)

        assignment, created = ProfileBadgeAssignment.objects.get_or_create(
            author=author,
            subject=subject,
            badge=badge,
        )
        if not created:
            assignment.delete()

        return _render_profile_annotation_partial(request, author, subject)


def _render_profile_annotation_partial(
    request: HttpRequest,
    author: Profile,
    subject: Profile,
) -> HttpResponse:
    """Render the private annotation partial for HTMX swaps.

    Args:
        request: The HTTP request.
        author: The viewing user's profile.
        subject: The profile being annotated.

    Returns:
        Rendered HTML partial.
    """
    from urbanlens.dashboard.models.badges.model import KIND_USER, Badge
    from urbanlens.dashboard.models.badges.profile_assignment import ProfileBadgeAssignment
    from urbanlens.dashboard.models.profile.note import ProfileNote

    viewer_notes = ProfileNote.objects.filter(author=author, subject=subject)
    user_badges = Badge.objects.user_badges().visible_to(author).ordered()
    assigned_ids = set(
        ProfileBadgeAssignment.objects.filter(author=author, subject=subject).values_list("badge_id", flat=True),
    )

    return render(
        request,
        "dashboard/partials/profile_annotation_partial.html",
        {
            "subject": subject,
            "viewer_notes": viewer_notes,
            "user_badges": user_badges,
            "assigned_badge_ids": assigned_ids,
        },
    )
