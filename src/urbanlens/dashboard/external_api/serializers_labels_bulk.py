"""Request/response types for label reorder and bulk delete/edit/convert.

Mirrors ``dashboard/controllers/organize.py`` (``OrganizePrioritySaveView``)
and ``dashboard/controllers/labels.py`` (``LabelBulk*View``), with one
structural departure: the internal bulk views are routed per label kind (one
tab of the Organize page at a time, via a ``label_kind`` URL segment), so a
single call only ever touches labels that already share a kind. A uuid-based
API caller has no reason to batch that way, so ``uuids`` here may span kinds
freely - each endpoint resolves and checks every label's own kind instead of
trusting a route parameter.
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES, KIND_CATEGORY, KIND_STATUS, KIND_TAG

#: Kinds a bulk convert may target. People/media labels are a structurally
#: separate hierarchy (see ``_parent_candidates`` in ``controllers/labels.py``)
#: that the internal UI never converts into via this mechanism either.
_CONVERTIBLE_KIND_CHOICES = [(KIND_TAG, "Tag"), (KIND_CATEGORY, "Category"), (KIND_STATUS, "Status")]


class LabelReorderSerializer(serializers.Serializer):
    """Validates a label priority-reorder request.

    ``uuids`` is the complete desired order, first = highest priority -
    matching ``OrganizePrioritySaveView``'s ``items`` semantics.
    """

    uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=1000)


class LabelReorderResponseSerializer(serializers.Serializer):
    """The outcome of a reorder request (schema-only)."""

    reordered = serializers.IntegerField(read_only=True)
    #: Global (site-wide) labels named in the request - a single shared
    #: ``order`` column means only the profile that owns a label may reorder
    #: it, so these were left untouched rather than silently rewriting the
    #: order everyone else on the site sees.
    skipped_global_uuids = serializers.ListField(child=serializers.UUIDField(), read_only=True)


class LabelBulkDeleteSerializer(serializers.Serializer):
    """Validates a bulk label-delete request."""

    uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=500)


class LabelBulkDeleteResponseSerializer(serializers.Serializer):
    """The outcome of a bulk delete (schema-only)."""

    deleted = serializers.IntegerField(read_only=True)


class LabelBulkEditSerializer(serializers.Serializer):
    """Validates a bulk label-edit request. Every field but ``uuids`` is optional.

    Absent means untouched, matching the rest of this API - contrast the
    internal view, whose ``icon``/``color`` blank out to null on any falsy
    submission rather than distinguishing "not sent" from "explicitly
    cleared".
    """

    uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1)
    icon = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=50)
    #: The same palette LabelWriteSerializer enforces on the single-label
    #: endpoints. Bulk edit writes the same column on the same model, and used
    #: to take any string and drop what it did not recognise.
    color = serializers.ChoiceField(choices=COLOR_CHOICES, required=False, allow_null=True, allow_blank=True)
    description = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    order = serializers.IntegerField(required=False)
    #: Delta-add only, matching the internal view - there is no bulk parent/
    #: child removal path there either.
    add_parent_uuids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=True)
    add_child_uuids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=True)

    def validate(self, attrs: dict) -> dict:
        """Bound every list against the same ceiling the internal door uses.

        `uuids` carried a literal ceiling and the two hierarchy lists carried
        none, which left the real cost - for every label, for every proposed
        parent, a `would_create_cycle` walk of the label graph in the database -
        as one capped number multiplied by an uncapped one.

        Read from settings at validation time rather than bound as a field
        `max_length`, which is fixed at import and so could be neither
        configured nor overridden in a test.

        Args:
            attrs: The validated field values.

        Returns:
            ``attrs`` unchanged.

        Raises:
            ValidationError: When any list is over the ceiling. Refused rather
                than trimmed, matching the internal door: a bulk edit that
                silently applied to part of what was selected is worse than one
                that says no.
        """
        from django.conf import settings

        ceiling = settings.LABEL_BULK_EDIT_MAX_IDS
        for field in ("uuids", "add_parent_uuids", "add_child_uuids"):
            if len(attrs.get(field) or []) > ceiling:
                raise serializers.ValidationError({field: f"Select at most {ceiling} items at a time."})
        return attrs


class LabelBulkEditResponseSerializer(serializers.Serializer):
    """The outcome of a bulk edit (schema-only)."""

    count = serializers.IntegerField(read_only=True)


class LabelBulkConvertSerializer(serializers.Serializer):
    """Validates a bulk label-kind-conversion request.

    A label already at ``target_kind``, or already a ``status`` label being
    converted to something else, is left untouched - matching the internal
    view, which has no path out of ``status`` at all (see
    ``LabelBulkConvertView._resolved_target_kind``).
    """

    uuids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=500)
    target_kind = serializers.ChoiceField(choices=_CONVERTIBLE_KIND_CHOICES)


class LabelBulkConvertResponseSerializer(serializers.Serializer):
    """The outcome of a bulk convert (schema-only)."""

    converted = serializers.IntegerField(read_only=True)
