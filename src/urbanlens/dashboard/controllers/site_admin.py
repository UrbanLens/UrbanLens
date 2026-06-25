"""Site administration panel controller."""

from __future__ import annotations

import contextlib
from datetime import timedelta
import json
import logging
import os
from pathlib import Path

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.models.site_settings import (
    EnvironmentOverrideChoice,
    SearchProviderChoice,
    SiteSettings,
)
from urbanlens.dashboard.services.site_admin import complete_site_admin_onboarding

logger = logging.getLogger(__name__)


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


def _server_uptime() -> str:
    """Return a human-readable uptime string, or an empty string if unavailable."""
    try:
        seconds = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{days}d {hours}h {minutes}m"
    except (OSError, ValueError):
        return ""


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
        total_locations = Location.objects.count()
        new_locations_30d = Location.objects.filter(created__gte=thirty_days_ago).count()
        total_pins = Pin.objects.count()
        total_photos = Image.objects.count()
        total_friendships = Friendship.objects.count()

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
            Location.objects.annotate_pin_count()
            .order_by("-pin_count")[:10]
            .values("name", "slug", "pin_count"),
        ) if hasattr(Location.objects, "annotate_pin_count") else []

        # ── Time-series chart data ─────────────────────────────────────────────
        user_labels, user_counts = _monthly_series(User.objects, "date_joined")
        location_labels, location_counts = _monthly_series(Location.objects, "created")

        # ── Server stats ──────────────────────────────────────────────────────
        uptime = _server_uptime()
        media_root = getattr(django_settings, "MEDIA_ROOT", "")
        media_size_mb = _dir_size_mb(media_root) if media_root else None

        import sys

        import django
        python_version = sys.version.split()[0]
        django_version = django.__version__

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
            "server_time": now,
        }
        return render(request, "dashboard/pages/site_admin_stats.html", context)
