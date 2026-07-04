from django.contrib import admin, messages
from django.http import HttpRequest, HttpResponseRedirect
from django.urls import reverse

from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.models.location_edit import LocationEdit
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.site_settings import SiteSettings


@admin.register(ApiRateLimit)
class ApiRateLimitAdmin(admin.ModelAdmin):
    """Admin for ApiRateLimit - per-service rate limiting configuration."""

    list_display = ["display_name", "service", "enabled", "calls_per_minute", "calls_per_day", "usa_only"]
    list_editable = ["enabled", "calls_per_minute", "calls_per_day", "usa_only"]
    search_fields = ["service", "display_name"]
    ordering = ["display_name"]


@admin.register(ApiCallLog)
class ApiCallLogAdmin(admin.ModelAdmin):
    """Admin for ApiCallLog - read-only view of API call history."""

    list_display = ["service", "created", "success", "response_ms", "was_rate_limited", "was_geo_filtered"]
    list_filter = ["service", "success", "was_rate_limited", "was_geo_filtered"]
    search_fields = ["service", "endpoint"]
    readonly_fields = ["service", "endpoint", "created", "updated", "success", "response_ms", "was_rate_limited", "was_geo_filtered"]
    ordering = ["-created"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Admin for the SiteSettings singleton.

    Enforces singleton behaviour: the changelist redirects straight to pk=1,
    and add/delete are disabled so only the one record can ever be edited.
    """

    fieldsets = [
        (
            "Environment",
            {
                "fields": ["environment_override"],
                "description": ("Override the deployment environment. Default uses the <code>UL_ENVIRONMENT</code> variable."),
            },
        ),
        (
            "Trip Settings",
            {
                "fields": ["max_trip_members", "max_bbox_area_km2"],
            },
        ),
        (
            "AI - Global",
            {
                "fields": ["ai_enabled", "ai_provider"],
                "description": ("Master controls for all AI features. Disabling <em>AI enabled</em> prevents every AI API call site-wide."),
            },
        ),
        (
            "AI - Models",
            {
                "fields": ["openai_model", "cloudflare_model"],
                "description": ("Model names are only used for the matching provider. Changing the model here takes effect immediately - no restart needed."),
                "classes": ["collapse"],
            },
        ),
        (
            "AI - Feature Toggles",
            {
                "fields": ["ai_category_suggestions_enabled"],
                "description": "Individual feature toggles. The global <em>AI enabled</em> switch overrides all of these.",
            },
        ),
        (
            "Google Places Layer",
            {
                "fields": ["google_places_cache_days"],
                "description": ("Controls the Places layer available to VIP users. Historical landmarks change rarely, so a long cache avoids unnecessary API calls."),
            },
        ),
    ]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: SiteSettings | None = None) -> bool:
        return False

    def changelist_view(self, request: HttpRequest, extra_context: dict | None = None):
        """Skip the list and go straight to the singleton record."""
        obj = SiteSettings.get_current()
        return HttpResponseRedirect(
            reverse("admin:dashboard_sitesettings_change", args=[obj.pk]),
        )


def _delete_unedited_community_pins(modeladmin, request: HttpRequest, queryset) -> None:
    """Delete community detail pins that have never been edited after their initial creation.

    A community detail pin is a Pin with ``parent_location`` set and ``parent_pin``
    null.  "Unedited" is detected by comparing the ``updated`` and ``created``
    timestamps: if they differ by less than 10 seconds, the pin was never saved
    again after its initial INSERT, meaning no user has moved, renamed, or changed
    it.

    NOTE: Community detail pins are created manually by users via the wiki page and
    are NOT auto-recreated by any background process.  Deleting them permanently
    removes them unless a user re-adds them.
    """
    from datetime import timedelta

    from django.db.models import DurationField, ExpressionWrapper, F

    community_pins = queryset.location_detail_pins()
    never_edited = community_pins.annotate(
        age_since_update=ExpressionWrapper(F("updated") - F("created"), output_field=DurationField()),
    ).filter(age_since_update__lt=timedelta(seconds=10))
    count = never_edited.count()
    never_edited.delete()
    modeladmin.message_user(
        request,
        f"Deleted {count} unedited community pin(s). Note: these pins are NOT auto-recreated and must be re-added manually.",
        messages.SUCCESS,
    )


_delete_unedited_community_pins.short_description = "Delete unedited community detail pins"  # type: ignore[attr-defined]


@admin.register(Pin)
class PinAdmin(admin.ModelAdmin):
    """Admin for Pin - primarily useful for bulk operations on community pins."""

    list_display = ["__str__", "profile", "location", "parent_location", "created"]
    list_filter = ["profile"]
    search_fields = ["name", "location__name"]
    readonly_fields = ["uuid", "slug", "created", "updated"]
    actions = [_delete_unedited_community_pins]
