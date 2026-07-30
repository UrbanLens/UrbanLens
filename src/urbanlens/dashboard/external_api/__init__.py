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

Errors
------

Every failure from every endpoint in this package renders in exactly one
envelope, so a generated client can have a single error path:

- ``{"error": "<message>"}`` - the general case, covering hand-written
  refusals as well as the 401/403/404/405/429 DRF raises before a handler
  runs.
- ``{"error": "Invalid request.", "fields": {"<name>": ["<message>"]}}`` -
  when the failure is per-field, so a form can still be annotated.

DRF's native ``{"detail": ...}`` and bare field-keyed dicts never reach the
wire here. That is enforced by ``errors.ErrorEnvelopeMixin``, inherited by both
view bases (``views.ExternalApiView`` and ``mixins.DualAuthJsonView``) rather
than opted into per endpoint, and specifically *not* by changing
``REST_FRAMEWORK["EXCEPTION_HANDLER"]``, which the internal session API shares.

One consequence is load-bearing rather than cosmetic: **every** 404 renders the
byte-identical body ``{"error": "Not found."}``, with any upstream detail
discarded. A resource that belongs to someone else must be indistinguishable
from one that never existed, or the endpoint becomes an existence oracle. See
``errors`` for the full rationale.
"""
