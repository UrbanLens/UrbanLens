"""SiteSettings model - site-wide configurable settings."""

from __future__ import annotations

from uuid import uuid4

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import SET_NULL, CheckConstraint, FloatField, ForeignKey, IntegerField, Q, UUIDField
from django.db.models.fields import BooleanField, CharField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.site_settings.meta import (
    DEFAULT_CLOUDFLARE_MODEL,
    DEFAULT_OPENAI_MODEL,
    AiProviderChoice,
    EnvironmentOverrideChoice,
    SearchProviderChoice,
)
from urbanlens.dashboard.models.site_settings.queryset import SiteSettingsManager
from urbanlens.UrbanLens.environments.factory import select_environment
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes


class SiteSettings(abstract.Model):
    """Singleton model for site-wide configurable settings.

    Always access via ``SiteSettings.get_current()``; never instantiate directly.
    """

    # Regenerated each time the database is wiped or the app is redeployed from scratch.
    # Clients embed this in their local pin cache; a mismatch signals a stale cache that
    # must be cleared (avoids ghost pins appearing after a DB reset).
    instance_uuid = UUIDField(default=uuid4, unique=True, editable=False)

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
        choices=AiProviderChoice.choices,
        default=AiProviderChoice.CLOUDFLARE,
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
        choices=SearchProviderChoice.choices,
        default=SearchProviderChoice.BRAVE,
        help_text="Which web search provider to use for pin news/search results.",
        verbose_name="Search provider",
    )

    search_cache_hours = IntegerField(
        default=24,
        help_text="How many hours to cache web search results per pin before re-fetching. Set to 0 to disable caching.",
        verbose_name="Search cache duration (hours)",
    )

    # --- Environment ---

    environment_override = CharField(
        max_length=20,
        choices=EnvironmentOverrideChoice.choices,
        default=EnvironmentOverrideChoice.DEFAULT,
        help_text=(
            "Override the deployment environment. "
            "Default uses the UL_ENVIRONMENT variable (or local when unset)."
        ),
        verbose_name="Environment",
    )

    # --- Branding ---

    app_title = CharField(
        max_length=100,
        default="UrbanLens",
        help_text="Name shown in the browser tab and navigation. Change this to brand your own instance.",
        verbose_name="App title",
    )

    # --- Login rate limiting ---

    login_max_attempts = IntegerField(
        default=5,
        help_text=(
            "Maximum number of consecutive failed login attempts before an account is temporarily locked. "
            "Set to 0 to disable rate limiting."
        ),
        verbose_name="Max failed login attempts",
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    login_lockout_minutes = IntegerField(
        default=15,
        help_text="How many minutes a locked account must wait before login attempts are accepted again.",
        verbose_name="Lockout duration (minutes)",
        validators=[MinValueValidator(1), MaxValueValidator(1440)],
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

    def get_effective_environment_type(self) -> EnvironmentTypes:
        """Return the active environment type, honoring admin override when set.

        When ``environment_override`` is ``default``, the value comes from
        ``UL_ENVIRONMENT`` (falling back to local when unset).

        Returns:
            The resolved ``EnvironmentTypes`` value for this site.
        """
        if self.environment_override and self.environment_override != EnvironmentOverrideChoice.DEFAULT:
            env_type = EnvironmentOverrideChoice.to_environment_type(self.environment_override)
            if env_type is not None:
                return env_type
        return select_environment(None).env_type

    def get_effective_environment_label(self) -> str:
        """Return a human-readable label for the active environment.

        Returns:
            Display label such as ``Development`` or ``Production``.
        """
        if self.environment_override and self.environment_override != EnvironmentOverrideChoice.DEFAULT:
            return EnvironmentOverrideChoice(self.environment_override).label
        env = select_environment(None)
        return env.name.replace("_", " ").title()

    def is_development_environment(self) -> bool:
        """Return whether the site is running in development mode.

        Returns:
            True when the effective environment type is ``development``.
        """
        return self.get_effective_environment_type() == EnvironmentTypes.DEVELOPMENT

    def show_dev_admin_features(self, user) -> bool:
        """Return whether dev-only admin UI should be visible to ``user``.

        Args:
            user: The current request user.

        Returns:
            True for site admins when the effective environment is development.
        """
        return (
            user.is_authenticated
            and user.has_perm("dashboard.view_site_admin")
            and self.is_development_environment()
        )

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_site_settings"
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

        constraints = [
            CheckConstraint(condition=Q(max_bbox_area_km2__lte=2600.0), name="max_bbox_area_lte_2600"),
            CheckConstraint(condition=Q(max_trip_members__gte=1), name="max_trip_members_gte_1"),
            CheckConstraint(condition=Q(max_trip_members__lte=100), name="max_trip_members_lte_100"),
            CheckConstraint(condition=Q(login_max_attempts__gte=0), name="login_max_attempts_gte_0"),
            CheckConstraint(condition=Q(login_lockout_minutes__gte=1), name="login_lockout_minutes_gte_1"),
        ]
