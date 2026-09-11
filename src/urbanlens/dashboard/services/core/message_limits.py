"""One sender's message budget, charged wherever a message is actually created.
The budget therefore lives at the service layer, which is the one place both doors meet, and the identity is built there too rather than by each caller."""

from __future__ import annotations

from django.conf import settings

from urbanlens.dashboard.services.core.frame_limits import FrameBudget


class MessageRateLimitedError(ValueError):
    """This sender's message budget for the current window is spent.
    A ``ValueError`` so the WebSocket consumers, which already answer ``ValueError`` with an error frame carrying its text, report it without knowing this class exists."""


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
    The idempotency guard in ``create_direct_message``/``create_group_message`` reads before it writes, so two requests carrying the same ``client_uuid`` can both miss it, both charge, and then have one of them lose the unique constraint and return the row the other created.

    Args:
        identity: The key that was charged."""
    _budget().refund(identity)


def sender_identity(sender_pk: int) -> str:
    """The budget key for one person's outbound direct and group messages.

    Args:
        sender_pk: The sending profile's pk.

    Returns:
        The budget key."""
    return f"dm:{sender_pk}"


def safety_chat_identity(checkin_pk: int, *, profile_pk: int | None, contact_pk: int | None) -> str:
    """The budget key for one participant of one safety check-in.
    Scoped to the check-in rather than to the person: a check-in is an emergency surface, and someone handling two at once must not have one throttle the other.

    Args:
        checkin_pk: The check-in's pk.
        profile_pk: The sending profile's pk, or None on the contact route.
        contact_pk: The authorizing contact's pk, or None on the owner route.

    Returns:
        The budget key."""
    who = str(profile_pk) if profile_pk is not None else f"c{contact_pk}"
    return f"safety:{checkin_pk}:{who}"


def session_chat_identity(session_label: str, session_pk: int, profile_pk: int) -> str:
    """The budget key for one participant of one game session.
    Scoped to the session for the same reason as a check-in: playing two games at once is legitimate, and one game's chat must not throttle the other's.

    Args:
        session_label: Distinguishes the games, e.g. ``dashboard.triviasession``.
        session_pk: The session's pk.
        profile_pk: The sending profile's pk.

    Returns:
        The budget key."""
    return f"session:{session_label}:{session_pk}:{profile_pk}"
