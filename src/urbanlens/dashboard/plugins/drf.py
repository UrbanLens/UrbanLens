"""Letting plugins add fields to core's API serializers, and shape the querysets its API lists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rest_framework import serializers

if TYPE_CHECKING:
    from collections.abc import Callable

    from rest_framework.serializers import Field


@dataclass(frozen=True, slots=True)
class SerializerField:
    """
    One field a plugin adds to a core serializer.

    Attributes:
        serializer: The core serializer class, or a base of several; the field is added to every serializer that is
            an instance of it and mixes in :class:`ExtensibleSerializerMixin`.
        name: The field's name in the representation. A name the serializer already has is left alone.
        field: Builds a fresh field instance, as DRF binds each field to one serializer.
    """

    serializer: type
    name: str
    field: Callable[[], Field]


class ExtensibleSerializerMixin(serializers.Serializer):
    """
    Mix into a serializer to let plugins add fields to it through ``points.SERIALIZER_FIELDS``.

    List it first, ahead of the DRF base: ``class PinSerializer(ExtensibleSerializerMixin, serializers.ModelSerializer)``.

    A plugin field that needs related rows should read them from what the view already prefetched, or be cheap
    enough to read per object: it runs for every object the serializer renders.
    """

    def get_fields(self) -> dict[str, Field]:
        from urbanlens.dashboard.plugins.points import SERIALIZER_FIELDS

        fields = super().get_fields()
        for extension in SERIALIZER_FIELDS:
            if isinstance(self, extension.serializer) and extension.name not in fields:
                fields[extension.name] = extension.field()
        return fields


class ExtensibleSerializer(ExtensibleSerializerMixin):
    """The base of core's API serializers: a plain serializer plugins can add fields to."""
