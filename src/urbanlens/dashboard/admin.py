from django.contrib import admin, messages
from django.http import HttpRequest, HttpResponseRedirect
from django.urls import reverse

from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.models.facts import Fact, FactEvidence
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.wiki import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit


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
                "fields": ["openai_model", "cloudflare_model", "anthropic_model"],
                "description": ("Model names are only used for the matching provider. Changing the model here takes effect immediately - no restart needed."),
                "classes": ["collapse"],
            },
        ),
        (
            "AI - Feature Toggles",
            {
                "fields": ["ai_category_suggestions_enabled", "ai_document_import_enabled", "ai_document_import_max_chars"],
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
        (
            "Notifications - Channels",
            {
                "fields": ["notify_admin_email", "notify_gotify_url", "notify_gotify_token"],
                "description": ("Where critical site notifications are sent. Email defaults to <code>UL_ADMIN_NOTIFICATION_EMAIL</code>; Gotify defaults to <code>UL_GOTIFY_URL</code> / <code>UL_GOTIFY_TOKEN</code>."),
            },
        ),
        (
            "Notifications - Routing",
            {
                "fields": ["notify_pin_import_errors_email", "notify_pin_import_errors_gotify"],
                "description": "Which critical notification types are sent to which channels above.",
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


@admin.register(Pin)
class PinAdmin(admin.ModelAdmin):
    """Admin for Pin - primarily useful for bulk operations on personal pins."""

    list_display = ["__str__", "profile", "location", "parent_pin", "created"]
    list_filter = ["profile"]
    search_fields = ["name", "location__official_name"]
    readonly_fields = ["uuid", "slug", "created", "updated"]


def _delete_unedited_child_wikis(modeladmin, request: HttpRequest, queryset) -> None:
    """Delete child wikis that have never been edited after their initial creation.

    A child wiki is a Wiki with ``parent_wiki`` set.  "Unedited" is detected by
    comparing the ``updated`` and ``created`` timestamps: if they differ by
    less than 10 seconds, the wiki was never saved again after its initial
    INSERT, meaning no user has moved, renamed, or changed it.

    NOTE: Child wikis are created manually by users via the wiki page and are
    NOT auto-recreated by any background process.  Deleting them permanently
    removes them unless a user re-adds them.
    """
    from datetime import timedelta

    from django.db.models import DurationField, ExpressionWrapper, F

    child_wikis = queryset.child_wikis()
    never_edited = child_wikis.annotate(
        age_since_update=ExpressionWrapper(F("updated") - F("created"), output_field=DurationField()),
    ).filter(age_since_update__lt=timedelta(seconds=10))
    count = never_edited.count()
    never_edited.delete()
    modeladmin.message_user(
        request,
        f"Deleted {count} unedited child wiki(s). Note: these are NOT auto-recreated and must be re-added manually.",
        messages.SUCCESS,
    )


_delete_unedited_child_wikis.short_description = "Delete unedited child wikis"  # type: ignore[attr-defined]


@admin.register(Wiki)
class WikiAdmin(admin.ModelAdmin):
    """Admin for Wiki - primarily useful for bulk operations on child wikis."""

    list_display = ["__str__", "location", "parent_wiki", "created"]
    list_filter = ["parent_wiki"]
    search_fields = ["name", "location__official_name"]
    readonly_fields = ["uuid", "slug", "created", "updated"]
    actions = [_delete_unedited_child_wikis]


@admin.register(WikiEdit)
class WikiEditAdmin(admin.ModelAdmin):
    """Admin for WikiEdit - community wiki edit history."""

    list_display = ["__str__", "wiki", "editor", "reverted", "created"]
    list_filter = ["reverted"]
    search_fields = ["wiki__name"]
    readonly_fields = ["created", "updated"]
    ordering = ["-created"]


class FactEvidenceInline(admin.TabularInline):
    """Read-only inline showing a Fact's recent evidence trail."""

    model = FactEvidence
    extra = 0
    fields = ["source_kind", "source_name", "get_value", "submitter", "submitter_trust_snapshot", "source_reliability", "superseded", "created"]
    readonly_fields = fields
    ordering = ["-created"]

    def has_add_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(Fact)
class FactAdmin(admin.ModelAdmin):
    """Admin for Fact - read-only ops visibility into confidence-tracked facts and their evidence."""

    list_display = ["__str__", "subject_type", "key", "status", "confidence", "evidence_count", "updated"]
    list_filter = ["subject_type", "status", "data_type", "key"]
    search_fields = ["key", "wiki__name", "location__official_name"]
    readonly_fields = [field.name for field in Fact._meta.fields]  # noqa: SLF001
    ordering = ["-updated"]
    inlines = [FactEvidenceInline]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(FactEvidence)
class FactEvidenceAdmin(admin.ModelAdmin):
    """Admin for FactEvidence - searchable by source across every fact, for debugging a specific source's contributions."""

    list_display = ["__str__", "source_kind", "source_name", "submitter", "superseded", "created"]
    list_filter = ["source_kind", "superseded"]
    search_fields = ["source_name", "fact__key"]
    readonly_fields = [field.name for field in FactEvidence._meta.fields]  # noqa: SLF001
    ordering = ["-created"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False
