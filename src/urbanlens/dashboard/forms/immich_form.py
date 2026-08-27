"""Form for connecting a user's self-hosted Immich server."""

from __future__ import annotations

from django import forms

from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, ensure_public_http_url


class ImmichAccountForm(forms.ModelForm):
    """Server URL and API key for the Settings "Connect Immich" form."""

    api_key = forms.CharField(widget=forms.PasswordInput(render_value=False), help_text="Generate one from Immich under Account Settings > API Keys.")

    class Meta:
        model = ImmichAccount
        fields = ["server_url", "api_key"]
        widgets = {
            "server_url": forms.URLInput(attrs={"placeholder": "https://photos.example.com"}),
        }

    def clean_server_url(self) -> str:
        """Strip a trailing slash and reject a server that isn't publicly reachable.

        A self-hosted Immich server is fetched from server-side code (both a
        live ``ping()`` on connect and every subsequent asset/thumbnail
        proxy), so an unvalidated ``server_url`` would let any authenticated
        user direct those requests at internal infrastructure (SSRF) - e.g.
        cloud metadata endpoints or services on UrbanLens's own network. This
        reuses the shared ``url_safety`` guard, which resolves the hostname
        and rejects anything that lands on a loopback/private/link-local/
        reserved address, not just an internal-looking literal.

        Returns:
            The validated url, with any trailing slash stripped so
            ``asset_web_url``/API paths never double up on ``//``.

        Raises:
            forms.ValidationError: If the url isn't publicly reachable.
        """
        url = self.cleaned_data["server_url"].rstrip("/")
        try:
            ensure_public_http_url(url)
        except UnsafeUrlError as exc:
            raise forms.ValidationError("This server address isn't publicly reachable and can't be used.") from exc
        return url
