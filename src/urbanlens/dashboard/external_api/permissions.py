"""Per-key scope enforcement for the external API.

Distinct from Django's user permissions and from the internal API's
``IsAuthenticated`` default: an ``ApiKey`` can only do what its own
``scopes`` list grants, regardless of what the underlying user account could
do if it were logged in normally through the site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


class HasApiKeyScope(BasePermission):
    """Requires the authenticating ``ApiKey`` to grant every scope in ``view.required_scopes``.

    Views using this must define a ``required_scopes`` class attribute (a set
    of :class:`~urbanlens.dashboard.models.account.model.ApiKeyScope` values).
    A view with an empty/missing ``required_scopes`` is always denied rather
    than treated as "no scope needed" - an endpoint added here without
    remembering to set it should fail closed, not open.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Check that ``request.auth`` (the resolved ``ApiKey``) grants the view's required scopes."""
        api_key = request.auth
        required_scopes: frozenset[str] = getattr(view, "required_scopes", frozenset())
        if api_key is None or not required_scopes:
            return False
        return set(required_scopes).issubset(set(getattr(api_key, "scopes", ())))
