"""Site administration panel controller."""

from __future__ import annotations

import contextlib
from datetime import timedelta
import json
import logging
import os
import sys
import time

import django
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.models.site_settings import (
    EnvironmentOverrideChoice,
    SearchProviderChoice,
    SiteSettings,
)
from urbanlens.dashboard.services.site_admin import SITE_ADMIN_GROUP_NAME, complete_site_admin_onboarding
from urbanlens.UrbanLens.settings.app import settings as app_settings

logger = logging.getLogger(__name__)
_APP_STARTED_MONOTONIC = time.monotonic()


def _monthly_series(queryset, date_field: str, months: int = 12) -> tuple[list[str], list[int]]:
    """Return (labels, counts) for the last ``months`` calendar months."""
    from django.db.models import Count
    from django.db.models.functions import TruncMonth

    now = timezone.now()
    # First day of the oldest month we care about
    start = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=(months - 1) * 30)).replace(day=1)

    rows = (
        queryset.filter(**{f"{date_field}__gte": start})
        .annotate(_month=TruncMonth(date_field))
        .values("_month")
        .annotate(n=Count("id"))
        .order_by("_month")
    )
    by_month = {r["_month"]: r["n"] for r in rows}

    labels: list[str] = []
    counts: list[int] = []
    cursor = start
    for _ in range(months):
        labels.append(cursor.strftime("%b %Y"))
        counts.append(by_month.get(cursor.replace(tzinfo=None), by_month.get(cursor, 0)))
        # Advance to next month
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    return labels, counts


def _format_duration(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{days}d {hours}h {minutes}m"


def _app_uptime() -> str:
    """Return uptime for the current Django app process, not the host server."""
    return _format_duration(max(0, time.monotonic() - _APP_STARTED_MONOTONIC))


def _server_uptime() -> str:
    """Backward-compatible alias for the app uptime metric."""
    return _app_uptime()


def _dir_size_mb(path: str) -> float:
    """Return disk usage of ``path`` in megabytes."""
    total = 0
    with contextlib.suppress(OSError):
        for dirpath, _dirs, files in os.walk(path):
            for fname in files:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(dirpath, fname))
    return round(total / 1_048_576, 1)


class SiteAdminView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Site admin settings page.

    Requires the ``dashboard.view_site_admin`` permission (superusers bypass
    this automatically via Django's permission system).

    GET  /site-admin/  → settings page
    POST /site-admin/  → save settings, re-render page
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def get(self, request):
        settings = SiteSettings.get_current()
        complete_site_admin_onboarding(request.user)
        return render(
            request,
            "dashboard/pages/site_admin.html",
            {
                "settings": settings,
                "page_name": "site-admin",
                "saved": request.GET.get("saved"),
                "search_provider_choices": SearchProviderChoice.choices,
                "environment_override_choices": EnvironmentOverrideChoice.choices,
                "effective_environment_label": settings.get_effective_environment_label(),
                "env_var_environment": os.getenv("UL_ENVIRONMENT", ""),
            },
        )

    def post(self, request):
        settings = SiteSettings.get_current()

        try:
            max_members = int(request.POST.get("max_trip_members", settings.max_trip_members))
            settings.max_trip_members = max(max_members, 1)
        except (ValueError, TypeError):
            pass

        try:
            max_bbox = float(request.POST.get("max_bbox_area_km2", settings.max_bbox_area_km2))
            if max_bbox > 0:
                settings.max_bbox_area_km2 = max_bbox
        except (ValueError, TypeError):
            pass

        app_title = request.POST.get("app_title", "").strip()
        if app_title:
            settings.app_title = app_title

        valid_providers = set(SearchProviderChoice.values)
        provider = request.POST.get("search_provider", "")
        if provider in valid_providers:
            settings.search_provider = provider

        try:
            cache_hours = int(request.POST.get("search_cache_hours", settings.search_cache_hours))
            settings.search_cache_hours = max(0, cache_hours)
        except (ValueError, TypeError):
            pass

        try:
            max_attempts = int(request.POST.get("login_max_attempts", settings.login_max_attempts))
            settings.login_max_attempts = max(0, max_attempts)
        except (ValueError, TypeError):
            pass

        try:
            lockout_minutes = int(request.POST.get("login_lockout_minutes", settings.login_lockout_minutes))
            settings.login_lockout_minutes = max(1, lockout_minutes)
        except (ValueError, TypeError):
            pass

        valid_environments = set(EnvironmentOverrideChoice.values)
        environment = request.POST.get("environment_override", "")
        if environment in valid_environments:
            settings.environment_override = environment

        settings.save()

        return HttpResponseRedirect(reverse("site_admin") + "?saved=1")


class SiteAdminUIComponentsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Development-only UI component showcase for site admins.

    GET /site-admin/ui-components/  → visual reference for reusable UI classes.

    Returns 403 when the effective environment is not development.
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def handle_no_permission(self) -> HttpResponse:
        """Send anonymous users to login; return 403 for authenticated users without permission."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()

    def get(self, request):
        settings = SiteSettings.get_current()
        if not settings.is_development_environment():
            return HttpResponse(status=403)

        return render(
            request,
            "dashboard/pages/site_admin_ui_components.html",
            {"page_name": "site-admin-ui-components"},
        )


class DevToolbarToggleThemeView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Toggle the current user's theme between light and dark (dev toolbar).

    POST /site-admin/dev/toggle-theme/
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def post(self, request):
        from urbanlens.dashboard.models.profile.model import Profile, ThemeChoice

        settings = SiteSettings.get_current()
        if not settings.is_development_environment():
            return HttpResponse(status=403)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if profile.theme_mode == ThemeChoice.DARK:
            profile.theme_mode = ThemeChoice.LIGHT
        else:
            profile.theme_mode = ThemeChoice.DARK
        profile.save(update_fields=["theme_mode"])

        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({"devThemeChanged": profile.theme_mode})
        return response


class DevToolbarToggleMapDarkModeView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Toggle the current user's map dark mode between light and dark (dev toolbar).

    POST /site-admin/dev/toggle-map-dark-mode/
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def post(self, request):
        from urbanlens.dashboard.models.profile.model import Profile, ThemeChoice

        settings = SiteSettings.get_current()
        if not settings.is_development_environment():
            return HttpResponse(status=403)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if profile.map_dark_mode == ThemeChoice.DARK:
            profile.map_dark_mode = ThemeChoice.LIGHT
        else:
            profile.map_dark_mode = ThemeChoice.DARK
        profile.save(update_fields=["map_dark_mode"])

        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({"devMapDarkModeChanged": profile.map_dark_mode})
        return response


class DevToolbarClearSessionView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Flush the Django session (dev toolbar).

    Clears server-side session state and signals the client to wipe ``sessionStorage``
    before reloading. The user will be logged out.

    POST /site-admin/dev/clear-session/
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def post(self, request):
        settings = SiteSettings.get_current()
        if not settings.is_development_environment():
            return HttpResponse(status=403)

        request.session.flush()

        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({"devSessionCleared": True})
        return response


class DevToolbarResetOnboardingView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Restore onboarding tips and hints for the current user (dev toolbar).

    Resets profile guidance to show walkthrough cards again and signals the client
    to clear dismissed onboarding keys from browser storage before reloading.

    POST /site-admin/dev/reset-onboarding/
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def post(self, request):
        from urbanlens.dashboard.models.profile.model import GuidanceLevel, Profile

        settings = SiteSettings.get_current()
        if not settings.is_development_environment():
            return HttpResponse(status=403)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.guidance_level = GuidanceLevel.ALL
        profile.save(update_fields=["guidance_level"])

        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({"devOnboardingReset": True})
        return response


class SiteAdminStatsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Site usage statistics dashboard.

    GET /site-admin/stats/  → read-only stats page with charts.
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def handle_no_permission(self) -> HttpResponse:
        """Send anonymous users to login; return 403 for authenticated users without permission."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()

    def get(self, request):
        from django.conf import settings as django_settings
        from django.contrib.auth.models import User

        from urbanlens.dashboard.models.friendship.model import Friendship
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.model import Profile

        now = timezone.now()
        thirty_days_ago = now - timedelta(days=30)

        # ── Totals ────────────────────────────────────────────────────────────
        total_users = User.objects.count()
        active_users_30d = User.objects.filter(last_login__gte=thirty_days_ago).count()
        new_users_30d = User.objects.filter(date_joined__gte=thirty_days_ago).count()
        total_locations = Location.objects.filter(pins__isnull=False).distinct().count()
        new_locations_30d = Location.objects.filter(pins__isnull=False, created__gte=thirty_days_ago).distinct().count()
        total_pins = Pin.objects.count()
        total_photos = Image.objects.count()
        total_friendships = Friendship.objects.count()
        from urbanlens.dashboard.models.subscriptions import UserSubscription
        total_subscriptions = UserSubscription.objects.filter(revoked_at__isnull=True).count()
        total_site_admins = User.objects.filter(groups__name=SITE_ADMIN_GROUP_NAME).distinct().count()

        # Reviews and trips are optional models - guard against missing tables.
        try:
            from urbanlens.dashboard.models.reviews.model import Review
            total_reviews = Review.objects.count()
        except Exception:
            total_reviews = None

        try:
            from urbanlens.dashboard.models.trips.model import Trip
            total_trips = Trip.objects.count()
            new_trips_30d = Trip.objects.filter(created__gte=thirty_days_ago).count()
        except Exception:
            total_trips = None
            new_trips_30d = None

        avg_pins_per_user = round(total_pins / total_users, 1) if total_users else 0

        # Top locations by pin count
        top_locations = list(
            Location.objects.filter(pins__isnull=False).distinct().annotate_pin_count()
            .order_by("-pin_count")[:10]
            .values("name", "slug", "pin_count"),
        ) if hasattr(Location.objects, "annotate_pin_count") else []

        # ── Time-series chart data ─────────────────────────────────────────────
        user_labels, user_counts = _monthly_series(User.objects, "date_joined")
        location_labels, location_counts = _monthly_series(Location.objects, "created")

        # ── Server stats ──────────────────────────────────────────────────────
        uptime = _app_uptime()
        media_root = getattr(django_settings, "MEDIA_ROOT", "")
        media_size_mb = _dir_size_mb(media_root) if media_root else None

        from urbanlens.core.version import (
            format_short_commit,
            get_current_git_branch,
            get_git_commit_at_start,
            get_git_update_status,
        )

        python_version = sys.version.split()[0]
        django_version = django.__version__
        app_version = app_settings.app_version
        git_update = get_git_update_status(get_git_commit_at_start())

        context = {
            "page_name": "site-admin-stats",
            # Totals
            "total_users": total_users,
            "active_users_30d": active_users_30d,
            "new_users_30d": new_users_30d,
            "total_locations": total_locations,
            "new_locations_30d": new_locations_30d,
            "total_pins": total_pins,
            "total_photos": total_photos,
            "total_friendships": total_friendships,
            "total_subscriptions": total_subscriptions,
            "total_site_admins": total_site_admins,
            "total_reviews": total_reviews,
            "total_trips": total_trips,
            "new_trips_30d": new_trips_30d,
            "avg_pins_per_user": avg_pins_per_user,
            "top_locations": top_locations,
            # Charts (JSON for JS)
            "chart_user_labels": json.dumps(user_labels),
            "chart_user_counts": json.dumps(user_counts),
            "chart_location_labels": json.dumps(location_labels),
            "chart_location_counts": json.dumps(location_counts),
            # Server
            "uptime": uptime,
            "media_size_mb": media_size_mb,
            "python_version": python_version,
            "django_version": django_version,
            "app_version": app_version,
            "git_branch": get_current_git_branch(),
            "deployed_commit_short": format_short_commit(git_update.deployed_commit),
            "current_commit_short": format_short_commit(git_update.current_commit),
            "upstream_commit_short": format_short_commit(git_update.upstream_commit),
            "git_commits_ahead": git_update.commits_ahead,
            "git_has_newer_commits": git_update.has_newer_commits,
            "git_available": git_update.git_available,
            "git_remote_refreshed": git_update.remote_refreshed,
            "show_git_pull_button": (
                SiteSettings.get_current().show_dev_admin_features(request.user)
                and git_update.has_newer_commits
            ),
            "server_time": now,
        }
        return render(request, "dashboard/pages/site_admin_stats.html", context)


class SiteAdminPullLatestCodeView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Development-only endpoint to pull latest code for local/dev deployments."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def post(self, request):
        settings = SiteSettings.get_current()
        if not settings.is_development_environment():
            return JsonResponse(
                {"ok": False, "message": "Pulling code from the admin UI is only available in development."},
                status=403,
            )

        from urbanlens.core.version import (
            apply_pending_migrations,
            get_current_git_commit,
            pull_latest_git_code,
            trigger_development_app_reload,
        )

        before_commit = get_current_git_commit()
        ok, message = pull_latest_git_code()
        after_commit = get_current_git_commit()

        if not ok:
            return JsonResponse({"ok": False, "message": message}, status=500)

        changed = bool(before_commit and after_commit and before_commit != after_commit)
        migration_ok = True
        migration_message = "Database migrations were not needed because the code was already up to date."
        reload_ok = True
        reload_message = "Development server reload was not needed because the code was already up to date."

        if changed:
            migration_ok, migration_message = apply_pending_migrations()
            if not migration_ok:
                return JsonResponse({"ok": False, "message": migration_message, "details": message}, status=500)

            reload_ok, reload_message = trigger_development_app_reload()
            if not reload_ok:
                return JsonResponse({"ok": False, "message": reload_message, "details": message}, status=500)

        return JsonResponse(
            {
                "ok": True,
                "changed": changed,
                "message": (
                    "Code updated, migrations applied, and app reload requested."
                    if changed
                    else "Code is already up to date."
                ),
                "details": message,
                "migration_details": migration_message,
                "reload_details": reload_message,
            },
        )


class SiteAdminSubscriptionsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Manage subscription grants without exposing a general user directory."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True

    def get(self, request):
        from urbanlens.dashboard.models.subscriptions import SubscriptionRole, UserSubscription

        SubscriptionRole.ensure_defaults()
        grants = UserSubscription.objects.filter(granted_by=request.user, revoked_at__isnull=True).select_related("user", "role")
        return render(
            request,
            "dashboard/pages/site_admin_subscriptions.html",
            {
                "page_name": "site-admin-subscriptions",
                "roles": SubscriptionRole.objects.all(),
                "grants": grants,
                "saved": request.GET.get("saved"),
                "error": request.GET.get("error"),
            },
        )

    def post(self, request):
        from urllib.parse import urlencode

        from django.contrib.auth.models import User
        from django.db.models import Q

        from urbanlens.dashboard.models.subscriptions import SubscriptionRole, UserSubscription, grant_subscription

        SubscriptionRole.ensure_defaults()
        action = request.POST.get("action", "grant")
        if action == "revoke":
            UserSubscription.objects.filter(pk=request.POST.get("subscription_id"), granted_by=request.user).update(revoked_at=timezone.now())
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?saved=revoked")

        if action == "update":
            sub = UserSubscription.objects.filter(pk=request.POST.get("subscription_id"), granted_by=request.user).first()
            if sub:
                sub.set_duration_months(_parse_duration_months(request.POST.get("duration_months")))
                sub.save(update_fields=["expires_at", "updated"])
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?saved=updated")

        identifier = request.POST.get("user_identifier", "").strip()
        role = SubscriptionRole.objects.filter(slug=request.POST.get("role_slug", "")).first()
        user = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier), is_active=True).first()
        if not identifier or not role or not user:
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"error": "User or role not found."}))
        grant_subscription(user, role, request.user, _parse_duration_months(request.POST.get("duration_months")))
        return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?saved=granted")


def _parse_duration_months(raw: str | None) -> int | None:
    """Parse form duration; blank/indefinite means no expiry."""
    if not raw or raw == "indefinite":
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None
