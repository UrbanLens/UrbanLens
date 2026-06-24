"""Profile view and edit controllers."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
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


class ViewProfileView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, profile_slug: UUID | None = None) -> HttpResponse:
        if profile_slug is not None:
            profile = get_object_or_404(Profile, slug=profile_slug)
            if not self._can_view_profile(request, profile):
                raise Http404
        else:
            profile, _ = Profile.objects.get_or_create(user=request.user)

        import contextlib

        from urbanlens.dashboard.services.social_links import get_profile_links

        viewer_profile: Profile | None = None
        if request.user.is_authenticated and request.user != profile.user:
            with contextlib.suppress(Profile.DoesNotExist):
                viewer_profile = Profile.objects.get(user=request.user)
        elif request.user.is_authenticated and request.user == profile.user:
            viewer_profile = profile

        can_view_contact = profile.can_view_contact_info(viewer_profile)
        contact_info = None
        if can_view_contact:
            contact_info = {
                "phone_number": profile.phone_number,
                "signal_username": profile.signal_username,
                "discord_username": profile.discord_username,
                "whatsapp_number": profile.whatsapp_number,
                "telegram_username": profile.telegram_username,
                "matrix_handle": profile.matrix_handle,
            }

        context = {
            "profile": profile,
            "social_links": get_profile_links(profile),
            "contact_info": contact_info,
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

        # Private annotations (notes, user badges, trust rating) - only when viewing someone else
        from urbanlens.dashboard.models.badges.model import Badge
        from urbanlens.dashboard.models.badges.profile_assignment import ProfileBadgeAssignment
        from urbanlens.dashboard.models.profile.note import ProfileNote
        from urbanlens.dashboard.models.profile.trust import ProfileTrust

        context["viewer_notes"] = ProfileNote.objects.filter(author=my_profile, subject=profile)
        context["user_badges"] = Badge.objects.user_badges().visible_to(my_profile).ordered()
        context["assigned_badge_ids"] = set(
            ProfileBadgeAssignment.objects.filter(author=my_profile, subject=profile).values_list(
                "badge_id", flat=True,
            ),
        )
        trust = ProfileTrust.objects.filter(author=my_profile, subject=profile).first()
        context["trust_rating"] = trust.rating if trust else 0
        context["my_profile"] = my_profile


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,30}$")


class ProfileFieldUpdateView(LoginRequiredMixin, View):
    """Save a single profile field immediately (auto-save AJAX endpoint).

    GET  ?field=username&value=foo  →  availability check (JSON)
    POST field=<name> value=<val>   →  save field (JSON)
    """

    _PROFILE_TEXT = frozenset({"bio", "area"})
    _PROFILE_CONTACT = frozenset({"phone_number", "signal_username", "discord_username", "whatsapp_number", "telegram_username", "matrix_handle"})
    _PROFILE_DATES = frozenset({"birth_date", "started_exploring"})
    _USER_FIELDS = frozenset({"first_name", "last_name"})

    def get(self, request: HttpRequest) -> JsonResponse:
        """Username availability check."""
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)
        field = request.GET.get("field", "")
        if field != "username":
            return JsonResponse({"error": "Unsupported."}, status=400)
        username = request.GET.get("value", "").strip()
        if not username:
            return JsonResponse({"available": False, "reason": "Username required"})
        if not _USERNAME_RE.match(username):
            return JsonResponse({"available": False, "reason": "3-30 characters: letters, numbers, and underscores only"})
        taken = User.objects.filter(username__iexact=username).exclude(pk=request.user.pk).exists()
        if taken:
            return JsonResponse({"available": False, "reason": "That username is already taken"})
        return JsonResponse({"available": True})

    def post(self, request: HttpRequest) -> JsonResponse:
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)
        field = request.POST.get("field", "")

        if field in self._USER_FIELDS:
            value = request.POST.get("value", "").strip()
            setattr(request.user, field, value)
            request.user.save(update_fields=[field])
            return JsonResponse({"ok": True})

        if field == "username":
            return self._save_username(request)

        if field == "setup_complete":
            profile, _ = Profile.objects.get_or_create(user=request.user)
            if not profile.profile_setup_complete:
                profile.profile_setup_complete = True
                profile.save(update_fields=["profile_setup_complete"])
            return JsonResponse({"ok": True})

        profile, _ = Profile.objects.get_or_create(user=request.user)

        if field == "avatar":
            file = request.FILES.get("file_value")
            if not file:
                return JsonResponse({"error": "No file provided."}, status=400)
            profile.avatar = file
            profile.save(update_fields=["avatar"])
            return JsonResponse({"ok": True, "avatar_url": profile.avatar.url})

        if field == "avatar_gravatar":
            return self._save_avatar_gravatar(request, profile)

        if field == "avatar_emoji":
            return self._save_avatar_emoji(request, profile)

        if field in self._PROFILE_CONTACT:
            value = request.POST.get("value", "").strip()
            setattr(profile, field, value)
            profile.save(update_fields=[field])
            return JsonResponse({"ok": True})

        if field == "contact_visibility":
            from urbanlens.dashboard.models.profile.model import VisibilityChoice
            value = request.POST.get("value", "").strip()
            if value not in VisibilityChoice.values:
                return JsonResponse({"error": "Invalid visibility option."}, status=400)
            profile.contact_visibility = value
            profile.save(update_fields=["contact_visibility"])
            return JsonResponse({"ok": True})

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

    def _save_username(self, request: HttpRequest) -> JsonResponse:
        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)
        username = request.POST.get("value", "").strip()
        if not username:
            return JsonResponse({"error": "Username is required."}, status=400)
        if not _USERNAME_RE.match(username):
            return JsonResponse({"error": "3-30 characters: letters, numbers, and underscores only."}, status=400)
        if User.objects.filter(username__iexact=username).exclude(pk=request.user.pk).exists():
            return JsonResponse({"error": "That username is already taken."}, status=409)
        request.user.username = username
        request.user.save(update_fields=["username"])
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.profile_setup_complete:
            profile.profile_setup_complete = True
            profile.save(update_fields=["profile_setup_complete"])
        return JsonResponse({"ok": True})

    def _save_avatar_gravatar(self, request: HttpRequest, profile: Profile) -> JsonResponse:
        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)
        import hashlib

        from django.core.files.base import ContentFile

        from urbanlens.dashboard.services.social_auth.pipeline import _download_image

        email = request.user.email or ""
        if not email:
            return JsonResponse({"error": "No email address on this account."}, status=400)
        digest = hashlib.md5(email.strip().lower().encode(), usedforsecurity=False).hexdigest()
        url = f"https://www.gravatar.com/avatar/{digest}?s=256&d=404"
        img = _download_image(url)
        if not img:
            return JsonResponse({"error": "No Gravatar found for your email address."}, status=404)
        profile.avatar.save(f"gravatar_{request.user.pk}.jpg", ContentFile(img), save=True)
        return JsonResponse({"ok": True, "avatar_url": profile.avatar.url})

    def _save_avatar_emoji(self, request: HttpRequest, profile: Profile) -> JsonResponse:
        from django.core.files.base import ContentFile

        from urbanlens.dashboard.models.colors import MaterialColor
        from urbanlens.dashboard.services.social_auth.pipeline import (
            _ANIMAL_EMOJIS,
            generate_emoji_avatar_svg,
        )

        animal = request.POST.get("animal", "fox")
        color = request.POST.get("color", "#4CAF50")
        emoji = _ANIMAL_EMOJIS.get(animal, "🦊")
        if color.lower() not in {v.lower() for v in MaterialColor.values}:
            color = MaterialColor.GREY.value
        svg = generate_emoji_avatar_svg(emoji, color)
        profile.avatar.save(
            f"emoji_{request.user.pk}.svg",
            ContentFile(svg.encode("utf-8")),
            save=True,
        )
        return JsonResponse({"ok": True, "avatar_url": profile.avatar.url})


class EditProfileView(LoginRequiredMixin, View):
    def _build_context(
        self,
        profile: Profile,
        form: ProfileForm,
        discord_form: DiscordHandleForm,
        link_error: str = "",
    ) -> dict:
        import hashlib

        from urbanlens.dashboard.services.social_auth.pipeline import random_emoji_options
        from urbanlens.dashboard.services.social_links import URL_INPUT_PLATFORM_LABELS, get_profile_links

        discord_link = profile.social_links.filter(platform="discord").first()
        if not discord_form.is_bound:
            discord_form = DiscordHandleForm(initial={"discord": discord_link.handle if discord_link else ""})

        email = profile.user.email or ""
        if email:
            gh = hashlib.md5(email.strip().lower().encode(), usedforsecurity=False).hexdigest()
            gravatar_preview_url = f"https://www.gravatar.com/avatar/{gh}?s=200&d=identicon"
        else:
            gravatar_preview_url = ""

        from urbanlens.dashboard.models.profile.model import VisibilityChoice

        return {
            "form": form,
            "discord_form": discord_form,
            "social_links": get_profile_links(profile),
            "link_error": link_error,
            "supported_platforms": URL_INPUT_PLATFORM_LABELS,
            "gravatar_preview_url": gravatar_preview_url,
            "emoji_options": random_emoji_options(4),
            "contact_visibility_choices": VisibilityChoice.choices,
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
            return self._social_links_response(request, profile, DiscordHandleForm())
        return self._social_links_response(request, profile, discord_form)

    def _add_link(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        from urbanlens.dashboard.models.social_link.model import SocialLink
        from urbanlens.dashboard.services.social_links import VERIFIABLE_PLATFORMS, parse_social_link

        raw = request.POST.get("link_input", "").strip()
        result = parse_social_link(raw) if raw else None
        if not result:
            return self._social_links_response(
                request,
                profile,
                DiscordHandleForm(),
                link_error="Couldn't recognise that URL. Try pasting the full profile link.",
            )
        platform, handle = result
        SocialLink.objects.update_or_create(
            profile=profile,
            platform=platform,
            defaults={"handle": handle},
        )
        verify_platform = platform if platform in VERIFIABLE_PLATFORMS else None
        return self._social_links_response(
            request,
            profile,
            DiscordHandleForm(),
            verify_platform=verify_platform,
            verify_handle=handle if verify_platform else None,
        )

    def _remove_link(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        from urbanlens.dashboard.services.social_links import KNOWN_PLATFORMS

        platform = request.POST.get("remove_platform", "")
        if platform in KNOWN_PLATFORMS:
            profile.social_links.filter(platform=platform).delete()
        return self._social_links_response(request, profile, DiscordHandleForm())

    def _social_links_response(
        self,
        request: HttpRequest,
        profile: Profile,
        discord_form: DiscordHandleForm,
        link_error: str = "",
        verify_platform: str | None = None,
        verify_handle: str | None = None,
    ) -> HttpResponse:
        """Return the social-links partial for HTMX requests, or redirect for plain requests."""
        from urbanlens.dashboard.services.social_links import get_profile_links

        if request.headers.get("HX-Request"):
            from urbanlens.dashboard.services.social_links import URL_INPUT_PLATFORM_LABELS

            discord_link = profile.social_links.filter(platform="discord").first()
            if not discord_form.is_bound:
                discord_form = DiscordHandleForm(initial={"discord": discord_link.handle if discord_link else ""})
            return render(
                request,
                "dashboard/partials/profile_social_links.html",
                {
                    "social_links": get_profile_links(profile),
                    "discord_form": discord_form,
                    "link_error": link_error,
                    "verify_platform": verify_platform,
                    "verify_handle": verify_handle,
                    "supported_platforms": URL_INPUT_PLATFORM_LABELS,
                },
            )
        return redirect("profile.edit")


class SocialLinkVerifyView(LoginRequiredMixin, View):
    """Verify that a just-saved social-link URL resolves to a valid profile page.

    Called automatically by HTMX after a new link is added.  Returns 204 when
    the link looks fine; returns 200 with an ``HX-Trigger`` toast payload when
    the remote server indicates the profile does not exist or is unreachable.

    Only verifiable platforms (see ``VERIFIABLE_PLATFORMS``) are checked; the
    view silently returns 204 for anything else so the client never has to
    guard against unrecognised platforms.
    """

    _TIMEOUT_SECONDS = 5
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def get(self, request: HttpRequest) -> HttpResponse:
        """Verify the platform+handle pair and return an optional toast trigger.

        Args:
            request: The HTTP request.  Query params: ``platform``, ``handle``.

        Returns:
            204 when the link appears valid or cannot be determined.
            200 with ``HX-Trigger`` when the link is demonstrably broken.
        """
        from urbanlens.dashboard.services.social_links import (
            PLATFORM_URL_TEMPLATE,
            VERIFIABLE_PLATFORMS,
            validate_handle,
        )

        platform = request.GET.get("platform", "").strip()
        handle = request.GET.get("handle", "").strip()

        if platform not in VERIFIABLE_PLATFORMS or not handle:
            return HttpResponse(status=204)

        if validate_handle(platform, handle) is not None:
            return HttpResponse(status=204)

        template = PLATFORM_URL_TEMPLATE.get(platform)
        if not template:
            return HttpResponse(status=204)

        url = template.format(handle=handle)
        return self._check_url(url)

    def _check_url(self, url: str) -> HttpResponse:
        """Fetch *url* and decide whether to surface a warning toast.

        Args:
            url: The fully-formed profile URL to probe.

        Returns:
            204 when the URL resolves successfully.
            200 with ``HX-Trigger`` when a problem is detected.
        """
        import requests
        from requests.exceptions import RequestException

        try:
            resp = requests.get(
                url,
                timeout=self._TIMEOUT_SECONDS,
                allow_redirects=True,
                stream=True,
                headers={"User-Agent": self._USER_AGENT},
            )
            resp.close()
            status_code = resp.status_code
        except RequestException:
            # Network error, DNS failure, timeout, SSL problem, etc.
            # Don't alarm the user — we simply cannot confirm either way.
            return HttpResponse(status=204)

        if status_code == 404:
            message = "That profile page returned 'not found' – double-check your username."
            level = "warning"
        elif 400 <= status_code < 600:
            message = f"That link returned an unexpected response (HTTP {status_code}). It may be incorrect."
            level = "warning"
        else:
            return HttpResponse(status=204)

        response = HttpResponse(status=200)
        response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
        return response


def _authenticated_profile(request: HttpRequest) -> Profile:
    """Return the authenticated user's Profile.

    LoginRequiredMixin guarantees this path is only reached by authenticated
    users, but mypy sees request.user as User | AnonymousUser.  The isinstance
    guard here makes that explicit and raises PermissionDenied (→ 403) for the
    theoretically-unreachable anonymous case.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    if not isinstance(request.user, User):
        raise PermissionDenied
    return get_object_or_404(Profile, user=request.user)


class ProfileNoteView(LoginRequiredMixin, View):
    """Create a new private note about another profile (HTMX)."""

    def post(self, request: HttpRequest, profile_slug: UUID) -> HttpResponse:
        from urbanlens.dashboard.models.profile.note import ProfileNote

        subject = get_object_or_404(Profile, slug=profile_slug)
        author = _authenticated_profile(request)
        if author == subject:
            return HttpResponse("Cannot annotate your own profile.", status=400)

        content = request.POST.get("content", "").strip()
        if content:
            ProfileNote.objects.create(author=author, subject=subject, content=content)

        return _render_profile_annotation_partial(request, author, subject)


class ProfileNoteDeleteView(LoginRequiredMixin, View):
    """Delete one of the viewer's private notes about another profile (HTMX)."""

    def post(self, request: HttpRequest, profile_slug: UUID, note_id: int) -> HttpResponse:
        from urbanlens.dashboard.models.profile.note import ProfileNote

        subject = get_object_or_404(Profile, slug=profile_slug)
        author = _authenticated_profile(request)
        ProfileNote.objects.filter(pk=note_id, author=author, subject=subject).delete()
        return _render_profile_annotation_partial(request, author, subject)


class ProfileNoteEditView(LoginRequiredMixin, View):
    """Edit (PATCH) the content of an existing private note (HTMX)."""

    def post(self, request: HttpRequest, profile_slug: UUID, note_id: int) -> HttpResponse:
        from urbanlens.dashboard.models.profile.note import ProfileNote

        subject = get_object_or_404(Profile, slug=profile_slug)
        author = _authenticated_profile(request)
        content = request.POST.get("content", "").strip()
        ProfileNote.objects.filter(pk=note_id, author=author, subject=subject).update(content=content)
        return _render_profile_annotation_partial(request, author, subject)


class ProfileBadgeToggleView(LoginRequiredMixin, View):
    """Toggle a user-type badge on another profile (HTMX - re-renders the badge chips)."""

    def post(self, request: HttpRequest, profile_slug: UUID, badge_id: int) -> HttpResponse:
        from urbanlens.dashboard.models.badges.model import KIND_USER, Badge
        from urbanlens.dashboard.models.badges.profile_assignment import ProfileBadgeAssignment

        subject = get_object_or_404(Profile, slug=profile_slug)
        author = _authenticated_profile(request)
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


class ProfileTrustView(LoginRequiredMixin, View):
    """Set or clear the viewer's private trust rating for another profile (HTMX).

    POST with ``rating`` (int 1-5) to set/update; POST with ``rating=0`` or
    omit ``rating`` to clear an existing rating.
    """

    def post(self, request: HttpRequest, profile_slug: UUID) -> HttpResponse:
        from urbanlens.dashboard.models.profile.trust import ProfileTrust

        subject = get_object_or_404(Profile, slug=profile_slug)
        author = _authenticated_profile(request)
        if author == subject:
            return HttpResponse("Cannot rate your own profile.", status=400)

        try:
            rating = int(request.POST.get("rating", 0))
        except (TypeError, ValueError):
            rating = 0

        if 1 <= rating <= 5:
            ProfileTrust.objects.update_or_create(
                author=author,
                subject=subject,
                defaults={"rating": rating},
            )
        else:
            ProfileTrust.objects.filter(author=author, subject=subject).delete()

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
    from urbanlens.dashboard.models.profile.trust import ProfileTrust

    viewer_notes = ProfileNote.objects.filter(author=author, subject=subject)
    user_badges = Badge.objects.user_badges().visible_to(author).ordered()
    assigned_ids = set(
        ProfileBadgeAssignment.objects.filter(author=author, subject=subject).values_list("badge_id", flat=True),
    )
    trust = ProfileTrust.objects.filter(author=author, subject=subject).first()

    return render(
        request,
        "dashboard/partials/profile_annotation_content.html",
        {
            "subject": subject,
            "viewer_notes": viewer_notes,
            "user_badges": user_badges,
            "assigned_badge_ids": assigned_ids,
            "trust_rating": trust.rating if trust else 0,
        },
    )
