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
import logging
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken
from django.db.models import CASCADE, CharField, DateTimeField, OneToOneField
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


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


def get_photos_account(profile: Profile) -> GooglePhotosAccount | None:
    """Fetch the profile's Google Photos connection, healing it if undecryptable.

    A row whose ``access_token``/``refresh_token`` can no longer be decrypted
    (e.g. ``UL_FIELD_ENCRYPTION_KEY`` rotated without migrating old rows) is
    permanently unusable - there is no way to recover the plaintext tokens.
    Rather than raising and crashing every page that touches the user's
    Google Photos connection, treat it as absent and delete the stale row so
    the "Connect Google Photos" flow is offered again.

    Args:
        profile: The profile whose Google Photos connection to look up.

    Returns:
        The connected account, or None if there isn't one (or it was just
        removed for being undecryptable).
    """
    try:
        return GooglePhotosAccount.objects.filter(profile=profile).first()
    except InvalidToken:
        logger.exception(
            "GooglePhotosAccount for profile %s has undecryptable tokens (field_encryption_key changed?) - removing it so the user can reconnect.",
            profile.id,
        )
        GooglePhotosAccount.objects.filter(profile=profile).delete()
        return None
