"""Request/response types for answering a pin someone shared with you.

Small surface, two deliberate choices:

- ``action`` is a strict ``ChoiceField``. ``services.sharing.pin_sharing.apply_pin_share_response``
  answers an unrecognized action with "Unknown action." and leaves the share
  pending, which is right for the internal HTMX forms (a typo must not 500) but
  wrong for an API: a client sending ``"Accept"`` would get a cheerful 200 back
  while nothing happened. Rejecting it here turns that into a 400 naming the
  field.
- The response reports the share's resulting ``status`` rather than echoing the
  action, so a client refreshes its local copy from what the server actually
  stored instead of from what it asked for.
"""

from __future__ import annotations

from rest_framework import serializers

#: The two decisions a recipient can make. Kept as a module constant so the
#: serializer and any future caller agree on the vocabulary.
PIN_SHARE_ACTIONS = ("accept", "reject")


class PinShareRespondSerializer(serializers.Serializer):
    """Validates a recipient's accept/reject decision on a pending share."""

    action = serializers.ChoiceField(choices=PIN_SHARE_ACTIONS)


class PinShareRespondResultSerializer(serializers.Serializer):
    """The outcome of responding to a share (schema-only)."""

    #: The share's status after the decision - ``"accepted"`` or ``"rejected"``.
    status = serializers.CharField(read_only=True)
    #: The recipient-side pin the acceptance produced (or the pin they already
    #: had at that place, which acceptance reuses rather than duplicating).
    #: Null on reject, so a client can navigate straight to the pin on accept
    #: without a second round-trip to find it.
    pin_slug = serializers.CharField(read_only=True, allow_null=True)
    #: Human-readable summary suitable for a toast, identical to the message the
    #: web UI shows for the same decision - including the child-pin count when
    #: the share carried a bundle.
    detail = serializers.CharField(read_only=True)
