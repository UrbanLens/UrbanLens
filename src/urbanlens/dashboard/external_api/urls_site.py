"""External-API routes for site-level, non-user-scoped information.

Owns what a client needs to know about the *installation* rather than about the person holding the
credential: the parts of ``SiteSettings`` that change how a client must behave (quotas, limits,
enabled features), feature flags and plugin availability, announcements and service notices, version
and health, and the site-admin operations available to staff.
First, a settings row contains operator secrets and internal knobs alongside the handful of values a
client legitimately needs; routes here expose an explicit allow-list, never a serialized model,
because a new admin-only field added later must not silently become public.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = []
