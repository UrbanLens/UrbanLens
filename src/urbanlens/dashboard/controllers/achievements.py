"""Views for earned achievements and for the site admin's award editor."""

# Generic imports
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

# Django Imports
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.achievements.model import (
    DEFAULT_ACHIEVEMENT_COLOR,
    DEFAULT_ACHIEVEMENT_ICON,
    Achievement,
    UserAchievement,
)
from urbanlens.dashboard.models.labels.model import COLOR_CHOICES, ICON_CATEGORIES
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.services.achievements.evaluate import progress_for_profile
from urbanlens.dashboard.services.achievements.metrics import all_metrics, grouped_metric_choices, streak_summary

# App Imports
from urbanlens.dashboard.services.core.colors import clean_color
from urbanlens.dashboard.services.core.numbers import safe_int

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile
    from django.db.models import QuerySet

logger = logging.getLogger(__name__)

PROFILE_GRID_LIMIT = 12


def _viewer_profile(request: HttpRequest) -> Profile | None:
    """Return the requesting user's profile, or None when they have none."""
    if not request.user.is_authenticated:
        return None
    return Profile.objects.filter(user=request.user).first()


def _visible_profile_or_404(request: HttpRequest, profile_slug: str) -> tuple[Profile, Profile | None]:
    """Return (subject, viewer), raising 404 when the viewer may not see the subject.

    Achievements are shown to exactly the audience that can see the profile
    itself, so this reuses ``Profile.can_view_profile`` rather than inventing a
    second visibility rule that could drift from it.
    """
    profile = get_object_or_404(Profile, slug=profile_slug)
    viewer = _viewer_profile(request)
    if viewer is not None and viewer.pk == profile.pk:
        return profile, viewer
    if not profile.can_view_profile(viewer):
        raise Http404
    return profile, viewer


class ProfileAchievementsView(LoginRequiredMixin, View):
    """HTMX partial: the achievement grid on a profile page.

    GET /profile/<slug>/achievements/
    """

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        profile, viewer = _visible_profile_or_404(request, profile_slug)
        is_owner = viewer is not None and viewer.pk == profile.pk

        earned = list(UserAchievement.objects.for_profile(profile).displayable())
        return render(
            request,
            "dashboard/partials/achievements/_profile_achievements.html",
            {
                "subject": profile,
                "is_owner": is_owner,
                "earned": earned[:PROFILE_GRID_LIMIT],
                "earned_total": len(earned),
                "remaining": max(0, len(earned) - PROFILE_GRID_LIMIT),
                # A 1-day streak is just "you did something today" - not worth surfacing.
                "streaks": [row for row in streak_summary(profile) if max(row["current"], row["longest"]) >= 2],
            },
        )


class AchievementListView(LoginRequiredMixin, View):
    """Full award catalogue for one profile, with progress for the owner.

    GET /profile/<slug>/achievements/all/
    """

    def get(self, request: HttpRequest, profile_slug: str) -> HttpResponse:
        profile, viewer = _visible_profile_or_404(request, profile_slug)
        is_owner = viewer is not None and viewer.pk == profile.pk
        rows = progress_for_profile(profile, viewer=viewer)
        return render(
            request,
            "dashboard/pages/achievements/index.html",
            {
                "subject": profile,
                "is_owner": is_owner,
                "profile_url": reverse("profile.view") if is_owner else reverse("profile.view_user", args=[profile.slug]),
                "rows": rows,
                "earned_count": sum(1 for row in rows if row["earned"]),
                "streaks": streak_summary(profile),
            },
        )


class _AchievementAdminMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Shared permission gate for the site-admin achievement editor."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; return 403 for authenticated non-admins."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()


def _achievement_rows() -> QuerySet[Achievement]:
    """Return every defined award, newest-relevant ordering, with award counts."""
    from django.db.models import Count

    return Achievement.objects.annotate(earned_count=Count("awards")).order_by("order", "metric", "threshold", "name")


def _admin_context(**extra: Any) -> dict[str, Any]:
    """Build the shared context for the site-admin achievements page."""
    achievements = list(_achievement_rows())
    context: dict[str, Any] = {
        "achievements": achievements,
        "stats": {
            "total": len(achievements),
            "active": sum(1 for a in achievements if a.is_active),
            "secret": sum(1 for a in achievements if a.is_secret),
            "earned_total": UserAchievement.objects.count(),
        },
        "metric_groups": grouped_metric_choices(),
        "metrics": all_metrics(),
        "icon_categories": ICON_CATEGORIES,
        "color_choices": COLOR_CHOICES,
        "default_icon": DEFAULT_ACHIEVEMENT_ICON,
        "default_color": DEFAULT_ACHIEVEMENT_COLOR,
        "active": "achievements",
    }
    context.update(extra)
    return context


def _uploaded_custom_icon(request: HttpRequest) -> UploadedFile | None:
    """Return the submitted custom-icon file, if any.

    Mirrors ``labels._uploaded_custom_icon``: the icon picker partial names its
    file input ``custom_icon-<picker_id>`` (scoped per widget instance), so the
    create form and every row's edit form can share one page without their
    uploads colliding on field name.
    """
    for field_name in request.FILES:
        if field_name == "custom_icon" or field_name.startswith("custom_icon-"):
            return request.FILES.get(field_name)
    return None


def _apply_form(achievement: Achievement, request: HttpRequest) -> None:
    """Copy submitted form values onto *achievement* without saving it.

    Args:
        achievement: The instance to populate.
        request: The request carrying the POSTed form.

    Raises:
        ValidationError: When a required field is missing or unparseable, so the
            caller can report it without a half-populated row being written.
    """
    name = (request.POST.get("name") or "").strip()
    if not name:
        raise ValidationError({"name": "Name is required."})

    raw_threshold = (request.POST.get("threshold") or "").strip()
    try:
        threshold = int(raw_threshold)
    except ValueError as exc:
        raise ValidationError({"threshold": "Threshold must be a whole number."}) from exc

    achievement.name = name
    achievement.description = (request.POST.get("description") or "").strip() or None
    # Truncated to the column widths: these are assigned straight from POST, and
    # CharField max_length is enforced by full_clean(), which save() does not call -
    # so an over-long value reached the database and returned a 500.
    achievement.metric = (request.POST.get("metric") or "").strip()[: Achievement._meta.get_field("metric").max_length]  # noqa: SLF001 - _meta is public API
    achievement.threshold = threshold
    achievement.icon = (request.POST.get("icon") or "").strip()[: Achievement._meta.get_field("icon").max_length] or None  # noqa: SLF001
    achievement.color = clean_color(request.POST.get("color"), default=DEFAULT_ACHIEVEMENT_COLOR)
    achievement.order = safe_int(request.POST.get("order"))
    achievement.is_active = request.POST.get("is_active") == "on"
    achievement.is_secret = request.POST.get("is_secret") == "on"

    if uploaded := _uploaded_custom_icon(request):
        from urbanlens.dashboard.models.images.model import MediaKind
        from urbanlens.dashboard.services.media.images import image_upload_error

        upload_error = image_upload_error(uploaded, MediaKind.PHOTO)
        if upload_error:
            raise ValidationError({"custom_icon": upload_error[0]})
        achievement.custom_icon = uploaded
    elif request.POST.get("clear_custom_icon"):
        achievement.custom_icon = None

    achievement.full_clean(exclude=["slug", "uuid"])


class SiteAdminAchievementsView(_AchievementAdminMixin, View):
    """Site-admin page for defining awards.

    GET  /site-admin/achievements/  → list and the create form
    POST /site-admin/achievements/  → create one award
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "dashboard/pages/site_admin_achievements.html", _admin_context())

    def post(self, request: HttpRequest) -> HttpResponse:
        achievement = Achievement()
        try:
            _apply_form(achievement, request)
        except ValidationError as exc:
            return render(
                request,
                "dashboard/partials/admin/_achievement_rows.html",
                _admin_context(form_errors=exc.message_dict),
                status=400,
            )

        achievement.save()
        messages.success(request, f"Created achievement “{achievement.name}”. Backfilling existing users…")
        return render(request, "dashboard/partials/admin/_achievement_rows.html", _admin_context())


class SiteAdminAchievementEditView(_AchievementAdminMixin, View):
    """Edit or delete one award.

    POST   /site-admin/achievements/<id>/  → save changes
    DELETE /site-admin/achievements/<id>/  → delete the award and every grant of it
    """

    def post(self, request: HttpRequest, achievement_id: int) -> HttpResponse:
        achievement = get_object_or_404(Achievement, pk=achievement_id)
        try:
            _apply_form(achievement, request)
        except ValidationError as exc:
            return render(
                request,
                "dashboard/partials/admin/_achievement_rows.html",
                _admin_context(form_errors=exc.message_dict),
                status=400,
            )

        achievement.save()
        messages.success(request, f"Updated achievement “{achievement.name}”.")
        return render(request, "dashboard/partials/admin/_achievement_rows.html", _admin_context())

    def delete(self, request: HttpRequest, achievement_id: int) -> HttpResponse:
        achievement = get_object_or_404(Achievement, pk=achievement_id)
        name = achievement.name
        # Cascades to every UserAchievement. Deactivating is the non-destructive
        # option and is what the UI steers toward; this is the deliberate escape
        # hatch for an award created by mistake.
        achievement.delete()
        messages.success(request, f"Deleted achievement “{name}”.")
        return render(request, "dashboard/partials/admin/_achievement_rows.html", _admin_context())


class SiteAdminAchievementBackfillView(_AchievementAdminMixin, View):
    """Re-check one award against every profile.

    POST /site-admin/achievements/<id>/backfill/
    """

    def post(self, request: HttpRequest, achievement_id: int) -> HttpResponse:
        from urbanlens.dashboard.services.achievements.evaluate import evaluate_achievement_for_all

        achievement = get_object_or_404(Achievement, pk=achievement_id)
        granted = evaluate_achievement_for_all(achievement)
        messages.success(request, f"“{achievement.name}” granted to {granted} more user(s).")
        return render(request, "dashboard/partials/admin/_achievement_rows.html", _admin_context())


class AchievementRedirectView(LoginRequiredMixin, View):
    """Send a bare /achievements/ request to the viewer's own catalogue."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _viewer_profile(request)
        if profile is None:
            raise Http404
        return redirect("achievement.list", profile_slug=profile.slug)
