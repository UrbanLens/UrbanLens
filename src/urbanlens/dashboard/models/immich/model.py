"""Per-user Immich server connection.

Each user connects *their own* self-hosted Immich server via a personal API
key - there is no site-wide Immich instance. Unlike ``GoogleCalendarAccount``
(whose OAuth tokens are scoped and remotely revocable), a raw Immich API key
grants direct read access to the user's whole photo library, so ``api_key``
is stored encrypted at rest via ``EncryptedTextField``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, DateTimeField, OneToOneField, URLField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField


class ImmichAccount(abstract.DashboardModel):
    """Credentials for one user's own self-hosted Immich server.

    One row per profile. Created by the Settings "Connect Immich" form after
    the server URL and API key are verified with a live ``ping()`` call, and
    deleted when the user disconnects.
    """

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="immich_account",
    )
    server_url = URLField(max_length=500, help_text="Base URL of the user's Immich server, e.g. https://photos.example.com")
    api_key = EncryptedTextField()
    connected_at = DateTimeField(auto_now_add=True)
    last_verified = DateTimeField(null=True, blank=True, help_text="When the credentials were last confirmed to work.")

    if TYPE_CHECKING:
        profile_id: int

    def asset_web_url(self, asset_id: str) -> str:
        """Return the Immich web URL for one asset.

        Used both as the "view on Immich" attribution link and as the de-dup
        key stored on ``Image.source_url`` - an asset already imported to a
        pin is recognised by matching this URL, without re-downloading it.

        Args:
            asset_id: The Immich asset id.

        Returns:
            The asset's URL in the Immich web UI.
        """
        return f"{self.server_url.rstrip('/')}/photos/{asset_id}"

    def __str__(self) -> str:
        return f"Immich account for {self.profile} ({self.server_url})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_immich_accounts"
