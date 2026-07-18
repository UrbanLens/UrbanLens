"""Per-conversation symmetric keys, sealed to each participant's public key."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CheckConstraint, F, ForeignKey, Index, PositiveIntegerField, Q, TextField, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.e2ee.queryset import ConversationKeyManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class ConversationKey(abstract.DashboardModel):
    """One version of a conversation's random symmetric key, wrapped for both parties.

    The key itself is generated in the browser of whichever participant sends
    first, then sealed (``crypto_box_seal``) once to each participant's public
    key - the server stores only the two sealed blobs and can decrypt neither.

    The pair is stored in canonical order (``profile_low.pk < profile_high.pk``)
    so one row serves both directions. ``version`` increments when a "reset
    keys" forces a fresh key; old versions are retained so the participant who
    did NOT reset can still unseal their copy of the history on a new device.
    """

    profile_low = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="conversation_keys_low",
    )
    profile_high = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="conversation_keys_high",
    )

    # crypto_box_seal blobs (base64) of the same symmetric key, one per party.
    wrapped_for_low = TextField()
    wrapped_for_high = TextField()

    version = PositiveIntegerField(default=1)

    created_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        related_name="conversation_keys_created",
        null=True,
        blank=True,
    )

    objects = ConversationKeyManager()

    if TYPE_CHECKING:
        profile_low_id: int
        profile_high_id: int

    @staticmethod
    def canonical_pair(profile_a: Profile, profile_b: Profile) -> tuple[Profile, Profile]:
        """Return the two profiles in canonical (low pk, high pk) order.

        Args:
            profile_a: One participant.
            profile_b: The other participant.

        Returns:
            ``(low, high)`` ordered by primary key.
        """
        return (profile_a, profile_b) if profile_a.pk < profile_b.pk else (profile_b, profile_a)

    def wrapped_for(self, profile_id: int) -> str:
        """Return the sealed key blob addressed to ``profile_id``.

        Args:
            profile_id: Primary key of one of the two participants.

        Returns:
            The base64 sealed blob that participant's private key can open.

        Raises:
            ValueError: If ``profile_id`` is not a participant of this key.
        """
        if profile_id == self.profile_low_id:
            return self.wrapped_for_low
        if profile_id == self.profile_high_id:
            return self.wrapped_for_high
        raise ValueError(f"Profile {profile_id} is not a participant of conversation key {self.pk}")

    def __str__(self) -> str:
        """Return a human-readable description of this key row.

        Returns:
            String like "ConversationKey(3<->7, v1)".
        """
        return f"ConversationKey({self.profile_low_id}<->{self.profile_high_id}, v{self.version})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_e2ee_conversation_key"
        indexes = [
            Index(fields=["profile_low", "profile_high"], name="idxdb_e2ee_convkey_pair"),
        ]
        constraints = [
            UniqueConstraint(
                fields=["profile_low", "profile_high", "version"],
                name="db_e2ee_convkey_pair_version",
            ),
            CheckConstraint(
                condition=Q(profile_low__lt=F("profile_high")),
                name="db_e2ee_convkey_canonical_order",
            ),
        ]
