"""The Organize page's label cards, prepared in one pass so the template only places them (P66).

The card template once derived all of this itself: about thirty variable renders and as many ``{% if %}``
nodes per card, which at 400 tags was nearly two seconds of template interpretation and no queries to speak
of. Numbers are handed over as strings, since Django localizes every integer it renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils.html import conditional_escape
from django.utils.safestring import SafeString, mark_safe

from urbanlens.dashboard.templatetags.dashboard_tags import is_material_icon, label_map_url

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.labels.model import Label

#: Kinds whose cards show pin counts; people and media labels are never counted.
_COUNTED_KINDS = frozenset({"tag", "category", "status"})


@dataclass(frozen=True)
class OrganizeLabelCard:
    """Everything one Organize card renders, beyond the label's own plain fields.

    Attributes:
        label: The label itself, for its name, description and flags.
        id: The label's pk, as text.
        data_attrs: The kind's ``data-<kind>-*`` attributes the organize scripts read, already escaped.
        display_name: The name shown, the viewer's override first.
        display_icon: The icon shown, the viewer's override first; ``""`` for none.
        display_color: The colour shown, the viewer's override first; ``""`` for none.
        icon_is_material: Whether ``display_icon`` names a Material icon rather than being an emoji.
        custom_icon_url: The uploaded icon to show in place of ``display_icon``, or ``""``.
        pin_count: Pins carrying the label, or None for a kind that is not counted.
        child_count: Direct child labels.
        has_children: Whether there are any.
        total_pins: Pins in the label's whole subtree, or None for a kind that is not counted.
        parent_names: The direct parents' names.
        selectable: Whether the card has a selection checkbox.
        editable: Whether the viewer may edit it.
        deletable: Whether the viewer may delete it.
        edit_url: Its edit dialog.
        delete_url: Where a delete posts.
        merge_url: Its merge dialog, or ``""`` where merging does not apply.
        customize_url: The viewer's display-override dialog for a global label, or ``""``.
        global_edit_url: The site-wide edit dialog for a global label, or ``""``.
        map_url: The map filtered to the label, or ``""`` when its subtree has no pins.
    """

    label: Label
    id: str
    data_attrs: SafeString
    display_name: str
    display_icon: str
    display_color: str
    icon_is_material: bool
    custom_icon_url: str
    pin_count: str | None
    child_count: str
    has_children: bool
    total_pins: str | None
    parent_names: list[str]
    selectable: bool
    editable: bool
    deletable: bool
    edit_url: str
    delete_url: str
    merge_url: str
    customize_url: str
    global_edit_url: str
    map_url: str


def build_organize_label_cards(
    labels: Iterable[Label],
    *,
    kind: str,
    url_kind: str,
    selectable: bool,
    editable: bool,
    deletable: bool,
    can_edit_global: bool,
) -> list[OrganizeLabelCard]:
    """Prepare one card per label.

    Counted kinds read ``total_pin_count``, so their labels should be primed with
    ``Label.prime_total_pin_counts`` first; people and media never read it.

    Args:
        labels: The labels, in display order, with parents and children prefetched.
        kind: The display kind (``tag``, ``category``, ``status``, ``people``, ``media``).
        url_kind: The kind as the label routes spell it.
        selectable: Whether cards get a selection checkbox, before per-label rules.
        editable: Whether the viewer may edit, before per-label rules.
        deletable: Whether the viewer may delete, before per-label rules.
        can_edit_global: Whether the viewer may edit site-wide labels.

    Returns:
        The cards, in the order given.
    """
    return [_card(label, kind, url_kind, selectable=selectable, editable=editable, deletable=deletable, can_edit_global=can_edit_global) for label in labels]


def _card(label: Label, kind: str, url_kind: str, *, selectable: bool, editable: bool, deletable: bool, can_edit_global: bool) -> OrganizeLabelCard:
    label_route = {"label_kind": url_kind, "label_id": label.pk}
    merge_url = ""
    if label.is_protected:
        selectable, editable, deletable = True, True, False
    elif kind == "people" and label.is_global:
        editable, deletable = False, False
    elif kind not in {"people", "media"}:
        merge_url = reverse("label.merge", kwargs=label_route)
    if label.is_global:
        # Only the owner may delete (`_can_modify_label`); an admin's delete sits with the site-wide edit controls.
        deletable = False

    counted = kind in _COUNTED_KINDS
    pin_count: int | None = getattr(label, "pin_count", None) if counted else None
    total = label.total_pin_count() if counted else None
    parents = list(label.parents.all())
    child_count = len(label.children.all())
    display_icon = label.effective_icon or label.icon or ""
    customizable = kind != "people" and label.is_global
    return OrganizeLabelCard(
        label=label,
        id=str(label.pk),
        data_attrs=_data_attrs(label, kind, parents, pin_count or 0),
        display_name=label.effective_name or label.name,
        display_icon=display_icon,
        display_color=label.effective_color or label.color or "",
        icon_is_material=is_material_icon(display_icon),
        custom_icon_url=label.custom_icon.url if label.custom_icon and not label.icon_is_overridden else "",
        pin_count=str(pin_count) if counted else None,
        child_count=str(child_count),
        has_children=bool(child_count),
        total_pins=str(total) if total is not None else None,
        parent_names=[parent.name for parent in parents],
        selectable=selectable,
        editable=editable,
        deletable=deletable,
        edit_url=reverse("label.edit", kwargs=label_route),
        delete_url=reverse("label.delete", kwargs=label_route),
        merge_url=merge_url,
        customize_url=reverse("label.customize", kwargs={"label_kind": kind, "label_id": label.pk}) if customizable else "",
        global_edit_url=reverse("label.edit", kwargs={"label_kind": kind, "label_id": label.pk}) if customizable and can_edit_global else "",
        map_url=label_map_url(label.pk) if total else "",
    )


def _data_attrs(label: Label, kind: str, parents: list[Label], pin_count: int) -> SafeString:
    custom_icon = label.custom_icon.url if label.custom_icon else ""
    fields: list[tuple[str, str | int]] = [("id", label.pk)]
    if kind == "tag":
        fields += [("name", label.effective_name), ("color", label.effective_color or ""), ("icon", label.effective_icon or "")]
    else:
        fields += [("name", label.name), ("color", label.color or ""), ("icon", label.icon or "")]
    if kind in _COUNTED_KINDS:
        fields.append(("custom-icon", custom_icon))
    if kind in {"tag", "category"}:
        fields.append(("desc", label.description or ""))
    fields.append(("pin-count", pin_count))
    if kind == "category":
        fields.append(("location-count", getattr(label, "location_count", None) or 0))
    if kind == "status":
        fields.append(("protected", "true" if label.is_protected else "false"))
    fields.append(("parents", ",".join(str(parent.pk) for parent in parents)))
    return mark_safe(" ".join(f'data-{kind}-{name}="{conditional_escape(str(value))}"' for name, value in fields))  # noqa: S308 - every value is escaped above
