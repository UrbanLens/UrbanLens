"""SiteSettings model - site-wide configurable settings."""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import SET_NULL, CheckConstraint, FloatField, ForeignKey, IntegerField, Q
from django.db.models.fields import BooleanField, CharField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.site_settings.meta import (
    AI_PROVIDER_CHOICES,
    AI_PROVIDER_CLOUDFLARE,
    DEFAULT_CLOUDFLARE_MODEL,
    DEFAULT_OPENAI_MODEL,
    SEARCH_PROVIDER_BRAVE,
    SEARCH_PROVIDER_CHOICES,
)
from urbanlens.dashboard.models.site_settings.queryset import SiteSettingsManager


class SiteSettings(abstract.Model):
    """Singleton model for site-wide configurable settings.

    Always access via ``SiteSettings.get_current()``; never instantiate directly.
    """

    # --- Trip settings ---

    max_trip_members = IntegerField(
        default=10,
        help_text="Maximum number of members allowed per trip.",
        validators=[MinValueValidator(0.0), MaxValueValidator(100.0)],
    )

    # Chernobyl Exclusion Zone ≈ 2,600 km².  Used as a sanity cap on how large
    # a user-drawn bounding box for a location can be.
    max_bbox_area_km2 = FloatField(
        default=1.0,
        help_text="Maximum allowed area (km²) for a location bounding box. Hard Maximum ≈ Chernobyl Exclusion Zone.",
        validators=[MinValueValidator(0.0), MaxValueValidator(2600.0)],
    )

    # --- AI - Global controls ---

    ai_enabled = BooleanField(
        default=True,
        help_text="Master toggle for all AI features. Disabling this prevents all AI API calls.",
        verbose_name="AI enabled",
    )
    ai_provider = CharField(
        max_length=20,
        choices=AI_PROVIDER_CHOICES,
        default=AI_PROVIDER_CLOUDFLARE,
        help_text="Which AI provider to use for all AI-powered features.",
        verbose_name="AI provider",
    )

    # --- AI - Model selection ---

    openai_model = CharField(
        max_length=100,
        default=DEFAULT_OPENAI_MODEL,
        help_text="OpenAI model name (e.g. gpt-4o, gpt-4o-mini, gpt-5-nano). Only used when provider is OpenAI.",
        verbose_name="OpenAI model",
    )
    cloudflare_model = CharField(
        max_length=200,
        default=DEFAULT_CLOUDFLARE_MODEL,
        help_text="Cloudflare Workers AI model name. Only used when provider is Cloudflare.",
        verbose_name="Cloudflare model",
    )

    # --- AI - Feature toggles ---

    ai_category_suggestions_enabled = BooleanField(
        default=True,
        help_text="Allow AI to suggest categories for pins and locations based on their metadata.",
        verbose_name="Category suggestions",
    )

    # --- Search provider ---

    search_provider = CharField(
        max_length=20,
        choices=SEARCH_PROVIDER_CHOICES,
        default=SEARCH_PROVIDER_BRAVE,
        help_text="Which web search provider to use for pin news/search results.",
        verbose_name="Search provider",
    )

    search_cache_hours = IntegerField(
        default=24,
        help_text="How many hours to cache web search results per pin before re-fetching. Set to 0 to disable caching.",
        verbose_name="Search cache duration (hours)",
    )

    # --- Bootstrap admin ---

    bootstrap_admin_user = ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="+",
        help_text="The first user created on this site; receives site admin and a one-time setup redirect.",
    )
    bootstrap_admin_onboarding_complete = BooleanField(
        default=False,
        help_text="True once the bootstrap admin has visited the site admin settings page.",
    )

    objects = SiteSettingsManager()

    def __str__(self) -> str:
        return "Site Settings"

    @classmethod
    def get_current(cls) -> SiteSettings:
        """Return (and create if missing) the singleton settings record."""
        return cls.objects.get_current()

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_site_settings"
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

        constraints = [
            CheckConstraint(condition=Q(max_bbox_area_km2__lte=2600.0), name="max_bbox_area_lte_2600"),
            CheckConstraint(condition=Q(max_trip_members__gte=1), name="max_trip_members_gte_1"),
            CheckConstraint(condition=Q(max_trip_members__lte=100), name="max_trip_members_lte_100"),
        ]
