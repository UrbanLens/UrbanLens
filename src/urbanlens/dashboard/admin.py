from django.contrib import admin
from django.http import HttpRequest, HttpResponseRedirect
from django.urls import reverse

from urbanlens.dashboard.models.site_settings import SiteSettings


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
                "description": (
                    "Override the deployment environment. "
                    "Default uses the <code>UL_ENVIRONMENT</code> variable."
                ),
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
                "description": (
                    "Master controls for all AI features. "
                    "Disabling <em>AI enabled</em> prevents every AI API call site-wide."
                ),
            },
        ),
        (
            "AI - Models",
            {
                "fields": ["openai_model", "cloudflare_model"],
                "description": (
                    "Model names are only used for the matching provider. "
                    "Changing the model here takes effect immediately - no restart needed."
                ),
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
