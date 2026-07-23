"""The external-facing REST API surface for third-party applications.

Deliberately separate from the internal REST surface under ``dashboard/rest/``
(see ``dashboard/urls.py``): different auth (API key, not session), different
serializers (a conservative, independently-versioned subset of fields, never
the internal ``PinSerializer``/``ProfileSerializer``), and different
permission model (per-key scopes via ``HasApiKeyScope``, not
``IsAuthenticated``). Nothing in this package should import from - or be
imported by - the internal viewsets in ``dashboard/models/*/viewset.py``.

Currently exposes exactly two things, matching what an ``ApiKey`` can be
scoped to grant (see ``models.account.model.ApiKeyScope``): reading the
owning user's uuid, and creating pins on their behalf through the same
``services.pin_creation.create_pin_for_profile`` call the map UI uses.
"""
