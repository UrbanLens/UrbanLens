"""Serializers for the *watcher* half of safety check-ins: chat, invites, partnered check-ins.

``serializers.py`` carries the explorer's half - the shapes an owner sees of
their own check-in. This module carries the shapes the people watching one see:
the shared chat transcript, a partner's own outstanding invitations, and the
check-ins they watch on someone else's behalf.

Two disclosure rules apply to everything here and are worth stating once:

**No contact token, ever.** ``SafetyCheckinContact.token`` is the sole
credential for the session-free contact portal - anyone holding it can read the
check-in, post to its chat, and mark the owner safe. It is deliberately absent
from every payload in ``serializers.py`` (see
:class:`~urbanlens.dashboard.external_api.serializers.SafetyCheckinContactSerializer`)
and must stay absent here. :class:`SafetyCheckinMessageSerializer` touches a
contact row directly, so it exposes only that contact's *display name*, never
the row itself.

**Identify a sender by name, and by account only when there is an account.** A
chat participant may be an email-only emergency contact with no account on this
site. Emitting a username or profile uuid for them is not merely null-shaped
noise: it would invite a client to treat "contact" and "user" as the same kind
of thing and, for instance, link a profile page that does not exist. The
``sender_kind`` discriminator exists so a client branches on the kind rather
than on which field happened to be null.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework import serializers

from urbanlens.dashboard.external_api.serializers import SafetyCheckinDetailSerializer, SafetyCheckinSummarySerializer

# The model types below are imported at runtime, not under TYPE_CHECKING, even
# though they appear only in annotations. drf-spectacular introspects every
# ``SerializerMethodField`` handler with ``typing.get_type_hints`` to infer the
# field's schema type, and that call *evaluates the whole signature* - including
# the ``obj`` parameter it has no interest in. Under
# ``from __future__ import annotations`` a TYPE_CHECKING-only name is
# unresolvable at that moment, so schema generation dies with a bare
# ``NameError: name 'SafetyCheckinMessage' is not defined`` and takes the entire
# published contract (``schema/`` and ``docs/``) down with it - a failure that
# no test of these endpoints' own behavior would ever surface, because it only
# appears when the OpenAPI document is generated. ``serializers.py`` imports its
# model types at runtime for exactly this reason.
#
# Names used only in a *local variable* annotation inside a method body are
# exempt - PEP 563 never evaluates those - which is why ``Profile`` stays in the
# type-checking block below where ruff's TC001 wants it.
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinMessage, SafetyCheckinPartner, SafetyCheckinPartnerStatus
from urbanlens.dashboard.services.safety import MAX_CHAT_MESSAGE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class SafetyCheckinMessageSerializer(serializers.Serializer):
    """One message in a check-in's shared chat (read-only).

    ``is_mine`` is resolved against a ``viewer`` profile passed in the
    serializer context. It is computed server-side rather than left to the
    client because a client comparing usernames would get it wrong for exactly
    the participants that matter: two different people can appear under the same
    display name (an email-only contact's ``name`` is free text the owner typed,
    and nothing stops it matching another participant's username), so the only
    reliable identity comparison is on the sender's profile id, which is not in
    the payload and should not be.
    """

    id = serializers.IntegerField(read_only=True)
    body = serializers.CharField(read_only=True, allow_blank=True)
    sender_name = serializers.CharField(read_only=True)
    sender_kind = serializers.SerializerMethodField()
    sender_username = serializers.SerializerMethodField()
    sender_profile_uuid = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    created = serializers.DateTimeField(read_only=True)

    def get_sender_kind(self, obj: SafetyCheckinMessage) -> str:
        """Return what kind of participant sent this message.

        Args:
            obj: The message being serialized.

        Returns:
            ``"profile"`` for the owner or a partner, ``"contact"`` for an
            emergency contact posting through their portal link, and
            ``"unknown"`` when neither is set. The last case is real, not
            defensive padding: both sender foreign keys are ``SET_NULL``, so a
            deleted account or a removed contact leaves its messages in place
            with no sender at all, and the transcript must still render.
        """
        if obj.sender_profile_id is not None:
            return "profile"
        if obj.sender_contact_id is not None:
            return "contact"
        return "unknown"

    def get_sender_username(self, obj: SafetyCheckinMessage) -> str | None:
        """Return the sending account's username, or null when there is no account.

        Args:
            obj: The message being serialized.

        Returns:
            The username, or None for a contact-sent or orphaned message.
        """
        return obj.sender_profile.username if obj.sender_profile is not None else None

    def get_sender_profile_uuid(self, obj: SafetyCheckinMessage) -> str | None:
        """Return the sending account's profile uuid, or null when there is no account.

        Args:
            obj: The message being serialized.

        Returns:
            The profile uuid as a string, or None for a contact-sent or orphaned message.
        """
        return str(obj.sender_profile.uuid) if obj.sender_profile is not None else None

    def get_is_mine(self, obj: SafetyCheckinMessage) -> bool:
        """Whether the requesting credential's owner sent this message.

        Args:
            obj: The message being serialized.

        Returns:
            True when the message's sending profile is the viewer. A
            contact-sent message is never "mine" even if the viewer happens to
            also be listed as a contact - ``resolve_message_sender`` attributes
            a logged-in contact's own messages to their profile, so a message
            that stayed on the contact row was posted through the portal link,
            not by this credential.
        """
        viewer: Profile | None = self.context.get("viewer")
        return viewer is not None and obj.sender_profile_id == viewer.pk


class SafetyCheckinMessageCreateSerializer(serializers.Serializer):
    """Validates an untrusted chat message submission.

    The bounds mirror ``services.safety.create_chat_message`` rather than
    replacing it. That duplication is deliberate: the service is the real gate
    (the WebSocket path reaches it without passing through here), and this
    serializer exists so a REST caller gets the uniform field-keyed 400 envelope
    instead of a message-shaped one. ``MAX_CHAT_MESSAGE_LENGTH`` is imported
    rather than restated so the two cannot drift into disagreeing about what
    "too long" means.

    ``trim_whitespace`` is DRF's default and is relied on: a body of spaces is
    blank once the service strips it, so it must be rejected here too, or the
    request would pass validation and then fail in the service with a
    differently-shaped error.
    """

    body = serializers.CharField(max_length=MAX_CHAT_MESSAGE_LENGTH, allow_blank=False, trim_whitespace=True)


class SafetyCheckinMessageListResponseSerializer(serializers.Serializer):
    """Documents the paginated chat transcript envelope (schema-only)."""

    count = serializers.IntegerField(read_only=True)
    next = serializers.CharField(read_only=True, allow_null=True)
    previous = serializers.CharField(read_only=True, allow_null=True)
    results = SafetyCheckinMessageSerializer(many=True, read_only=True)


class SafetyPartnerRoleSerializer(serializers.Serializer):
    """The caller's own partner row on someone else's check-in (read-only).

    Serves both the pending-invite listing and the accept response, so a client
    can render an invitation and its accepted result from one shape.

    It carries the check-in's ``title`` even though the caller may not have
    accepted yet, because that is already disclosed: the invitation
    notification and email both name the check-in
    (``services.safety._notify_checkin_partner_invite``). Withholding it here
    would only leave the client rendering "someone invited you to something".
    Nothing *else* about the check-in is exposed before acceptance - no plan, no
    destination, no contacts, no live position - and the ``checkin_uuid`` is the
    caller's own accept/decline handle, not a readable address: the partner
    detail endpoint resolves it through
    ``services.safety.get_partnered_checkin``, which requires ACCEPTED.
    """

    checkin_uuid = serializers.SerializerMethodField()
    checkin_title = serializers.SerializerMethodField()
    checkin_status = serializers.SerializerMethodField()
    checkin_by = serializers.SerializerMethodField()
    owner_username = serializers.SerializerMethodField()
    owner_profile_uuid = serializers.SerializerMethodField()
    invited_by_username = serializers.SerializerMethodField()
    status = serializers.ChoiceField(choices=SafetyCheckinPartnerStatus.choices, read_only=True)
    invited_at = serializers.DateTimeField(source="created", read_only=True)
    accepted_at = serializers.DateTimeField(read_only=True, allow_null=True)

    def get_checkin_uuid(self, obj: SafetyCheckinPartner) -> str:
        """Return the uuid of the check-in this invitation is for.

        Args:
            obj: The partner row being serialized.

        Returns:
            The check-in's uuid as a string - the path segment of the accept and
            decline endpoints. Uuid, not slug: a slug is unique only per owner,
            so it cannot address another person's check-in.
        """
        return str(obj.checkin.uuid)

    def get_checkin_title(self, obj: SafetyCheckinPartner) -> str:
        """Return the check-in's title.

        Args:
            obj: The partner row being serialized.

        Returns:
            The title, which is blank for a check-in whose PII has already been
            scrubbed into its encrypted archive.
        """
        return obj.checkin.title

    def get_checkin_status(self, obj: SafetyCheckinPartner) -> str:
        """Return the check-in's lifecycle status.

        Args:
            obj: The partner row being serialized.

        Returns:
            The status value, so a client can tell a still-live invitation from
            one whose check-in already concluded while it sat unanswered.
        """
        return obj.checkin.status

    def get_checkin_by(self, obj: SafetyCheckinPartner) -> str:
        """Return the check-in's deadline.

        Args:
            obj: The partner row being serialized.

        Returns:
            ISO-8601 timestamp of when the owner is due to check in.
        """
        return obj.checkin.checkin_by.isoformat()

    def get_owner_username(self, obj: SafetyCheckinPartner) -> str:
        """Return the username of the person the check-in is about.

        Args:
            obj: The partner row being serialized.

        Returns:
            The check-in owner's username - who the caller would be watching.
        """
        return obj.checkin.profile.username

    def get_owner_profile_uuid(self, obj: SafetyCheckinPartner) -> str:
        """Return the check-in owner's profile uuid.

        Args:
            obj: The partner row being serialized.

        Returns:
            The owner's profile uuid as a string.
        """
        return str(obj.checkin.profile.uuid)

    def get_invited_by_username(self, obj: SafetyCheckinPartner) -> str | None:
        """Return the username of whoever sent the invitation.

        Args:
            obj: The partner row being serialized.

        Returns:
            The inviter's username, or None if that account is gone. Normally
            the same as ``owner_username`` - only an owner may invite a partner -
            but kept separate rather than assumed equal, so the payload does not
            quietly become wrong if delegated invites ever exist.
        """
        return obj.invited_by.username if obj.invited_by is not None else None


class SafetyPartnerInviteListResponseSerializer(serializers.Serializer):
    """Documents the paginated pending-invite envelope (schema-only)."""

    count = serializers.IntegerField(read_only=True)
    next = serializers.CharField(read_only=True, allow_null=True)
    previous = serializers.CharField(read_only=True, allow_null=True)
    results = SafetyPartnerRoleSerializer(many=True, read_only=True)


class _PartneredCheckinOwnerFieldsMixin:
    """Adds the check-in owner's identity to a check-in payload.

    Every owner-facing check-in serializer omits the owner, because there the
    owner is always the caller. On the partner surface the owner is the single
    most important field in the payload - the whole row means "this person is
    out somewhere" - so both the summary and the detail shape gain it, from one
    definition rather than two.
    """

    def get_owner_username(self, obj: SafetyCheckin) -> str:
        """Return the username of the person this check-in is about.

        Args:
            obj: The check-in being serialized.

        Returns:
            The owner's username.
        """
        return obj.profile.username

    def get_owner_profile_uuid(self, obj: SafetyCheckin) -> str:
        """Return the profile uuid of the person this check-in is about.

        Args:
            obj: The check-in being serialized.

        Returns:
            The owner's profile uuid as a string.
        """
        return str(obj.profile.uuid)


class PartneredCheckinSummarySerializer(_PartneredCheckinOwnerFieldsMixin, SafetyCheckinSummarySerializer):
    """A watched check-in as it appears in the partner's list.

    Identical to the owner's own list row plus the owner's identity - a partner
    watching several people needs to tell the rows apart, and deliberately no
    thinner than the owner's row otherwise, because an accepted partner already
    has the full detail view on the website.
    """

    owner_username = serializers.SerializerMethodField()
    owner_profile_uuid = serializers.SerializerMethodField()


class PartneredCheckinDetailSerializer(_PartneredCheckinOwnerFieldsMixin, SafetyCheckinDetailSerializer):
    """The full document for a check-in the caller watches as an accepted partner.

    Deliberately the owner's own detail shape, not a redacted one. That is the
    existing product rule, not a new decision: the website hands an accepted
    partner the exact same detail page as the owner, minus the edit controls
    (``controllers.safety.SafetyCheckinDetailView._render_full_detail`` with
    ``viewer_is_partner=True``), on the grounds that a partner's entire purpose
    is seeing the plan *before* something goes wrong. Serving a thinner payload
    here would mean the mobile client showed a watcher less than the website
    already does, and the two would drift on which fields are "partner-safe".

    The write side is where the surfaces differ, and that difference lives in
    the routing, not in this serializer: a partner reaches this document through
    the partner endpoints, which expose exactly one write (mark-safe).
    """

    owner_username = serializers.SerializerMethodField()
    owner_profile_uuid = serializers.SerializerMethodField()


class PartneredCheckinListResponseSerializer(serializers.Serializer):
    """Documents the paginated partnered-check-in envelope (schema-only)."""

    count = serializers.IntegerField(read_only=True)
    next = serializers.CharField(read_only=True, allow_null=True)
    previous = serializers.CharField(read_only=True, allow_null=True)
    results = PartneredCheckinSummarySerializer(many=True, read_only=True)
