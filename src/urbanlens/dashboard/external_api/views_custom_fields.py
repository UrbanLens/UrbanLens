"""External-facing custom-field endpoints: definitions and their values.

Definitions (``custom-fields/``) span every entity type the model supports
(pins, photos, people, maps) since they are one shared ``CustomField`` row;
this domain only exposes values for photos so far - see the module's phase
notes in ``docs/EXTERNAL_API.md``. Every lookup is scoped to
``profile=request.user.profile`` (or ``profile__user=request.user`` for the
target object), so another user's field or photo is indistinguishable from a
nonexistent one, per the package-wide anti-enumeration convention.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import IntegrityError
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.controllers.custom_fields import rows_for_target, save_value
from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.external_api.serializers import CustomFieldValueSerializer, ErrorSerializer
from urbanlens.dashboard.external_api.serializers_custom_fields import (
    CustomFieldDefinitionSerializer,
    CustomFieldDefinitionWriteSerializer,
    CustomFieldValueWriteSerializer,
    normalize_select_options,
)
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldType
from urbanlens.dashboard.models.images.model import Image

if TYPE_CHECKING:
    from uuid import UUID

    from rest_framework.request import Request


class CustomFieldDefinitionsView(PaginatedListMixin, ExternalApiView):
    """The caller's custom field definitions: GET lists them, POST creates one.

    Definitions span every entity type since they are one shared model -
    filter with ``?entity_type=`` to scope to one (``pin``, ``photo``,
    ``profile``, or ``markup_map``).
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.CUSTOM_FIELDS_READ}),
        "POST": frozenset({ApiKeyScope.CUSTOM_FIELDS_WRITE}),
    }

    @extend_schema(responses={200: CustomFieldDefinitionSerializer(many=True), 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Return the caller's field definitions, optionally filtered by entity_type."""
        entity_type = request.query_params.get("entity_type") or None
        if entity_type is not None and entity_type not in CustomFieldEntity.values:
            return Response({"error": "Invalid entity_type."}, status=400)

        queryset = CustomField.objects.owned_by(request.user.profile).order_by("entity_type", "order", "name")
        if entity_type:
            queryset = queryset.filter(entity_type=entity_type)
        return self.paginated_response(queryset, CustomFieldDefinitionSerializer, request)

    @extend_schema(request=CustomFieldDefinitionWriteSerializer, responses={201: CustomFieldDefinitionSerializer, 400: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Create a custom field definition owned by the caller."""
        serializer = CustomFieldDefinitionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        if CustomField.objects.filter(profile=profile, entity_type=data["entity_type"], name__iexact=data["name"]).exists():
            return Response({"error": f"You already have a “{data['name']}” field there."}, status=400)

        config = {"choices": data["options"]} if data["field_type"] == CustomFieldType.SELECT else {}
        try:
            field = CustomField.objects.create(
                profile=profile,
                entity_type=data["entity_type"],
                name=data["name"],
                field_type=data["field_type"],
                order=data["order"],
                config=config,
            )
        except IntegrityError:
            return Response({"error": f"You already have a “{data['name']}” field there."}, status=400)
        return Response(CustomFieldDefinitionSerializer(field).data, status=201)


class CustomFieldDefinitionDetailView(ExternalApiView):
    """PATCH: partially update one of the caller's field definitions.  DELETE: remove it."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PATCH": frozenset({ApiKeyScope.CUSTOM_FIELDS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.CUSTOM_FIELDS_WRITE}),
    }

    @staticmethod
    def _get_field(request: Request, field_id: int) -> CustomField | None:
        """The caller's own field with this id, or None."""
        return CustomField.objects.filter(pk=field_id, profile=request.user.profile).first()

    @extend_schema(request=CustomFieldDefinitionWriteSerializer, responses={200: CustomFieldDefinitionSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, field_id: int) -> Response:
        """Apply a partial update to one of the caller's own field definitions.

        ``entity_type`` is accepted by the serializer (for symmetry with
        create) but never applied here: migrating a field between entity
        types would orphan every value already stored under it, the same
        reason the web settings panel never offers it either.
        """
        field = self._get_field(request, field_id)
        if field is None:
            return Response({"error": "No such custom field."}, status=404)

        serializer = CustomFieldDefinitionWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        new_field_type = data.get("field_type", field.field_type)
        if new_field_type != field.field_type and field.values.exists():
            return Response({"error": "This field already has values - clear them before changing its type."}, status=400)

        new_name = data.get("name", field.name)
        if new_name.lower() != field.name.lower() and CustomField.objects.filter(profile=field.profile, entity_type=field.entity_type, name__iexact=new_name).exclude(pk=field.pk).exists():
            return Response({"error": f"You already have a “{new_name}” field there."}, status=400)

        config = dict(field.config or {})
        if new_field_type == CustomFieldType.SELECT and "options" in data:
            options, error = normalize_select_options(data["options"])
            if error:
                return Response({"error": error}, status=400)
            new_options = set(options)
            if field.field_type == CustomFieldType.SELECT:
                orphaned = sorted(field.values.exclude(value_text__in=new_options).values_list("value_text", flat=True).distinct())
                if orphaned:
                    preview = ", ".join(f"“{value}”" for value in orphaned[:5])
                    if len(orphaned) > 5:
                        preview += f", and {len(orphaned) - 5} more"
                    return Response({"error": f"Can't remove option(s) still in use: {preview}. Update or clear those values first."}, status=400)
            config["choices"] = options
        elif new_field_type != CustomFieldType.SELECT:
            config.pop("choices", None)

        field.name = new_name
        field.field_type = new_field_type
        field.config = config
        if "order" in data:
            field.order = data["order"]
        try:
            field.save(update_fields=["name", "field_type", "config", "order", "updated"])
        except IntegrityError:
            return Response({"error": f"You already have a “{new_name}” field there."}, status=400)
        return Response(CustomFieldDefinitionSerializer(field).data)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, field_id: int) -> Response:
        """Delete one of the caller's field definitions and every value stored under it."""
        field = self._get_field(request, field_id)
        if field is None:
            return Response({"error": "No such custom field."}, status=404)
        field.delete()
        return Response(status=204)


class PhotoCustomFieldsView(ExternalApiView):
    """GET: every one of the caller's PHOTO-entity fields, with this photo's value.

    Lists every defined field, not just ones with a value already set on this
    photo - a native client renders the whole set as an editable form, the
    same way the web lightbox strip does
    (``controllers.custom_fields._render_strip``). An unset field reports
    ``value: null``.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.CUSTOM_FIELDS_READ, ApiKeyScope.PHOTOS_READ}),
    }

    @extend_schema(responses={200: CustomFieldValueSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, image_uuid: UUID) -> Response:
        """Return every PHOTO-entity field definition paired with its value on this photo."""
        image = Image.objects.filter(uuid=image_uuid, profile__user=request.user).first()
        if image is None:
            return Response({"error": "No such photo."}, status=404)

        rows = rows_for_target(request.user.profile, CustomFieldEntity.PHOTO, image)
        payload = [
            {
                "id": row["field"].pk,
                "name": row["field"].name,
                "type": row["field"].field_type,
                "value": row["value"].export_value() if row["value"] is not None else None,
            }
            for row in rows
        ]
        return Response(CustomFieldValueSerializer(payload, many=True).data)


class PhotoCustomFieldValueView(ExternalApiView):
    """PUT: set one custom field's value on one of the caller's photos.  DELETE: clear it."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PUT": frozenset({ApiKeyScope.CUSTOM_FIELDS_WRITE, ApiKeyScope.PHOTOS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.CUSTOM_FIELDS_WRITE, ApiKeyScope.PHOTOS_WRITE}),
    }

    @staticmethod
    def _resolve(request: Request, image_uuid: UUID, field_id: int) -> tuple[Image | None, CustomField | None]:
        """Return (photo, field) - either may be None when not the caller's own."""
        profile = request.user.profile
        image = Image.objects.filter(uuid=image_uuid, profile=profile).first()
        field = CustomField.objects.filter(pk=field_id, profile=profile, entity_type=CustomFieldEntity.PHOTO).first()
        return image, field

    @extend_schema(request=CustomFieldValueWriteSerializer, responses={200: CustomFieldValueSerializer, 204: None, 400: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, image_uuid: UUID, field_id: int) -> Response:
        """Set (or, with an empty value, clear) this field's value on the photo."""
        image, field = self._resolve(request, image_uuid, field_id)
        if image is None:
            return Response({"error": "No such photo."}, status=404)
        if field is None:
            return Response({"error": "No such custom field."}, status=404)

        serializer = CustomFieldValueWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        value, error = save_value(field, image, serializer.validated_data["value"])
        if error is not None:
            return Response({"error": error}, status=400)
        if value is None:
            return Response(status=204)
        return Response(CustomFieldValueSerializer({"id": field.pk, "name": field.name, "type": field.field_type, "value": value.export_value()}).data)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, image_uuid: UUID, field_id: int) -> Response:
        """Clear this field's value on the photo (idempotent)."""
        image, field = self._resolve(request, image_uuid, field_id)
        if image is None:
            return Response({"error": "No such photo."}, status=404)
        if field is None:
            return Response({"error": "No such custom field."}, status=404)
        save_value(field, image, "")
        return Response(status=204)
