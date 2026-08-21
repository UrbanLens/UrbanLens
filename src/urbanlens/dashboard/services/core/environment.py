"""Which deployment this process is, for service code that must behave differently off production.

Most environment branching in this codebase is about convenience (debug
toolbars, relaxed HTTPS, seeded demo data) and is decided once in
``settings/base.py``. This module exists for the case where it is about
*consequences*: work that reaches outside this deployment and leaves a durable
mark on somebody else's system.

The motivating case is REData. Nearly every REData endpoint is safe to call
from anywhere - even the POST-shaped ones, which mostly just ask REData to go
fetch third-party data about a place and cache it, so pointing a dev slot at
production REData is actively *better* than giving it its own instance (shared
cache, no duplicate third-party quota burn). A handful of endpoints are
different in kind: they send UrbanLens's own content - photo observations,
relevance votes, a user's label taxonomy and which labels they put where - for
REData to store and train models on. Those are the ones a throwaway deployment
must not touch, because fabricated demo data is indistinguishable from real
data once it is in the corpus.

Guards live at the gateway methods that own those endpoints (see
``services.apis.photos.redata_photos_gateway`` and
``services.apis.labels.redata_labels_gateway``), so every caller - Celery task,
management command, or shell - is covered by the same check.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def is_production() -> bool:
    """Whether this process is the real production deployment.

    Reads ``settings.IS_PRODUCTION``, which ``settings/base.py`` derives from
    ``UL_ENVIRONMENT`` through ``settings._env.is_production_environment``.

    Fail-closed twice over: that classifier only recognises an explicit
    allow-list of names, and the ``getattr`` default here means a settings
    module that somehow never defined the flag reads as non-production rather
    than raising or defaulting true.

    Returns:
        True only when this deployment is production.
    """
    return bool(getattr(settings, "IS_PRODUCTION", False))


def skip_upstream_contribution(surface: str, *, detail: str = "") -> bool:
    """Whether to skip sending UrbanLens's own data to ``surface`` for storage/training.

    Call this at the point of departure, and return the callee's normal
    "nothing to send" result when it answers True - a skip is an ordinary
    outcome of running outside production, not a failure, so it must not
    raise, must not be reported as an error, and must not be retried.

    Logged at INFO rather than WARNING for the same reason: the message exists
    so a developer wondering why their dev instance's photos never got scored
    can find the answer, not to flag a problem.

    Args:
        surface: Human-readable name of what would have been written, e.g.
            ``"REData photo observations (POST /photos/)"``. Appears verbatim
            in the log line, so make it greppable against the code.
        detail: Optional extra context for the log line, e.g. how many records
            were held back.

    Returns:
        True when the caller must not send, False on production.
    """
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
