"""External-API routes for third-party account connections.

Owns the integrations a user links to their own profile - Immich, the OAuth
identity providers, and the other plugin-backed services - covering the connect
and disconnect flows, the current status of a link, and any manual sync or
re-authorization trigger.

The hard constraint here is that a connection *is* a credential. Access tokens
are held in ``EncryptedTextField`` columns whose key derives from Django's
``SECRET_KEY``, and nothing routed in this module may ever echo a stored secret
back to the caller: status endpoints report whether a link exists, which account
it points at and whether it still authenticates - never the token itself. An API
key is already a delegated credential, so returning a linked service's token
through it would quietly turn a scoped UrbanLens key into full access to that
third-party account.

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

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = []
