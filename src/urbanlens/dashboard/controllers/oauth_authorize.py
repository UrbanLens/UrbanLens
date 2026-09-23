"""The OAuth2 consent page, with its form allowed to hand the code back to the client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from oauth2_provider.views import AuthorizationView

if TYPE_CHECKING:
    from django.http import HttpResponse


def form_action_source(redirect_uri: str) -> str | None:
    """The CSP source matching a client's redirect URI.

    Args:
        redirect_uri: A redirect URI the toolkit has already validated against the client.

    Returns:
        ``scheme://host[:port]`` for http(s), ``scheme:`` for a native app's custom scheme, or
        None when the URI has no usable scheme.
    """
    parts = urlsplit(redirect_uri)
    if not parts.scheme:
        return None
    if parts.scheme in {"http", "https"}:
        return f"{parts.scheme}://{parts.netloc}" if parts.netloc else None
    return f"{parts.scheme}:"


class ConsentAuthorizationView(AuthorizationView):
    """``oauth2_provider``'s authorize view, admitting the client's redirect to ``form-action``.

    Approving answers the form POST with a redirect to the client, and browsers hold that redirect
    to the page's ``form-action``, so the site's ``'self'``-only policy would strand the code.
    Only the rendered consent form is widened, and only to the redirect URI the GET validated.
    """

    def render_to_response(self, context: dict[str, Any], **response_kwargs: Any) -> HttpResponse:
        response = super().render_to_response(context, **response_kwargs)
        redirect_uri = self.oauth2_data.get("redirect_uri") if context.get("form") is not None else None
        source = form_action_source(redirect_uri) if isinstance(redirect_uri, str) else None
        if source:
            # The attributes django-csp's csp_update decorator sets, one per header mode.
            for attribute in ("_csp_update", "_csp_update_ro"):
                setattr(response, attribute, {"form-action": [source]})
        return response
