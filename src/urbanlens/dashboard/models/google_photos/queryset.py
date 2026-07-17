"""Custom manager for GooglePhotosAccount."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class GooglePhotosAccountManager(abstract.DashboardManager):
    """Adds lookups that self-heal when a stored token can't be decrypted.

    Mirrors ``ImmichAccountManager.get_for_profile()``/``GoogleCalendarAccountManager.get_for_profile()`` -
    a field-encryption-key change (see ``models.fields.EncryptedTextField``)
    leaves any previously-saved ``access_token``/``refresh_token`` permanently
    unreadable, so every page or task that touches the account crashes with
    ``InvalidToken`` unless callers treat that the same as "never connected"
    and remove the now-useless row.
    """

    def get_for_profile(self, profile: Profile) -> GooglePhotosAccount | None:
        """Return this profile's Google Photos connection, or None if absent or undecryptable.

        Args:
            profile: The profile whose Google Photos connection to look up.

        Returns:
            The connected account, or None if there isn't one (or it was just
            removed for being undecryptable).
        """
        try:
            return self.filter(profile=profile).first()
        except InvalidToken:
            logger.exception(
                "GooglePhotosAccount for profile %s has undecryptable tokens (field_encryption_key changed?) - removing it so the user can reconnect.",
                profile.id,
            )
            self.filter(profile=profile).delete()
            return None

    def delete_for_profile(self, profile: Profile) -> None:
        """Delete this profile's Google Photos connection, if any.

        Args:
            profile: The profile whose Google Photos connection to remove.
        """
        self.filter(profile=profile).delete()
