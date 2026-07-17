"""Profile view and edit controllers."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import chain
import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.forms.profile_form import (
    DiscordHandleForm,
    ProfileForm,
    validate_birth_date,
    validate_started_exploring,
)
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.labels.meta import KIND_STATUS
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.username import USERNAME_RE, username_is_taken

if TYPE_CHECKING:
    from uuid import UUID

    from urbanlens.dashboard.models.profile.email import ProfileEmail

logger = logging.getLogger(__name__)


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
            "can_view_contact": can_view_contact,
        }
        if request.user == profile.user:
            from urbanlens.dashboard.services.profile_preview import preview_modes

            context["preview_modes"] = preview_modes()
            context.update(self._own_profile_activity_context(profile))
        self._add_common_context(request, profile, context)
        return render(request, "dashboard/pages/profile/index.html", context)

    def _own_profile_activity_context(self, profile: Profile) -> dict[str, object]:
        """Build dashboard strips for the signed-in user's own profile page."""
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.markup.model import MarkupMap
        from urbanlens.dashboard.models.reviews.model import Review
        from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
        from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
        from urbanlens.dashboard.models.undo.model import UndoAction
        from urbanlens.dashboard.models.wiki_edit import WikiEdit

        recent_pin_comments = Comment.objects.filter(profile=profile).select_related("pin", "wiki", "wiki__location").order_by("-created")[:5]
        recent_trip_comments = TripComment.objects.filter(author=profile).select_related("trip").order_by("-created")[:5]
        recent_comments = sorted(
            chain(recent_pin_comments, recent_trip_comments),
            key=lambda comment: comment.created,
            reverse=True,
        )[:5]

        active_checkin_statuses = (
            SafetyCheckinStatus.SCHEDULED,
            SafetyCheckinStatus.AWAITING_CHECKIN,
            SafetyCheckinStatus.OVERDUE,
        )

        maps_count = MarkupMap.objects.for_profile(profile).count()
        photos_count = Image.objects.filter(profile=profile).count()
        comments_count = Comment.objects.filter(profile=profile).count() + TripComment.objects.filter(author=profile).count()
        safety_checkins_count = SafetyCheckin.objects.filter(profile=profile).count() + UndoAction.objects.filter(profile=profile, model_label="safety_checkin").count()
        trips_created_count = Trip.objects.filter(creator=profile).count()
        trips_joined_count = TripMembership.objects.filter(profile=profile).exclude(trip__creator=profile).count()

        return {
            "profile_private_stats": [
                {"label": "Maps created", "value": maps_count, "icon": "gesture"},
                {"label": "Wiki edits", "value": WikiEdit.objects.filter(editor=profile).count(), "icon": "edit_note"},
                {"label": "Safety check-ins", "value": safety_checkins_count, "icon": "emergency_home"},
                {"label": "Trips created", "value": trips_created_count, "icon": "add_road"},
                {"label": "Trips joined", "value": trips_joined_count, "icon": "group_add"},
                {"label": "Photos uploaded", "value": photos_count, "icon": "photo_camera"},
                {"label": "Comments posted", "value": comments_count, "icon": "forum"},
                {"label": "Pins rated", "value": Review.objects.filter(profile=profile).count(), "icon": "star"},
            ],
            "profile_recent_photos": Image.objects.uploaded_by(profile)[:8],
            "profile_recent_pins": Pin.objects.filter(profile=profile).select_related("location").order_by("-created")[:6],
            "profile_recent_markup_maps": MarkupMap.objects.for_profile(profile).prefetch_related("items").order_by("-created")[:6],
            "profile_priority_unvisited_pins": (
                Pin.objects.filter(profile=profile, priority__gt=0, last_visited__isnull=True, visit_history__isnull=True)
                .select_related("location")
                .order_by("-priority", "-updated")[:6]
            ),
            "profile_recent_comments": recent_comments,
            "profile_recent_trips": Trip.objects.recently_active_past(profile, since=timezone.now() - timedelta(days=7)),
            "profile_upcoming_trips": Trip.objects.upcoming(profile).order_by("start_date", "name")[:6],
            "profile_active_checkin": (SafetyCheckin.objects.filter(profile=profile, status__in=active_checkin_statuses).select_related("destination_location").order_by("checkin_by").first()),
        }

    def post(self, request: HttpRequest, **kwargs) -> HttpResponse:
        """Handle avatar upload from the profile hero card."""
        profile = Profile.objects.get(user=request.user)
        if "avatar" in request.FILES:
            profile.avatar = request.FILES["avatar"]
            profile.save(update_fields=["avatar"])
        return redirect("profile.view")

    def _can_view_profile(self, request: HttpRequest, profile: Profile) -> bool:
        """Return True if the requesting user is allowed to view this profile.

        Delegates to :meth:`Profile.can_view_profile` so all relationship
        checks (friends, common pin/friend/trip, anything-in-common) live in
        one place.
        """
        if request.user == profile.user:
            return True

        viewer = None
        if request.user.is_authenticated:
            viewer = Profile.objects.filter(user=request.user).first()
        return profile.can_view_profile(viewer)

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

        # Visited by both - has the protected "Visited" status label, or has a last_visited date
        # TODO: Whatever is happening here is probably wrong.
        visited_filter = Q(labels__name="Visited", labels__kind=KIND_STATUS) | Q(last_visited__isnull=False)
        their_visited_ids = set(
            Pin.objects.filter(profile=profile, location__isnull=False).filter(visited_filter).values_list("location_id", flat=True),
        )
        my_visited_ids = set(
            Pin.objects.filter(profile=my_profile, location__isnull=False).filter(visited_filter).values_list("location_id", flat=True),
        )
        shared_visited_ids = their_visited_ids & my_visited_ids

        context["common_pin_count"] = len(common_ids)
        context["shared_visited"] = Location.objects.filter(id__in=shared_visited_ids).select_related("wiki").order_by("wiki__name", "official_name") if shared_visited_ids else Location.objects.none()

        # Friendship relationship
        from urbanlens.dashboard.models.friendship.model import Friendship

        friendship = Friendship.objects.all().between(my_profile, profile)
        context["friendship"] = friendship
        # Template compares this against lowercase literals ('accepted', 'requested', ...) -
        # FriendshipStatus values are capitalized ("Accepted", "Requested"), so normalize here.
        context["friendship_status"] = friendship.status.lower() if friendship else None
        context["friends_since"] = friendship.updated if friendship and friendship.status == FriendshipStatus.ACCEPTED else None

        # Trips in common
        from urbanlens.dashboard.models.trips.model import TripMembership

        my_trip_ids = set(TripMembership.objects.trip_ids_for(my_profile))
        their_trip_ids = set(TripMembership.objects.trip_ids_for(profile))
        common_trip_ids = my_trip_ids & their_trip_ids
        if common_trip_ids:
            from urbanlens.dashboard.models.trips.model import Trip

            context["trips_in_common"] = Trip.objects.filter(id__in=common_trip_ids).order_by("name")
        else:
            from urbanlens.dashboard.models.trips.model import Trip

            context["trips_in_common"] = Trip.objects.none()

        # Private annotations (notes, user labels, trust rating, nickname) - only when viewing someone else
        from urbanlens.dashboard.models.labels.model import Label
        from urbanlens.dashboard.models.labels.profile_assignment import ProfileLabelAssignment
        from urbanlens.dashboard.models.profile.nickname import ProfileNickname
        from urbanlens.dashboard.models.profile.note import ProfileNote
        from urbanlens.dashboard.models.profile.trust import ProfileTrust

        context["viewer_notes"] = ProfileNote.objects.filter(author=my_profile, subject=profile)
        nickname = ProfileNickname.objects.filter(author=my_profile, subject=profile).first()
        context["nickname"] = nickname.nickname if nickname else ""
        user_labels = Label.objects.user_labels().visible_to(my_profile).ordered()
        assigned_label_ids = set(
            ProfileLabelAssignment.objects.filter(author=my_profile, subject=profile).values_list(
                "label_id",
                flat=True,
            ),
        )
        context["user_labels"] = user_labels
        context["assigned_label_ids"] = assigned_label_ids
        context["unassigned_labels"] = [label for label in user_labels if label.id not in assigned_label_ids]
        trust = ProfileTrust.objects.filter(author=my_profile, subject=profile).first()
        context["trust_rating"] = trust.rating if trust else 0

        from urbanlens.dashboard.controllers.custom_fields import rows_for_target
        from urbanlens.dashboard.models.custom_fields.model import CustomFieldEntity

        context["custom_field_rows"] = rows_for_target(my_profile, CustomFieldEntity.PROFILE, profile)
        context["my_profile"] = my_profile

        # Message button: shown if privacy settings permit it, or an existing
        # conversation already exists (mirrors ConversationView's own gate).
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage
        from urbanlens.dashboard.services.direct_messages import can_direct_message

        context["can_message"] = can_direct_message(my_profile, profile) or DirectMessage.objects.between(my_profile, profile).exists()


class ProfilePreviewStartView(LoginRequiredMixin, View):
    """Start previewing your own profile as a selected type of user.

    Stores the preview state in the session and redirects to the public
    profile URL; ``ProfilePreviewMiddleware`` then renders that page as a
    simulated user with the chosen relationship.
    """

    def post(self, request: HttpRequest, mode: str) -> HttpResponse:
        """Activate a preview session for the given audience.

        Args:
            request: The HTTP request.
            mode: A ``VisibilityChoice`` value selecting the simulated viewer.

        Returns:
            Redirect to the previewed profile page (or back to the profile
            when the mode is unknown).
        """
        from django.urls import reverse

        from urbanlens.dashboard.services.profile_preview import SESSION_KEY, preview_modes

        if mode not in dict(preview_modes()):
            return redirect("profile.view")

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.slug:
            # Slugs are generated on save; force one so the public URL exists.
            profile.save()
        if not profile.slug:
            return redirect("profile.view")

        path = reverse("profile.view_user", kwargs={"profile_slug": profile.slug})
        request.session[SESSION_KEY] = {"mode": mode, "path": path, "owner_id": profile.pk}
        return redirect(path)


class ProfilePreviewStopView(LoginRequiredMixin, View):
    """Exit profile preview mode and return to the normal profile page."""

    def get(self, request: HttpRequest) -> HttpResponse:
        """Clear any active preview session.

        Args:
            request: The HTTP request.

        Returns:
            Redirect to the owner's profile page.
        """
        from urbanlens.dashboard.services.profile_preview import SESSION_KEY

        request.session.pop(SESSION_KEY, None)
        return redirect("profile.view")


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
        if not USERNAME_RE.match(username):
            return JsonResponse({"available": False, "reason": "3-30 characters: letters, numbers, and underscores only"})
        if username_is_taken(username, exclude_user_id=request.user.pk):
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

        if field == "email":
            from urbanlens.dashboard.services.email_normalization import is_email_taken

            value = request.POST.get("value", "").strip()
            if not value:
                return JsonResponse({"error": "Email address is required."}, status=400)
            try:
                validate_email(value)
            except ValidationError:
                return JsonResponse({"error": "Enter a valid email address."}, status=400)
            if is_email_taken(value, exclude_user_id=request.user.pk):
                return JsonResponse({"error": "Another account already uses this email address."}, status=409)
            request.user.email = value
            request.user.save(update_fields=["email"])
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
        if not USERNAME_RE.match(username):
            return JsonResponse({"error": "3-30 characters: letters, numbers, and underscores only."}, status=400)
        if username_is_taken(username, exclude_user_id=request.user.pk):
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

        from urbanlens.dashboard.services.avatar import AvatarService

        email = request.user.email or ""
        if not email:
            return JsonResponse({"error": "No email address on this account."}, status=400)
        digest = hashlib.md5(email.strip().lower().encode(), usedforsecurity=False).hexdigest()
        url = f"https://www.gravatar.com/avatar/{digest}?s=256&d=404"
        img = AvatarService.download(url)
        if not img:
            return JsonResponse({"error": "No Gravatar found for your email address."}, status=404)
        profile.avatar.save(f"gravatar_{request.user.pk}.jpg", ContentFile(img), save=True)
        return JsonResponse({"ok": True, "avatar_url": profile.avatar.url})

    def _save_avatar_emoji(self, request: HttpRequest, profile: Profile) -> JsonResponse:
        from django.core.files.base import ContentFile

        from urbanlens.dashboard.models.colors import MaterialColor
        from urbanlens.dashboard.services.avatar import AvatarService

        animal = request.POST.get("animal", "fox")
        color = request.POST.get("color", "#4CAF50")
        emoji = AvatarService.ANIMAL_EMOJIS.get(animal, "🦊")
        if color.lower() not in {v.lower() for v in MaterialColor.values}:
            color = MaterialColor.GREY.value
        svg = AvatarService.generate_emoji_svg(emoji, color)
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

        from urbanlens.dashboard.services.avatar import AvatarService
        from urbanlens.dashboard.services.profile_preview import preview_modes
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

        return {
            "profile": profile,
            "can_view_contact": True,
            "preview_modes": preview_modes(),
            "form": form,
            "discord_form": discord_form,
            "social_links": get_profile_links(profile),
            "link_error": link_error,
            "supported_platforms": URL_INPUT_PLATFORM_LABELS,
            "gravatar_preview_url": gravatar_preview_url,
            "emoji_options": AvatarService.random_options(4),
            "secondary_emails": profile.secondary_emails.all(),
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
        if action == "add_email":
            return self._add_email(request, profile)
        if action == "remove_email":
            return self._remove_email(request, profile)
        if action == "resend_email_verification":
            return self._resend_email_verification(request, profile)
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
                "dashboard/partials/profile/profile_social_links.html",
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

    def _add_email(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        from urbanlens.dashboard.models.profile.email import ProfileEmail
        from urbanlens.dashboard.services.email_normalization import is_email_taken, normalize_email

        raw = request.POST.get("email_input", "").strip().lower()
        email_error = ""
        try:
            validate_email(raw)
        except ValidationError:
            email_error = "Enter a valid email address."
        else:
            normalized = normalize_email(raw)
            if is_email_taken(raw, exclude_user_id=request.user.pk):
                email_error = "That email address is already in use."
            elif profile.secondary_emails.filter(normalized_email=normalized).exists():
                email_error = "You've already added that email address."
            else:
                secondary_email = ProfileEmail.objects.create(profile=profile, email=raw)
                _send_profile_email_verification(request, secondary_email)
        return self._emails_response(request, profile, email_error=email_error)

    def _remove_email(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        email_id = request.POST.get("email_id", "")
        profile.secondary_emails.filter(pk=email_id).delete()
        return self._emails_response(request, profile)

    def _resend_email_verification(self, request: HttpRequest, profile: Profile) -> HttpResponse:
        email_status = ""
        secondary_email = profile.secondary_emails.filter(pk=request.POST.get("email_id", ""), is_verified=False).first()
        if secondary_email:
            _send_profile_email_verification(request, secondary_email)
            email_status = f"Verification email resent to {secondary_email.email}."
        return self._emails_response(request, profile, email_status=email_status)

    def _emails_response(
        self,
        request: HttpRequest,
        profile: Profile,
        email_error: str = "",
        email_status: str = "",
    ) -> HttpResponse:
        """Return the secondary-emails partial for HTMX requests, or redirect for plain requests."""
        if request.headers.get("HX-Request"):
            return render(
                request,
                "dashboard/partials/profile/profile_emails.html",
                {
                    "secondary_emails": profile.secondary_emails.all(),
                    "email_error": email_error,
                    "email_status": email_status,
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
    _USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

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
            # Don't alarm the user - we simply cannot confirm either way.
            return HttpResponse(status=204)

        if status_code == 404:
            message = "That profile page returned 'not found' - double-check your username."
            level = "warning"
        elif 400 <= status_code < 600:
            message = f"That link returned an unexpected response (HTTP {status_code}). It may be incorrect."
            level = "warning"
        else:
            return HttpResponse(status=204)

        response = HttpResponse(status=200)
        response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
        return response


def _send_profile_email_verification(request: HttpRequest, secondary_email: ProfileEmail) -> None:
    """Email a confirm-ownership link for a newly-added (or re-sent) secondary email.

    Args:
        request: The HTTP request (used to build an absolute verification URL).
        secondary_email: The unverified ``ProfileEmail`` to send a link for.
    """
    import smtplib

    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.urls import reverse

    verify_url = request.build_absolute_uri(
        reverse("profile.email.verify", args=[str(secondary_email.verification_token)]),
    )
    context = {"profile": secondary_email.profile, "verify_url": verify_url}
    subject = "Confirm your email address for UrbanLens"
    text_body = f"Hi {secondary_email.profile.username},\n\nConfirm this email address so it can be used to find your UrbanLens account and to log in:\n{verify_url}\n\nIf you didn't request this, you can ignore this email.\n\n- UrbanLens"
    html_body = render_to_string("dashboard/email/verify_profile_email.html", context)

    try:
        msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[secondary_email.email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send profile email verification to %s", secondary_email.email)


class ProfileEmailVerifyView(View):
    """Click-through from a secondary-email confirmation link (no login required).

    Anyone holding the emailed link can confirm ownership of that inbox, the
    same way the initial signup verification link works - the visitor may not
    be logged in as the owning profile at the time they click it.
    """

    def get(self, request: HttpRequest, token) -> HttpResponse:
        from django.contrib import messages
        from django.db import IntegrityError

        from urbanlens.dashboard.models.profile.email import ProfileEmail

        secondary_email = ProfileEmail.objects.filter(verification_token=token).select_related("profile").first()
        if not secondary_email:
            messages.error(request, "That verification link is invalid or has already been used.")
        elif secondary_email.is_verified:
            messages.info(request, f"{secondary_email.email} is already verified.")
        else:
            try:
                secondary_email.mark_verified()
            except IntegrityError:
                messages.error(request, "That email address is already verified on another account.")
            else:
                # Deliver any friend requests + visit suggestions that were
                # waiting on this address (visit participants tagged by email
                # before this account claimed it).
                from urbanlens.dashboard.services.visit_invites import process_pending_visit_invites

                process_pending_visit_invites(secondary_email.profile.user, email=secondary_email.email)
                messages.success(request, f"{secondary_email.email} is verified and can now be used to find you and to log in.")
        return redirect("profile.edit")


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


class ProfileLabelToggleView(LoginRequiredMixin, View):
    """Toggle a user-type label on another profile (HTMX - re-renders the label chips)."""

    def post(self, request: HttpRequest, profile_slug: UUID, label_id: int) -> HttpResponse:
        from urbanlens.dashboard.models.labels.model import KIND_USER, Label
        from urbanlens.dashboard.models.labels.profile_assignment import ProfileLabelAssignment

        subject = get_object_or_404(Profile, slug=profile_slug)
        author = _authenticated_profile(request)
        if author == subject:
            return HttpResponse("Cannot annotate your own profile.", status=400)

        # visible_to keeps forged ids from attaching (and thereby exposing)
        # another user's private people labels.
        label = get_object_or_404(Label.objects.visible_to(author), pk=label_id, kind=KIND_USER)

        assignment, created = ProfileLabelAssignment.objects.get_or_create(
            author=author,
            subject=subject,
            label=label,
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


class ProfileNicknameView(LoginRequiredMixin, View):
    """Set or clear the viewer's private nickname for another profile (HTMX).

    POST with ``nickname`` to set/update; POST with a blank ``nickname``
    to clear an existing one.
    """

    def post(self, request: HttpRequest, profile_slug: UUID) -> HttpResponse:
        from urbanlens.dashboard.models.profile.nickname import ProfileNickname

        subject = get_object_or_404(Profile, slug=profile_slug)
        author = _authenticated_profile(request)
        if author == subject:
            return HttpResponse("Cannot nickname your own profile.", status=400)

        nickname = request.POST.get("nickname", "").strip()

        if nickname:
            ProfileNickname.objects.update_or_create(
                author=author,
                subject=subject,
                defaults={"nickname": nickname},
            )
        else:
            ProfileNickname.objects.filter(author=author, subject=subject).delete()

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
    from urbanlens.dashboard.controllers.custom_fields import rows_for_target
    from urbanlens.dashboard.models.custom_fields.model import CustomFieldEntity
    from urbanlens.dashboard.models.labels.model import KIND_USER, Label
    from urbanlens.dashboard.models.labels.profile_assignment import ProfileLabelAssignment
    from urbanlens.dashboard.models.profile.nickname import ProfileNickname
    from urbanlens.dashboard.models.profile.note import ProfileNote
    from urbanlens.dashboard.models.profile.trust import ProfileTrust

    viewer_notes = ProfileNote.objects.filter(author=author, subject=subject)
    user_labels = Label.objects.user_labels().visible_to(author).ordered()
    assigned_ids = set(
        ProfileLabelAssignment.objects.filter(author=author, subject=subject).values_list("label_id", flat=True),
    )
    trust = ProfileTrust.objects.filter(author=author, subject=subject).first()
    nickname = ProfileNickname.objects.filter(author=author, subject=subject).first()

    return render(
        request,
        "dashboard/partials/profile/profile_annotation_content.html",
        {
            "subject": subject,
            "viewer_notes": viewer_notes,
            "user_labels": user_labels,
            "assigned_label_ids": assigned_ids,
            "unassigned_labels": [label for label in user_labels if label.id not in assigned_ids],
            "trust_rating": trust.rating if trust else 0,
            "nickname": nickname.nickname if nickname else "",
            "custom_field_rows": rows_for_target(author, CustomFieldEntity.PROFILE, subject),
        },
    )
