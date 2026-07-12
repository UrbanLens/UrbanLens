"""Form for connecting a user's self-hosted Immich server."""

from __future__ import annotations

from django import forms

from urbanlens.dashboard.models.immich.model import ImmichAccount


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
        """Strip a trailing slash so ``asset_web_url``/API paths never double up on ``//``."""
        return self.cleaned_data["server_url"].rstrip("/")
