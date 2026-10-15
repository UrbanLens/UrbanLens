"""Routes, page assets, the content-security policy, and API serializers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ExtensionPoint, FilterHook

if TYPE_CHECKING:
    from typing import Any

    from django.db.models import QuerySet
    from django.urls import URLPattern, URLResolver
    from rest_framework.request import Request
    from rest_framework.serializers import BaseSerializer

    from urbanlens.dashboard.plugins.drf import SerializerField


ROOT_URLS: ExtensionPoint[URLPattern | URLResolver] = ExtensionPoint(
    "urls.root",
    "URL patterns mounted under the dashboard urlconf, after core's own. A plugin's routes exist only where it is active, so templates link to them from a template slot, never from a core template.",
)

CSP_SOURCES: ExtensionPoint[tuple[str, str]] = ExtensionPoint(
    "security.csp_sources",
    "``(directive, origin)`` pairs added to the Content-Security-Policy, such as ``('form-action', 'https://pay.example')``.",
)

STYLESHEETS: ExtensionPoint[str] = ExtensionPoint(
    "assets.stylesheets",
    "Static paths of stylesheets every page loads, after core's own.",
)

SCRIPTS: ExtensionPoint[str] = ExtensionPoint(
    "assets.scripts",
    "Static paths of ES-module scripts every page loads, after core's own.",
)

EXTERNAL_API_URLS: ExtensionPoint[URLPattern] = ExtensionPoint(
    "external_api.urls",
    "Flat ``path()`` routes added to the external API's ``external_api:`` namespace. They are sorted by specificity with the core routes, so contribution order does not matter. Use ``path()``, never ``include()``.",
)

SERIALIZER_FIELDS: ExtensionPoint[SerializerField] = ExtensionPoint(
    "api.serializer_fields",
    "Fields added to core API serializers that mix in ``ExtensibleSerializerMixin``. See :class:`~urbanlens.dashboard.plugins.drf.SerializerField`.",
)

API_LIST_QUERYSETS: FilterHook[QuerySet[Any], [type[BaseSerializer], Request]] = FilterHook(
    "api.list_querysets",
    "The queryset an external API list endpoint pages through. Receives it, the serializer each row is rendered with and the request, and returns it narrowed (by the plugin's own query parameters) or annotated and prefetched (for the fields it adds through ``SERIALIZER_FIELDS``). Core's own visibility filtering has already been applied; a callback must only narrow it.",
)

__all__ = ["API_LIST_QUERYSETS", "CSP_SOURCES", "EXTERNAL_API_URLS", "ROOT_URLS", "SCRIPTS", "SERIALIZER_FIELDS", "STYLESHEETS"]
