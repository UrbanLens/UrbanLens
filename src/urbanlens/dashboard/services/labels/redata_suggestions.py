"""Wires UrbanLens's tag/category labels up to REData's label-suggestion service.

**Only ``KIND_TAG`` and ``KIND_CATEGORY`` labels are ever synced or suggested.**
Status, people, and media labels never leave this codebase - they aren't
"which of my labels applies to this place" in the sense REData models, and
people/media labels in particular attach to profiles/images rather than
places at all.

Three kinds of data flow out to REData
(``services.apis.labels.redata_labels_gateway``):

- **Taxonomy** (``POST /labels/``), whenever a tag/category label is
  created, edited, reparented, retired (kind changed away from tag/category,
  or deleted) - see :func:`queue_label_definition_sync` and
  :func:`queue_label_retirement`. REData's taxonomy is per-profile
  (``user_id``), so a *global* label (``profile=None``, visible to every
  profile) is synced once per profile rather than once overall - see
  :func:`_profile_ids_for_label`.
- **Assignments** (``POST /labels/assignments/``), whenever a pin's
  tag/category label set changes - see :func:`queue_pin_assignment_sync`.
  Always sent as ``replace: true`` with the pin's complete current set: pin
  labels are mutated from ~20 call sites across the codebase (manual
  tagging, keyword/AI auto-tag, imports, merges, undo), so resyncing the
  full set on every change is far simpler and more robust than tracking
  incremental deltas - and REData explicitly documents a resend of
  unchanged data as a no-op.
- **Suggestions** (``POST /labels/suggest/``), a live read called directly
  from the pin label dialog - see :func:`get_suggestions`. Not queued: the
  caller needs the answer to render a response.

All three are best-effort: a missing/unreachable REData deployment (the
common case for most installs - see ``_redata_configured``) is a silent
no-op, not an error, exactly like every other REData integration in this
codebase (see ``services.apis.locations.places_resolution._redata_configured``).

The first two send UrbanLens's own content for REData to store and train on,
so they additionally only fire from production. The gateway is what enforces
that (see ``services.core.environment``), which is why the queue/sync helpers
here do not re-check it; :func:`backfill_profile` is the one exception, and
only so the counts it reports stay truthful. Suggestions are a read and are
unaffected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def _redata_configured() -> bool:
    """Whether REData is configured, mirroring ``places_resolution._redata_configured``."""
    from urbanlens.UrbanLens.settings.app import settings

    return bool(settings.redata_api_url and settings.redata_api_key)


def redata_labels_configured() -> bool:
    """Public alias of :func:`_redata_configured`, for callers outside this module (e.g. management commands)."""
    return _redata_configured()


def _user_id(profile: Profile) -> str:
    """REData ``user_id`` for a profile - namespaced separately from the photos feature's own hashing."""
    return str(profile.uuid)


def _label_definition(label: Label, *, is_active: bool) -> dict[str, Any]:
    """Build one ``POST /labels/`` definition dict for ``label``.

    ``name`` is always the label's canonical (non-customized) name: a
    profile's ``LabelCustomization`` override is cosmetic and per-viewer, and
    would pollute REData's ``canonical_key`` folding (meant to link *shared*
    vocabulary across users) with per-user styling.

    Args:
        label: The label to describe.
        is_active: False retires the label - see :func:`queue_label_retirement`.
    """
    return {
        "external_id": str(label.uuid),
        "name": label.name,
        "parent_ids": [str(u) for u in label.parents.suggestable().values_list("uuid", flat=True)],
        "description": label.description or "",
        "is_active": is_active,
    }


def _profile_ids_for_label(label: Label) -> list[int]:
    """Every profile whose REData taxonomy should carry this label's definition.

    A profile-owned label belongs to just that profile's taxonomy. A global
    label (``profile=None``) is visible to - and so synced for - every
    profile, since REData's taxonomy is namespaced per ``user_id`` with no
    concept of a label shared across namespaces (only ``canonical_key``
    links them, computed independently on REData's side).
    """
    if label.profile_id is not None:
        return [label.profile_id]
    from urbanlens.dashboard.models.profile.model import Profile

    return list(Profile.objects.values_list("pk", flat=True))


def _pin_assignment_entry(pin: Pin) -> dict[str, Any]:
    """Build one ``POST /labels/assignments/`` location entry for ``pin``'s current tag/category set."""
    from django.utils import timezone

    entry: dict[str, Any] = {
        "external_id": str(pin.uuid),
        "latitude": pin.effective_latitude,
        "longitude": pin.effective_longitude,
        "label_ids": [str(u) for u in pin.labels.suggestable().values_list("uuid", flat=True)],
        "replace": True,
        "assigned_at": timezone.now().isoformat(),
    }
    if pin.name:
        entry["names"] = [pin.name]
    return entry


def sync_label_definitions(profile_ids: list[int], definitions: list[dict[str, Any]]) -> None:
    """Push ``definitions`` into every listed profile's REData taxonomy.

    Called from the ``sync_redata_label_definitions`` Celery task - never
    call this synchronously from a request/view. One REData call per
    profile, since ``POST /labels/`` is scoped to a single ``user_id``.
    Best-effort: a REData request failure is logged and swallowed for that
    profile, and the remaining profiles are still attempted.

    Args:
        profile_ids: Profiles whose taxonomy should receive ``definitions``.
        definitions: Definition dicts built by :func:`_label_definition`.
    """
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.apis.labels.redata_labels_gateway import RedataLabelsGateway
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError

    if not _redata_configured() or not definitions:
        return

    gateway = RedataLabelsGateway()
    for profile in Profile.objects.filter(pk__in=profile_ids):
        try:
            gateway.define_labels(_user_id(profile), definitions)
        except GatewayRequestError as exc:
            logger.warning("REData label definition sync failed for profile %s: %s", profile.pk, exc)


def sync_pin_assignment(pin: Pin) -> None:
    """Push ``pin``'s complete current tag/category label set to REData.

    Called from the ``sync_redata_pin_assignment`` Celery task - never call
    this synchronously from a request/view. Best-effort like
    :func:`sync_label_definitions`.

    Args:
        pin: The pin whose assignment changed. Skipped (not sent) if it has
            no profile.
    """
    if not _redata_configured() or pin.profile_id is None:
        return

    from urbanlens.dashboard.services.apis.labels.redata_labels_gateway import RedataLabelsGateway
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError

    try:
        RedataLabelsGateway().sync_assignments(_user_id(pin.profile), [_pin_assignment_entry(pin)])
    except GatewayRequestError as exc:
        logger.warning("REData assignment sync failed for pin %s: %s", pin.pk, exc)


def backfill_profile(profile: Profile) -> tuple[int, int]:
    """Push a profile's complete current tag/category taxonomy and pin assignments to REData.

    Safe to call repeatedly - REData's own upsert/resend semantics mean this
    never duplicates or corrupts existing state. Used by the
    ``backfill_redata_labels`` management command to prime a fresh (or
    freshly reset) REData deployment with data that predates this
    integration; ongoing changes are kept in sync automatically via
    ``models.labels.signals``/``models.pin.signals`` instead, so this is
    never queued or triggered from a signal itself.

    Args:
        profile: The profile to backfill.

    Returns:
        ``(labels_synced, pins_synced)`` counts - not a success/failure
        signal, since individual batch failures are logged and swallowed
        exactly like every other REData call in this module. ``(0, 0)`` off
        production, where nothing is sent at all.
    """
    from urbanlens.dashboard.services.core.environment import skip_upstream_contribution

    if not _redata_configured():
        return (0, 0)
    # Checked here as well as in the gateway (which is what actually enforces
    # it) so the counts this returns - and the per-profile totals the
    # backfill_redata_labels command prints from them - do not claim work that
    # never left the process.
    if skip_upstream_contribution("REData label backfill", detail=f"profile {profile.pk}"):
        return (0, 0)

    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.labels.redata_labels_gateway import (
        MAX_LABELS_PER_DEFINE_REQUEST,
        MAX_LOCATIONS_PER_ASSIGNMENT_REQUEST,
        RedataLabelsGateway,
    )
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError

    labels = list(Label.objects.visible_to(profile).suggestable())
    definitions = [_label_definition(label, is_active=True) for label in labels]
    for i in range(0, len(definitions), MAX_LABELS_PER_DEFINE_REQUEST):
        sync_label_definitions([profile.pk], definitions[i : i + MAX_LABELS_PER_DEFINE_REQUEST])

    pins = list(Pin.objects.filter(profile=profile, location__isnull=False))
    entries = [_pin_assignment_entry(pin) for pin in pins]
    gateway = RedataLabelsGateway()
    for i in range(0, len(entries), MAX_LOCATIONS_PER_ASSIGNMENT_REQUEST):
        try:
            gateway.sync_assignments(_user_id(profile), entries[i : i + MAX_LOCATIONS_PER_ASSIGNMENT_REQUEST])
        except GatewayRequestError as exc:
            logger.warning("REData assignment backfill failed for profile %s: %s", profile.pk, exc)

    return (len(definitions), len(pins))


def queue_label_definition_sync(label: Label) -> None:
    """Queue an upsert of ``label``'s REData definition, if it's a tag/category label.

    Safe to call unconditionally from any Label save - a status/people/media
    label is silently skipped, so callers never need their own kind guard.

    Args:
        label: The saved label.
    """
    from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG

    if not _redata_configured() or label.kind not in (KIND_TAG, KIND_CATEGORY):
        return
    _queue_definitions(_profile_ids_for_label(label), [_label_definition(label, is_active=True)])


def queue_label_retirement(label: Label) -> None:
    """Queue retirement (``is_active: false``) of a label that used to be tag/category.

    Called when a label is deleted, or its kind changes away from
    tag/category - REData has no delete endpoint, only retirement, and a
    retired label "stops being suggested, but its history stays."

    Not gated on ``label.kind`` here - unlike :func:`queue_label_definition_sync`.
    By the time a kind-change caller reaches this function, ``label.kind`` is
    already the *new*, non-suggestable kind (that's why it's retiring), so a
    check against the current kind could never pass. Both real callers
    (``models.labels.signals``' ``post_delete`` and kind-change handlers)
    already decide whether retirement is appropriate - based on the kind the
    label *was* - before calling this.

    Args:
        label: The label being deleted or converted away. ``parent_ids`` are
            deliberately omitted (always ``[]``) rather than queried - a
            label mid-delete may have already lost its M2M rows, and a
            retired definition's parents are moot.
    """
    if not _redata_configured():
        return
    definition = {"external_id": str(label.uuid), "name": label.name, "parent_ids": [], "description": label.description or "", "is_active": False}
    _queue_definitions(_profile_ids_for_label(label), [definition])


def _queue_definitions(profile_ids: list[int], definitions: list[dict[str, Any]]) -> None:
    if not profile_ids:
        return
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import sync_redata_label_definitions

    transaction.on_commit(lambda: safely_enqueue_task(sync_redata_label_definitions, profile_ids, definitions))


def queue_pin_assignment_sync(pin_id: int) -> None:
    """Queue a resync of one pin's tag/category assignment set.

    Safe to call unconditionally from any ``Pin.labels`` change - see
    ``models.pin.signals``' ``m2m_changed`` receiver, which is the only
    caller. Not gated on the pin's kind mix here: a resync always sends the
    pin's *current* tag/category set regardless of which label kind actually
    changed, so an unnecessary call is a harmless no-op resend, not a
    correctness issue.

    Args:
        pin_id: PK of the pin whose label set changed.
    """
    if not _redata_configured():
        return

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import sync_redata_pin_assignment

    transaction.on_commit(lambda: safely_enqueue_task(sync_redata_pin_assignment, pin_id))


def get_suggestions(pin: Pin, *, limit: int | None = None) -> list[tuple[Label, float]] | None:
    """Ask REData which of the pin owner's own tag/category labels likely apply here.

    Called synchronously from ``controllers.labels.LabelPinSuggestionsView`` -
    a live read, not queued. REData's ``implied`` (ancestors of what's
    already applied) is deliberately not surfaced here: UrbanLens already
    knows its own hierarchy and renders applied labels - including their
    ancestors - through the existing label-chip UI, so re-deriving that from
    REData's answer would be redundant.

    Args:
        pin: The pin to suggest labels for.
        limit: Max results - passed through to REData (default 10, max 50).

    Returns:
        ``(label, confidence)`` pairs for labels REData suggests that still
        resolve to a real local ``Label`` row, highest confidence first
        (REData's own ordering is preserved). ``None`` when REData isn't
        configured or the request fails - callers should render no
        suggestions, not an error.
    """
    if not _redata_configured() or pin.profile_id is None:
        return None

    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.services.apis.labels.redata_labels_gateway import RedataLabelsGateway
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError

    profile = pin.profile
    applied_ids = [str(u) for u in pin.labels.suggestable().values_list("uuid", flat=True)]
    try:
        response = RedataLabelsGateway().suggest_labels(
            _user_id(profile),
            pin.effective_latitude,
            pin.effective_longitude,
            names=[pin.name] if pin.name else None,
            applied_label_ids=applied_ids or None,
            limit=limit,
        )
    except GatewayRequestError as exc:
        logger.warning("REData label suggestion failed for pin %s: %s", pin.pk, exc)
        return None

    import uuid as uuid_module

    results = response.get("results") or []
    result_uuids: list[str] = []
    for r in results:
        if not isinstance(r, dict) or not r.get("label_id"):
            continue
        try:
            result_uuids.append(str(uuid_module.UUID(str(r["label_id"]))))
        except (ValueError, AttributeError, TypeError):
            continue
    if not result_uuids:
        return []
    labels_by_uuid = {str(label.uuid): label for label in Label.objects.filter(uuid__in=result_uuids).suggestable()}

    suggestions: list[tuple[Label, float]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        label = labels_by_uuid.get(str(result.get("label_id") or ""))
        confidence = result.get("confidence")
        if label is not None and isinstance(confidence, int | float):
            suggestions.append((label, float(confidence)))
    return suggestions
