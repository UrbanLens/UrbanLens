"""Startup checks that keep whole classes of mistake from reaching production.

Registered from :meth:`urbanlens.dashboard.apps.DashboardConfig.ready`, so they
run on every ``manage.py check``, ``migrate``, ``runserver``, and test session.
"""

from __future__ import annotations

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

#: Only this project's own models are checked. A third-party app's FileField
#: writes into MEDIA_ROOT too, but its prefix is not ours to authorize and the
#: gate already refuses what it does not recognize - so an unregistered
#: dependency fails closed rather than failing the check.
_OWN_APP_PREFIX = "urbanlens."


def _declared_family(field: FileField) -> tuple[str | None, str | None]:
    """Resolve the media family a file field writes into.

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
            # getattr, not a bare .__qualname__: an upload_to can be any callable
            # (a callable class instance, a functools.partial, ...), not only a
            # plain function/method, and only those guarantee __qualname__.
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
    """Verify every stored-file field has someone deciding who may read it.

    ``MediaGateView`` authorizes a request by the file's leading path segment
    and refuses anything it does not recognize. That is the right runtime
    behaviour, but on its own it means a new ``upload_to`` prefix breaks image
    loading silently and at the worst moment. This check turns the same
    omission into a startup error naming the field, so the choice of policy is
    made when the field is added rather than discovered in production.

    The pairing is what makes the media gate structural: unknown files are
    denied, and you cannot ship an unknown file family by accident.

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
    """Verify a configured media origin can actually issue its credential.

    Serving uploads from their own hostname (``UL_MEDIA_BASE_URL``) only works
    if the browser will send the media cookie there, and that hinges entirely
    on the ``Domain`` attribute
    :func:`~urbanlens.dashboard.services.media.origin.cookie_domain` derives
    from ``SITE_URL`` and the media host. When it cannot derive one, every
    ``set_media_cookie`` call becomes a no-op and the media origin answers 404
    for every request - with no exception, no log line, and a working-looking
    app whose images have all silently vanished.

    That is not hypothetical: this check exists because the first version of
    that function read ``settings.UL_SITE_URL`` (the *environment variable's*
    spelling; the setting is ``SITE_URL``), got ``""`` on every real
    deployment, and would have shipped exactly that outage. A test suite that
    ``override_settings``-es a name into existence cannot catch that class of
    mistake, because the override invents the very setting production lacks -
    so the guard has to run against the real settings, which is what a system
    check is.

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


#: The credentials that must exist only in the ai-inference container. Named
#: here rather than imported from ``AppSettings`` so this check keeps working
#: if a field is renamed there - a rename that silently emptied this tuple
#: would turn the check into a no-op, which is worse than a stale name.
_PROVIDER_KEY_SETTINGS = ("anthropic_api_key", "openai_api_key", "cloudflare_ai_api_key")


@register()
def check_provider_keys_are_not_on_the_app_tier(app_configs: Sequence[AppConfig] | None = None, **kwargs: object) -> list[CheckMessage]:
    """Warn when a provider API key is readable by a process that routes inference remotely.

    ``ai-inference`` exists so provider credentials never sit in the same
    process as the database credentials and the field-encryption key. The one
    way that quietly stops being true is a key finding its way back into the
    root ``.env``, which ``app`` and ``celery-worker`` load wholesale via
    ``env_file`` - nothing would break, and nothing would say so.

    Deliberately a warning, not an error: the key being present is a
    misconfiguration, not a failure, and blocking every ``manage.py`` command
    over it would be worse than the problem. Deliberately silent when
    ``ai_inference_url`` is unset, because that is a local checkout using
    ``LocalInferenceClient``, where these keys are exactly where they should be.

    Args:
        app_configs: Unused; part of Django's check signature.
        **kwargs: Unused; part of Django's check signature.

    Returns:
        One warning naming every provider key that should have been in
        ``.env.ai`` instead, or an empty list.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    if not getattr(app_settings, "ai_inference_url", None):
        # No remote inference tier configured - in-process provider calls are
        # the intended path here, so the keys belong in this process.
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
