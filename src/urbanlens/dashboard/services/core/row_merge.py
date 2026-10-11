"""Moving rows that reference one record onto its twin, and snapshotting rows a merge has to drop.
Shared by the pin merge (``services.pins.pin_merge``) and the duplicate building merge (``services.places.building_merge``): both fold one row into another of the same model and must not let the delete that follows take anything with it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from django.contrib.gis.geos import GEOSGeometry
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models.fields.files import FieldFile

if TYPE_CHECKING:
    from django.db.models import Model


#: Hard ceiling on a parent walk, so a corrupted tree ends the walk rather than hanging it.
_MAX_TREE_DEPTH = 64


def _ancestor_ids(row: Model, parent_field: str) -> list[int]:
    """The pks above *row* through its own ``parent_field``, nearest first.

    Args:
        row: The row to start from.
        parent_field: The self-referential foreign key to follow.

    Returns:
        Ancestor pks, stopping at the top, at a repeat, or at the depth ceiling.
    """
    manager = type(row)._base_manager  # noqa: SLF001
    attname = row._meta.get_field(parent_field).attname  # noqa: SLF001
    seen: list[int] = []
    current = manager.filter(pk=row.pk).values_list(attname, flat=True).first()
    while current is not None and current not in seen and current != row.pk and len(seen) < _MAX_TREE_DEPTH:
        seen.append(current)
        current = manager.filter(pk=current).values_list(attname, flat=True).first()
    return seen


def repoint_references(duplicate: Model, kept: Model) -> None:
    """Point every row that references *duplicate* at *kept*, so deleting *duplicate* takes nothing with it.
    Generic over the duplicate's reverse relations - a pending share is referenced by reshares, bundled shares, direct-message links and exposure rows, an owner by the sales that list it - so a relation added later is carried without the caller having to learn about it.
    A row that would collide with one *kept* already has is left where it is, for the caller to deal with: :func:`remaining_references` lists them.

    Args:
        duplicate: The row about to be deleted.
        kept: Its twin, which stays.
    """
    for relation in duplicate._meta.related_objects:  # noqa: SLF001
        remote = relation.field
        if relation.many_to_many:
            through = remote.remote_field.through
            column = remote.m2m_reverse_field_name()
            rows = through._base_manager.filter(**{column: duplicate.pk})  # noqa: SLF001
        else:
            column = remote.name
            rows = relation.related_model._base_manager.filter(**{column: duplicate.pk})  # noqa: SLF001
            if relation.related_model is type(kept):
                # A row above *kept* in this same tree would close a loop once it pointed at *kept* (K -> M -> K), as
                # would *kept* itself. They stay put; ``remaining_references`` reports them to the caller.
                rows = rows.exclude(pk__in=[kept.pk, *_ancestor_ids(kept, column)])
        try:
            with transaction.atomic():
                rows.update(**{column: kept.pk})
        except IntegrityError:
            for pk in list(rows.values_list("pk", flat=True)):
                try:
                    with transaction.atomic():
                        rows.model._base_manager.filter(pk=pk).update(**{column: kept.pk})  # noqa: SLF001
                except IntegrityError:
                    continue


def remaining_references(target: Model) -> dict[str, list[Model]]:
    """Every row still referencing *target*, by ``Model.field``.

    Args:
        target: The row about to be deleted.

    Returns:
        The referencing rows, empty when nothing references it. Many-to-many links are listed as their through rows.
    """
    found: dict[str, list[Model]] = {}
    for relation in target._meta.related_objects:  # noqa: SLF001
        remote = relation.field
        if relation.many_to_many:
            model = remote.remote_field.through
            rows = list(model._base_manager.filter(**{remote.m2m_reverse_field_name(): target.pk}))  # noqa: SLF001
        else:
            model = relation.related_model
            queryset = model._base_manager.filter(**{remote.name: target.pk})  # noqa: SLF001
            rows = list(queryset.exclude(pk=target.pk) if model is type(target) else queryset)
        if rows:
            found[f"{model.__name__}.{remote.name}"] = rows
    return found


def snapshot_row(instance: Model) -> dict[str, Any]:
    """A JSON-safe copy of every column on *instance*, for an audit record of something a merge drops.

    Args:
        instance: The row.

    Returns:
        ``{"model", "pk", "fields"}``; geometry as GeoJSON, files by name, foreign keys by id.
    """
    fields: dict[str, Any] = {}
    for model_field in instance._meta.concrete_fields:  # noqa: SLF001
        value = getattr(instance, model_field.attname)
        if isinstance(value, GEOSGeometry):
            value = json.loads(value.geojson)
        elif isinstance(value, FieldFile):
            value = value.name or ""
        fields[model_field.attname] = value
    return json.loads(json.dumps({"model": type(instance).__name__, "pk": instance.pk, "fields": fields}, cls=DjangoJSONEncoder))
