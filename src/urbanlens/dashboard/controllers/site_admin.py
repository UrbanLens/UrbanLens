"""
Site administration panel controller.

TODO: I could be mistaken, but I believe the override of handle_no_permission is not necessary throughout this file.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
import json
import logging
import os
import re
import sys
import time
from urllib.parse import urlencode

import django
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.db.models import CharField, Q
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.http.response import HttpResponseForbidden
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.models.site_settings import (
    EnvironmentOverrideChoice,
    SearchProviderChoice,
    SiteSettings,
)
from urbanlens.dashboard.services.infrastructure_stats import _format_duration
from urbanlens.dashboard.services.json_safety import safe_json_for_script
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

    rows = queryset.filter(**{f"{date_field}__gte": start}).annotate(_month=TruncMonth(date_field)).values("_month").annotate(n=Count("id")).order_by("_month")
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


def _app_uptime() -> str:
    """Return uptime for the current Django app process, not the host server."""
    return _format_duration(max(0, time.monotonic() - _APP_STARTED_MONOTONIC))


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

    GET  /site-admin/settings/  → settings page
    POST /site-admin/settings/  → save settings, re-render page
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def get(self, request: HttpRequest):
        # The user should never be anonymous, but just in case.
        if not isinstance(request.user, User):
            return HttpResponseForbidden()

        from urbanlens.dashboard.plugins.registry import plugin_registry

        settings = SiteSettings.get_current()
        complete_site_admin_onboarding(request.user)

        # Ranked sources first, in the admin's configured priority order, followed
        # by any remaining available sources (unranked, falling back to plugin
        # order at resolution time).
        priority_slugs = settings.name_source_priority_list
        providers_by_slug = {provider.source: provider.verbose_name for provider in plugin_registry.name_providers()}
        name_source_order = [(slug, providers_by_slug[slug], True) for slug in priority_slugs if slug in providers_by_slug]
        ranked_slugs = {slug for slug, _label, _ranked in name_source_order}
        name_source_order += [(slug, label, False) for slug, label in providers_by_slug.items() if slug not in ranked_slugs]

        from urbanlens.dashboard.services.enrichment import enrichment_sources, last_run_summary, self_reported_skip

        enrichment_source_rows = [
            {
                "key": source.key,
                "verbose_name": source.verbose_name or source.key,
                "services": ", ".join(source.service_keys),
                "available": self_reported_skip(source) is None,
            }
            for source in enrichment_sources()
        ]

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
                "name_source_order": name_source_order,
                "enrichment_sources": enrichment_source_rows,
                "enrichment_last_run": last_run_summary(),
            },
        )

    def post(self, request: HttpRequest):
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

        for limit_field in (
            "max_trip_activities",
            "max_upcoming_trips_per_user",
            "max_pins_per_list",
            "max_friends_per_user",
            "max_group_chat_members",
            "max_safety_checkin_contacts",
        ):
            if limit_field in request.POST:
                with contextlib.suppress(ValueError, TypeError):
                    setattr(settings, limit_field, max(0, int(request.POST.get(limit_field, getattr(settings, limit_field)))))

        app_title = request.POST.get("app_title", "").strip()
        if app_title:
            settings.app_title = app_title

        valid_providers = set(SearchProviderChoice.values)
        provider = request.POST.get("search_provider", "")
        if provider in valid_providers:
            settings.search_provider = provider

        try:
            cache_days = int(request.POST.get("external_data_cache_days", settings.external_data_cache_days))
            settings.external_data_cache_days = max(1, cache_days)
        except (ValueError, TypeError):
            pass

        if "default_name_source_priority" in request.POST:
            # Unknown slugs are tolerated: the name resolver ignores sources it
            # never sees, so a disabled plugin's slug can stay configured.
            slugs = [token.strip().lower() for token in request.POST.get("default_name_source_priority", "").split(",")]
            settings.default_name_source_priority = ",".join(slug for slug in slugs if re.fullmatch(r"[a-z0-9_-]+", slug))

        if "enrichment_enabled" in request.POST:
            settings.enrichment_enabled = request.POST.get("enrichment_enabled") in {"1", "true", "on", "True"}
        for enrichment_field, low, high in (
            ("enrichment_start_hour", 0, 23),
            ("enrichment_end_hour", 0, 23),
            ("enrichment_buffer_percent", 0, 90),
            ("enrichment_max_per_service_per_run", 1, 500),
        ):
            if enrichment_field in request.POST:
                with contextlib.suppress(ValueError, TypeError):
                    setattr(settings, enrichment_field, min(max(low, int(request.POST.get(enrichment_field, getattr(settings, enrichment_field)))), high))

        if "backup_enabled" in request.POST or "backup_frequency_hours" in request.POST or "backup_retention" in request.POST:
            settings.backup_enabled = request.POST.get("backup_enabled") in {"1", "true", "on", "True"}
            with contextlib.suppress(ValueError, TypeError):
                settings.backup_frequency_hours = max(1, int(request.POST.get("backup_frequency_hours", settings.backup_frequency_hours)))
            with contextlib.suppress(ValueError, TypeError):
                settings.backup_retention = max(1, int(request.POST.get("backup_retention", settings.backup_retention)))

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

        if "signup_restricted" in request.POST:
            settings.signup_restricted = request.POST.get("signup_restricted") in {"1", "true", "on", "True"}

        for email_limit_field in ("email_limit_per_hour", "email_limit_per_day", "email_limit_per_month"):
            if email_limit_field in request.POST:
                with contextlib.suppress(ValueError, TypeError):
                    setattr(settings, email_limit_field, max(0, int(request.POST.get(email_limit_field, getattr(settings, email_limit_field)))))

        if "storage_quota_gb" in request.POST:
            with contextlib.suppress(ValueError, TypeError):
                settings.storage_quota_gb = max(0, int(request.POST.get("storage_quota_gb", settings.storage_quota_gb)))
        if "image_downscale_enabled" in request.POST:
            settings.image_downscale_enabled = request.POST.get("image_downscale_enabled") in {"1", "true", "on", "True"}
        if "image_downscale_max_dimension" in request.POST:
            with contextlib.suppress(ValueError, TypeError):
                settings.image_downscale_max_dimension = min(max(256, int(request.POST.get("image_downscale_max_dimension", settings.image_downscale_max_dimension))), 20_000)
        if "image_convert_webp" in request.POST:
            settings.image_convert_webp = request.POST.get("image_convert_webp") in {"1", "true", "on", "True"}
        if "image_downscale_vip" in request.POST:
            settings.image_downscale_vip = request.POST.get("image_downscale_vip") in {"1", "true", "on", "True"}
        if "max_upload_file_size_mb" in request.POST:
            with contextlib.suppress(ValueError, TypeError):
                settings.max_upload_file_size_mb = max(1, int(request.POST.get("max_upload_file_size_mb", settings.max_upload_file_size_mb)))
        if "video_downscale_enabled" in request.POST:
            settings.video_downscale_enabled = request.POST.get("video_downscale_enabled") in {"1", "true", "on", "True"}
        if "video_downscale_max_height" in request.POST:
            with contextlib.suppress(ValueError, TypeError):
                settings.video_downscale_max_height = min(max(240, int(request.POST.get("video_downscale_max_height", settings.video_downscale_max_height))), 8_000)
        if "video_downscale_vip" in request.POST:
            settings.video_downscale_vip = request.POST.get("video_downscale_vip") in {"1", "true", "on", "True"}

        settings.save()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            # Several numeric fields above are silently clamped into range rather
            # than rejected (e.g. image_downscale_max_dimension to [256, 20000]) -
            # report back what was actually persisted so the autosave JS can
            # repaint the input instead of leaving it showing the raw value the
            # admin typed while claiming "Saved".
            clamped_fields = (
                "max_trip_members",
                "max_bbox_area_km2",
                "external_data_cache_days",
                "login_max_attempts",
                "login_lockout_minutes",
                "backup_frequency_hours",
                "backup_retention",
                "email_limit_per_hour",
                "email_limit_per_day",
                "email_limit_per_month",
                "storage_quota_gb",
                "image_downscale_max_dimension",
                "max_upload_file_size_mb",
                "video_downscale_max_height",
                "max_trip_activities",
                "max_upcoming_trips_per_user",
                "max_pins_per_list",
                "max_friends_per_user",
                "max_group_chat_members",
                "max_safety_checkin_contacts",
                "enrichment_start_hour",
                "enrichment_end_hour",
                "enrichment_buffer_percent",
                "enrichment_max_per_service_per_run",
            )
            values = {field: getattr(settings, field) for field in clamped_fields if field in request.POST}
            return JsonResponse({"ok": True, "values": values})
        return HttpResponseRedirect(reverse("site_admin") + "?saved=1")


class SiteAdminUIComponentsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Development-only UI component showcase for site admins.

    GET /site-admin/ui-components/  → visual reference for reusable UI classes.

    Returns 403 when the effective environment is not development.
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; return 403 for authenticated users without permission."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()

    def get(self, request: HttpRequest):
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
    request: HttpRequest

    def post(self, request: HttpRequest):
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
    request: HttpRequest

    def post(self, request: HttpRequest):
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
    request: HttpRequest

    def post(self, request: HttpRequest):
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
    request: HttpRequest

    def post(self, request: HttpRequest):
        from urbanlens.dashboard.models.profile.model import GuidanceLevel, Profile

        settings = SiteSettings.get_current()
        if not settings.is_development_environment():
            return HttpResponse(status=403)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.guidance_level = GuidanceLevel.ALL
        profile.welcome_onboarding_complete = False
        profile.save(update_fields=["guidance_level", "welcome_onboarding_complete"])

        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({"devOnboardingReset": True})
        return response


class SiteAdminStatsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Site usage statistics dashboard.

    GET /site-admin/stats/  → read-only stats page with charts.
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; return 403 for authenticated users without permission."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()

    def get(self, request: HttpRequest):
        from django.contrib.auth.models import User

        from urbanlens.dashboard.models.location.model import Location

        now = timezone.now()

        # Only run fast chart queries here; heavy data is fetched by HTMX partials.
        user_labels, user_counts = _monthly_series(User.objects, "date_joined")
        location_labels, location_counts = _monthly_series(Location.objects, "created")

        return render(
            request,
            "dashboard/pages/site_admin_stats.html",
            {
                "page_name": "site-admin-stats",
                "server_time": now,
                "chart_user_labels": safe_json_for_script(user_labels),
                "chart_user_counts": safe_json_for_script(user_counts),
                "chart_location_labels": safe_json_for_script(location_labels),
                "chart_location_counts": safe_json_for_script(location_counts),
            },
        )


class SiteAdminPullLatestCodeView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Development-only endpoint to enqueue code updates for local/dev deployments."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def post(self, request: HttpRequest):
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
        migration_message = "Database migrations were not needed because the code was already up to date."
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
                "message": "Code updated, migrations applied, and app reload requested." if changed else "Code is already up to date.",
                "details": message,
                "migration_details": migration_message,
                "reload_details": reload_message,
            },
            status=200,
        )


class SiteAdminSubscriptionsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Manage subscription grants without exposing a general user directory."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def get(self, request: HttpRequest):
        from urbanlens.dashboard.models.site_settings import SiteSettings
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, UserSubscription

        SubscriptionRole.ensure_defaults()
        grants = UserSubscription.objects.granted_by_admin(request.user).select_related("user", "role")
        return render(
            request,
            "dashboard/pages/site_admin_subscriptions.html",
            {
                "page_name": "site-admin-subscriptions",
                "roles": SubscriptionRole.objects.all(),
                "grants": grants,
                "site_features": SiteFeature.choices,
                "site_settings": SiteSettings.get_current(),
                "saved": request.GET.get("saved"),
                "error": request.GET.get("error"),
            },
        )

    def _grants_list_response(self, request: HttpRequest, *, toast: tuple[str, str] | None = None) -> HttpResponse:
        """Re-render the "Your active grants" partial, optionally with a toast.

        Args:
            request: The current request (used for ``request.user`` scoping).
            toast: Optional (level, message) toast to trigger via HX-Trigger.

        Returns:
            The rendered grants-list partial.
        """
        from urbanlens.dashboard.models.subscriptions import UserSubscription

        grants = UserSubscription.objects.granted_by_admin(request.user).select_related("user", "role")
        response = render(request, "dashboard/partials/site_admin/_subscription_grants_list.html", {"grants": grants})
        if toast:
            response["HX-Trigger"] = json.dumps({"showToast": {"level": toast[0], "message": toast[1]}})
        return response

    def post(self, request: HttpRequest):
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, UserSubscription, grant_subscription

        if not isinstance(request.user, User):
            return HttpResponseForbidden()

        SubscriptionRole.ensure_defaults()
        is_htmx = bool(request.headers.get("HX-Request"))
        action = request.POST.get("action", "grant")

        if action == "revoke":
            UserSubscription.objects.filter(pk=request.POST.get("subscription_id"), granted_by=request.user).update(revoked_at=timezone.now())
            if is_htmx:
                return self._grants_list_response(request, toast=("info", "Subscription revoked."))
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?saved=revoked")

        if action == "update":
            sub = UserSubscription.objects.filter(pk=request.POST.get("subscription_id"), granted_by=request.user).first()
            if sub:
                sub.set_duration_months(_parse_duration_months(request.POST.get("duration_months")))
                sub.save(update_fields=["expires_at", "updated"])
            if is_htmx:
                return self._grants_list_response(request, toast=("success", "Subscription updated."))
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?saved=updated")

        if action == "role_quota":
            role = SubscriptionRole.objects.get_by_slug(request.POST.get("role_slug", ""))
            if role is None:
                if is_htmx:
                    response = HttpResponse(status=404)
                    response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": "Role not found - no changes saved."}})
                    return response
                return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"error": "Role not found."}))
            raw_quota = (request.POST.get("storage_quota_gb") or "").strip()
            if raw_quota == "":
                quota = None
            else:
                try:
                    quota = max(0, int(raw_quota))
                except (ValueError, TypeError):
                    if is_htmx:
                        response = HttpResponse(status=400)
                        response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": "Storage quota must be a whole number of GB."}})
                        return response
                    return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"error": "Storage quota must be a whole number of GB."}))
            SubscriptionRole.objects.filter(pk=role.pk).update(storage_quota_gb=quota)
            if is_htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps({"roleSettingsSaved": {"field_group": "role_quota", "role": role.slug}})
                return response
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"saved": "storage quota saved"}))

        if action == "role_email_limits":
            role = SubscriptionRole.objects.get_by_slug(request.POST.get("role_slug", ""))
            if role is None:
                if is_htmx:
                    response = HttpResponse(status=404)
                    response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": "Role not found - no changes saved."}})
                    return response
                return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"error": "Role not found."}))
            updates: dict[str, int | None] = {}
            for field in ("email_limit_per_hour", "email_limit_per_day", "email_limit_per_month"):
                raw = (request.POST.get(field) or "").strip()
                if raw == "":
                    updates[field] = None
                    continue
                try:
                    updates[field] = max(0, int(raw))
                except (ValueError, TypeError):
                    if is_htmx:
                        response = HttpResponse(status=400)
                        response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": "Email limits must be whole numbers."}})
                        return response
                    return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"error": "Email limits must be whole numbers."}))
            SubscriptionRole.objects.filter(pk=role.pk).update(**updates)
            if is_htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps({"roleSettingsSaved": {"field_group": "role_email_limits", "role": role.slug}})
                return response
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"saved": "email limits saved"}))

        if action == "role_features":
            role = SubscriptionRole.objects.get_by_slug(request.POST.get("role_slug", ""))
            if role is None:
                if is_htmx:
                    response = HttpResponse(status=404)
                    response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": "Role not found - no changes saved."}})
                    return response
                return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"error": "Role not found."}))
            valid_features = set(SiteFeature.values)
            selected = sorted(value for value in request.POST.getlist("features") if value in valid_features)
            SubscriptionRole.objects.filter(pk=role.pk).update(features=",".join(selected))
            if is_htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps({"roleSettingsSaved": {"field_group": "role_features", "role": role.slug}})
                return response
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"saved": "features saved"}))

        if action == "default_features":
            from urbanlens.dashboard.models.site_settings import SiteSettings

            settings_obj = SiteSettings.get_current()
            valid_features = set(SiteFeature.values)
            selected = sorted(value for value in request.POST.getlist("features") if value in valid_features)
            SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=",".join(selected))
            if is_htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps({"roleSettingsSaved": {"field_group": "default_features", "role": "__default__"}})
                return response
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"saved": "default features saved"}))

        identifier = request.POST.get("user_identifier", "").strip()
        role = SubscriptionRole.objects.get_by_slug(request.POST.get("role_slug", ""))
        user = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier), is_active=True).first()
        if not identifier or not role or not user:
            if is_htmx:
                response = self._grants_list_response(request, toast=("error", "User or role not found."))
                response.status_code = 400
                return response
            return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?" + urlencode({"error": "User or role not found."}))
        grant_subscription(user, role, request.user, _parse_duration_months(request.POST.get("duration_months")))
        if is_htmx:
            return self._grants_list_response(request, toast=("success", f"Granted {role.name} to {user.username}."))
        return HttpResponseRedirect(reverse("site_admin_subscriptions") + "?saved=granted")


def _parse_duration_months(raw: str | None) -> int | None:
    """Parse form duration; blank/indefinite means no expiry."""
    if not raw or raw == "indefinite":
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


#: Groups services into tabs on the API limits page. No such taxonomy exists
#: on ServiceDefaults/ApiRateLimit itself (plugins declare only display_name/
#: limits/usa_only/notes) - this is a manually curated, best-effort mapping
#: rather than an exhaustive one; anything absent falls into "Other" so a new
#: service is never hidden, just uncategorized until someone adds it here.
_API_LIMIT_CATEGORIES: dict[str, str] = {
    # Geocoding & Places
    "google_geocoding": "Geocoding & Places",
    "google_places": "Geocoding & Places",
    "nominatim": "Geocoding & Places",
    "photon": "Geocoding & Places",
    "datagov": "Geocoding & Places",
    # Search & News
    "google_search": "Search & News",
    "brave_search": "Search & News",
    "news": "Search & News",
    "gdelt": "Search & News",
    "marginalia_search": "Search & News",
    "mojeek_search": "Search & News",
    "searxng": "Search & News",
    "duckduckgo": "Search & News",
    # Imagery & Maps
    "azure_maps": "Imagery & Maps",
    "bing_maps": "Imagery & Maps",
    "google_maps": "Imagery & Maps",
    "apple_maps": "Imagery & Maps",
    "mapbox": "Imagery & Maps",
    "opentopomap": "Imagery & Maps",
    "esri": "Imagery & Maps",
    "nasa_gibs": "Imagery & Maps",
    "open_aerial_map": "Imagery & Maps",
    "panoramax": "Imagery & Maps",
    "mapillary": "Imagery & Maps",
    "kartaview": "Imagery & Maps",
    "google_earth": "Imagery & Maps",
    "osrm": "Imagery & Maps",
    "routexl": "Imagery & Maps",
    "overture_building_attributes": "Imagery & Maps",
    # Weather
    "openweathermap": "Weather",
    "open_meteo": "Weather",
    # Boundaries & GIS
    "overpass": "Boundaries & GIS",
    "census_tigerweb": "Boundaries & GIS",
    "openhistoricalmap": "Boundaries & GIS",
    "open_elevation": "Boundaries & GIS",
    # Reference & Archives
    "wikipedia": "Reference & Archives",
    "wikimedia": "Reference & Archives",
    "smithsonian": "Reference & Archives",
    "digital_commonwealth": "Reference & Archives",
    "library_of_congress": "Reference & Archives",
    "internet_archive": "Reference & Archives",
    "wayback_machine": "Reference & Archives",
    # Parks & Regulatory
    "nps": "Parks & Regulatory",
    "epa_echo": "Parks & Regulatory",
    "usgs": "Parks & Regulatory",
    "usgs_earthquakes": "Parks & Regulatory",
    "inaturalist": "Parks & Regulatory",
    # Business & Places Data
    "yelp": "Business & Places Data",
    "loopnet": "Business & Places Data",
    # Notifications
    "sms": "Notifications",
    "whatsapp": "Notifications",
    "hibp": "Notifications",
    # Personal Media & Accounts
    "flickr": "Personal Media & Accounts",
    "google_photos": "Personal Media & Accounts",
    "immich": "Personal Media & Accounts",
    "google_calendar": "Personal Media & Accounts",
    # AI
    "ollama": "AI",
}


class SiteAdminApiLimitsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """API rate limit configuration page.

    GET  /site-admin/api-limits/  → view and edit per-service rate limits
    POST /site-admin/api-limits/  → save one or more service configs
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; return 403 for authenticated users without permission."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()

    def _get_all_configs(self):
        """Return ApiRateLimit rows for every known service, creating missing ones."""
        from urbanlens.dashboard.services.rate_limiter import all_service_defaults, get_limit_config

        return [get_limit_config(key) for key in sorted(all_service_defaults())]

    def get(self, request: HttpRequest):
        from urbanlens.dashboard.models.api_call_log import ApiCallLog

        configs = self._get_all_configs()

        # Build a quick usage summary indexed by service key for the last 30 days
        summaries = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

        enriched = []
        categories: dict[str, int] = {}
        for cfg in configs:
            summary = summaries.get(cfg.service, {})
            category = _API_LIMIT_CATEGORIES.get(cfg.service, "Other")
            categories[category] = categories.get(category, 0) + 1
            enriched.append(
                {
                    "config": cfg,
                    "category": category,
                    "calls_30d": summary.get("total", 0),
                    "blocked_30d": summary.get("blocked", 0),
                    "geo_skipped_30d": summary.get("geo_skipped", 0),
                    "errors_30d": summary.get("errors", 0),
                    "avg_ms": round(summary.get("avg_response_ms") or 0),
                }
            )

        # "Other" always sorts last - it's the uncategorized catch-all, not a
        # real grouping a user would look for by name.
        tab_order = sorted(categories, key=lambda name: (name == "Other", name))
        tabs = [{"name": name, "count": categories[name]} for name in tab_order]

        return render(
            request,
            "dashboard/pages/site_admin_api_limits.html",
            {
                "page_name": "site-admin-api-limits",
                "services": enriched,
                "tabs": tabs,
            },
        )

    @staticmethod
    def _apply_rate_limit_config(cfg, post_data) -> None:
        """Apply POSTed rate-limit fields to an ``ApiRateLimit`` row."""
        cfg.enabled = post_data.get("enabled") in {"1", "true", "on", "True"}
        cfg.usa_only = post_data.get("usa_only") in {"1", "true", "on", "True"}

        try:
            raw_per_min = post_data.get("calls_per_minute", "").strip()
            cfg.calls_per_minute = int(raw_per_min) if raw_per_min else None
        except (ValueError, TypeError):
            pass

        try:
            raw_per_day = post_data.get("calls_per_day", "").strip()
            cfg.calls_per_day = int(raw_per_day) if raw_per_day else None
        except (ValueError, TypeError):
            pass

        try:
            raw_per_30_days = post_data.get("calls_per_30_days", "").strip()
            cfg.calls_per_30_days = int(raw_per_30_days) if raw_per_30_days else None
        except (ValueError, TypeError):
            pass

        cfg.notes = post_data.get("notes", "").strip()

    def post(self, request: HttpRequest):
        from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit

        service = request.POST.get("service", "").strip()
        cfg = ApiRateLimit.objects.filter(service=service).first()
        is_htmx = bool(request.headers.get("HX-Request"))

        if not cfg:
            if is_htmx:
                response = HttpResponse(status=404)
                response["HX-Trigger"] = json.dumps(
                    {
                        "showToast": {"level": "error", "message": "Service not found - no changes saved."},
                    }
                )
                return response
            return HttpResponseRedirect(reverse("site_admin_api_limits") + "?saved=error")

        self._apply_rate_limit_config(cfg, request.POST)
        cfg.save()

        if is_htmx:
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps({"apiLimitSaved": {"service": service}})
            return response

        return HttpResponseRedirect(reverse("site_admin_api_limits") + "?saved=1")


class SiteAdminPluginsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Read-only listing of every discovered UrbanLens plugin.

    GET /site-admin/plugins/ → plugin inventory page

    Shows each plugin's metadata, discovery source, contributions, and the
    enabled state of the services it declares. Per-service runtime toggles
    live on the API limits page; install-level plugin disabling is done via
    the ``UL_DISABLED_PLUGINS`` env setting.
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; return 403 for authenticated users without permission."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()

    def get(self, request: HttpRequest):
        from urbanlens.dashboard.plugins import plugin_registry
        from urbanlens.dashboard.services.rate_limiter import get_limit_config

        entries = []
        for info in plugin_registry.plugins():
            plugin = info.plugin
            plugin_enabled = plugin_registry.is_enabled(plugin.name)
            services = []
            if plugin_enabled:
                for service_key in sorted(plugin.get_service_defaults()):
                    config = get_limit_config(service_key)
                    services.append({"key": service_key, "enabled": config.enabled})
            entries.append(
                {
                    "plugin": plugin,
                    "source": info.source,
                    "module": info.module,
                    "enabled": plugin_enabled,
                    "services": services,
                    "panel_count": len(plugin.get_panel_sources()) if plugin_enabled else 0,
                    "satellite_count": len(plugin.get_satellite_providers()) if plugin_enabled else 0,
                    "street_count": len(plugin.get_street_view_providers()) if plugin_enabled else 0,
                    "name_sources": [provider.source for provider in plugin.get_name_providers()] if plugin_enabled else [],
                }
            )

        return render(
            request,
            "dashboard/pages/site_admin_plugins.html",
            {
                "page_name": "site-admin-plugins",
                "plugins": entries,
            },
        )


class SiteAdminUsersView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Read-only directory of registered users for site administrators.

    GET /site-admin/users/ → paginated, searchable list of users.

    This is deliberately privacy-preserving: even a site admin does not get a
    backdoor around a user's ``contact_visibility`` setting here. Email is
    only shown when the viewing admin's own profile would satisfy that
    user's configured visibility rule, exactly as
    ``Profile.can_view_contact_info`` evaluates for any other viewer (e.g. a
    "Friends only" user's email stays hidden unless the admin happens to be
    their friend).
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest
    PAGE_SIZE = 25

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; return 403 for authenticated non-admins."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()

    def get(self, request: HttpRequest):
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.subscriptions import active_subscription_roles
        from urbanlens.dashboard.services.pagination import get_page
        from urbanlens.dashboard.services.storage import get_quota_bytes, get_storage_used_bytes

        if not isinstance(request.user, User):
            return HttpResponseForbidden()

        search = request.GET.get("q", "").strip()
        users_qs = User.objects.select_related("profile").prefetch_related("groups").order_by("username")
        if search:
            users_qs = users_qs.filter(Q(username__icontains=search) | Q(email__icontains=search) | Q(first_name__icontains=search))

        page = get_page(request, users_qs, self.PAGE_SIZE)

        viewer_profile, _ = Profile.objects.get_or_create(user=request.user)

        rows = []
        for member in page.object_list:
            profile = getattr(member, "profile", None)
            if profile is None:
                # Legacy/incomplete accounts without a Profile row yet - every
                # user should still appear in the directory.
                profile, _ = Profile.objects.get_or_create(user=member)

            email_visible = profile.can_view_contact_info(viewer_profile)
            profile_visible = profile.can_view_profile(viewer_profile)
            quota_bytes = get_quota_bytes(profile)
            used_bytes = get_storage_used_bytes(profile)
            percent_used = 0
            if quota_bytes:
                percent_used = min(round(used_bytes * 100 / quota_bytes), 100)

            rows.append(
                {
                    "user": member,
                    "profile": profile,
                    "profile_visible": profile_visible,
                    "display_username": member.username if profile_visible else "Invisible User",
                    "display_first_name": member.first_name if profile_visible else "",
                    "email_visible": email_visible,
                    "is_site_admin": member.is_superuser or any(group.name == SITE_ADMIN_GROUP_NAME for group in member.groups.all()),
                    "roles": active_subscription_roles(member),
                    "quota_bytes": quota_bytes,
                    "used_bytes": used_bytes,
                    "percent_used": percent_used,
                    "avatar_hue": sum(ord(char) for char in member.username) % 360,
                    "is_pending_deletion": profile.is_pending_deletion,
                    "deletion_scheduled_for": profile.deletion_scheduled_for,
                }
            )

        return render(
            request,
            "dashboard/pages/site_admin_users.html",
            {
                "page_name": "site-admin-users",
                "rows": rows,
                "page_obj": page,
                "search": search,
                "total_users": page.paginator.count,
            },
        )

    def post(self, request: HttpRequest):
        """Admin-initiated account deletion, sharing the self-service flow's grace period and undo.

        Reuses ``request_deletion``/``cancel_deletion`` verbatim - a deletion
        triggered here goes through the exact same 7-day grace period,
        reminder, and hard-delete task as a user deleting their own data, and
        can be undone the same way (either the user logging back in, or an
        admin using ``cancel_delete`` here).
        """
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.account_deletion import cancel_deletion, request_deletion

        if not isinstance(request.user, User):
            return HttpResponseForbidden()

        redirect_params = {k: v for k, v in {"q": request.POST.get("q", ""), "page": request.POST.get("page", "")}.items() if v}
        redirect_url = reverse("site_admin_users") + (f"?{urlencode(redirect_params)}" if redirect_params else "")

        target = User.objects.filter(pk=request.POST.get("user_id")).select_related("profile").first()
        if target is None:
            messages.error(request, "User not found.")
            return HttpResponseRedirect(redirect_url)

        profile, _ = Profile.objects.get_or_create(user=target)
        viewer_profile, _ = Profile.objects.get_or_create(user=request.user)
        profile_visible = profile.can_view_profile(viewer_profile)
        display_username = target.username if profile_visible else "this hidden user"

        action = request.POST.get("action")

        if action == "cancel_delete":
            cancel_deletion(profile)
            messages.success(request, f"Deletion cancelled for {display_username}.")
            return HttpResponseRedirect(redirect_url)

        if action == "request_delete":
            if target.pk == request.user.pk:
                messages.error(request, 'Use "Delete my data" in your own settings to delete your own account.')
                return HttpResponseRedirect(redirect_url)
            if target.is_superuser or any(group.name == SITE_ADMIN_GROUP_NAME for group in target.groups.all()):
                messages.error(request, "Admin accounts can't be deleted this way.")
                return HttpResponseRedirect(redirect_url)

            expected = target.username if profile_visible else "hidden user"
            confirm_text = (request.POST.get("confirm_text") or "").strip()
            if confirm_text.lower() != expected.lower():
                messages.error(request, f'Type "{expected}" exactly to confirm.')
                return HttpResponseRedirect(redirect_url)

            request_deletion(profile)
            messages.success(
                request,
                f"{display_username.capitalize()}'s account is scheduled for deletion on {profile.deletion_scheduled_for:%B %d, %Y}. You (or they) can undo this any time before then.",
            )
            return HttpResponseRedirect(redirect_url)

        return HttpResponseRedirect(redirect_url)


class SiteAdminHomeView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Admin dashboard homepage.

    GET /site-admin/  → navigation hub with quick stats and health overview.
    """

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

    def get(self, request: HttpRequest):
        from django.contrib.auth.models import User

        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        now = timezone.now()
        thirty_days_ago = now - timedelta(days=30)

        total_users = User.objects.count()
        active_users_30d = User.objects.filter(last_login__gte=thirty_days_ago).count()
        new_users_30d = User.objects.filter(date_joined__gte=thirty_days_ago).count()
        total_locations = Location.objects.distinct().count()
        total_pins = Pin.objects.count()
        total_photos = Image.objects.count()

        total_subscriptions = 0
        with contextlib.suppress(Exception):
            from urbanlens.dashboard.models.subscriptions import UserSubscription

            total_subscriptions = UserSubscription.objects.not_revoked().count()

        site_settings = SiteSettings.get_current()

        # Service health (Postgres/Valkey/Celery/nginx pings) and the git
        # update check (a `git fetch` against the remote, only cached for the
        # life of this worker process) are both real I/O, not DB lookups -
        # fetched by SiteAdminHomeStatusPartialView below instead of blocking
        # this page's initial render, same as the /site-admin/stats/ page
        # already lazy-loads its own system panel.
        return render(
            request,
            "dashboard/pages/site_admin_home.html",
            {
                "page_name": "site-admin-home",
                "server_time": now,
                "total_users": total_users,
                "active_users_30d": active_users_30d,
                "new_users_30d": new_users_30d,
                "total_locations": total_locations,
                "total_pins": total_pins,
                "total_photos": total_photos,
                "total_subscriptions": total_subscriptions,
                "app_version": app_settings.app_version,
                "show_dev_toolbar": site_settings.show_dev_admin_features(request.user),
            },
        )


class _AdminPermissionMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Shared mixin for all site-admin HTMX partial views.

    Enforces ``dashboard.view_site_admin`` permission and redirects anonymous
    users to the login page rather than returning 403.
    """

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


class SiteAdminHomeStatusPartialView(_AdminPermissionMixin, View):
    """HTMX partial: infrastructure health + git update badges for the admin home page.

    GET /site-admin/status/

    Split out of ``SiteAdminHomeView`` because ``collect_infrastructure_service_stats``
    pings Postgres/Valkey/Celery/nginx and ``get_git_update_status`` runs a
    ``git fetch`` - real I/O that shouldn't block the page's initial render.
    """

    def get(self, request: HttpRequest):
        from urbanlens.core.version import get_current_git_branch, get_git_commit_at_start, get_git_update_status
        from urbanlens.dashboard.services.infrastructure_stats import collect_infrastructure_service_stats

        infra_services = collect_infrastructure_service_stats()
        unhealthy_count = sum(1 for s in infra_services if s.status == "unhealthy")

        git_update = get_git_update_status(get_git_commit_at_start())

        return render(
            request,
            "dashboard/partials/admin/admin_home_status.html",
            {
                "unhealthy_services": unhealthy_count,
                "total_services": len(infra_services),
                "git_has_newer_commits": git_update.has_newer_commits,
                "git_available": git_update.git_available,
                "git_branch": get_current_git_branch(),
            },
        )


class SiteAdminStatsKpiPartialView(_AdminPermissionMixin, View):
    """HTMX partial: KPI cards + top locations for the stats page.

    GET /site-admin/stats/kpi/
    """

    def get(self, request: HttpRequest):
        from django.contrib.auth.models import User

        from urbanlens.dashboard.models.friendship.model import Friendship
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        now = timezone.now()
        thirty_days_ago = now - timedelta(days=30)

        total_users = User.objects.count()
        active_users_30d = User.objects.filter(last_login__gte=thirty_days_ago).count()
        new_users_30d = User.objects.filter(date_joined__gte=thirty_days_ago).count()
        total_locations = Location.objects.distinct().count()
        new_locations_30d = Location.objects.filter(created__gte=thirty_days_ago).distinct().count()
        total_pins = Pin.objects.count()
        total_photos = Image.objects.count()
        total_friendships = Friendship.objects.count()
        avg_pins_per_user = round(total_pins / total_users, 1) if total_users else 0

        total_subscriptions = 0
        with contextlib.suppress(Exception):
            from urbanlens.dashboard.models.subscriptions import UserSubscription

            total_subscriptions = UserSubscription.objects.not_revoked().count()

        total_site_admins = User.objects.filter(groups__name=SITE_ADMIN_GROUP_NAME).distinct().count()

        total_reviews = None
        with contextlib.suppress(Exception):
            from urbanlens.dashboard.models.reviews.model import Review

            total_reviews = Review.objects.count()

        total_trips = None
        new_trips_30d = None
        with contextlib.suppress(Exception):
            from urbanlens.dashboard.models.trips.model import Trip

            total_trips = Trip.objects.count()
            new_trips_30d = Trip.objects.filter(created__gte=thirty_days_ago).count()

        top_locations: list = []
        with contextlib.suppress(Exception):
            from urbanlens.dashboard.models.location.model import Location as Loc

            if hasattr(Loc.objects, "annotate_pin_count"):
                top_locations = list(
                    Loc.objects.filter(pins__isnull=False)
                    .distinct()
                    .annotate_pin_count()
                    .annotate(display_name=Coalesce("wiki__name", "official_name", output_field=CharField()))
                    .order_by("-pin_count")[:10]
                    .values("display_name", "slug", "pin_count"),
                )

        return render(
            request,
            "dashboard/partials/admin/admin_stats_kpi.html",
            {
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
            },
        )


class SiteAdminStatsSystemPartialView(_AdminPermissionMixin, View):
    """HTMX partial: Application, infrastructure services, and server health.

    GET /site-admin/stats/system/
    """

    def get(self, request: HttpRequest):
        from django.conf import settings as django_settings

        from urbanlens.core.version import (
            format_short_commit,
            get_current_git_branch,
            get_git_commit_at_start,
            get_git_update_status,
        )
        from urbanlens.dashboard.services.backups import collect_backup_stats
        from urbanlens.dashboard.services.infrastructure_stats import collect_infrastructure_service_stats

        uptime = _app_uptime()
        media_root = getattr(django_settings, "MEDIA_ROOT", "")
        media_size_mb = _dir_size_mb(media_root) if media_root else None

        git_update = get_git_update_status(get_git_commit_at_start())

        return render(
            request,
            "dashboard/partials/admin/admin_stats_system.html",
            {
                "uptime": uptime,
                "media_size_mb": media_size_mb,
                "python_version": sys.version.split()[0],
                "django_version": django.__version__,
                "app_version": app_settings.app_version,
                "git_branch": get_current_git_branch(),
                "deployed_commit_short": format_short_commit(git_update.deployed_commit),
                "current_commit_short": format_short_commit(git_update.current_commit),
                "upstream_commit_short": format_short_commit(git_update.upstream_commit),
                "git_commits_ahead": git_update.commits_ahead,
                "git_has_newer_commits": git_update.has_newer_commits,
                "git_available": git_update.git_available,
                "git_remote_refreshed": git_update.remote_refreshed,
                "show_git_pull_button": (SiteSettings.get_current().show_dev_admin_features(request.user) and git_update.has_newer_commits),
                "infrastructure_services": collect_infrastructure_service_stats(),
                "backup_stats": collect_backup_stats(),
            },
        )


class SiteAdminStatsApiUsagePartialView(_AdminPermissionMixin, View):
    """HTMX partial: External API usage table for the stats page.

    GET /site-admin/stats/api/
    """

    def get(self, request: HttpRequest):
        from urbanlens.dashboard.services.rate_limiter import all_service_defaults

        # all_service_defaults() (SERVICE_REGISTRY + every plugin's own
        # get_service_defaults()) - the static registry alone only covers the
        # handful of integrations not yet converted to plugins, which would
        # silently omit the great majority of this app's API usage/cost data.
        service_defaults = all_service_defaults()
        api_usage: list[dict] = []
        total_cost_30d = None
        with contextlib.suppress(Exception):
            from urbanlens.dashboard.models.api_call_log import ApiCallLog
            from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit

            summaries = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}
            rate_configs = {r.service: r for r in ApiRateLimit.objects.all()}

            for svc in sorted(service_defaults):
                cfg = rate_configs.get(svc)
                row = summaries.get(svc, {})
                cost_30d = row.get("total_cost")
                if cost_30d is not None:
                    total_cost_30d = cost_30d if total_cost_30d is None else total_cost_30d + cost_30d
                api_usage.append(
                    {
                        "service": svc,
                        "display_name": cfg.display_name if cfg else service_defaults[svc].display_name,
                        "enabled": cfg.enabled if cfg else True,
                        "calls_per_day": cfg.calls_per_day if cfg else service_defaults[svc].calls_per_day,
                        "usa_only": cfg.usa_only if cfg else service_defaults[svc].usa_only,
                        "total": row.get("total", 0),
                        "blocked": row.get("blocked", 0),
                        "geo_skipped": row.get("geo_skipped", 0),
                        "errors": row.get("errors", 0),
                        "avg_ms": round(row.get("avg_response_ms") or 0),
                        "cost_30d": cost_30d,
                    }
                )

        return render(
            request,
            "dashboard/partials/admin/admin_stats_api_usage.html",
            {"api_usage": api_usage, "total_cost_30d": total_cost_30d},
        )


class CeleryTaskStatusView(LoginRequiredMixin, View):
    """Return normalized Celery task progress for polling progress bars."""

    def get(self, request, task_id: str):
        from urbanlens.dashboard.services.celery import get_task_progress

        try:
            return JsonResponse(get_task_progress(task_id).as_dict())
        except Exception:
            logger.exception("Failed to read Celery task progress for task %s", task_id)
            return JsonResponse(
                {
                    "task_id": task_id,
                    "state": "UNKNOWN",
                    "current": 0,
                    "total": 1,
                    "percent": 0,
                    "message": "Unable to read task status.",
                    "result": None,
                    "error": "Unable to read task status.",
                    "ready": True,
                },
                status=503,
            )
