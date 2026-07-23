"""Custom manager for FlickrAccount."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.flickr.model import FlickrAccount
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class FlickrAccountManager(abstract.DashboardManager):
    """Adds lookups that self-heal when a stored token can't be decrypted.

    Mirrors ``ImmichAccountManager``/``GoogleCalendarAccountManager``/
    ``GooglePhotosAccountManager``'s ``get_for_profile()`` - a
    field-encryption-key change (see ``models.fields.EncryptedTextField``)
    leaves any previously-saved ``oauth_token``/``oauth_token_secret``
    permanently unreadable. Unlike those three, nothing here ever caught
    ``InvalidToken`` at all - every page or task that touched a Flickr
    connection after a key rotation would 500 outright rather than treating
    it as "never connected" and offering reconnection.
    """

    def get_for_profile(self, profile: Profile) -> FlickrAccount | None:
        """Return this profile's Flickr connection, or None if absent or undecryptable.

        Args:
            profile: The profile whose Flickr connection to look up.

        Returns:
            The connected account, or None if there isn't one (or it was just
            removed for being undecryptable).
        """
        try:
            return self.filter(profile=profile).first()
        except InvalidToken:
            logger.exception(
                "FlickrAccount for profile %s has undecryptable tokens (field_encryption_key changed?) - removing it so the user can reconnect.",
                profile.id,
            )
            self.filter(profile=profile).delete()
            return None

    def delete_for_profile(self, profile: Profile) -> None:
        """Delete this profile's Flickr connection, if any.

        Args:
            profile: The profile whose Flickr connection to remove.
        """
        self.filter(profile=profile).delete()
