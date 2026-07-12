"""Per-user Google Photos Picker connection.

A separate model from ``GoogleCalendarAccount`` even though both are Google
OAuth grants against the same site-wide client (``UL_GOOGLE_CLIENT_ID``/
``UL_GOOGLE_CLIENT_SECRET``): Calendar and Photos are independent features a
user may connect one of without the other, and Google issues distinct token
pairs per distinct scope grant, so conflating them into one row would make
"disconnect Calendar" accidentally revoke Photos access and vice versa.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, DateTimeField, OneToOneField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField


class GooglePhotosAccount(abstract.DashboardModel):
    """OAuth credentials for one user's own Google Photos Picker access.

    One row per profile. Created by the "Connect Google Photos" flow and
    deleted (after best-effort token revocation) when the user disconnects.
    """

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="google_photos_account",
    )
    google_email = CharField(max_length=255, null=True, blank=True, help_text="Email of the connected Google account, for display only.")
    access_token = EncryptedTextField()
    refresh_token = EncryptedTextField(null=True, blank=True)
    token_expiry = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        profile_id: int

    @property
    def is_token_expired(self) -> bool:
        """Whether the access token is expired or about to expire.

        A 60-second safety margin is applied so a token that expires mid-call
        is treated as already expired.

        Returns:
            True when the token must be refreshed before use.
        """
        if self.token_expiry is None:
            return True
        return self.token_expiry <= timezone.now() + datetime.timedelta(seconds=60)

    def __str__(self) -> str:
        return f"Google Photos for {self.profile} ({self.google_email or 'unknown email'})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_google_photos_accounts"
