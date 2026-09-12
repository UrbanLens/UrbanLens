"""Startup checks that fail fast on deploy-time misconfiguration."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning, register
from django.db.models import FileField

from urbanlens.dashboard.services.media.access import MEDIA_FAMILY_ATTR, registered_families

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.apps.config import AppConfig
    from django.core.checks import CheckMessage

#: Only our own models; third-party prefixes fail closed at runtime.
_OWN_APP_PREFIX = "urbanlens."


def _declared_family(field: FileField) -> tuple[str | None, str | None]:
    """Resolve the media family a file field writes into."""
    upload_to = field.upload_to
    if callable(upload_to):
        family = getattr(upload_to, MEDIA_FAMILY_ATTR, None)
        if not family:
            name = getattr(upload_to, "__qualname__", repr(upload_to))
            hint = f"its upload_to callable {name!r} does not declare which directory it writes into."
            return None, f"{hint} Decorate it with @declares_media_family('<prefix>') from urbanlens.dashboard.services.media.access."
        return str(family), None

    prefix = str(upload_to).strip("/").split("/", 1)[0]
    if not prefix:
        return None, "it has no upload_to directory, so its files land directly in MEDIA_ROOT with no family to authorize. Give it an upload_to."
    return prefix, None


@register()
def check_media_authorizers(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """Fail startup when a file field has no registered media authorizer."""
    known = registered_families()
    errors: list[CheckMessage] = []

    models = apps.get_models() if app_configs is None else [model for config in app_configs for model in config.get_models()]
    for model in models:
        meta = model._meta  # noqa: SLF001 - Model._meta is Django's documented metadata API
        if not meta.app_config.name.startswith(_OWN_APP_PREFIX):
            continue
        for field in meta.get_fields():
            if not isinstance(field, FileField):
                continue
            family, hint = _declared_family(field)
            if hint is not None:
                errors.append(
                    Error(
                        f"{meta.label}.{field.name} stores files under MEDIA_ROOT, but {hint}",
                        obj=field,
                        id="dashboard.E001",
                    ),
                )
                continue
            if family not in known:
                errors.append(
                    Error(
                        f"{meta.label}.{field.name} stores files under {family!r}/, which no media authorizer covers. "
                        f"Every file served by MediaGateView needs a policy for who may read it, and an unregistered "
                        f"directory is refused outright - these files would 404 for everyone. "
                        f"Register one with @media_authorizer({family!r}) in urbanlens.dashboard.services.media.access.",
                        obj=field,
                        id="dashboard.E002",
                    ),
                )

    return errors


@register()
def check_media_origin_cookie_domain(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """Fail startup when a configured media origin cannot issue its cookie."""
    from urllib.parse import urlsplit

    from urbanlens.dashboard.services.media.origin import PUBLIC_SUFFIXES, cookie_domain, media_origin, media_origin_host, shared_suffix

    if not media_origin():
        return []

    if not media_origin_host():
        return [
            Error(
                f"UL_MEDIA_BASE_URL is set to {media_origin()!r}, which has no hostname. It must be a full origin, e.g. https://media.urbanlens.org.",
                id="dashboard.E003",
            ),
        ]

    domain = cookie_domain()
    if not domain:
        raw = shared_suffix(media_origin_host(), urlsplit(str(settings.SITE_URL or "")).hostname or "")
        if raw and raw.lower() in PUBLIC_SUFFIXES:
            return [
                Error(
                    f"SITE_URL and UL_MEDIA_BASE_URL share only {raw!r}, which is a public suffix - a cookie cannot be "
                    f"scoped to one, so the media origin would 404 for every viewer. Point them at hosts under a common "
                    f"registrable domain, or set UL_MEDIA_COOKIE_DOMAIN explicitly.",
                    id="dashboard.E005",
                ),
            ]
        return [
            Error(
                f"UL_MEDIA_BASE_URL is set ({media_origin_host()!r}), but no cookie domain can be derived from it and "
                f"SITE_URL ({str(settings.SITE_URL)!r}). The media cookie would never be set, so every media URL would "
                f"404 for every viewer. Either point the two at hosts that share a registrable domain "
                f"(urbanlens.org + media.urbanlens.org), or set UL_MEDIA_COOKIE_DOMAIN explicitly.",
                id="dashboard.E004",
            ),
        ]

    return []


#: Scratch subtrees under MEDIA_ROOT kept on local disk.
_LOCAL_ONLY_MEDIA_SUBTREES = (
    ("exports/", "services.import_export.export.export_dir"),
    ("imports/", "services.import_export.import_data"),
    ("preview_sources/", "services.media.previews"),
)


@register()
def check_object_storage_is_configured(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """Fail startup when the object-store backend is incomplete or bypasses the media gate."""
    if getattr(settings, "UL_MEDIA_STORAGE_BACKEND", "filesystem") != "s3":
        if getattr(settings, "MEDIA_X_ACCEL_OBJECT_PREFIX", ""):
            return [
                Error(
                    "UL_MEDIA_X_ACCEL_OBJECT_PREFIX is set but UL_MEDIA_STORAGE_BACKEND is not 's3', so nothing would ever use it. Either switch the backend or unset the prefix.",
                    id="dashboard.E009",
                ),
            ]
        return []

    # Deferred import: django-storages/boto3 only needed for s3 backends.
    from django.core.files.storage import default_storage

    from urbanlens.dashboard.services.media.object_storage import GatedS3Storage

    if not isinstance(default_storage, GatedS3Storage):
        return [
            Error(
                f"UL_MEDIA_STORAGE_BACKEND is 's3' but STORAGES['default'] resolves to "
                f"{default_storage.__class__.__name__}, not GatedS3Storage. Only GatedS3Storage keeps FileField.url "
                f"pointing at /media/; the upstream backend returns a presigned bucket URL instead, which serves "
                f"every upload to anyone holding the link and bypasses the media gate.",
                id="dashboard.E010",
            ),
        ]

    messages: list[CheckMessage] = []

    missing: list[str] = []
    if not default_storage.bucket_name:
        missing.append("UL_S3_BUCKET_NAME")
    if not (default_storage.access_key and default_storage.secret_key) and not os.environ.get("AWS_ACCESS_KEY_ID"):
        missing.append("UL_S3_ACCESS_KEY_ID/UL_S3_SECRET_ACCESS_KEY")
    if missing:
        messages.append(
            Error(
                f"UL_MEDIA_STORAGE_BACKEND is 's3' but {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} unset. Every media read and write would fail.",
                id="dashboard.E011",
            ),
        )

    if default_storage.default_acl is not None:
        messages.append(
            Error(
                f"The media storage sets default_acl={default_storage.default_acl!r}. Uploads must carry no ACL of their own: a bucket-level grant is the one way to reach a file without passing the media gate.",
                id="dashboard.E012",
            ),
        )

    messages.append(
        CheckWarning(
            "UL_MEDIA_STORAGE_BACKEND is 's3', but " + ", ".join(f"MEDIA_ROOT/{subtree} ({owner})" for subtree, owner in _LOCAL_ONLY_MEDIA_SUBTREES) + " still read and write the local filesystem directly. They are shared scratch between containers "
            "rather than user media, and none is served through the media gate - but the media volume cannot be "
            "removed while they exist.",
            id="dashboard.W002",
        ),
    )

    return messages


#: Provider keys that belong only in the ai-inference container.
_PROVIDER_KEY_SETTINGS = ("anthropic_api_key", "openai_api_key", "cloudflare_ai_api_key")


@register()
def check_provider_keys_are_not_on_the_app_tier(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """Warn when a provider key is readable on the app tier instead of ai-inference."""
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not getattr(app_settings, "ai_inference_url", None):
        return []
    if getattr(settings, "UL_PROCESS_ROLE", "") == "inference":
        return []

    present = [name for name in _PROVIDER_KEY_SETTINGS if getattr(app_settings, name, None)]
    if not present:
        return []
    return [
        CheckWarning(
            f"Provider API key(s) readable by this process: {', '.join(sorted(present))}.",
            hint=(
                "This deployment routes inference through ai-inference, so nothing here reads these. "
                "They are almost certainly set in the root .env, which app and celery-worker load in full - "
                "putting a provider credential in the same environment as UL_DB_PASS and "
                "UL_FIELD_ENCRYPTION_KEY. Move them to .env.ai, which only ai-inference reads. "
                "See .env.ai-sample and docs/AI_PIPELINE.md."
            ),
            id="dashboard.W001",
        ),
    ]


#: Hosts a dev deployment may point REData at without warning.
_LOCAL_REDATA_HOSTS = ("localhost", "127.0.0.1", "::1")

#: Host fragments that identify a non-production REData.
_LOCAL_REDATA_MARKERS = (".dev.", "urbanlens_redata", "redata-", "_redata")


def _redata_host_is_local(url: str) -> bool:
    """Whether *url* points at a local or dev REData instance."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return True
    if host in _LOCAL_REDATA_HOSTS or host.endswith(".local"):
        return True
    if any(marker in host for marker in _LOCAL_REDATA_MARKERS):
        return True
    if "." not in host:
        return True
    return host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20."))


@register()
def check_dev_is_not_pointed_at_a_real_redata(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """Warn when a dev deployment points at a non-local REData."""
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    environment = str(getattr(settings, "ENVIRONMENT_NAME", "")).lower()
    if environment not in {"development", "local"}:
        return []

    url = getattr(app_settings, "redata_api_url", None)
    if not url or _redata_host_is_local(str(url)):
        return []

    allowed = bool(getattr(app_settings, "allow_outbound_apis", False))
    state = "UL_ALLOW_OUTBOUND_APIS is on, so these calls are going out right now" if allowed else "UL_ALLOW_OUTBOUND_APIS is off, so nothing is calling it yet - but turning that on, which is what working on an integration means, makes it live"
    return [
        CheckWarning(
            f"This {environment} deployment's REData is {url}, which is not a local or dev instance.",
            hint=(
                f"{state}. REData reaches billable providers on our behalf, so a dev box pointed at a "
                "real one spends a real budget on background work nobody is watching - one pin import "
                "enqueues thousands of such calls. Point UL_REDATA_API_URL at your own instance "
                "(dev_env.py --own-redata), or clear it (--no-redata) and accept empty panels."
            ),
            id="dashboard.W003",
        ),
    ]


@register()
def check_metrics_endpoint_is_guarded(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """Fail startup when /metrics is enabled without a token or allowlist on production."""
    if not getattr(settings, "UL_METRICS_ENABLED", False):
        return []
    if getattr(settings, "UL_METRICS_TOKEN", "") or getattr(settings, "UL_METRICS_ALLOWED_CIDRS", ""):
        return []
    if not getattr(settings, "IS_PRODUCTION", False):
        return []

    return [
        Error(
            "UL_METRICS_ENABLED is on with neither UL_METRICS_TOKEN nor UL_METRICS_ALLOWED_CIDRS set, so /metrics "
            "would answer any request that reaches it with a description of every view this deployment serves. "
            "Set a token for the scraper to present, or the CIDRs it scrapes from (or both), or turn the endpoint off.",
            id="dashboard.E006",
        ),
    ]


@register()
def check_celery_failures_cannot_requeue_forever(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """Fail startup when Celery settings allow unbounded task requeue."""
    if not getattr(settings, "CELERY_TASK_ACKS_LATE", False):
        return []

    errors: list[CheckMessage] = []
    if getattr(settings, "CELERY_TASK_REJECT_ON_WORKER_LOST", False):
        errors.append(
            Error(
                "CELERY_TASK_REJECT_ON_WORKER_LOST is on together with CELERY_TASK_ACKS_LATE, so a task whose child is "
                "killed mid-run (an OOM kill, or a decoder segfault) is requeued unconditionally and immediately, to a "
                "worker that will die the same way. The loop is unbounded and emits no failure event, so it silently "
                "consumes a concurrency slot. Set it to False, so the loss is acknowledged and reported once.",
                id="dashboard.E007",
            ),
        )
    if not getattr(settings, "CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT", True):
        errors.append(
            Error(
                "CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT is off together with CELERY_TASK_ACKS_LATE, so any task that "
                "exceeds CELERY_TASK_TIME_LIMIT is requeued unconditionally rather than failed, and will exceed the "
                "limit again on every redelivery. Leave it at Celery's default of True.",
                id="dashboard.E008",
            ),
        )
    return errors


def websocket_frame_cap_conflict() -> str | None:
    """Report when daphne flags cap frames below the app-level limit."""
    required = int(getattr(settings, "UL_WEBSOCKET_MAX_MESSAGE_BYTES", 0) or 0)
    for flag in ("--websocket-max-message-size", "--websocket-max-frame-size"):
        if flag not in sys.argv:
            continue
        index = sys.argv.index(flag) + 1
        try:
            deployed = int(sys.argv[index]) if index < len(sys.argv) else 0
        except ValueError:
            continue
        if deployed < required:
            return (
                f"daphne was started with {flag}={deployed}, below the {required} bytes "
                "UL_WEBSOCKET_MAX_FRAME_CHARS implies. A frame the consumers would accept is refused by "
                "autobahn instead, which closes the socket without sending anything - the user sees an "
                f"unexplained disconnect. Raise {flag} to at least {required}, or lower "
                "UL_WEBSOCKET_MAX_FRAME_CHARS."
            )
    return None
