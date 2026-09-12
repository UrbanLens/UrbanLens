"""External-API routes for third-party account connections.

Access tokens are held in ``EncryptedTextField`` columns whose key derives from Django's
``SECRET_KEY``, and nothing routed in this module may ever echo a stored secret back to the caller:
status endpoints report whether a link exists, which account it points at and whether it still
authenticates - never the token itself.
An API key is already a delegated credential, so returning a linked service's token through it would
quietly turn a scoped UrbanLens key into full access to that third-party account.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = []
