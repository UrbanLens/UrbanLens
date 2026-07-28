"""External-API routes for user-defined custom fields.

Owns both halves of the custom-field system: the *definitions* a user or the
community creates (name, kind, choices, ordering, which object types they apply
to) and the *values* those definitions carry on individual pins and wikis.

They are routed together on purpose. A value is meaningless without its
definition - a native client rendering a custom field needs the field's kind and
choice list to draw the right control at all - so a client that has one and not
the other cannot show the feature, and splitting them across two domains would
guarantee they drift. Definitions are also effectively schema: changing a
field's kind reinterprets every value already stored under it, so routes here
should treat a definition edit as a migration with a defined answer for existing
values, never as a plain PATCH.

Wiring: ``urls.py`` concatenates the ``urlpatterns`` below into the flat
``external_api:`` namespace and re-sorts the combined list with
:func:`~urbanlens.dashboard.external_api.urls.order_by_specificity`, so
declaration order inside this module only breaks ties between routes of
identical shape. Use ``path()`` (``re_path()`` cannot be ordered and is
rejected at import time) and keep every ``name=`` unique across the whole
external API.
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
