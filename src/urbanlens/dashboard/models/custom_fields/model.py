"""Custom field models - user-defined fields attachable to pins, photos, people, and maps.

A :class:`CustomField` is a private, per-user field *definition* (e.g. "Gate code",
text, for pins). A :class:`CustomFieldValue` stores that field's value for one
specific target object. Both are only ever visible to the field's owner - custom
fields are a personal organization tool, never shared or community data.

Adding support for a new target entity requires:
    1. A new :class:`CustomFieldEntity` choice.
    2. A new nullable FK on :class:`CustomFieldValue` (plus the constraint updates).
    3. An entry in :data:`CustomFieldValue.TARGET_FIELD_BY_ENTITY`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import logging
from typing import TYPE_CHECKING, Any

from django.db.models import (
    CASCADE,
    CharField,
    CheckConstraint,
    DateField,
    DecimalField,
    ForeignKey,
    Index,
    PositiveSmallIntegerField,
    Q,
    TextChoices,
    TextField,
    UniqueConstraint,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.custom_fields.queryset import CustomFieldManager, CustomFieldValueManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.markup.model import MarkupMap
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class CustomFieldEntity(TextChoices):
    """The kinds of objects a custom field can be defined for.

    Each entity maps to one nullable FK on :class:`CustomFieldValue`. New
    entities may be added over time (trips, visits, ...).
    """

    PIN = "pin", "Pins"
    PHOTO = "photo", "Photos"
    PROFILE = "profile", "People"
    MARKUP_MAP = "markup_map", "Maps"


class CustomFieldType(TextChoices):
    """The data type of a custom field's values."""

    TEXT = "text", "Text"
    NUMBER = "number", "Number"
    DATE = "date", "Date"


#: Material Symbols icon representing each field type in the UI.
FIELD_TYPE_ICONS: dict[str, str] = {
    CustomFieldType.TEXT: "notes",
    CustomFieldType.NUMBER: "tag",
    CustomFieldType.DATE: "calendar_month",
}

#: Material Symbols icon representing each entity type in the UI.
ENTITY_ICONS: dict[str, str] = {
    CustomFieldEntity.PIN: "place",
    CustomFieldEntity.PHOTO: "photo_library",
    CustomFieldEntity.PROFILE: "person",
    CustomFieldEntity.MARKUP_MAP: "map",
}


class CustomField(abstract.FrontendDashboardModel):
    """A user-defined field definition for one entity type.

    Custom fields are private to their owning profile: only the owner sees
    them, their values, and the filter controls they add to the map.

    Attributes:
        profile: The owning profile. Fields (and their values) are deleted
            with the profile.
        entity_type: Which kind of object this field applies to
            (:class:`CustomFieldEntity`).
        name: Display name, unique per owner+entity type (case-insensitive
            duplicates are allowed by the DB but rejected by the views).
        field_type: Value data type (:class:`CustomFieldType`).
        order: Manual sort order within an entity group (lower first).
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="custom_fields",
    )
    entity_type = CharField(max_length=20, choices=CustomFieldEntity.choices)
    name = CharField(max_length=100)
    field_type = CharField(max_length=10, choices=CustomFieldType.choices, default=CustomFieldType.TEXT)
    order = PositiveSmallIntegerField(default=0)

    objects: CustomFieldManager = CustomFieldManager()

    if TYPE_CHECKING:
        profile_id: int

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_custom_fields"
        ordering = ["order", "name"]
        indexes = [
            Index(fields=["profile", "entity_type"], name="idxdb_cf_profile_entity"),
        ]
        constraints = [
            UniqueConstraint(fields=["profile", "entity_type", "name"], name="db_cf_unique_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_entity_type_display()} {self.get_field_type_display()})"

    @property
    def type_icon(self) -> str:
        """Material Symbols icon name for this field's data type."""
        return FIELD_TYPE_ICONS.get(self.field_type, "notes")

    @property
    def input_type(self) -> str:
        """The HTML ``<input type>`` matching this field's data type."""
        input_types: dict[str, str] = {
            CustomFieldType.TEXT: "text",
            CustomFieldType.NUMBER: "number",
            CustomFieldType.DATE: "date",
        }
        return input_types.get(self.field_type, "text")


class CustomFieldValue(abstract.DashboardModel):
    """The value of one custom field on one target object.

    Exactly one target FK is set, matching ``field.entity_type``. The value is
    stored in the typed column matching ``field.field_type`` so numbers and
    dates filter/sort correctly in SQL.

    Values are private to ``field.profile``. Deleting the field, the target,
    or the owning profile deletes the value.
    """

    field = ForeignKey(
        CustomField,
        on_delete=CASCADE,
        related_name="values",
    )

    # -- Targets (exactly one set; add a new FK per future entity type) --------
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="custom_field_values",
    )
    image = ForeignKey(
        "dashboard.Image",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="custom_field_values",
    )
    target_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="custom_field_values_about",
    )
    markup_map = ForeignKey(
        "dashboard.MarkupMap",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="custom_field_values",
    )

    # -- Typed value columns (one populated, per field.field_type) -------------
    value_text = TextField(blank=True, default="")
    value_number = DecimalField(max_digits=24, decimal_places=6, null=True, blank=True)
    value_date = DateField(null=True, blank=True)

    objects: CustomFieldValueManager = CustomFieldValueManager()

    #: Maps entity type -> the FK attribute holding that entity's target.
    TARGET_FIELD_BY_ENTITY: dict[str, str] = {
        CustomFieldEntity.PIN: "pin",
        CustomFieldEntity.PHOTO: "image",
        CustomFieldEntity.PROFILE: "target_profile",
        CustomFieldEntity.MARKUP_MAP: "markup_map",
    }

    if TYPE_CHECKING:
        field_id: int
        pin_id: int | None
        image_id: int | None
        target_profile_id: int | None
        markup_map_id: int | None

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_custom_field_values"
        indexes = [
            Index(fields=["field", "value_number"], name="idxdb_cfv_field_number"),
            Index(fields=["field", "value_date"], name="idxdb_cfv_field_date"),
        ]
        constraints = [
            UniqueConstraint(fields=["field", "pin"], name="db_cfv_unique_pin", condition=Q(pin__isnull=False)),
            UniqueConstraint(fields=["field", "image"], name="db_cfv_unique_image", condition=Q(image__isnull=False)),
            UniqueConstraint(fields=["field", "target_profile"], name="db_cfv_unique_profile", condition=Q(target_profile__isnull=False)),
            UniqueConstraint(fields=["field", "markup_map"], name="db_cfv_unique_map", condition=Q(markup_map__isnull=False)),
            CheckConstraint(
                name="db_cfv_exactly_one_target",
                condition=(
                    Q(pin__isnull=False, image__isnull=True, target_profile__isnull=True, markup_map__isnull=True)
                    | Q(pin__isnull=True, image__isnull=False, target_profile__isnull=True, markup_map__isnull=True)
                    | Q(pin__isnull=True, image__isnull=True, target_profile__isnull=False, markup_map__isnull=True)
                    | Q(pin__isnull=True, image__isnull=True, target_profile__isnull=True, markup_map__isnull=False)
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"CustomFieldValue(field={self.field_id}, value={self.display_value!r})"

    @property
    def target(self) -> Pin | Image | Profile | MarkupMap | None:
        """The object this value is attached to.

        Returns:
            The target model instance, or None when the row is malformed.
        """
        for attr in self.TARGET_FIELD_BY_ENTITY.values():
            obj = getattr(self, attr)
            if obj is not None:
                return obj
        return None

    @property
    def value(self) -> str | Decimal | date | None:
        """The typed value, read from the column matching ``field.field_type``.

        Returns:
            The stored value as its natural Python type, or None/"" when unset.
        """
        field_type = self.field.field_type
        if field_type == CustomFieldType.NUMBER:
            return self.value_number
        if field_type == CustomFieldType.DATE:
            return self.value_date
        return self.value_text

    @property
    def display_value(self) -> str:
        """The value formatted for display (numbers without trailing zeros)."""
        raw = self.value
        if raw is None or raw == "":
            return ""
        if isinstance(raw, Decimal):
            normalized = raw.normalize()
            # normalize() renders large round numbers in E notation (1E+3); undo that.
            return format(normalized, "f")
        if isinstance(raw, date):
            return raw.isoformat()
        return str(raw)

    @property
    def input_value(self) -> str:
        """The value formatted for an HTML input's ``value`` attribute."""
        return self.display_value

    def set_value(self, raw: str) -> None:
        """Parse and store a raw string value into the typed column for this field.

        Args:
            raw: User-entered value. Whitespace is stripped.

        Raises:
            ValueError: When the raw value cannot be parsed as the field's type,
                or when it is empty (callers should delete the row instead).
        """
        raw = (raw or "").strip()
        if not raw:
            raise ValueError("Empty value - delete the row instead of storing a blank.")

        field_type = self.field.field_type
        self.value_text = ""
        self.value_number = None
        self.value_date = None

        if field_type == CustomFieldType.NUMBER:
            try:
                self.value_number = Decimal(raw)
            except InvalidOperation as e:
                raise ValueError(f"{raw!r} is not a valid number.") from e
        elif field_type == CustomFieldType.DATE:
            try:
                self.value_date = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError as e:
                raise ValueError(f"{raw!r} is not a valid date (expected YYYY-MM-DD).") from e
        else:
            self.value_text = raw

    def export_value(self) -> Any:
        """The value in a JSON-serializable form for data exports."""
        raw = self.value
        if isinstance(raw, Decimal):
            return self.display_value
        if isinstance(raw, date):
            return raw.isoformat()
        return raw
