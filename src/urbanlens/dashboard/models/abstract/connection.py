"""Manager for a per-profile provider connection whose credentials are encrypted at rest."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken
from django.db import connections, router, transaction
from django.db.models import ForeignKey, Model

from urbanlens.dashboard.models.abstract.queryset import DashboardManager

if TYPE_CHECKING:
    from collections.abc import Mapping

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class ProfileConnectionManager[ConnectionT: Model](DashboardManager):
    """Lookups for a one-per-profile connection that this process may be unable to decrypt.

    A row this process cannot decrypt is not necessarily dead: another process may hold a key this
    one has not been given yet, mid key-rotation. Reads therefore treat it as absent and leave it
    in place; only an explicit disconnect, or a reconnect replacing it, removes it.

    The model must have a ``profile`` relation that is unique per row.
    """

    def get_for_profile(self, profile: Profile) -> ConnectionT | None:
        """Return this profile's connection, or None if there is none this process can read.

        Args:
            profile: The profile whose connection to look up.

        Returns:
            The connection, or None when absent or undecryptable here. An undecryptable row is kept.
        """
        try:
            return self.filter(profile=profile).first()
        except InvalidToken:
            logger.warning("%s for profile %s cannot be decrypted with this process's keys; treating it as not connected", self.model.__name__, profile.pk)
            return None

    def delete_for_profile(self, profile: Profile) -> None:
        """Remove this profile's connection, whether or not this process can decrypt it.

        Args:
            profile: The profile whose connection to remove.
        """
        try:
            self.filter(profile=profile).delete()
        except InvalidToken:
            self._delete_rows_of(profile)

    def clear_undecryptable(self, profile: Profile) -> bool:
        """Remove this profile's connection only if this process cannot decrypt it.

        Args:
            profile: The profile whose connection to check.

        Returns:
            True if an undecryptable row was removed.
        """
        try:
            self.filter(profile=profile).first()
        except InvalidToken:
            logger.warning("Replacing %s for profile %s, which cannot be decrypted with this process's keys", self.model.__name__, profile.pk)
            self._delete_rows_of(profile)
            return True
        return False

    def connect_for_profile(self, profile: Profile, defaults: Mapping[str, Any]) -> tuple[ConnectionT, bool]:
        """Create or update this profile's connection, replacing one this process cannot decrypt.

        A readable connection is updated in place, so fields the caller does not pass (its
        creation time, a refresh token the provider omitted on re-consent) survive.

        Args:
            profile: The profile connecting.
            defaults: Field values to write.

        Returns:
            The connection and whether it was created.
        """
        with transaction.atomic(using=router.db_for_write(self.model)):
            self.clear_undecryptable(profile)
            return self.update_or_create(profile=profile, defaults=dict(defaults))

    def _delete_rows_of(self, profile: Profile) -> None:
        """Delete by SQL: a queryset delete that has to load rows (for signals or cascades) would raise again."""
        meta = self.model._meta  # noqa: SLF001 - _meta is public API
        field = meta.get_field("profile")
        if not isinstance(field, ForeignKey):
            raise TypeError(f"{self.model.__name__}.profile must be a ForeignKey or OneToOneField")
        db = connections[router.db_for_write(self.model)]
        with db.cursor() as cursor:
            cursor.execute(f"DELETE FROM {db.ops.quote_name(meta.db_table)} WHERE {db.ops.quote_name(field.column)} = %s", [profile.pk])  # noqa: S608 - identifiers from _meta
