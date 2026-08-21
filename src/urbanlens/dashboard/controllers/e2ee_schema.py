"""Schema serializers for the E2EE key-distribution API.

Documentation-only: the views parse JSON by hand (deliberately - key blobs
are validated as opaque size-bounded strings, and DRF field coercion adds
nothing), but the published OpenAPI schema previously said `object` about
every payload here. A native client generates its E2EE types from these, so
each field mirrors the view's actual reads/writes exactly; drift is caught by
`test_external_api_schema_e2ee.py`.

Blob fields are base64-encoded byte strings whose maximum lengths are
enforced in the views (`valid_blob`); they are documented as plain strings.
"""

from __future__ import annotations

from rest_framework import serializers


class E2EEEnrollRequestSerializer(serializers.Serializer):
    """POST /e2ee/keys/ body."""

    public_key = serializers.CharField()
    recovery_wrapped_secret = serializers.CharField()
    password_wrapped_secret = serializers.CharField(required=False, allow_blank=True)
    password_wrap_salt = serializers.CharField(required=False, allow_blank=True)
    auth_key = serializers.CharField(required=False, allow_blank=True, help_text="Derived login credential; password accounts only.")
    auth_salt = serializers.CharField(required=False, allow_blank=True)
    current_password = serializers.CharField(required=False, allow_blank=True, help_text="Proof of possession; required on accounts that have a password.")
    kdf_opslimit = serializers.IntegerField()
    kdf_memlimit = serializers.IntegerField()


class E2EEOwnKeysResponseSerializer(serializers.Serializer):
    """GET /e2ee/keys/ body. ``enrolled: false`` alone when no bundle exists."""

    enrolled = serializers.BooleanField()
    public_key = serializers.CharField(required=False)
    password_wrapped_secret = serializers.CharField(required=False, allow_blank=True)
    password_wrap_salt = serializers.CharField(required=False, allow_blank=True)
    password_wrap_stale = serializers.BooleanField(required=False)
    recovery_wrapped_secret = serializers.CharField(required=False)
    kdf_opslimit = serializers.IntegerField(required=False)
    kdf_memlimit = serializers.IntegerField(required=False)
    version = serializers.IntegerField(required=False)
    profile_slug = serializers.CharField(required=False)


class E2EEPartnerKeyResponseSerializer(serializers.Serializer):
    """GET /e2ee/keys/{profile_slug}/ body."""

    public_key = serializers.CharField()
    version = serializers.IntegerField()


class E2EEWrappedKeySerializer(serializers.Serializer):
    """One conversation/group key version wrapped for the caller."""

    version = serializers.IntegerField()
    wrapped_key = serializers.CharField()


class E2EEConversationKeysResponseSerializer(serializers.Serializer):
    """GET conversation-key body: every version wrapped for the caller."""

    keys = E2EEWrappedKeySerializer(many=True)
    latest = serializers.IntegerField()


class E2EEConversationKeyCreateRequestSerializer(serializers.Serializer):
    """POST conversation-key body: the new key wrapped for both parties."""

    wrapped_for_me = serializers.CharField()
    wrapped_for_partner = serializers.CharField()
    version = serializers.IntegerField(required=False, help_text="Expected next version; a stale value loses the race and gets the winner back with 200.")


class E2EEGroupMemberSerializer(serializers.Serializer):
    """A member the caller must wrap the next group key for."""

    id = serializers.CharField(help_text="Opaque per-(group, member) token - never a profile identifier.")
    public_key = serializers.CharField()


class E2EEGroupKeysResponseSerializer(serializers.Serializer):
    """GET group-key body."""

    keys = E2EEWrappedKeySerializer(many=True)
    latest = serializers.IntegerField()
    needs_rotation = serializers.BooleanField()
    members = E2EEGroupMemberSerializer(many=True)


class E2EEGroupKeyCreateRequestSerializer(serializers.Serializer):
    """POST group-key body: the rotated key wrapped per member token."""

    wrapped = serializers.DictField(child=serializers.CharField(), help_text="Member token -> wrapped key, covering every current member.")
    version = serializers.IntegerField(required=False)


class E2EERewrapRequestSerializer(serializers.Serializer):
    """POST rewrap body: the same secret re-wrapped under new credentials."""

    password_wrapped_secret = serializers.CharField(required=False, allow_blank=True)
    password_wrap_salt = serializers.CharField(required=False, allow_blank=True)
    recovery_wrapped_secret = serializers.CharField(required=False, allow_blank=True)


class E2EEOkResponseSerializer(serializers.Serializer):
    """Minimal success body."""

    ok = serializers.BooleanField()


class E2EEEnvelopeRefSerializer(serializers.Serializer):
    """One stored envelope the caller can rewrap."""

    id = serializers.IntegerField()
    wrapped_key = serializers.CharField()


class E2EERewrapAllResponseSerializer(serializers.Serializer):
    """GET rewrap-all body: everything wrapped for the caller, for bulk re-wrapping."""

    conversation_keys = E2EEEnvelopeRefSerializer(many=True)
    group_envelopes = E2EEEnvelopeRefSerializer(many=True)


class E2EEResetRequestSerializer(serializers.Serializer):
    """POST reset body: explicit confirmation plus a fresh bundle."""

    confirm = serializers.CharField(help_text="Must equal the documented reset confirmation phrase.")
    public_key = serializers.CharField()
    recovery_wrapped_secret = serializers.CharField()
    password_wrapped_secret = serializers.CharField(required=False, allow_blank=True)
    password_wrap_salt = serializers.CharField(required=False, allow_blank=True)
    kdf_opslimit = serializers.IntegerField(required=False)
    kdf_memlimit = serializers.IntegerField(required=False)


class E2EEResetResponseSerializer(serializers.Serializer):
    """POST reset response: the new bundle version and what happened to history.

    Declared explicitly because the endpoint never returned the generic
    ``{"ok": true}`` its schema previously claimed, and because
    ``not_rewrapped`` is the only signal a client gets that some of the
    caller's threads are now permanently unreadable.
    """

    version = serializers.IntegerField(help_text="The new key bundle version.")
    rewrapped = serializers.IntegerField(help_text="Key copies re-sealed to the new keypair; these stay readable.")
    not_rewrapped = serializers.IntegerField(
        help_text=("The caller's own conversation keys and group envelopes that were NOT re-sealed. They remain sealed to the retired key and are permanently unreadable - surface this to the user."),
    )
