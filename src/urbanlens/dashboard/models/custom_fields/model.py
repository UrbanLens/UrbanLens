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

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import logging
import math
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    CheckConstraint,
    DateField,
    DecimalField,
    ForeignKey,
    Index,
    JSONField,
    PositiveSmallIntegerField,
    Q,
    TextChoices,
    TextField,
    TimeField,
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
    TIME = "time", "Time"
    SELECT = "select", "Select"
    CHECKBOX = "checkbox", "Checkbox"
    URL = "url", "Link"
    REFERENCE = "reference", "Reference"


class CustomFieldDisplay(TextChoices):
    """Where a field appears on the pin detail page.

    Only meaningful for pin fields today - other entity types render in
    compact strips with no section layout to place a field into.
    """

    DEFAULT = "default", "In the Custom Fields card"
    SECTION = "section", "Its own section"
    FIXED = "fixed", "Fixed on screen (draggable)"


class CustomFieldStyle(TextChoices):
    """Presentation styles for a custom field's value input.

    Which styles apply depends on the field type (see :data:`STYLES_BY_TYPE`);
    an empty ``CustomField.style`` means the type's default style.
    """

    SHORT_TEXT = "short", "Short (single line)"
    LONG_TEXT = "long", "Long (multi-line)"
    NUMBER_INPUT = "input", "Number input"
    STARS = "stars", "Star rating"
    SLIDER = "slider", "Slider"


#: The style choices available for each field type. Types not listed have a
#: single fixed presentation and no style choice in the UI.
STYLES_BY_TYPE: dict[str, list[tuple[str, str]]] = {
    CustomFieldType.TEXT: [
        (CustomFieldStyle.SHORT_TEXT, CustomFieldStyle.SHORT_TEXT.label),
        (CustomFieldStyle.LONG_TEXT, CustomFieldStyle.LONG_TEXT.label),
    ],
    CustomFieldType.NUMBER: [
        (CustomFieldStyle.NUMBER_INPUT, CustomFieldStyle.NUMBER_INPUT.label),
        (CustomFieldStyle.STARS, CustomFieldStyle.STARS.label),
        (CustomFieldStyle.SLIDER, CustomFieldStyle.SLIDER.label),
    ],
}

#: Default (implicit) style per type, used when ``CustomField.style`` is blank.
DEFAULT_STYLE_BY_TYPE: dict[str, str] = {
    CustomFieldType.TEXT: CustomFieldStyle.SHORT_TEXT,
    CustomFieldType.NUMBER: CustomFieldStyle.NUMBER_INPUT,
}

#: Slider bounds used when the field's config doesn't override them.
SLIDER_DEFAULT_MIN = 0
SLIDER_DEFAULT_MAX = 100

#: Star ratings are a fixed 1-5 scale, matching the site's other star widgets.
STARS_MAX = 5

#: Bounds (viewport percentages) for a fixed-display field's dragged position,
#: leaving room for the element itself so it can't be parked fully off-screen.
FIXED_POS_MAX_LEFT = 92
FIXED_POS_MAX_TOP = 88

#: Material Symbols icon representing each field type in the UI.
FIELD_TYPE_ICONS: dict[str, str] = {
    CustomFieldType.TEXT: "notes",
    CustomFieldType.NUMBER: "tag",
    CustomFieldType.DATE: "calendar_month",
    CustomFieldType.TIME: "schedule",
    CustomFieldType.SELECT: "list",
    CustomFieldType.CHECKBOX: "check_box",
    CustomFieldType.URL: "link",
    CustomFieldType.REFERENCE: "attach_file",
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
        style: Presentation style for the value input (:class:`CustomFieldStyle`);
            blank means the type's default. Only meaningful for types listed in
            :data:`STYLES_BY_TYPE`.
        display: Where the field appears on the pin detail page
            (:class:`CustomFieldDisplay`). Only meaningful for pin fields.
        config: Type/style-specific configuration: ``{"choices": [...]}`` for
            select fields, optional ``{"min": ..., "max": ...}`` for sliders,
            ``{"fixed_pos": {"left": ..., "top": ...}}`` (viewport percentages)
            for the dragged position of a fixed-display field.
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
    style = CharField(max_length=10, choices=CustomFieldStyle.choices, blank=True, default="")
    display = CharField(max_length=10, choices=CustomFieldDisplay.choices, default=CustomFieldDisplay.DEFAULT)
    config = JSONField(default=dict, blank=True)
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
            CustomFieldType.TIME: "time",
            CustomFieldType.URL: "url",
            CustomFieldType.CHECKBOX: "checkbox",
        }
        return input_types.get(self.field_type, "text")

    @property
    def effective_style(self) -> str:
        """The presentation style in effect, resolving blank to the type default.

        Returns:
            A :class:`CustomFieldStyle` value, or "" for types with a single
            fixed presentation (date, time, select, checkbox, url).
        """
        if self.style and any(self.style == value for value, _ in STYLES_BY_TYPE.get(self.field_type, [])):
            return self.style
        return DEFAULT_STYLE_BY_TYPE.get(self.field_type, "")

    @property
    def select_choices(self) -> list[str]:
        """The configured choices for a select field (empty for other types)."""
        if self.field_type != CustomFieldType.SELECT:
            return []
        raw = (self.config or {}).get("choices")
        if not isinstance(raw, list):
            return []
        return [str(choice) for choice in raw if str(choice).strip()]

    @property
    def options_text(self) -> str:
        """The select choices as newline-separated text for the options editor."""
        return "\n".join(self.select_choices)

    @property
    def reference_kind(self) -> str:
        """The configured target kind for a reference field ("" for other types).

        Returns:
            A ``services.custom_field_references.REFERENCE_KINDS`` value, or ""
            when this isn't a reference field or the config is missing/invalid.
        """
        if self.field_type != CustomFieldType.REFERENCE:
            return ""
        from urbanlens.dashboard.services.custom_field_references import REFERENCE_KINDS

        raw = (self.config or {}).get("ref_type") or ""
        return raw if any(raw == kind for kind, _ in REFERENCE_KINDS) else ""

    @property
    def reference_kind_label(self) -> str:
        """Display label for the configured reference kind ("" when not set)."""
        from urbanlens.dashboard.services.custom_field_references import REFERENCE_KINDS

        return dict(REFERENCE_KINDS).get(self.reference_kind, "")

    def reference_choices(self, *, include_pk: int | None = None) -> list[tuple[int, str]]:
        """(pk, label) picker choices for this reference field's target kind.

        Args:
            include_pk: A pk to force into the list even when past the cap
                (used to keep the currently stored value selectable).

        Returns:
            Access-scoped choices for this field's owner, or [] for
            non-reference fields.
        """
        kind = self.reference_kind
        if not kind:
            return []
        from urbanlens.dashboard.services.custom_field_references import reference_choices

        return reference_choices(kind, self.profile, include_pk=include_pk)

    @property
    def fixed_position(self) -> dict[str, float] | None:
        """The saved drag position of a fixed-display field, clamped to bounds.

        Returns:
            ``{"left": ..., "top": ...}`` viewport percentages, or None when the
            field has never been dragged (callers pick a default placement).
        """
        raw = (self.config or {}).get("fixed_pos")
        if not isinstance(raw, dict):
            return None
        try:
            left = float(raw.get("left"))  # type: ignore[arg-type]
            top = float(raw.get("top"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(left) and math.isfinite(top)):
            return None
        return {
            "left": min(max(left, 0.0), float(FIXED_POS_MAX_LEFT)),
            "top": min(max(top, 0.0), float(FIXED_POS_MAX_TOP)),
        }

    @property
    def slider_min(self) -> Decimal:
        """The slider's lower bound (config override or the default)."""
        return self._config_bound("min", SLIDER_DEFAULT_MIN)

    @property
    def slider_max(self) -> Decimal:
        """The slider's upper bound (config override or the default)."""
        return self._config_bound("max", SLIDER_DEFAULT_MAX)

    def _config_bound(self, key: str, default: int) -> Decimal:
        """Read a numeric bound from config, falling back on bad/missing data."""
        raw = (self.config or {}).get(key)
        if raw is None:
            return Decimal(default)
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return Decimal(default)


#: The reference FK columns on CustomFieldValue, used to build the constraint.
_REF_COLUMNS: tuple[str, ...] = ("ref_pin", "ref_wiki", "ref_markup_map", "ref_trip", "ref_image", "ref_pin_list", "ref_profile")


def _at_most_one_of(columns: tuple[str, ...]) -> Q:
    """A check-constraint condition allowing all-null or exactly one non-null."""
    condition = Q(**{f"{column}__isnull": True for column in columns})
    for column in columns:
        exactly_this = {f"{column}__isnull": False}
        exactly_this.update({f"{other}__isnull": True for other in columns if other != column})
        condition = condition | Q(**exactly_this)
    return condition


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
    value_time = TimeField(null=True, blank=True)
    value_boolean = BooleanField(null=True, blank=True)

    # -- Reference value FKs (at most one set, matching field.config ref_type) --
    # Deleting the referenced object deletes the value row: a reference to a
    # gone object carries no information, unlike SET_NULL which would leave an
    # invalid "value with nothing in it" row behind.
    ref_pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, null=True, blank=True, related_name="custom_field_references")
    ref_wiki = ForeignKey("dashboard.Wiki", on_delete=CASCADE, null=True, blank=True, related_name="custom_field_references")
    ref_markup_map = ForeignKey("dashboard.MarkupMap", on_delete=CASCADE, null=True, blank=True, related_name="custom_field_references")
    ref_trip = ForeignKey("dashboard.Trip", on_delete=CASCADE, null=True, blank=True, related_name="custom_field_references")
    ref_image = ForeignKey("dashboard.Image", on_delete=CASCADE, null=True, blank=True, related_name="custom_field_references")
    ref_pin_list = ForeignKey("dashboard.PinList", on_delete=CASCADE, null=True, blank=True, related_name="custom_field_references")
    ref_profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, null=True, blank=True, related_name="custom_field_references")

    objects: CustomFieldValueManager = CustomFieldValueManager()

    #: Maps entity type -> the FK attribute holding that entity's target.
    TARGET_FIELD_BY_ENTITY: dict[str, str] = {
        CustomFieldEntity.PIN: "pin",
        CustomFieldEntity.PHOTO: "image",
        CustomFieldEntity.PROFILE: "target_profile",
        CustomFieldEntity.MARKUP_MAP: "markup_map",
    }

    #: Maps a reference field's configured kind -> the ref FK attribute above.
    REF_FIELD_BY_KIND: dict[str, str] = {
        "pin": "ref_pin",
        "wiki": "ref_wiki",
        "markup_map": "ref_markup_map",
        "trip": "ref_trip",
        "photo": "ref_image",
        "list": "ref_pin_list",
        "profile": "ref_profile",
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
            Index(fields=["field", "value_time"], name="idxdb_cfv_field_time"),
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
            CheckConstraint(name="db_cfv_at_most_one_ref", condition=_at_most_one_of(_REF_COLUMNS)),
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
    def value(self) -> Any:
        """The typed value, read from the column matching ``field.field_type``.

        Returns:
            The stored value as its natural Python type (the referenced model
            instance for reference fields), or None/"" when unset.
        """
        field_type = self.field.field_type
        if field_type == CustomFieldType.NUMBER:
            return self.value_number
        if field_type == CustomFieldType.DATE:
            return self.value_date
        if field_type == CustomFieldType.TIME:
            return self.value_time
        if field_type == CustomFieldType.CHECKBOX:
            return self.value_boolean
        if field_type == CustomFieldType.REFERENCE:
            return self.reference_target
        return self.value_text

    @property
    def reference_target(self) -> Any | None:
        """The object a reference value points at (None for other types/unset)."""
        attr = self.REF_FIELD_BY_KIND.get(self.field.reference_kind)
        return getattr(self, attr) if attr else None

    @property
    def reference_pk(self) -> int | None:
        """The referenced object's pk, for select-option comparison in templates."""
        attr = self.REF_FIELD_BY_KIND.get(self.field.reference_kind)
        return getattr(self, f"{attr}_id") if attr else None

    @property
    def reference_url(self) -> str | None:
        """Detail-page URL for the referenced object, or None when it has none."""
        from urbanlens.dashboard.services.custom_field_references import reference_url

        return reference_url(self.field.reference_kind, self.reference_target)

    @property
    def display_value(self) -> str:
        """The value formatted for display (numbers without trailing zeros)."""
        if self.field.field_type == CustomFieldType.REFERENCE:
            from urbanlens.dashboard.services.custom_field_references import reference_label

            return reference_label(self.field.reference_kind, self.reference_target)
        raw = self.value
        if raw is None or raw == "":
            return ""
        if isinstance(raw, bool):
            return "Yes" if raw else "No"
        if isinstance(raw, Decimal):
            normalized = raw.normalize()
            # normalize() renders large round numbers in E notation (1E+3); undo that.
            return format(normalized, "f")
        if isinstance(raw, time):
            return raw.isoformat("minutes") if raw.second == 0 and raw.microsecond == 0 else raw.isoformat()
        if isinstance(raw, date):
            return raw.isoformat()
        return str(raw)

    @property
    def input_value(self) -> str:
        """The value formatted for an HTML input's ``value`` attribute."""
        raw = self.value
        if isinstance(raw, bool):
            return "true" if raw else "false"
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
        self.value_time = None
        self.value_boolean = None
        for ref_attr in self.REF_FIELD_BY_KIND.values():
            setattr(self, ref_attr, None)

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
        elif field_type == CustomFieldType.TIME:
            try:
                self.value_time = time.fromisoformat(raw)
            except ValueError as e:
                raise ValueError(f"{raw!r} is not a valid time (expected HH:MM).") from e
        elif field_type == CustomFieldType.CHECKBOX:
            lowered = raw.lower()
            if lowered in ("1", "true", "on", "yes", "checked"):
                self.value_boolean = True
            elif lowered in ("0", "false", "off", "no", "unchecked"):
                self.value_boolean = False
            else:
                raise ValueError(f"{raw!r} is not a valid checkbox value.")
        elif field_type == CustomFieldType.SELECT:
            choices = self.field.select_choices
            if raw not in choices:
                raise ValueError(f"{raw!r} is not one of this field's options.")
            self.value_text = raw
        elif field_type == CustomFieldType.URL:
            candidate = raw if "://" in raw else f"https://{raw}"
            try:
                URLValidator(schemes=["http", "https"])(candidate)
            except ValidationError as e:
                raise ValueError(f"{raw!r} is not a valid link.") from e
            self.value_text = candidate
        elif field_type == CustomFieldType.REFERENCE:
            from urbanlens.dashboard.services.custom_field_references import resolve_reference

            kind = self.field.reference_kind
            ref_field = self.REF_FIELD_BY_KIND.get(kind)
            if ref_field is None:
                raise ValueError("This reference field has no target kind configured.")
            target = resolve_reference(kind, raw, self.field.profile)
            if target is None:
                raise ValueError("That item wasn't found (or you can't reference it).")
            setattr(self, ref_field, target)
        else:
            self.value_text = raw

    def export_value(self) -> Any:
        """The value in a JSON-serializable form for data exports."""
        if self.field.field_type == CustomFieldType.REFERENCE:
            target = self.reference_target
            if target is None:
                return None
            return {"kind": self.field.reference_kind, "uuid": str(target.uuid), "label": self.display_value}
        raw = self.value
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, Decimal):
            return self.display_value
        if isinstance(raw, (date, time)):
            return raw.isoformat()
        return raw
