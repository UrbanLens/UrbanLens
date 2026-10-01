"""The external-facing REST API surface for third-party applications.

Deliberately separate from the internal REST surface under ``dashboard/rest/`` (see
``dashboard/urls.py``): different auth (API key or OAuth2 access token, not session), different
serializers (a conservative, independently-versioned subset of fields, never the internal
``PinSerializer``/``ProfileSerializer``), and different permission model (per-credential scopes via
``HasApiKeyScope``, not ``IsAuthenticated``).
DRF's native ``{"detail": ...}`` and bare field-keyed dicts never reach the wire here.

- ``{"error": "<message>"}`` - the general case, covering hand-written refusals as well as the
  401/403/404/405/429 DRF raises before a handler runs.
- ``{"error": "Invalid request.", "fields": {"<name>": ["<message>"]}}`` - when the failure is
  per-field, so a form can still be annotated.
"""
