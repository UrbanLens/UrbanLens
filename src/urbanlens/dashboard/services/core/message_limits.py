"""One sender's message budget, charged wherever a message is actually created.

The chat sockets were bounded first, and a socket limit on its own is theatre:
every write those sockets perform is also reachable over plain HTTP.
``ConversationSendView``, ``GroupSendView`` and ``SafetyCheckinMessageView`` are
plain Django ``View``s calling the identical create functions, and DRF's
throttle classes do not cover a plain ``View`` - so a POST loop routed straight
around the socket's budget (P31).

The budget therefore lives at the service layer, which is the one place both
doors meet, and the identity is built there too rather than by each caller. A
caller that had to construct its own key is a caller that can get it wrong, and
"the socket and the view spell the same sender differently" is a bypass that
looks like a working limit from either side.

Distinct from ``frame_limits``, which bounds *frames* on a socket - a keep-alive
and a typing indicator are frames and are not messages. The two budgets are
separate on purpose: a burst of keep-alives should not consume somebody's
allowance for saying something.

Fails open, for the reason ``frame_limits`` documents: a cache blip must not
silence chat.
"""

from __future__ import annotations

from django.conf import settings

from urbanlens.dashboard.services.core.frame_limits import FrameBudget


class MessageRateLimitedError(ValueError):
    """This sender's message budget for the current window is spent.

    A ``ValueError`` so the WebSocket consumers, which already answer
    ``ValueError`` with an error frame carrying its text, report it without
    knowing this class exists. HTTP callers catch it by name and answer 429.

    The message is for logs, not the response: a catch site authors its own
    user-facing text rather than relaying it, the same convention
    :class:`~urbanlens.dashboard.services.pins.pin_creation.PinCreationError`
    uses. There is only ever one raise site, so no subclass is needed.
    """


def _budget() -> FrameBudget:
    """The configured budget, read at call time so ``override_settings`` works."""
    return FrameBudget(name="message", limit=int(getattr(settings, "UL_MESSAGES_PER_MINUTE", 0) or 0))


def charge_message(identity: str) -> None:
    """Charge one message send against *identity*.

    Args:
        identity: Who to charge, from :func:`sender_identity` or one of the
            per-feature builders below.

    Raises:
        MessageRateLimitedError: The budget for this window is spent.
    """
    if not _budget().consume(identity):
        raise MessageRateLimitedError(f"message budget spent for identity={identity!r}")


def refund_message(identity: str) -> None:
    """Give back a charge for a message that turned out not to exist.

    The idempotency guard in ``create_direct_message``/``create_group_message``
    reads before it writes, so two requests carrying the same ``client_uuid``
    can both miss it, both charge, and then have one of them lose the unique
    constraint and return the row the other created. One message, two charges.

    Args:
        identity: The key that was charged.
    """
    _budget().refund(identity)


def sender_identity(sender_pk: int) -> str:
    """The budget key for one person's outbound direct and group messages.

    Deliberately one key for both. A budget per conversation would let a sender
    multiply their allowance by opening more conversations, which is the shape
    the limit exists to stop.

    Args:
        sender_pk: The sending profile's pk.

    Returns:
        The budget key.
    """
    return f"dm:{sender_pk}"


def safety_chat_identity(checkin_pk: int, *, profile_pk: int | None, contact_pk: int | None) -> str:
    """The budget key for one participant of one safety check-in.

    Scoped to the check-in rather than to the person: a check-in is an emergency
    surface, and someone handling two at once must not have one throttle the
    other.

    The contact route has no profile - its authority is a magic-link token - so
    an emergency contact is keyed by their own row instead. ``profile_pk`` is
    preferred when both are present, so a contact who is also the owner is
    charged once rather than through two keys.

    Args:
        checkin_pk: The check-in's pk.
        profile_pk: The sending profile's pk, or None on the contact route.
        contact_pk: The authorizing contact's pk, or None on the owner route.

    Returns:
        The budget key.
    """
    who = str(profile_pk) if profile_pk is not None else f"c{contact_pk}"
    return f"safety:{checkin_pk}:{who}"


def session_chat_identity(session_label: str, session_pk: int, profile_pk: int) -> str:
    """The budget key for one participant of one game session.

    Scoped to the session for the same reason as a check-in: playing two games
    at once is legitimate, and one game's chat must not throttle the other's.

    Args:
        session_label: Distinguishes the games, e.g. ``dashboard.triviasession``.
        session_pk: The session's pk.
        profile_pk: The sending profile's pk.

    Returns:
        The budget key.
    """
    return f"session:{session_label}:{session_pk}:{profile_pk}"
