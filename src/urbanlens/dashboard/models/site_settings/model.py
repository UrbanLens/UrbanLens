"""SiteSettings model - site-wide configurable settings."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import SET_NULL, CheckConstraint, FloatField, ForeignKey, IntegerField, Q
from django.db.models.fields import BooleanField, CharField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField
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

if TYPE_CHECKING:
    from urbanlens.dashboard.models.subscriptions.model import SiteFeature


class SiteSettings(abstract.FrontendDashboardModel):
    """Singleton model for site-wide configurable settings.

    Always access via ``SiteSettings.get_current()``; never instantiate directly.
    """

    # --- Trip settings ---

    max_trip_members = IntegerField(
        default=10,
        help_text="Maximum number of members allowed per trip.",
        validators=[MinValueValidator(0.0), MaxValueValidator(100.0)],
    )
    max_trip_activities = IntegerField(
        default=100,
        help_text="Maximum number of activities allowed per trip. Set to 0 for unlimited.",
        verbose_name="Max activities per trip",
        validators=[MinValueValidator(0), MaxValueValidator(10_000)],
    )
    max_upcoming_trips_per_user = IntegerField(
        default=100,
        help_text="Maximum number of upcoming (or undated, still-planning) trips a user may belong to at once. Set to 0 for unlimited.",
        verbose_name="Max upcoming trips per user",
        validators=[MinValueValidator(0), MaxValueValidator(10_000)],
    )

    # --- Pin lists ---

    max_pins_per_list = IntegerField(
        default=0,
        help_text="Maximum number of pins allowed in a single list. Set to 0 for unlimited.",
        verbose_name="Max pins per list",
        validators=[MinValueValidator(0), MaxValueValidator(1_000_000)],
    )

    # --- Friendships ---

    max_friends_per_user = IntegerField(
        default=0,
        help_text="Maximum number of friends a user may have. Set to 0 for unlimited.",
        verbose_name="Max friends per user",
        validators=[MinValueValidator(0), MaxValueValidator(1_000_000)],
    )

    # --- Direct message group chats ---

    max_group_chat_members = IntegerField(
        default=20,
        help_text="Maximum number of members allowed in a direct message group chat. Set to 0 for unlimited.",
        verbose_name="Max group chat members",
        validators=[MinValueValidator(0), MaxValueValidator(10_000)],
    )

    # --- Safety check-ins ---

    max_safety_checkin_contacts = IntegerField(
        default=5,
        help_text="Maximum number of contacts a user may notify per safety check-in. Set to 0 for unlimited.",
        verbose_name="Max contacts per safety check-in",
        validators=[MinValueValidator(0), MaxValueValidator(1_000)],
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
    ai_document_import_enabled = BooleanField(
        default=True,
        help_text="Allow AI to extract pins from uploaded plain-text and Word documents during pin import.",
        verbose_name="Document pin import",
    )
    ai_document_import_max_chars = IntegerField(
        default=20_000,
        help_text=(
            "Maximum number of characters read from an uploaded .txt/.docx file before AI import rejects it outright, to bound token usage per document. Uploads longer than this are not truncated - the user is asked to shorten the file instead."
        ),
        verbose_name="Document import max length (characters)",
        validators=[MinValueValidator(500), MaxValueValidator(200_000)],
    )

    # --- Storage quotas & upload processing ---

    storage_quota_gb = IntegerField(
        default=10,
        help_text="Storage quota (GB) for photo/video uploads per regular user. Subscription roles can override this with a larger quota. Set to 0 for unlimited.",
        verbose_name="Default storage quota (GB)",
        validators=[MinValueValidator(0), MaxValueValidator(1_000_000)],
    )
    image_downscale_enabled = BooleanField(
        default=True,
        help_text="Downscale uploaded photos that exceed the maximum dimension below, to save storage space. The original EXIF metadata is always preserved on the image record.",
        verbose_name="Downscale uploaded photos",
    )
    image_downscale_max_dimension = IntegerField(
        default=1920,
        help_text="Longest edge (pixels) uploaded photos are downscaled to when downscaling is enabled. 1920px keeps plenty of detail for screens while reducing the size of a modern phone photo to roughly 1/8th.",
        verbose_name="Max photo dimension (px)",
        validators=[MinValueValidator(256), MaxValueValidator(20_000)],
    )
    image_convert_webp = BooleanField(
        default=True,
        help_text="Re-encode processed uploads as WebP for additional storage savings.",
        verbose_name="Convert uploads to WebP",
    )
    image_downscale_vip = BooleanField(
        default=False,
        help_text="Also downscale/convert uploads from users with an active subscription. When off, subscribers keep their original files (unless they opt into downscaling themselves).",
        verbose_name="Downscale subscriber uploads",
    )
    max_upload_file_size_mb = IntegerField(
        default=250,
        help_text="Maximum size (MB) for a single photo, video, or document upload. Enforced on both the frontend and backend.",
        verbose_name="Max upload file size (MB)",
        validators=[MinValueValidator(1), MaxValueValidator(20_000)],
    )
    video_downscale_enabled = BooleanField(
        default=True,
        help_text="Downscale uploaded videos that exceed the maximum height below, to save storage space.",
        verbose_name="Downscale uploaded videos",
    )
    video_downscale_max_height = IntegerField(
        default=1080,
        help_text="Vertical resolution (px) uploaded videos are downscaled to when downscaling is enabled.",
        verbose_name="Max video height (px)",
        validators=[MinValueValidator(240), MaxValueValidator(8_000)],
    )
    video_downscale_vip = BooleanField(
        default=False,
        help_text="Also downscale video uploads from users with an active subscription. When off, subscribers keep their original video resolution (unless they opt into downscaling themselves).",
        verbose_name="Downscale subscriber videos",
    )

    # --- Search provider ---

    search_provider = CharField(
        max_length=20,
        choices=SearchProviderChoice.choices,
        default=SearchProviderChoice.BRAVE,
        help_text="Preferred web search provider for pin news/search results, tried first. If it's unconfigured, rate-limited, or fails, the remaining providers (SearXNG, Google, Brave, DuckDuckGo, Mojeek, Marginalia) are tried automatically in that order.",
        verbose_name="Search provider",
    )

    external_data_cache_days = IntegerField(
        default=7,
        help_text=(
            "Minimum number of days to cache external API responses before they are considered stale and re-fetched. "
            "Applies to every shared, Location-scoped external data source (Wikipedia, NPS, LoopNet, USGS, Nominatim, "
            "media archives, Google Places, web search, and satellite/street view imagery)."
        ),
        verbose_name="External data cache (days)",
        validators=[MinValueValidator(1), MaxValueValidator(365)],
    )

    # --- Place naming ---

    default_name_source_priority = CharField(
        max_length=500,
        blank=True,
        default="nominatim,wikipedia,nps,google_places",
        help_text=("Comma-separated name-provider slugs, highest priority first, used to pick a location's official name from external candidates. Sources not listed rank last, in plugin order. Blank = plugin order only."),
        verbose_name="Name source priority",
    )

    # --- Background enrichment ---
    # Hourly Celery task (tasks.run_scheduled_enrichment) that proactively
    # backfills high-value external data (official names, aliases, addresses,
    # boundaries) for every pinned/wiki'd Location, spending only the API
    # budget left over after organic traffic. See services.enrichment.

    enrichment_enabled = BooleanField(
        default=True,
        help_text="Proactively backfill official names, aliases, addresses, and boundaries for all pins and wikis in the background, within each API's configured rate limits.",
        verbose_name="Background enrichment",
    )
    enrichment_start_hour = IntegerField(
        default=0,
        help_text="UTC hour (0-23) the daily enrichment window opens. Set start and end to the same value to allow enrichment at any hour.",
        verbose_name="Enrichment window start (UTC hour)",
        validators=[MinValueValidator(0), MaxValueValidator(23)],
    )
    enrichment_end_hour = IntegerField(
        default=0,
        help_text="UTC hour (0-23) the daily enrichment window closes. May be earlier than the start hour to wrap past midnight (e.g. 22 to 4).",
        verbose_name="Enrichment window end (UTC hour)",
        validators=[MinValueValidator(0), MaxValueValidator(23)],
    )
    enrichment_buffer_percent = IntegerField(
        default=10,
        help_text="Percentage of every API limit kept in reserve for organic traffic spikes. Background enrichment never spends into this buffer.",
        verbose_name="Enrichment rate-limit buffer (%)",
        validators=[MinValueValidator(0), MaxValueValidator(90)],
    )
    enrichment_max_per_service_per_run = IntegerField(
        default=10,
        help_text="Maximum locations enriched per API service in one hourly run, even when the service's rate limit would allow more - keeps bursts against generous APIs polite.",
        verbose_name="Enrichment max items per service per run",
        validators=[MinValueValidator(1), MaxValueValidator(500)],
    )

    # --- Environment ---

    environment_override = CharField(
        max_length=20,
        choices=EnvironmentOverrideChoice.choices,
        default=EnvironmentOverrideChoice.DEFAULT,
        help_text=("Override the deployment environment. Default uses the UL_ENVIRONMENT variable (or local when unset)."),
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
        help_text=("Maximum number of consecutive failed login attempts before an account is temporarily locked. Set to 0 to disable rate limiting."),
        verbose_name="Max failed login attempts",
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    login_lockout_minutes = IntegerField(
        default=15,
        help_text="How many minutes a locked account must wait before login attempts are accepted again.",
        verbose_name="Lockout duration (minutes)",
        validators=[MinValueValidator(1), MaxValueValidator(1440)],
    )

    # --- Admin notifications ---

    notify_admin_email = CharField(
        max_length=254,
        blank=True,
        default=os.getenv("UL_ADMIN_NOTIFICATION_EMAIL", ""),
        help_text="Email address that receives critical site notifications (e.g. pin import failures). Defaults to the UL_ADMIN_NOTIFICATION_EMAIL environment variable.",
        verbose_name="Admin notification email",
    )
    notify_gotify_url = CharField(
        max_length=500,
        blank=True,
        default=os.getenv("UL_GOTIFY_URL", ""),
        help_text="Base URL of a Gotify server (e.g. https://gotify.example.com) used to push critical site notifications. Defaults to the UL_GOTIFY_URL environment variable.",
        verbose_name="Gotify server URL",
    )
    notify_gotify_token = EncryptedTextField(
        blank=True,
        default=os.getenv("UL_GOTIFY_TOKEN", ""),
        help_text="Gotify application token used to authenticate pushes to the server above. Defaults to the UL_GOTIFY_TOKEN environment variable.",
        verbose_name="Gotify app token",
    )

    # --- Notification routing ---
    # Each critical-issue notification type has its own per-channel toggle so the
    # admin can route different events to different channels (e.g. email only for
    # low-urgency events, email + Gotify push for anything needing prompt attention).

    notify_pin_import_errors_email = BooleanField(
        default=True,
        help_text="Email the admin notification address when a pin import fails to process an uploaded file.",
        verbose_name="Pin import errors (email)",
    )
    notify_pin_import_errors_gotify = BooleanField(
        default=False,
        help_text="Send a Gotify push notification when a pin import fails to process an uploaded file.",
        verbose_name="Pin import errors (Gotify)",
    )

    # --- Google Places layer ---

    google_places_cache_days = IntegerField(
        default=90,
        help_text="How many days to cache Google Places nearby-search results before re-fetching. Historical landmarks rarely change.",
        verbose_name="Places layer cache (days)",
        validators=[MinValueValidator(1), MaxValueValidator(365)],
    )

    # --- Database backups ---

    backup_enabled = BooleanField(
        default=os.getenv("UL_BACKUP_ENABLED", "True").lower() in {"true", "1", "yes"},
        help_text="Whether scheduled database backups are enabled.",
        verbose_name="Backups enabled",
    )
    backup_frequency_hours = IntegerField(
        default=int(os.getenv("UL_BACKUP_FREQUENCY_HOURS", "24")),
        help_text="Minimum number of hours between scheduled database backups.",
        verbose_name="Backup frequency (hours)",
        validators=[MinValueValidator(1), MaxValueValidator(24 * 30)],
    )
    backup_retention = IntegerField(
        default=int(os.getenv("UL_BACKUP_RETENTION", "30")),
        help_text="Number of backup files to retain.",
        verbose_name="Backup retention",
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
    )

    # --- Outbound email limits ---
    # Caps on user-triggered emails to third parties (friend/visit invites).
    # Subscription roles can raise these per-tier; the largest applicable
    # limit wins and 0 means unlimited (see services.email_safety).

    email_limit_per_hour = IntegerField(
        default=5,
        help_text="Maximum user-triggered emails (invitations etc.) one user may send per hour. Subscription roles can raise this. 0 = unlimited.",
        verbose_name="Email limit (per hour)",
        validators=[MinValueValidator(0), MaxValueValidator(100_000)],
    )
    email_limit_per_day = IntegerField(
        default=20,
        help_text="Maximum user-triggered emails one user may send per day. Subscription roles can raise this. 0 = unlimited.",
        verbose_name="Email limit (per day)",
        validators=[MinValueValidator(0), MaxValueValidator(100_000)],
    )
    email_limit_per_month = IntegerField(
        default=100,
        help_text="Maximum user-triggered emails one user may send per 30 days. Subscription roles can raise this. 0 = unlimited.",
        verbose_name="Email limit (per month)",
        validators=[MinValueValidator(0), MaxValueValidator(100_000)],
    )

    # --- Subscription features (site-wide default) ---
    # Features granted to every user, including those with no active subscription
    # role. Subscription roles can only add features on top of this baseline, never
    # take one away - see SubscriptionRole.features and user_has_feature().

    default_features = CharField(
        max_length=500,
        blank=True,
        help_text="Comma-separated SiteFeature values granted to every user, including those with no active subscription.",
        verbose_name="Default features (everyone)",
    )

    # --- Registration ---

    signup_restricted = BooleanField(
        default=False,
        help_text=("When enabled, new accounts cannot be created via the public sign-up page. Only users invited by an existing member can join."),
        verbose_name="Restrict sign-ups (invite-only)",
    )

    # --- Bootstrap admin ---
    bootstrap_admin_onboarding_complete = BooleanField(
        default=False,
        help_text="True once the bootstrap admin has visited the site admin settings page.",
    )
    bootstrap_admin_user = ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="+",
        help_text="The first user created on this site; receives site admin and a one-time setup redirect.",
    )

    if TYPE_CHECKING:
        bootstrap_admin_user_id: int | None

    objects = SiteSettingsManager()

    def __str__(self) -> str:
        return "Site Settings"

    @classmethod
    def get_current(cls) -> SiteSettings:
        """Return (and create if missing) the singleton settings record."""
        return cls.objects.get_current()

    @property
    def feature_set(self) -> set[str]:
        """The default ``SiteFeature`` values granted to every user."""
        return {feature.strip() for feature in (self.default_features or "").split(",") if feature.strip()}

    @property
    def feature_labels(self) -> list[str]:
        """Human-readable labels for the default features, in declaration order."""
        from urbanlens.dashboard.models.subscriptions.model import SiteFeature

        feature_set = self.feature_set
        return [label for value, label in SiteFeature.choices if value in feature_set]

    def grants(self, feature: SiteFeature | str) -> bool:
        """Return whether the site-wide default grants ``feature`` to every user."""
        return str(feature) in self.feature_set

    @property
    def name_source_priority_list(self) -> list[str]:
        """The configured name-source priority as an ordered slug list.

        Returns:
            Provider slugs in descending priority; empty when unconfigured.
        """
        return [slug for raw in self.default_name_source_priority.split(",") if (slug := raw.strip())]

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
            True when the effective environment type is ``development`` or ``local``.
            ``local`` is the default when ``UL_ENVIRONMENT`` is unset, and is treated
            as a development environment for toolbar and debug-feature purposes.
        """
        return self.get_effective_environment_type() in {EnvironmentTypes.DEVELOPMENT, EnvironmentTypes.LOCAL}

    def show_dev_admin_features(self, user) -> bool:
        """Return whether dev-only admin UI (e.g. the developer toolbar) should be visible to ``user``.

        Site admins see it whenever the effective environment is development or local.
        Non-admin users can also see it, but only when the ``UL_ALLOW_DEV_TOOLBAR_FOR_NON_ADMINS``
        env var is enabled AND the effective environment is development, local, or testing -
        this lets QA/test accounts exercise dev tooling without granting them site-admin permission,
        while staying off by default and never active in staging/production.

        Args:
            user: The current request user.

        Returns:
            True when dev-only admin UI should be shown to ``user``.
        """
        if not user.is_authenticated:
            return False

        if user.has_perm("dashboard.view_site_admin"):
            return self.is_development_environment()

        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.allow_dev_toolbar_for_non_admins:
            return False

        return self.get_effective_environment_type() in {
            EnvironmentTypes.DEVELOPMENT,
            EnvironmentTypes.LOCAL,
            EnvironmentTypes.TESTING,
        }

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_site_settings"
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

        constraints = [
            CheckConstraint(condition=Q(max_bbox_area_km2__lte=2600.0), name="max_bbox_area_lte_2600"),
            CheckConstraint(condition=Q(max_trip_members__gte=1), name="max_trip_members_gte_1"),
            CheckConstraint(condition=Q(max_trip_members__lte=100), name="max_trip_members_lte_100"),
            CheckConstraint(condition=Q(login_max_attempts__gte=0), name="login_max_attempts_gte_0"),
            CheckConstraint(condition=Q(login_lockout_minutes__gte=1), name="login_lockout_minutes_gte_1"),
            CheckConstraint(condition=Q(backup_frequency_hours__gte=1), name="backup_frequency_hours_gte_1"),
            CheckConstraint(condition=Q(backup_retention__gte=1), name="backup_retention_gte_1"),
            CheckConstraint(condition=Q(google_places_cache_days__gte=1), name="google_places_cache_days_gte_1"),
            CheckConstraint(condition=Q(external_data_cache_days__gte=1), name="external_data_cache_days_gte_1"),
            CheckConstraint(condition=Q(storage_quota_gb__gte=0), name="storage_quota_gb_gte_0"),
            CheckConstraint(condition=Q(email_limit_per_hour__gte=0), name="email_limit_per_hour_gte_0"),
            CheckConstraint(condition=Q(email_limit_per_day__gte=0), name="email_limit_per_day_gte_0"),
            CheckConstraint(condition=Q(email_limit_per_month__gte=0), name="email_limit_per_month_gte_0"),
            CheckConstraint(condition=Q(image_downscale_max_dimension__gte=256), name="image_downscale_max_dim_gte_256"),
            CheckConstraint(condition=Q(max_upload_file_size_mb__gte=1), name="max_upload_file_size_mb_gte_1"),
            CheckConstraint(condition=Q(video_downscale_max_height__gte=240), name="video_downscale_max_height_gte_240"),
            CheckConstraint(condition=Q(max_trip_activities__gte=0), name="max_trip_activities_gte_0"),
            CheckConstraint(condition=Q(max_upcoming_trips_per_user__gte=0), name="max_upcoming_trips_per_user_gte_0"),
            CheckConstraint(condition=Q(max_pins_per_list__gte=0), name="max_pins_per_list_gte_0"),
            CheckConstraint(condition=Q(max_friends_per_user__gte=0), name="max_friends_per_user_gte_0"),
            CheckConstraint(condition=Q(max_group_chat_members__gte=0), name="max_group_chat_members_gte_0"),
            CheckConstraint(condition=Q(max_safety_checkin_contacts__gte=0), name="max_safety_checkin_contacts_gte_0"),
        ]
