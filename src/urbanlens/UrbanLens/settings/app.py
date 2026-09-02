from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Any, Self

from django import conf
from django.conf import LazySettings
from django.core.management.utils import get_random_secret_key
from pydantic import Field, field_validator, model_validator
from pydantic._internal._model_construction import ModelMetaclass
from pydantic_core import Url
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.factory import select_environment
from urbanlens.UrbanLens.environments.meta import DebugTypes, EnvironmentTypes
from urbanlens.UrbanLens.settings.meta.app import DEFAULT_PATH_PARENTS, DEFAULT_ROOT

logger = logging.getLogger(__name__)

# DEFAULT_ROOT is src/, but .env lives one level up at the project root.
# List both so either location works; later entry wins if both exist.
_ENV_FILE_PATHS = [
    Path(DEFAULT_ROOT, ".env"),
    Path(DEFAULT_ROOT.parent, ".env"),
]

#: Environments where a working ``.env`` is a local-checkout convenience.
#: Everywhere else the process is configured entirely from real environment
#: variables (compose's ``env_file:``) and ``.env`` is deliberately kept out
#: of the image - see ``bin/init.py``'s ``copy_sample_env``, which reached
#: this same allow-list after treating a missing .env as needing action took
#: staging down on 2026-08-17. Kept in sync with that one by hand: this
#: module cannot import ``bin/init.py`` (a standalone, Django-free script
#: that must run before Django is even configured) without creating the
#: layering violation it exists to avoid.
_ENV_FILE_ENVIRONMENTS = frozenset({EnvironmentTypes.LOCAL, EnvironmentTypes.DEVELOPMENT, EnvironmentTypes.TESTING})

#: Floors enforced on ``field_encryption_key`` (see ``_reject_weak_encryption_keys``).
#: 32 characters is well below the 64 the documented generator produces, so it
#: rejects hand-typed keys without failing a legitimately generated one. The
#: alphabet floor is set against the distribution of random output rather than
#: against any particular bad key: a random 32-character urlsafe-base64 string
#: has ~26 distinct characters on average and falls below 16 only very rarely,
#: while degenerate input (repeated characters, a short string concatenated with
#: itself) lands under it immediately.
MIN_FIELD_ENCRYPTION_KEY_LENGTH = 32
MIN_FIELD_ENCRYPTION_KEY_ALPHABET = 16


def _encryption_key_weakness(key: str) -> str | None:
    """Describe why a field-encryption key is too weak to encrypt under, if it is.

    Args:
        key: The candidate key.

    Returns:
        An operator-facing explanation, or None when the key clears both floors.
    """
    if len(key) < MIN_FIELD_ENCRYPTION_KEY_LENGTH:
        return (
            f"field_encryption_key must be at least {MIN_FIELD_ENCRYPTION_KEY_LENGTH} characters "
            f"(got {len(key)}). Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(64))"'
        )
    # A crude stand-in for entropy, calibrated against random output rather
    # than against any specific bad key - see the constant.
    if len(set(key)) < MIN_FIELD_ENCRYPTION_KEY_ALPHABET:
        return (
            f"field_encryption_key uses only {len(set(key))} distinct characters, which is too "
            "predictable to resist an offline attack against a stolen database. Use a random "
            'value: python -c "import secrets; print(secrets.token_urlsafe(64))"'
        )
    return None


def _default_allowed_hosts() -> list[str]:
    """Return the default ``ALLOWED_HOSTS`` list for the current environment.

    ``localhost`` and ``127.0.0.1`` are always included so Docker's internal
    ``curl http://localhost:8000/health/`` healthchecks (see docker-compose.yml)
    succeed without opening the app to arbitrary public Host headers. Override
    the full list via ``UL_ALLOWED_HOSTS`` when deploying to a custom domain.
    """
    return ["urbanlens.org", "localhost", "127.0.0.1"]


class AppSettingsMeta(ModelMetaclass):
    """
    Metaclass to ensure only one instance of the class is created
    """

    _instances: dict[type, Any] = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
        return cls._instances[cls]


class AppSettings(BaseSettings, metaclass=AppSettingsMeta):
    """
    Class to hold settings for the application.
    """

    project_root: Path = Field(default=DEFAULT_ROOT, description="The root directory of the project")
    project_name: str = Field(default="URBANLENS", description="The name of the project")
    app_version: str = Field(default="", description="Semantic application version from pyproject.toml")
    environment_name: str = Field(default=EnvironmentTypes.LOCAL, description="The name of the environment")
    debug_override: bool | None = Field(description="Whether or not to enable debugging", alias="DEBUG", default=None)
    # default_factory, not default=get_random_secret_key() - a plain `default=` value is
    # computed once at class-definition time rather than per instantiation, which is
    # the standard pydantic mutable/computed-default footgun even though it's harmless
    # here specifically (AppSettingsMeta makes this class a process-wide singleton).
    # NOTE: this field has no wired env var in any deployment (UL_SECRET_KEY is never
    # set) - nothing outside this file should ever read it as if it were stable across
    # processes. dashboard/models/fields.py used to and that was a real bug; see its
    # comment for the fix (falls back to Django's actual SECRET_KEY instead).
    secret_key: str = Field(default_factory=get_random_secret_key, description="The secret key")
    field_encryption_key: str | None = Field(
        default=None,
        description=(
            "Base64 key used to encrypt sensitive database fields (e.g. Immich API keys) via Fernet. "
            "When unset, a key is derived from Django's SECRET_KEY (DJANGO_SECRET_KEY) so existing installs "
            "keep working without a new required secret - set this explicitly in production so field "
            "encryption survives a SECRET_KEY rotation."
        ),
    )
    field_encryption_key_fallbacks: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Retired field-encryption keys, comma-separated. Values are always written under the active "
            "key but can still be read under any key listed here, so a key change is a rolling change "
            "rather than data loss: add the new key, deploy, run `manage.py rotate_field_encryption`, "
            "then drop the retired key from this list. Django's SECRET_KEY is always tried as a final "
            "fallback, so setting field_encryption_key for the first time never orphans existing rows; "
            "list the *old* SECRET_KEY here when rotating SECRET_KEY itself."
        ),
    )
    root_urlconf: str = Field(default="urbanlens.UrbanLens.urls", description="The root urlconf")
    admin_username: str = Field(default="Admin", description="The username to use for the admin user")
    admin_email: str = Field(default="admin@yourdomain.com", description="The email to use for the admin user")
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=_default_allowed_hosts, description="The allowed hosts")
    plugin_modules: Annotated[list[str], NoDecode] = Field(default_factory=list, description="Dotted module paths of additional UrbanLens plugins to load (comma-separated)")
    disabled_plugins: Annotated[list[str], NoDecode] = Field(default_factory=list, description="Names of discovered UrbanLens plugins to disable for this install (comma-separated)")
    language_code: str = Field(default="en-us", description="The language code")
    time_zone: str = Field(default="EST", description="The time zone")
    use_i18n: bool = Field(default=True, description="Whether or not to use i18n")
    use_tz: bool = Field(default=True, description="Whether or not to use tz")
    email_backend: str = Field(default="django.core.mail.backends.console.EmailBackend", description="Django email backend class path")
    email_from: str = Field(default="noreply@yourdomain.com", description="The from email")
    email_host: str = Field(default="smtp.gmail.com", description="The email host")
    email_port: int = Field(default=587, description="The email port")
    email_user: str | None = Field(default=None, description="SMTP username / sending address")
    email_password: str | None = Field(default=None, description="SMTP password or app password")
    email_tls: bool = Field(default=True, description="Use STARTTLS (port 587)")
    email_use_ssl: bool = Field(default=False, description="Use SSL instead of STARTTLS (port 465)")
    backup_enabled: bool = Field(default=True, description="Whether scheduled database backups are enabled")
    backup_frequency_hours: int = Field(default=24, description="How often scheduled database backups should run, in hours")
    backup_retention: int = Field(default=30, description="The number of backup files to retain")
    clamav_enabled: bool = Field(
        default=True,
        description=(
            "Whether every user-uploaded file (photo/video/document, article images) is scanned for "
            "malware via a clamd daemon before it's stored. Uploads are rejected if the scanner is "
            "enabled but unreachable (fail closed). Set UL_CLAMAV_ENABLED=false for local development, "
            "where no clamd container is running."
        ),
    )
    clamav_host: str = Field(default="urbanlens_clamav", description="Hostname of the clamd daemon (see the clamav service in docker-compose.yml)")
    clamav_port: int = Field(default=3310, description="Port of the clamd daemon")
    clamav_timeout_seconds: float = Field(default=15.0, description="Socket timeout for a single clamd scan request")
    media_base_url: str = Field(
        default="",
        description=(
            "Origin that serves user uploads, e.g. https://media.urbanlens.org. Point it at the media-nginx "
            "container's published port (UL_MEDIA_PORT). Serving uploads from their own origin means anything "
            "that slips past validation executes where there is no session cookie and no app data; authentication "
            "there uses a separate media-only signed cookie. Leave empty to serve /media/ from the app's own "
            "hostname, which is what local development does. The host must also appear in UL_ALLOWED_HOSTS."
        ),
    )
    media_cookie_domain: str = Field(
        default="",
        description=(
            "Domain attribute for the media cookie. Empty (the default) derives the deepest domain the site host "
            "and media host share, which is right whenever the media origin is a sibling subdomain. Set this only "
            "when the two hosts are not related that way, and never to a public suffix."
        ),
    )
    process_role: str = Field(
        default="unspecified",
        description=(
            "What this process is: web, websocket, worker, panels, beat, or sandbox. Set per service in "
            "docker-compose.yml. Only 'sandbox' may hand untrusted uploaded bytes to a parser (Pillow, ffmpeg, "
            "LibreOffice, GDAL, zipfile); see UL_UNTRUSTED_PARSE_POLICY."
        ),
    )
    sandbox_enabled: bool = Field(
        default=True,
        description=(
            "Whether the media-worker containers are deployed to drain the 'sandbox' and 'sandbox_batch' Celery queues. When false, "
            "untrusted-parse tasks fall back to the default queue so an install without that container still "
            "processes uploads - without the isolation. Set UL_SANDBOX_ENABLED=false only where no media-worker runs."
        ),
    )
    untrusted_parse_policy: str = Field(
        default="warn",
        description=(
            "How a process reacts when it is about to parse untrusted uploaded bytes outside the sandbox worker: "
            "'deny' raises, 'warn' logs and proceeds, 'allow' disables the check. Production should be 'deny'; "
            "'warn' exists to collect violations during a rollout, and the test settings force 'allow' because the "
            "suite calls the parsers directly."
        ),
    )
    allow_dev_toolbar_for_non_admins: bool = Field(
        default=False,
        description=(
            "Allow authenticated users without site-admin permission to see the developer toolbar. "
            "Only takes effect in development, local, or testing environments - ignored in staging/production."
        ),
    )
    csp_enforce: bool = Field(
        default=False,
        description=(
            "Send the Content-Security-Policy as an enforcing header instead of "
            "Content-Security-Policy-Report-Only. Defaults to report-only so a deployment collects "
            "violation reports for a release before anything is actually blocked. Set UL_CSP_ENFORCE=true "
            "per environment once the reports for that environment are clean - see docs/NOTES.md."
        ),
    )
    trusted_proxy_count: int = Field(
        default=1,
        description=(
            "How many reverse proxies sit between the app and the internet, each appending to "
            "X-Forwarded-For. Per-IP rate limiting reads the entry that many places from the right, "
            "since anything further left was supplied by the client and can be forged. The default of 1 "
            "matches the shipped topology (config/nginx appends its real_ip-resolved client address). "
            "Set 0 when nothing fronts the app, so REMOTE_ADDR is used and X-Forwarded-For ignored - "
            "but never leave it at 0 behind a proxy, or every request keys to the proxy's own address "
            "and one attacker throttles the whole site."
        ),
    )

    demo_mode: bool = Field(
        default=False,
        description=(
            "This deployment IS the public demo instance. Enables the demo-login endpoint and the "
            "demo banner. Isolation is the deployment boundary itself - a demo instance runs against "
            "its own database, seeded synthetically, and never against real data. Setting this true "
            "on an instance holding real user data would expose it: see docs/DEMO.md."
        ),
    )
    demo_real_site_url: str = Field(
        default="",
        description=(
            "Set on the demo instance: absolute URL of the real site, e.g. https://urbanlens.org. "
            "The demo banner links there for 'create a real account'. Empty hides that link - the "
            "demo's own signup would only make another throwaway account on the instance that is "
            "about to delete it."
        ),
    )
    demo_locations_file: str = Field(
        default="",
        description=(
            "Path to the JSON manifest of locations a demo instance pins into every new demo "
            "account (written by `manage.py import_public_locations`). Empty means no manifest, and "
            "a demo account is seeded with no pins - which is the correct outcome when nothing has "
            "been imported yet, rather than a reason to invent coordinates."
        ),
    )
    demo_url: str = Field(
        default="",
        description=(
            "Absolute URL of the demo instance, e.g. https://demo.urbanlens.org. Set on the *real* "
            "site to show the 'Try the demo' button on the login and registration pages; empty hides "
            "it. Deliberately a URL rather than a boolean, so the button cannot appear without a "
            "destination that someone has actually provisioned."
        ),
    )

    # Classes
    default_auto_field: str = Field(default="django.db.models.BigAutoField", description="The default auto field")
    wsgi_application: str = Field(default="urbanlens.UrbanLens.wsgi.application", description="The wsgi application")
    asgi_application: str = Field(default="urbanlens.UrbanLens.asgi.application", description="The asgi application")
    test_runner: str = Field(default="urbanlens.core.tests.runner.TestRunner", description="The test runner")

    # Urls
    deployment_name : str | None = Field(default=None, description="K3s site/node name, if any. Used to distinguish between deployments.")
    login_url: str = Field(default="login", description="The login url")
    static_url: str = Field(default="static/", description="The static url")

    # Directory settings
    base_dir: Path = Field(default=Path("urbanlens"), description="The name of the base directory")
    media_root: Path = Field(default=Path("downloads"), description="The name of the media directory")
    downloads_dir: Path = Field(default=Path("downloads"), description="The name of the downloads directory")
    backups_dir: Path = Field(default=Path("backups"), description="The name of the backups directory")
    log_root: Path = Field(default=Path("logs"), description="The name of the log directory")
    exports_dir: Path = Field(default=Path("exports"), description="The name of the exports directory")
    static_root: Path = Field(default=Path("frontend/static"), description="The name of the static directory")

    # APIs
    vendor_asset_base_url: Url | None = Field(default=None, description="Root of a mirror of the third-party JS/CSS assets; unset uses the public CDNs")
    cloudflare_ai_endpoint: Url | None = Field(default=None, description="The cloudflare ai endpoint")
    cloudflare_worker_ai_endpoint: Url | None = Field(default=None, description="The cloudflare worker ai endpoint")
    cloudflare_ai_api_key: str | None = Field(default=None, description="The cloudflare ai key")
    huggingface_ai_endpoint: Url | None = Field(default=None, description="The huggingface ai endpoint")
    huggingface_ai_api_key: str | None = Field(default=None, description="The huggingface ai key")
    openai_api_key: str | None = Field(default=None, description="The openai key")
    anthropic_api_key: str | None = Field(default=None, description="The anthropic (claude) key")
    google_unrestricted_api_key: str | None = Field(default=None, description="The google unrestricted api key")
    google_domain_restricted_api_key: str | None = Field(default=None, description="The google domain restricted api key")
    google_public_api_key: str | None = Field(default=None, description="The google public api key")
    google_search_tenant: str | None = Field(default=None, description="The google search tenant")
    google_client_id: str | None = Field(default=None, description="The google client id")
    google_client_secret: str | None = Field(default=None, description="The google client secret")
    flickr_api_key: str | None = Field(default=None, description="The Flickr API (consumer) key, for the per-user photo import OAuth 1.0a flow")
    flickr_api_secret: str | None = Field(default=None, description="The Flickr API (consumer) secret")
    apple_maps_api_key: str | None = Field(default=None, description="The apple maps JWT (pre-generated from Apple Developer private key)")
    usgs_api_key: str | None = Field(default=None, description="The USGS M2M application token (from EarthExplorer account settings)")
    usgs_username: str | None = Field(default=None, description="The USGS EarthExplorer username (required alongside usgs_api_key for M2M auth)")
    azure_maps_subscription_key: str | None = Field(default=None, description="The Azure Maps subscription key (Azure Portal -> Azure Maps account -> Authentication)")
    ollama_base_url: str | None = Field(default=None, description="Base URL of a self-hosted Ollama server (e.g. http://localhost:11434) for local, free AI photo-keyword generation")
    ollama_vision_model: str = Field(default="llava", description="Ollama vision model name used for photo keyword generation")
    openweathermap_api_key: str | None = Field(default=None, description="The openweathermap key")
    redata_api_url: str | None = Field(default=None, description="Base URL of the REData property-records service (e.g. https://redata.example.com), no trailing slash needed")
    redata_api_key: str | None = Field(default=None, description="Bearer API key for REData's external API - needs at least the parcels:read scope")
    virustotal_api_key: str | None = Field(
        default=None,
        description=(
            "VirusTotal API key (public/free tier), sent as the x-apikey header. When set, externally-fetched "
            "(non-upload) image assets are looked up by hash on VirusTotal before falling back to ClamAV - see "
            "services.security.virustotal_scan. Unset (the default) skips VirusTotal entirely; those assets go "
            "straight to ClamAV, same as every upload."
        ),
    )
    discord_client_secret: str | None = Field(default=None, description="The discord client secret")
    discord_client_id: str | None = Field(default=None, description="The discord client ID")
    twilio_account_sid: str | None = Field(default=None, description="The Twilio account SID, for outbound SMS/WhatsApp notifications")
    twilio_auth_token: str | None = Field(default=None, description="The Twilio auth token")
    twilio_sms_from_number: str | None = Field(default=None, description="The Twilio phone number SMS notifications are sent from (E.164 format)")
    twilio_whatsapp_from_number: str | None = Field(default=None, description="The Twilio-approved WhatsApp sender number (E.164 format, without the 'whatsapp:' prefix)")
    stripe_secret_key: str | None = Field(default=None, description="The Stripe secret API key (sk_...), for paid subscription checkout/billing-portal/webhook calls")
    stripe_webhook_secret: str | None = Field(default=None, description="The Stripe webhook signing secret (whsec_...), for verifying incoming /billing/webhooks/stripe/ requests")

    # Note: DATABASES is built directly in
    # settings/base.py from UL_DB_* environment variables

    _secrets: dict | None = None
    _environment: BaseEnvironment | None = None

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE_PATHS,
        env_prefix="UL_",
        str_strip_whitespace=True,
        env_ignore_empty=True,
        extra="ignore",
    )

    @property
    def debug(self) -> bool:
        """
        Whether or not debugging is enabled
        """
        if self._environment:
            return self._environment.debug
        # This is only used prior to the environment being set.
        # -- after that, it is propogated to the environment
        return self.debug_override or False

    @debug.setter
    def debug(self, value: bool) -> None:
        """
        Set the debug value
        """
        self.debug_override = value

        if self._environment:
            self._environment.debug_override = DebugTypes.OVERRIDE_ON if value else DebugTypes.OVERRIDE_OFF

    @property
    def environment(self) -> BaseEnvironment | None:
        """
        The environment
        """
        return self._environment

    @property
    def secrets(self) -> dict:
        """
        The secrets dictionary
        """
        return self._secrets or {}

    @property
    def paths(self) -> dict[str, Path]:
        """
        Returns a dictionary of directories
        """
        return {
            "project_root": self.project_root,
            "base_dir": self.base_dir,
            "media_root": self.media_root,
            "downloads_dir": self.downloads_dir,
            "backups_dir": self.backups_dir,
            "log_root": self.log_root,
            "exports_dir": self.exports_dir,
            "static_root": self.static_root,
        }

    @property
    def databases(self) -> dict[str, dict[str, Any]]:
        return conf.settings.DATABASES

    @property
    def logging(self) -> dict[str, Any]:
        return conf.settings.LOGGING

    @property
    def django(self) -> LazySettings:
        return conf.settings

    @field_validator("allowed_hosts", "plugin_modules", "disabled_plugins", "field_encryption_key_fallbacks", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: Any) -> Any:
        """Allow list-valued settings to be provided as comma-separated strings via env vars."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("field_encryption_key", mode="after")
    @classmethod
    def _reject_weak_encryption_keys(cls, value: str | None) -> str | None:
        """Refuse an active field-encryption key weak enough to brute-force offline.

        The derivation is a single unsalted SHA256 (``models.fields._derive_fernet``),
        so key strength *is* input strength - there is no stretching to hide behind.
        Fernet tokens carry their own HMAC, so one stolen ciphertext row lets an
        attacker verify guesses offline at hashing speed. Rejecting at configuration
        time is the only point where this is still cheap to fix, and the operator
        setting this variable is by definition trying to harden the install.

        This is a floor, not an entropy oracle: it reliably catches short and
        degenerate keys, but a sufficiently long hand-written passphrase will pass
        while still being far weaker than generated output. Use the documented
        ``secrets.token_urlsafe(64)`` command rather than treating acceptance here
        as an endorsement.

        Args:
            value: The configured active key, if any.

        Returns:
            The value unchanged when it is acceptable.

        Raises:
            ValueError: When the key is too short or too repetitive to be a
                machine-generated secret.
        """
        weakness = _encryption_key_weakness(value) if value else None
        if weakness:
            raise ValueError(weakness)
        return value

    @field_validator("field_encryption_key_fallbacks", mode="after")
    @classmethod
    def _warn_about_weak_fallback_keys(cls, value: list[str]) -> list[str]:
        """Accept retired keys that would be refused as the active key, and say so.

        Applying the floor here too would strand exactly the installs that need
        to move off a weak key: the documented rotation is to list the old key
        as a fallback, run ``manage.py rotate_field_encryption``, then drop it -
        and a validator that refuses the old key stops the settings module from
        loading at all, so the command that fixes it cannot start either. A
        fallback only ever decrypts, and only until the rotation finishes, so
        the useful action is a loud warning rather than a locked door.

        Args:
            value: The configured retired keys.

        Returns:
            The value unchanged.
        """
        for key in value:
            weakness = _encryption_key_weakness(key)
            if weakness:
                logger.warning(
                    "A retired field-encryption key is too weak to be accepted as the active key. "
                    "It still decrypts existing rows, but finish the rotation and drop it: %s",
                    weakness,
                )
        return value

    @model_validator(mode="after")
    def _resolve_version_metadata(self) -> Self:
        """Populate version metadata from pyproject.toml when not configured."""
        from urbanlens.core.version import get_app_version

        if not self.app_version:
            self.app_version = get_app_version()
        return self

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._check_env_file()
        self.ensure_paths()

    def _check_env_file(self) -> None:
        for env_path in _ENV_FILE_PATHS:
            if env_path.exists():
                if env_path.stat().st_size == 0:
                    logger.warning("Found .env file but it is empty: %s", env_path)
                return
        checked = ", ".join(str(p) for p in _ENV_FILE_PATHS)
        # self.environment_name isn't wired to UL_ENVIRONMENT (select_environment(),
        # which would set it, is never called in the runtime app - see the module-level
        # ENVIRONMENT_NAME in settings/base.py for the value Django itself branches on).
        # Read the same env var the same way base.py does, rather than trust this field.
        environment_name = os.getenv("UL_ENVIRONMENT", "local").strip().lower()
        if environment_name not in _ENV_FILE_ENVIRONMENTS:
            logger.info(
                "No .env file in %s, and none is needed: this environment is configured from real environment variables. Checked: %s",
                environment_name,
                checked,
            )
            return
        logger.warning(".env file not found; API keys and secrets will be missing. Checked: %s", checked)

    def ensure_paths(self) -> None:
        """
        Ensure the directories are absolute and exist.
        """
        for key, value in self.paths.items():
            try:
                if not isinstance(value, Path):
                    value = Path(value)
                    setattr(self, key, value)

                if not value.is_absolute():
                    parent = getattr(self, str(DEFAULT_PATH_PARENTS[key]))
                    value = Path(parent, value)
                    setattr(self, key, value)

                if not value.exists():
                    # A path containing a period is inferred to be a file, so only its
                    # parent is ensured; anything else is a directory and is created
                    # itself. Getting this backwards silently leaves directory-valued
                    # settings (backups_dir, downloads_dir, exports_dir, static_root)
                    # uncreated while their parents exist, which fails far from here.
                    if "." not in value.name:
                        value.mkdir(parents=True, exist_ok=True)
                    else:
                        value.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                # OSError, not FileNotFoundError: a read-only or wrong-owner app
                # directory raises PermissionError, and letting that escape takes
                # down settings import - and therefore every process - without
                # reporting which path was at fault.
                logger.warning("Could not ensure path %s (%s); continuing without it.", key, value, exc_info=True)

        # Ensure app.log, debugging.log, and test.log exist in log dir
        for filename in ["app.log", "debugging.log", "test.log"]:
            try:
                if not self.log_root.exists():
                    self.log_root.mkdir(parents=True, exist_ok=True)
                filepath = Path(self.log_root, filename)
                if not filepath.exists():
                    filepath.write_text("")
            except OSError:
                # Pre-creating these is a convenience for the file handlers, not a
                # requirement. Django's own logging config reports an unwritable log
                # directory far more usefully than an unhandled error at import time,
                # which surfaces as a silent container that never binds a port.
                logger.warning("Could not pre-create %s in %s; continuing.", filename, self.log_root, exc_info=True)

    def refresh_django(self):
        """
        Refresh django.conf.settings with the current settings
        """
        for key, value in self.__dict__.items():
            # Filter out settings we don't want to propogate back to django
            if key.startswith("_") or key in ["model_config", "paths", "secrets", "databases", "logging", "django"]:
                continue

            const_name = key.upper()
            setattr(conf.settings, const_name, value)

        # Refresh properties as well
        conf.settings.DEBUG = self.debug
        conf.settings.ENVIRONMENT = self.environment

        # Refresh django.conf
        """
        conf.settings.configure(
            default_settings = self
        )
        """

    def select_environment(self, new_environment_name: EnvironmentTypes | None = None) -> BaseEnvironment:
        """
        Select the environment

        Args:
            new_environment_name (EnvironmentTypes | None, optional):
                The name of the environment to switch to. Defaults to None.

        Returns:
            BaseEnvironment: The environment to use
        """
        if self._environment is not None and self.environment_name == new_environment_name:
            return self._environment

        if new_environment_name:
            self.environment_name = new_environment_name
            logger.info("Switching to environment: %s", new_environment_name)
        self._environment = select_environment(self.environment_name)

        # If we set debug_override to any bool, propogate it to the new env
        if self.debug_override is True or self.debug_override is False:
            self._environment.debug_override = DebugTypes.OVERRIDE_ON if self.debug_override else DebugTypes.OVERRIDE_OFF

        self.refresh_django()
        return self._environment

    def __getattr__(self, name: str):
        """
        Get an attribute that is fully uppercase (from django settings) as a parameter here (as lowercase)
        """
        key = name.lower()
        if key in self.__dict__:
            return self.__dict__[key]

        return super().__getattr__(name)


settings = AppSettings()
