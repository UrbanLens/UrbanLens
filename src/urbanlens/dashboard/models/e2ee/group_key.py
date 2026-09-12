"""Per-group symmetric keys, sealed individually to each member's public key.
The server stores only the sealed envelopes and can open none of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, ForeignKey, PositiveIntegerField, TextField, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.e2ee.queryset import GroupKeyManager


class GroupKey(abstract.DashboardModel):
    """One version of one group chat's symmetric message key."""

    group = ForeignKey(
        "dashboard.GroupChat",
        on_delete=CASCADE,
        related_name="keys",
    )
    version = PositiveIntegerField(default=1)

    created_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        related_name="group_keys_created",
        null=True,
        blank=True,
    )

    objects = GroupKeyManager()

    if TYPE_CHECKING:
        group_id: int

    def __str__(self) -> str:
        """Return a human-readable description of this key version.

        Returns:
            String like "GroupKey(group=3, v2)".
        """
        return f"GroupKey(group={self.group_id}, v{self.version})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_e2ee_group_key"
        constraints = [
            UniqueConstraint(fields=["group", "version"], name="db_e2ee_groupkey_group_version"),
        ]


class GroupKeyEnvelope(abstract.DashboardModel):
    """One member's sealed copy of one group key version.

    ``wrapped_key`` is a base64 ``crypto_box_seal`` blob addressed to the
    member's identity public key; only that member's private key can open it.
    """

    key = ForeignKey(
        "dashboard.GroupKey",
        on_delete=CASCADE,
        related_name="envelopes",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="group_key_envelopes",
    )
    wrapped_key = TextField()

    if TYPE_CHECKING:
        key_id: int
        profile_id: int

    def __str__(self) -> str:
        """Return a human-readable description of this envelope.

        Returns:
            String like "GroupKeyEnvelope(key=3, profile=7)".
        """
        return f"GroupKeyEnvelope(key={self.key_id}, profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_e2ee_group_key_envelope"
        constraints = [
            UniqueConstraint(fields=["key", "profile"], name="db_e2ee_groupkey_one_envelope"),
        ]
