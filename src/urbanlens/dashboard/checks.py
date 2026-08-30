"""Startup checks that keep whole classes of mistake from reaching production.

Registered from :meth:`urbanlens.dashboard.apps.DashboardConfig.ready`, so they
run on every ``manage.py check``, ``migrate``, ``runserver``, and test session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.apps import apps
from django.core.checks import Error, register
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
            hint = f"its upload_to callable {upload_to.__qualname__!r} does not declare which directory it writes into."
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
