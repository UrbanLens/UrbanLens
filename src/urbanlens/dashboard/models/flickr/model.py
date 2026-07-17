"""Per-user Flickr OAuth 1.0a connection.

Each user connects *their own* Flickr account via OAuth 1.0a - there is no
site-wide Flickr account. ``oauth_token``/``oauth_token_secret`` are the
long-lived credential pair Flickr issues after the user authorizes the app;
together they're equivalent in sensitivity to a Google OAuth refresh token,
so both are stored encrypted at rest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, DateTimeField, OneToOneField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField
from urbanlens.dashboard.models.flickr.queryset import FlickrAccountManager


class FlickrAccount(abstract.DashboardModel):
    """OAuth 1.0a credentials for one user's own Flickr account.

    One row per profile. Created by the "Connect Flickr" flow once the user
    authorizes the app and the request token is exchanged for an access
    token, and deleted when the user disconnects.
    """

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="flickr_account",
    )
    oauth_token = EncryptedTextField()
    oauth_token_secret = EncryptedTextField()
    flickr_user_id = CharField(max_length=64, help_text="Flickr NSID (user id) of the connected account.")
    flickr_username = CharField(max_length=255, null=True, blank=True, help_text="Flickr username, for display only.")
    connected_at = DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        profile_id: int

    objects = FlickrAccountManager()

    def photo_web_url(self, photo_id: str) -> str:
        """Return the Flickr web URL for one photo.

        Used both as the "view on Flickr" attribution link and as the de-dup
        key stored on ``Image.source_url`` - a photo already imported to a
        pin is recognised by matching this URL, without re-downloading it.

        Args:
            photo_id: The Flickr photo id.

        Returns:
            The photo's URL in the Flickr web UI.
        """
        return f"https://www.flickr.com/photos/{self.flickr_user_id}/{photo_id}/"

    def __str__(self) -> str:
        return f"Flickr account for {self.profile} ({self.flickr_username or self.flickr_user_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_flickr_accounts"
