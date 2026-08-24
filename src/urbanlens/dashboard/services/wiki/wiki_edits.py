"""Shared wiki field-editing and revert logic.

Extracted from ``controllers/location_wiki.py`` so the internal HTMX views and
the external API apply community edits through one implementation - the audit
trail (``WikiEdit.changes``) and the conflict-aware revert rules are exactly
the kind of thing that silently diverges when two callers each grow their own
copy.

The one deliberate behavioral difference between the two callers is the
``strict`` flag on :func:`apply_wiki_edit`; see its docstring.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.core.text_limits import MAX_WIKI_DESCRIPTION_LENGTH, text_length_error

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

#: The eight security indicators a community member may edit. Mirrors
#: ``models.abstract.security.SECURITY_FIELDS`` (names only, in the same order).
WIKI_SECURITY_FIELDS: tuple[str, ...] = ("fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked")

#: Fields a community member may edit via "Suggest edits". Coordinates are not
#: editable here: a Wiki's Location is fixed at creation and is not something
#: a community edit may repoint.
WIKI_EDITABLE_FIELDS: tuple[str, ...] = ("name", "description", *WIKI_SECURITY_FIELDS, "date_abandoned", "date_last_active")

#: Fields parsed as ``YYYY-MM-DD`` dates rather than free text.
WIKI_DATE_FIELDS = frozenset({"date_abandoned", "date_last_active"})


class WikiEditValidationError(ValueError):
    """A submitted wiki edit was rejected.

    Attributes:
        message: Human-readable explanation, safe to return to the caller.
        field: The offending field name, or None for a whole-payload problem.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        """Store the message and the field it applies to."""
        super().__init__(message)
        self.message = message
        self.field = field


def save_edited_fields(wiki: Wiki, changed_fields: Iterable[str]) -> None:
    """Persist only the wiki columns named in *changed_fields*.

    A bare ``wiki.save()`` writes every column from the in-memory instance, which
    on a community-editable row means reverting whatever was committed since the
    request loaded it - a second editor's change to a different field, or
    ``viewed_by_other``, which the view sets through its own targeted update.
    That also silently defeats :func:`revert_edit_fields`' conflict check, which
    goes to the trouble of leaving changed-since fields alone.

    Args:
        wiki: The wiki to save. Already mutated by the caller.
        changed_fields: Names of the fields that were changed. Boundary keys
            (``bounding_box``, ``boundary_*``) are dropped: those edits live on
            ``Boundary`` rows, which are saved separately.
    """
    columns = [field for field in changed_fields if field != "bounding_box" and not field.startswith("boundary_")]
    wiki.save(update_fields=[*columns, "updated"])


def apply_wiki_edit(wiki: Wiki, profile: Profile, changes: dict[str, Any], *, strict: bool) -> WikiEdit | None:
    """Apply a community edit to *wiki* and record it in the audit trail.

    Only keys in :data:`WIKI_EDITABLE_FIELDS` are considered; anything else in
    *changes* is ignored. A value equal to the wiki's current value is not
    recorded as a change.

    Args:
        wiki: The wiki to edit. Mutated and saved in place when anything
            actually changes.
        profile: The editing profile, recorded as ``WikiEdit.editor``.
        changes: Raw submitted ``{field: value}`` mapping.
        strict: How to treat a value that fails validation - an unrecognized
            security level, or a date that isn't ``YYYY-MM-DD``.

            ``True`` (the external API) raises
            :class:`WikiEditValidationError`, so a client is told its write was
            rejected instead of being handed a success it didn't get.

            ``False`` (the internal HTMX view) skips the offending field and
            continues, preserving that view's long-standing behavior. This is a
            real defect - the user sees ``{"ok": true}`` and the field silently
            never changes - recorded under "Messaging / external API (noted
            2026-07-26)" in ``docs/PROBLEMS.md`` (the strict-vs-lenient wiki
            edit item) rather than
            being changed blind here, because fixing it properly needs
            field-level error rendering in the About card.

    Returns:
        The recorded :class:`WikiEdit`, or ``None`` when nothing changed.

    Raises:
        WikiEditValidationError: A description over
            :data:`MAX_WIKI_DESCRIPTION_LENGTH` (in both modes), or - when
            *strict* - an invalid security value or unparseable date.
    """
    valid_security = {value for value, _label in SecurityLevel.choices}
    # new_vals holds the actual Python values to set on the wiki.
    # audit holds JSON-safe strings for the WikiEdit audit record.
    new_vals: dict[str, object] = {}
    audit: dict[str, dict] = {}

    for field in WIKI_EDITABLE_FIELDS:
        if field not in changes:
            continue
        raw = changes[field]
        old_val = getattr(wiki, field, None)
        if str(raw) == str(old_val):
            continue

        if field in WIKI_SECURITY_FIELDS:
            if raw not in valid_security:
                if strict:
                    raise WikiEditValidationError(f"'{raw}' is not a valid value for {field}.", field)
                continue
            new_val: object = raw
        elif field in WIKI_DATE_FIELDS:
            if not raw:
                new_val = None
            elif isinstance(raw, datetime):
                new_val = raw.date()
            else:
                try:
                    new_val = datetime.strptime(str(raw), "%Y-%m-%d").date()
                except ValueError:
                    if strict:
                        raise WikiEditValidationError(f"{field} must be a date in YYYY-MM-DD format.", field) from None
                    continue
        elif field == "description":
            length_error = text_length_error(raw, MAX_WIKI_DESCRIPTION_LENGTH, "Description")
            if length_error:
                # Rejected in both modes: the internal view already returned a
                # 400 here rather than skipping, so this is not the silent-skip
                # behavior the strict flag governs.
                raise WikiEditValidationError(length_error, field)
            new_val = raw
        else:
            new_val = raw

        new_vals[field] = new_val
        audit[field] = {"from": str(old_val), "to": str(new_val)}

    if not audit:
        return None

    for field, value in new_vals.items():
        setattr(wiki, field, value)
    save_edited_fields(wiki, new_vals)

    return WikiEdit.objects.create(wiki=wiki, editor=profile, changes=audit)


def revert_edit_fields(location: Location, wiki: Wiki, target_edit: WikiEdit) -> tuple[dict[str, dict], list[str]]:
    """Restore the fields captured in ``target_edit.changes`` to their prior ("from") values.

    Mutates ``wiki`` (and any associated Boundary rows) in place - the caller
    is responsible for persisting it afterwards, via :func:`save_edited_fields`
    and the returned diff - a bare ``save()`` would undo the conflict check
    below by writing back every field it deliberately left alone.

    Before restoring each field, checks whether the wiki's *current* value for
    that field still matches ``target_edit``'s stored "to" value. If someone
    else changed the field again after this edit (so the current value no
    longer matches), restoring the edit's "from" value would silently clobber
    that later change - so the field is left untouched and its name is
    collected in the returned skip list instead.

    Args:
        location: The wiki's Location, needed to create a Boundary row when
            reverting a boundary change that had deleted one.
        wiki: The wiki being reverted. Mutated in place.
        target_edit: The edit whose changes are being undone.

    Returns:
        A tuple of:
        - A diff dict in the same ``{"field": {"from": ..., "to": ...}}``
          shape used by ``WikiEdit.changes``, computed against the values as
          they stood right before this call - only for fields actually
          reverted.
        - A list of field names skipped because their current value no
          longer matched this edit's "to" value (a conflicting later edit).
    """
    revert_changes: dict[str, dict] = {}
    skipped_fields: list[str] = []
    for field, diff in target_edit.changes.items():
        old_val = diff.get("from")
        to_val = diff.get("to")
        if field == "bounding_box" or field.startswith("boundary_"):
            # "bounding_box" is the legacy audit key from the single-boundary
            # era; treat it as the property boundary.
            boundary_type = field.removeprefix("boundary_") if field.startswith("boundary_") else BoundaryType.PROPERTY
            if boundary_type not in BoundaryType.values:
                continue
            row = Boundary.objects.row_for_wiki(wiki, boundary_type)
            current_val = row.polygon.wkt if row and row.polygon else None
            if current_val != to_val:
                skipped_fields.append(field)
                continue
            revert_changes[field] = {"from": current_val, "to": old_val}
            if old_val:
                restored = GEOSGeometry(old_val, srid=4326)
                if isinstance(restored, Polygon):
                    restored = MultiPolygon(restored, srid=restored.srid)
                if row is None:
                    row = Boundary(wiki=wiki, location=location, boundary_type=boundary_type)
                row.polygon = restored
                row.save()
            elif row is not None:
                row.delete()
        elif field in {"latitude", "longitude"}:
            # Coordinates are no longer editable - a Wiki's Location is fixed
            # at creation. Skip so legacy WikiEdit rows recorded before this
            # rule don't error out on revert.
            continue
        else:
            current_val = getattr(wiki, field, None)
            if str(current_val) != to_val:
                skipped_fields.append(field)
                continue
            revert_changes[field] = {"from": current_val, "to": old_val}
            setattr(wiki, field, old_val)
    return revert_changes, skipped_fields


def revert_wiki_edit(location: Location, wiki: Wiki, profile: Profile, target_edit: WikiEdit) -> tuple[WikiEdit | None, list[str]]:
    """Undo *target_edit*, recording the reversal as a new :class:`WikiEdit`.

    Args:
        location: The wiki's Location (see :func:`revert_edit_fields`).
        wiki: The wiki being reverted.
        profile: The profile performing the revert.
        target_edit: The edit to undo. Must not already be reverted - callers
            check ``target_edit.reverted`` first and report that separately.

    Returns:
        A tuple of the new reverting :class:`WikiEdit` (or ``None`` when every
        field had been changed again since, so there was nothing left to
        revert) and the list of field names skipped for that reason.
    """
    revert_changes, skipped_fields = revert_edit_fields(location, wiki, target_edit)

    if not revert_changes:
        # Every field this edit touched was changed again by someone else
        # since - nothing left to revert. Don't record a no-op WikiEdit or
        # mark the original as reverted.
        return None, skipped_fields

    save_edited_fields(wiki, revert_changes)

    revert_edit = WikiEdit.objects.create(
        wiki=wiki,
        editor=profile,
        changes=revert_changes,
    )
    target_edit.reverted = True
    target_edit.reverted_by = revert_edit
    target_edit.save(update_fields=["reverted", "reverted_by", "updated"])

    # Reverting a *revert* puts the original edit's content back in force, so
    # its "reverted" flag would now lie - the history display and the
    # wiki-edits achievement metric (which excludes reverted rows) both read
    # it. Cleared only when this revert applied fully: after a partial revert
    # (skipped_fields non-empty) the earlier edit is only partially back, and
    # a conservative flag beats a false "in force".
    if not skipped_fields:
        undone_ids = list(target_edit.reverts.values_list("pk", flat=True))
        if undone_ids:
            WikiEdit.objects.filter(pk__in=undone_ids).update(reverted=False, reverted_by=None)
            _restore_reputation_for(undone_ids)

    return revert_edit, skipped_fields


def _restore_reputation_for(edit_ids: list[int]) -> None:
    """Un-retract the ledger rows for edits whose revert was itself reverted.

    Called here rather than left to the reputation signal, because the line
    above is a queryset ``update()`` and emits no ``post_save`` - so the
    handler watching for the flag to clear never sees it. Retraction was
    designed as a reversible flag specifically because a revert-of-a-revert
    puts the original edit back in force; without this the reversal half never
    ran, and the design's justification for the flag was untrue.

    Args:
        edit_ids: WikiEdit pks whose reverts have just been undone.
    """
    from urbanlens.dashboard.models.reputation.meta import TargetKind
    from urbanlens.dashboard.models.reputation.model import ReputationEvent
    from urbanlens.dashboard.services.reputation.scoring import restore_event

    rows = ReputationEvent.objects.filter(
        rule_key="wiki_field_edit",
        target_kind=TargetKind.WIKI_EDIT,
        target_id__in=edit_ids,
        retracted=True,
    )
    for event in rows:
        restore_event(event)
