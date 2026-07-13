"""Per-user Immich server connection.

Each user connects *their own* self-hosted Immich server via a personal API
key - there is no site-wide Immich instance. Unlike ``GoogleCalendarAccount``
(whose OAuth tokens are scoped and remotely revocable), a raw Immich API key
grants direct read access to the user's whole photo library, so ``api_key``
is stored encrypted at rest via ``EncryptedTextField``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken
from django.db import connection
from django.db.models import CASCADE, DateTimeField, OneToOneField, URLField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.fields import EncryptedTextField

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class ImmichAccountManager(abstract.DashboardManager):
    """Adds lookups that self-heal when a stored api_key can't be decrypted.

    A field-encryption-key change (see ``models.fields.EncryptedTextField``)
    leaves any previously-saved ``api_key`` permanently unreadable - every
    plain ``.filter(...).first()``/``.get(...)`` on this model raises
    ``InvalidToken`` the moment the row is materialized, which used to 500
    every page and Celery task that touched the account. These methods treat
    that exactly like "never connected" and remove the now-useless row so
    the user can just reconnect.
    """

    def get_for_profile(self, profile: Profile) -> ImmichAccount | None:
        """Return this profile's Immich account, or None if absent or undecryptable."""
        try:
            return self.filter(profile=profile).first()
        except InvalidToken:
            logger.warning("ImmichAccount for profile %s is undecryptable (stale field_encryption_key) - clearing it", profile.pk)
            self._delete_undecryptable(profile.pk)
            return None

    def delete_for_profile(self, profile: Profile) -> None:
        """Delete this profile's Immich account, tolerating an undecryptable stored key."""
        try:
            self.filter(profile=profile).delete()
        except InvalidToken:
            self._delete_undecryptable(profile.pk)

    def _delete_undecryptable(self, profile_id: int) -> None:
        """Hard-delete via raw SQL.

        A normal queryset ``.delete()`` still instantiates matching rows to
        dispatch delete signals, which re-triggers the same decrypt failure -
        raw SQL (identifiers pulled from ``_meta``, not user input) sidesteps
        that entirely.
        """
        table = self.model._meta.db_table  # noqa: SLF001 - _meta is public API despite the underscore
        column = self.model._meta.get_field("profile").column  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE {column} = %s", [profile_id])  # noqa: S608 - identifiers from Django _meta, not user input


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

    objects = ImmichAccountManager()

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
