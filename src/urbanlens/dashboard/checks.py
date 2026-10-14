"""Startup checks that fail fast on deploy-time misconfiguration."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Tags, Warning as CheckWarning, register
from django.db import DatabaseError, connections
from django.db.migrations.loader import MigrationLoader
from django.db.models import FileField

from urbanlens.dashboard.services.media.access import MEDIA_FAMILY_ATTR, registered_families

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.apps.config import AppConfig
    from django.core.checks import CheckMessage

#: Only our own models; third-party prefixes fail closed at runtime.
_OWN_APP_PREFIX = "urbanlens."


def _declared_family(field: FileField) -> tuple[str | None, str | None]:
    """
    Resolve the media family a file field writes into.

    Args:
        field: The model field to inspect.

    Returns:
        Tuple of (family, error_hint). Exactly one is None: a resolved family
        means no error, and a hint means the family could not be determined.
    """
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
    """
    Fail startup when a file field has no registered media authorizer.

    Args:
        app_configs: The app configs being checked, or None for all of them.
        **kwargs: Ignored; Django passes ``databases`` and friends.

    Returns:
        One error per file field whose family has no registered authorizer.
    """
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
    """
    Fail startup when a configured media origin cannot issue its cookie.

    Args:
        app_configs: The app configs being checked, or None for all of them.
        **kwargs: Ignored; Django passes ``databases`` and friends.

    Returns:
        One error when a media origin is configured but unusable.
    """
    from urllib.parse import urlsplit

    from urbanlens.dashboard.services.media.origin import PUBLIC_SUFFIXES, cookie_domain, media_origin, media_origin_host, shared_suffix

    if not media_origin():
        return []

    if not media_origin_host():
        return [
            Error(
                f"UL_MEDIA_BASE_URL is set to {media_origin()!r}, which has no hostname. It must be a full origin, e.g. https://media.example.com.",
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
                f"(example.com + media.example.com), or set UL_MEDIA_COOKIE_DOMAIN explicitly.",
                id="dashboard.E004",
            ),
        ]

    return []


#: Scratch subtrees under MEDIA_ROOT kept on local disk.
_LOCAL_ONLY_MEDIA_SUBTREES = (
    ("exports/", "services.import_export.export.export_dir"),
    ("imports/", "services.import_export.import_data"),
    ("preview_sources/", "services.media.previews"),
    ("import_previews/", "services.pins.import_preview"),
    ("confirmed_imports/", "services.pins.confirmed_import"),
)


@register()
def check_object_storage_is_configured(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """
    Fail startup when the object-store backend is incomplete or bypasses the media gate.

    Args:
        app_configs: The app configs being checked, or None for all of them.
        **kwargs: Ignored; Django passes ``databases`` and friends.

    Returns:
        Errors for an unusable configuration, and one warning naming what stays
        on local disk regardless.
    """
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


@register()
def check_metrics_endpoint_is_guarded(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """
    Fail startup when /metrics is enabled without a token or allowlist on production.

    Args:
        app_configs: The app configs being checked, or None for all of them.
        **kwargs: Ignored; Django passes ``databases`` and friends.

    Returns:
        One error when the endpoint is enabled, unguarded, and on a deployment
        that counts as production.
    """
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
    """
    Fail startup when Celery settings allow unbounded task requeue.

    Args:
        app_configs: The app configs being checked, or None for all of them.
        **kwargs: Ignored; Django passes ``databases`` and friends.

    Returns:
        One error per settings combination that can requeue without bound.
    """
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
    """
    Report when daphne flags cap frames below the app-level limit.

    Returns:
        A description of the conflict, or None when there is none - including
        when this process was not started with the flags at all.
    """
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


@register()
def check_every_task_declares_a_queue(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """
    Refuse to start when a task this codebase defines names no queue.

    A task with no ``queue=`` lands on ``celery``, which is where 87 of 96 of
    them were when it was measured - one pool, drained by one worker, so an
    account-sized job and a safety escalation shared four concurrency slots.
    The classification fixes that; this is what keeps it fixed, because the
    failure mode is silent. A task added without a queue goes on working, on
    whichever pool drains the default, and nothing says so.

    Scoped to tasks defined in ``urbanlens``: a third-party package's task is
    not ours to declare, and failing startup over one would make this a check
    somebody switches off. Test modules are out too - a probe task
    defined by a test is never enqueued by the running site, and requiring a
    class of it would make every such test carry a declaration that means
    nothing.

    Args:
        app_configs: The app configs being checked, or None for all of them.
        **kwargs: Ignored; Django passes ``databases`` and friends.

    Returns:
        One error naming every task that declares no queue.
    """
    from urbanlens.UrbanLens.celery import app as celery_app

    unrouted = sorted(name for name, task in celery_app.tasks.items() if (module := getattr(task, "__module__", "")).startswith("urbanlens.") and ".tests." not in module and not getattr(task, "queue", None))
    if not unrouted:
        return []
    return [
        Error(
            f"{len(unrouted)} Celery task(s) declare no queue, so they run on whichever pool drains the default "
            f"instead of the class they belong to: {', '.join(unrouted)}. Add queue=Queue.INTERACTIVE, Queue.BULK "
            "or Queue.MAINTENANCE to the @shared_task decorator - see services/sandbox/queues.py for which.",
            id="dashboard.E010",
        ),
    ]


@register()
def check_every_task_has_a_time_limit(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """
    Refuse to start when a task's time limits are missing, inverted, or above its queue's ceiling.

    Limits come from the decorator or, failing that, the queue default ``QueueTimeLimits`` annotates. The
    ceiling keeps an interactive task from holding a slot the safety escalations share for as long as a bulk
    job may, and every task within the hold the broker's timeouts are sized for, past which it would be delivered twice.

    Args:
        app_configs: The app configs being checked, or None for all of them.
        **kwargs: Ignored; Django passes ``databases`` and friends.

    Returns:
        One error naming every task whose limits are wrong.
    """
    from urbanlens.dashboard.services.core.task_limits import ceiling_for
    from urbanlens.UrbanLens.celery import app as celery_app

    problems = []
    for name, task in sorted(celery_app.tasks.items()):
        module = getattr(task, "__module__", "")
        if not module.startswith("urbanlens.") or ".tests." in module:
            continue
        soft, hard = task.soft_time_limit, task.time_limit
        queue = str(getattr(task, "queue", None) or "")
        if soft is None or hard is None:
            problems.append(f"{name} has no {'soft ' if soft is None else 'hard '}time limit")
        elif soft >= hard:
            problems.append(f"{name}'s soft limit ({soft}s) is not below its hard limit ({hard}s)")
        elif hard > (ceiling := ceiling_for(queue)):
            problems.append(f"{name}'s hard limit ({hard}s) is above the {queue} queue's ceiling ({ceiling}s)")
    if not problems:
        return []
    return [
        Error(
            f"{len(problems)} Celery task(s) have wrong time limits: {'; '.join(problems)}. Set soft_time_limit and time_limit on the @shared_task decorator, or move a long task to a batch queue - see services/core/task_limits.py.",
            id="dashboard.E013",
        ),
    ]


@register()
def check_a_channel_layer_is_configured(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """
    Warn when no channel layer is configured, which turns off every WebSocket.

    Args:
        app_configs: Unused; part of Django's check signature.
        **kwargs: Unused; part of Django's check signature.

    Returns:
        One warning, or an empty list.
    """
    if getattr(settings, "CHANNEL_LAYERS", None):
        return []

    return [
        CheckWarning(
            "No CHANNEL_LAYERS is configured, so every WebSocket connection is refused (close code 4503).",
            hint=(
                "settings/base.py defines one only when UL_DRAGONFLY_URL (or UL_VALKEY_URL/UL_REDIS_URL) is set, so a "
                "deployment without that store has nowhere to carry a broadcast: live notifications, direct messages, "
                "safety check-in chat and the game sessions all fall back to polling or stop working. Point the store "
                "variable at a reachable Dragonfly/Redis, or accept that this deployment has no live features."
            ),
            id="dashboard.W004",
        ),
    ]


@register(Tags.database)
def check_migration_history_is_known(app_configs: Sequence[AppConfig] | None = None, databases: Sequence[str] | None = None, **kwargs: object) -> list[CheckMessage]:
    """
    Refuse a database whose recorded migrations of our own apps this code does not have.

    Django ignores a recorded migration it cannot find, so a database migrated under an earlier, since consolidated
    history would otherwise look current while its schema is not. Runs where database checks run, which includes
    ``migrate``.

    Returns:
        One error per such database.
    """
    labels = {config.label for config in apps.get_app_configs() if config.name.startswith(_OWN_APP_PREFIX)}
    errors: list[CheckMessage] = []
    for alias in databases or ():
        try:
            loader = MigrationLoader(connections[alias], ignore_no_migrations=True)
        except DatabaseError:
            continue
        replaced = {key for migration in loader.disk_migrations.values() for key in migration.replaces}
        unknown = sorted(key for key in loader.applied_migrations or {} if key[0] in labels and key not in loader.disk_migrations and key not in replaced)
        if unknown:
            errors.append(
                Error(
                    f"Database {alias!r} records {len(unknown)} migration(s) this code does not have, such as {unknown[0][0]}.{unknown[0][1]}, so its schema may not be the one these models expect.",
                    hint=(
                        "It was migrated under an earlier migration history that has since been consolidated. A plugin "
                        "carrying that history brings such a database forward when migrate runs; without one, start "
                        "from a fresh database or restore a backup taken under the current history."
                    ),
                    id="dashboard.E014",
                ),
            )
    return errors


@register()
def check_broker_password_is_not_the_default(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """
    Warn when a deployment's broker still uses RabbitMQ's published default password.

    Compose builds the broker URL from UL_RABBITMQ_USER and UL_RABBITMQ_PASS, defaulting the password to "guest". The
    broker is reachable only inside the Compose network, so this is a warning rather than a refusal to start.
    """
    from urllib.parse import urlparse

    from urbanlens.UrbanLens.environments.meta import EPHEMERAL_ENVIRONMENTS

    if getattr(settings, "ENVIRONMENT_NAME", "") in EPHEMERAL_ENVIRONMENTS:
        return []
    if urlparse(getattr(settings, "CELERY_BROKER_URL", "") or "").password != "guest":  # noqa: S105
        return []
    return [
        CheckWarning(
            "The Celery broker's password is RabbitMQ's published default, 'guest'.",
            hint="Set UL_RABBITMQ_PASS (Compose) or a UL_RABBITMQ_URL with a password of your own; see .env-sample.",
            id="dashboard.W005",
        ),
    ]
