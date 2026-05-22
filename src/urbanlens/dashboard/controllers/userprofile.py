"""Profile view and edit controllers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from urbanlens.dashboard.forms.profile_form import DiscordHandleForm, ProfileForm
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class ViewProfileView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, profile_id: int | None = None) -> HttpResponse:
        if profile_id is not None:
            profile = get_object_or_404(Profile, id=profile_id)
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

        if visibility == VisibilityChoice.EVERYONE:
            return True

        if not request.user.is_authenticated:
            return False

        if visibility == VisibilityChoice.ONLY_ME:
            return False

        try:
            my_profile = Profile.objects.get(user=request.user)
        except Profile.DoesNotExist:
            return False

        if visibility == VisibilityChoice.FRIENDS:
            from urbanlens.dashboard.models.friendship.model import Friendship
            try:
                friendship = Friendship.objects.between(my_profile, profile)
                return FriendshipStatus.is_friend(friendship.status)
            except Exception:
                return False

        if visibility == VisibilityChoice.COMMON_LOCATIONS:
            my_loc_ids = set(
                Pin.objects.filter(profile=my_profile, location__isnull=False)
                .values_list("location_id", flat=True),
            )
            their_loc_ids = set(
                Pin.objects.filter(profile=profile, location__isnull=False)
                .values_list("location_id", flat=True),
            )
            return bool(my_loc_ids & their_loc_ids)

        return False

    def _add_common_context(self, request: HttpRequest, profile: Profile, context: dict) -> None:
        """Populate common-pins stats when the viewer is looking at someone else's profile."""
        if not request.user.is_authenticated or profile.user == request.user:
            return

        try:
            my_profile = Profile.objects.get(user=request.user)
        except Profile.DoesNotExist:
            return

        # Location IDs pinned by this profile
        their_loc_ids = set(
            Pin.objects.filter(profile=profile, location__isnull=False)
            .values_list("location_id", flat=True),
        )
        # Location IDs pinned by the current user
        my_loc_ids = set(
            Pin.objects.filter(profile=my_profile, location__isnull=False)
            .values_list("location_id", flat=True),
        )
        common_ids = their_loc_ids & my_loc_ids

        # Visited by both (status="visited" or last_visited set)
        visited_filter = Q(status="visited") | Q(last_visited__isnull=False)
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
            Location.objects.filter(id__in=shared_visited_ids)
            .order_by("name")
            if shared_visited_ids
            else Location.objects.none()
        )


class EditProfileView(LoginRequiredMixin, View):
    def _build_context(self, profile: Profile, form: ProfileForm, discord_form: DiscordHandleForm, link_error: str = "") -> dict:
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
        form = ProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
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
