"""Which deployment this process is, for service code that must behave differently off production."""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def is_production() -> bool:
    """Whether this process is the real production deployment.
    Fail-closed twice over: that classifier only recognises an explicit allow-list of names, and the ``getattr`` default here means a settings module that somehow never defined the flag reads as non-production rather than raising or defaulting true.

    Returns:
        True only when this deployment is production."""
    return bool(getattr(settings, "IS_PRODUCTION", False))


def skip_upstream_contribution(surface: str, *, detail: str = "") -> bool:
    """Whether to skip sending UrbanLens's own data to ``surface`` for storage/training.

    Args:
        surface: Human-readable name of what would have been written, e.g. ``"REData photo observations (POST /photos/)"``.
        detail: Optional extra context for the log line, e.g. how many records were held back.

    Returns:
        True when the caller must not send, False on production."""
    if is_production():
        return False

    logger.info(
        "Skipped %s%s: UrbanLens contributes its own data to upstream training surfaces only from "
        "production, and this deployment's UL_ENVIRONMENT is %r. Nothing was sent and nothing failed "
        "- reads and cache-fill calls against the same service are unaffected.",
        surface,
        f" ({detail})" if detail else "",
        getattr(settings, "ENVIRONMENT_NAME", None),
    )
    return True
