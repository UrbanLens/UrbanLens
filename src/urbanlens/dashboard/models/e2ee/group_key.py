"""Per-group symmetric keys, sealed individually to each member's public key.

The group-chat extension of ``ConversationKey``: one random 32-byte key per
``(group, version)``, generated in a member's browser and sealed
(``crypto_box_seal``) once per member. The server stores only the sealed
envelopes and can open none of them.

Versioning is what enforces membership boundaries cryptographically:

- A **new member** gets envelopes only for versions created after they
  joined - old ciphertext stays unreadable to them even if they somehow
  obtained it (the server additionally never serves them pre-join messages).
- A **removed member** keeps their old envelopes (their own history stays
  readable, matching the recoverability-over-forward-secrecy trade documented
  in ``docs/e2ee.md``) but is excluded from every later version, so messages
  sent after their removal are unreadable to them.

Clients rotate to a new version whenever the latest version's envelope set no
longer matches the group's active membership (the key endpoint reports this
as ``needs_rotation``), and the server refuses to store a version whose
envelopes don't cover the active membership exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, ForeignKey, PositiveIntegerField, TextField, UniqueConstraint

from urbanlens.dashboard.models import abstract


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
