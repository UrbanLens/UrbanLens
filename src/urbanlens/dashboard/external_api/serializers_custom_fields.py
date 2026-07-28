"""Serializers for the external API's custom-field domain.

Definitions (:class:`CustomFieldDefinitionSerializer`/
:class:`CustomFieldDefinitionWriteSerializer`) describe the field itself;
values on a specific target reuse :class:`~urbanlens.dashboard.external_api.serializers.CustomFieldValueSerializer`,
the same shape the pin-detail payload already nests.
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldType

#: Field types creatable through the external API. REFERENCE is excluded: a
#: reference value points at another one of the owner's objects (a pin, wiki,
#: trip...) and needs its own resolution design this domain doesn't cover yet.
WRITABLE_FIELD_TYPES: tuple[str, ...] = (
    CustomFieldType.TEXT,
    CustomFieldType.NUMBER,
    CustomFieldType.DATE,
    CustomFieldType.TIME,
    CustomFieldType.SELECT,
    CustomFieldType.CHECKBOX,
    CustomFieldType.URL,
)

#: Hard cap on select options, matching the web settings panel's own limit
#: (``controllers.custom_fields._MAX_SELECT_OPTIONS``).
_MAX_SELECT_OPTIONS = 100


def normalize_select_options(raw_options: list[str]) -> tuple[list[str], str | None]:
    """Dedupe and bound-check a select field's submitted options.

    Shared between :class:`CustomFieldDefinitionWriteSerializer` (create,
    where ``field_type`` is always present) and the PATCH view (where a
    partial update omitting ``field_type`` never reaches ``validate()``'s
    SELECT branch, since DRF skips absent fields - including their
    defaults - entirely in partial mode).

    Args:
        raw_options: The submitted options, in request order.

    Returns:
        Tuple of (deduplicated options, error message or None). The list is
        empty when there's an error.
    """
    deduped: list[str] = []
    for raw_option in raw_options:
        option = raw_option.strip()
        if option and option not in deduped:
            deduped.append(option)
    if not deduped:
        return [], "Select fields need at least one option."
    if len(deduped) > _MAX_SELECT_OPTIONS:
        return [], f"Too many options ({_MAX_SELECT_OPTIONS} max)."
    return deduped, None


class CustomFieldDefinitionSerializer(serializers.Serializer):
    """One of the caller's custom field definitions."""

    id = serializers.IntegerField(read_only=True)
    entity_type = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    field_type = serializers.CharField(read_only=True)
    options = serializers.SerializerMethodField()
    order = serializers.IntegerField(read_only=True)

    def get_options(self, field: CustomField) -> list[str]:
        """The configured choices for a select field (empty for other types)."""
        return field.select_choices


class CustomFieldDefinitionWriteSerializer(serializers.Serializer):
    """A custom field definition submitted for create or partial update.

    ``entity_type`` is accepted on create only - the view never applies it on
    PATCH, since changing which object type a field applies to would orphan
    every value already stored under it.
    """

    entity_type = serializers.ChoiceField(choices=CustomFieldEntity.choices)
    name = serializers.CharField(max_length=100)
    field_type = serializers.ChoiceField(choices=[(value, value) for value in WRITABLE_FIELD_TYPES], default=CustomFieldType.TEXT)
    options = serializers.ListField(child=serializers.CharField(max_length=100), required=False, default=list)
    order = serializers.IntegerField(required=False, default=0, min_value=0)

    def validate(self, attrs: dict) -> dict:
        """Require and normalize choices when the field type is SELECT.

        Only fires here when ``field_type`` itself is present in the
        request - a partial update that submits ``options`` alone (the
        common case) skips this branch entirely, since DRF never populates
        omitted fields - not even with their declared default - in partial
        mode. The view re-runs :func:`normalize_select_options` against the
        *effective* field type (submitted or existing) to cover that case.
        """
        if attrs.get("field_type") == CustomFieldType.SELECT:
            deduped, error = normalize_select_options(attrs.get("options") or [])
            if error:
                raise serializers.ValidationError({"options": error})
            attrs["options"] = deduped
        return attrs


class CustomFieldValueWriteSerializer(serializers.Serializer):
    """A value submitted for one custom field on a target object.

    ``value`` is a string regardless of the field's type, matching
    ``CustomFieldValue.set_value``'s own contract - the typed parsing (number,
    date, time, checkbox, select-membership, url) happens there, one place,
    shared with the web form. An absent or blank value clears the field.
    """

    value = serializers.CharField(required=False, allow_blank=True, default="")
