"""The external-facing REST API surface for third-party applications.

Deliberately separate from the internal REST surface under ``dashboard/rest/``
(see ``dashboard/urls.py``): different auth (API key or OAuth2 access token,
not session), different serializers (a conservative, independently-versioned
subset of fields, never the internal ``PinSerializer``/``ProfileSerializer``),
and different permission model (per-credential scopes via ``HasApiKeyScope``,
not ``IsAuthenticated``). Nothing in this package should import from - or be
imported by - the internal viewsets in ``dashboard/models/*/viewset.py``.

Exposes exactly what a credential can be scoped to grant (see
``models.account.model.ApiKeyScope``, mirrored by
``OAUTH2_PROVIDER["SCOPES"]``): reading the owning user's uuid
(``whoami/``), delta-syncing their pins and pin deletions
(``pins/``, ``pins/deleted/`` - cursor + ``modified_since`` + tombstones,
built for the native apps' offline-first sync), and creating pins on their
behalf (``POST pins/``, idempotent via client-generated uuid) through the
same ``services.pin_creation.create_pin_for_profile`` call the map UI uses.
The OpenAPI contract for this surface - and nothing else - is served at
``schema/`` (browsable at ``docs/``).
"""
