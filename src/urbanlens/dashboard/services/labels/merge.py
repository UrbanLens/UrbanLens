"""Merging one or more labels into a single surviving target label.

Extracted from ``controllers.labels``'s ``LabelMergeView`` (single-source, form
POST) and ``LabelMultiMergeView`` (multi-source, JSON POST), which each grew
their own copy of the logic. Both now delegate here, as does the external API's
merge endpoint.

Three real bugs in the controller versions are fixed rather than carried over:

1. **Non-atomic.** Both views performed a multi-step "move the attachments,
   then delete the source" sequence with no transaction. A failure partway
   through - an ``images.add`` that raised after the pins had moved, say -
   left attachments split across a label that was about to be deleted and one
   that wasn't, with no way to tell what had already run.
2. **Children were orphaned.** Neither view reparented ``source.children``
   before deleting the source. ``Label.parents`` is a plain M2M, so deleting a
   parent silently drops the edge and the whole subtree below it detached to
   the root - a data loss that is invisible until someone notices their
   hierarchy has flattened. Children are now reparented onto the target first.
3. **Wiki attachments were silently dropped by the multi-source path.**
   ``LabelMergeView`` moved ``wikis`` for every kind it supported;
   ``LabelMultiMergeView`` moved them only for ``KIND_CATEGORY``, so merging
   two tags or statuses through the bulk path lost their wiki attachments
   outright. This module moves ``wikis`` for every pin-style kind, which is
   the non-lossy reading and matches the single-merge path.

Merging is destructive and is **not** covered by the undo framework: once the
sources are gone there is no staged entry to restore them from. Callers
exposing this to a client should say so plainly.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.db import transaction

from urbanlens.dashboard.models.labels.meta import KIND_MEDIA, KIND_USER

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class LabelMergeError(Exception):
    """A merge was refused. ``safe_message`` is safe to show a caller verbatim."""

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


@dataclass(frozen=True)
class LabelMergeResult:
    """What one :func:`merge_labels` call did.

    Attributes:
        target_id: Primary key of the surviving label.
        merged_ids: Primary keys of the labels that were consumed and deleted,
            in the order they were processed.
        pins_moved: How many distinct pins gained the target label as a result.
            Pins that already carried the target are not counted, so this
            reports newly-created attachments rather than the sources' total
            pin count. Always 0 for the user and media kinds, which attach to
            profiles and images rather than pins.
    """

    target_id: int
    merged_ids: list[int]
    pins_moved: int


def _validate(target: Label, sources: Sequence[Label], profile: Profile) -> None:
    """Refuse any merge that crosses an ownership, kind, or protection boundary.

    Args:
        target: The surviving label.
        sources: The labels to consume.
        profile: The profile on whose behalf the merge runs.

    Raises:
        LabelMergeError: If any rule below is violated.
    """
    from urbanlens.dashboard.models.labels.model import Label as LabelModel

    if not sources:
        raise LabelMergeError("Select at least one label to merge.")

    # The target must be one this profile can actually see. Global labels are
    # legitimate *targets* (merging your own tag into a global category is a
    # normal cleanup), which is why this is visible_to rather than an
    # ownership check.
    if not LabelModel.objects.visible_to(profile).filter(pk=target.pk).exists():
        raise LabelMergeError("No such label to merge into.")

    source_ids = {source.pk for source in sources}
    if target.pk in source_ids:
        raise LabelMergeError("Cannot merge a label into itself.")

    for source in sources:
        if source.kind != target.kind:
            raise LabelMergeError("Labels must be of the same kind to be merged.")
        # Global labels (profile=None) can never be a merge source: they are
        # shared by every user, so consuming one would destroy other people's
        # data. A user who wants one gone can only stop using it.
        if source.profile_id != profile.pk:
            raise LabelMergeError("You can only merge labels you own.")
        if source.is_protected:
            raise LabelMergeError("Protected labels cannot be merged away.")


def _reparent_children(target: Label, source: Label) -> None:
    """Move *source*'s children onto *target* before *source* is deleted.

    Skips the target itself - a target that happens to be a child of the
    source must not be made its own parent, which would create the exact cycle
    ``services.labels.hierarchy.would_create_cycle`` exists to prevent.

    Args:
        target: The surviving label the children are moved onto.
        source: The label about to be deleted.
    """
    children = list(source.children.exclude(pk=target.pk))
    for child in children:
        child.parents.add(target)
        child.parents.remove(source)


@transaction.atomic
def merge_labels(*, target: Label, sources: Sequence[Label], profile: Profile) -> LabelMergeResult:
    """Merge every label in *sources* into *target*, then delete the sources.

    What "merging" moves depends on the kind, mirroring where labels of that
    kind actually attach:

    - ``KIND_USER`` - ``ProfileLabelAssignment`` rows are reassigned to the
      target via ``get_or_create``, so an author/subject pair already carrying
      the target is not duplicated.
    - ``KIND_MEDIA`` - the sources' images.
    - everything else (tag, category, status) - the sources' pins *and* wikis.

    In all cases the sources' children are reparented onto the target first,
    and the sources are deleted last. The whole operation is one transaction,
    so a failure anywhere leaves the labels exactly as they were.

    This is destructive and not undoable - see the module docstring.

    Args:
        target: The label that survives.
        sources: The labels to consume. Must all share ``target.kind``, be
            owned by *profile*, and not be protected.
        profile: The profile on whose behalf the merge runs.

    Returns:
        A :class:`LabelMergeResult` describing the merge.

    Raises:
        LabelMergeError: If the merge is refused (see :func:`_validate`). The
            message is safe to surface to the caller.
    """
    _validate(target, sources, profile)

    merged_ids: list[int] = []
    pins_moved = 0

    if target.kind == KIND_USER:
        from urbanlens.dashboard.models.labels.profile_assignment import ProfileLabelAssignment

        for source in sources:
            for assignment in ProfileLabelAssignment.objects.filter(label=source):
                ProfileLabelAssignment.objects.get_or_create(
                    author=assignment.author,
                    subject=assignment.subject,
                    label=target,
                )
            _reparent_children(target, source)
            merged_ids.append(source.pk)
            source.delete()
    elif target.kind == KIND_MEDIA:
        for source in sources:
            target.images.add(*source.images.all())
            _reparent_children(target, source)
            merged_ids.append(source.pk)
            source.delete()
    else:
        existing_pin_ids = set(target.pins.values_list("pk", flat=True))
        for source in sources:
            source_pin_ids = set(source.pins.values_list("pk", flat=True))
            pins_moved += len(source_pin_ids - existing_pin_ids)
            existing_pin_ids |= source_pin_ids

            target.pins.add(*source.pins.all())
            # Moved for every pin-style kind, not just categories - see the
            # module docstring's bug 3.
            target.wikis.add(*source.wikis.all())
            _reparent_children(target, source)
            merged_ids.append(source.pk)
            source.delete()

    logger.info("Merged labels %s into label %s for profile %s", merged_ids, target.pk, profile.pk)
    return LabelMergeResult(target_id=target.pk, merged_ids=merged_ids, pins_moved=pins_moved)
