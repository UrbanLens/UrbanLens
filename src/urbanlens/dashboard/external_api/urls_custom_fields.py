"""External-API routes for user-defined custom fields.

A value is meaningless without its definition - a native client rendering a custom field needs the
field's kind and choice list to draw the right control at all - so a client that has one and not the
other cannot show the feature, and splitting them across two domains would guarantee they drift.
Definitions are also effectively schema: changing a field's kind reinterprets every value already
stored under it, so routes here should treat a definition edit as a migration with a defined answer
for existing values, never as a plain PATCH.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_custom_fields

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = [
    path("custom-fields/", views_custom_fields.CustomFieldDefinitionsView.as_view(), name="custom_fields"),
    path("custom-fields/<int:field_id>/", views_custom_fields.CustomFieldDefinitionDetailView.as_view(), name="custom_fields.detail"),
    path("photos/<uuid:image_uuid>/custom-fields/", views_custom_fields.PhotoCustomFieldsView.as_view(), name="custom_fields.photo"),
    path("photos/<uuid:image_uuid>/custom-fields/<int:field_id>/", views_custom_fields.PhotoCustomFieldValueView.as_view(), name="custom_fields.photo.detail"),
]
