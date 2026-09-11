"""Private annotations one profile keeps about another: nickname and trust rating.
Extracted from ``controllers.userprofile``'s ``ProfileNicknameView`` and ``ProfileTrustView``, which held the only implementation and returned rendered HTML - unusable from an API credential."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.profile.nickname import ProfileNickname
from urbanlens.dashboard.models.profile.note import ProfileNote
from urbanlens.dashboard.models.profile.trust import ProfileTrust

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: Matches ``ProfileNickname.nickname``'s column width. Enforced here as well as
#: on the field so an over-long value is refused before it reaches a database
#: that would silently truncate it on some backends.
MAX_PROFILE_NICKNAME_LENGTH = 100

#: Inclusive bounds on ``ProfileTrust.rating``, matching the model validators.
MIN_TRUST_RATING = 1
MAX_TRUST_RATING = 5


class AnnotationError(ValueError):
    """An annotation could not be written.
    The message is for logs, not the response: a caller's HTTP-facing code should catch a specific subclass below (or this base class as a fallback) and author its own user-facing text, rather than relaying the message - that keeps a future raise site here from being able to smuggle unreviewed text into a response just by adding a new ``raise``."""


class SelfAnnotationError(AnnotationError):
    """The author and the subject are the same profile.

    Kept as its own class because it is the one refusal that is about *who* is
    being annotated rather than about the value submitted.
    """


class NicknameEmptyError(AnnotationError):
    """The submitted nickname was empty (after stripping whitespace)."""


class NicknameTooLongError(AnnotationError):
    """The submitted nickname exceeds :data:`MAX_PROFILE_NICKNAME_LENGTH`."""


class TrustRatingOutOfRangeError(AnnotationError):
    """The submitted rating fell outside :data:`MIN_TRUST_RATING`-:data:`MAX_TRUST_RATING`."""


@dataclass(frozen=True, slots=True)
class ProfileAnnotations:
    """Everything one viewer privately records about one subject.
    A read model rather than a row: ``nickname`` and ``trust`` come from two different tables and ``note_count`` from a third, and a client rendering a profile header wants all three without three round trips."""

    #: The private nickname the viewer assigned, or None when they assigned none.
    nickname: str | None
    #: The viewer's private 1-5 trust rating, or None when unrated.
    trust: int | None
    #: How many private notes the viewer holds about this subject.
    note_count: int


def require_distinct(author: Profile, subject: Profile, message: str) -> None:
    """Refuse an annotation a profile is trying to write about itself.
    Public because the HTMX widgets treat a blank submission as "clear this" rather than as a value, and must still refuse a self-annotation on that path - so they need the check without going through :func:`set_nickname`.

    Args:
        author: The profile writing the annotation.
        subject: The profile being annotated.
        message: Log-only context for this particular annotation kind - never
            shown to a user.

    Raises:
        SelfAnnotationError: ``author`` and ``subject`` are the same profile."""
    if author.pk == subject.pk:
        raise SelfAnnotationError(message)


def get_annotations(author: Profile, subject: Profile) -> ProfileAnnotations:
    """Return everything ``author`` privately records about ``subject``.
    Never raises for a self-lookup: a profile reading its own annotations gets the (normally empty) rows it wrote about itself, which is both harmless and simpler for a client than a special case.

    Args:
        author: The viewing profile - always the caller, never the subject.
        subject: The profile being looked up.

    Returns:
        The nickname, trust rating and note count, with None for anything unset."""
    nickname = ProfileNickname.objects.for_pair(author, subject).first()
    trust = ProfileTrust.objects.for_pair(author, subject).first()
    return ProfileAnnotations(
        nickname=nickname.nickname if nickname else None,
        trust=trust.rating if trust else None,
        note_count=ProfileNote.objects.for_pair(author, subject).count(),
    )


def set_nickname(author: Profile, subject: Profile, nickname: str) -> ProfileNickname:
    """Set or replace ``author``'s private nickname for ``subject``.

    Args:
        author: The profile assigning the nickname.
        subject: The profile being nicknamed.
        nickname: The nickname text.

    Returns:
        The stored nickname row.

    Raises:
        SelfAnnotationError: A profile cannot nickname itself.
        NicknameEmptyError: ``nickname`` was blank (after stripping).
        NicknameTooLongError: ``nickname`` exceeds
            :data:`MAX_PROFILE_NICKNAME_LENGTH`.
    """
    require_distinct(author, subject, "self-nickname attempt")

    nickname = (nickname or "").strip()
    if not nickname:
        raise NicknameEmptyError("nickname was blank after stripping whitespace")
    if len(nickname) > MAX_PROFILE_NICKNAME_LENGTH:
        raise NicknameTooLongError(f"nickname length {len(nickname)} exceeds MAX_PROFILE_NICKNAME_LENGTH={MAX_PROFILE_NICKNAME_LENGTH}")

    row, _created = ProfileNickname.objects.update_or_create(
        author=author,
        subject=subject,
        defaults={"nickname": nickname},
    )
    return row


def clear_nickname(author: Profile, subject: Profile) -> None:
    """Remove ``author``'s nickname for ``subject``, if any. Idempotent, so a retried DELETE is safe.

    Args:
        author: The profile whose nickname is being cleared.
        subject: The profile it was about."""
    ProfileNickname.objects.for_pair(author, subject).delete()


def set_trust(author: Profile, subject: Profile, rating: int) -> ProfileTrust:
    """Set or replace ``author``'s private trust rating for ``subject``.

    Args:
        author: The profile giving the rating.
        subject: The profile being rated.
        rating: A value from :data:`MIN_TRUST_RATING` to
            :data:`MAX_TRUST_RATING`.

    Returns:
        The stored trust row.

    Raises:
        SelfAnnotationError: A profile cannot rate itself.
        TrustRatingOutOfRangeError: The rating is outside the permitted
            range. Checked here rather than left to the field validators,
            because ``update_or_create`` does not run them - an out-of-range
            value would otherwise be written and only fail later, if ever.
    """
    require_distinct(author, subject, "self-rating attempt")

    if not MIN_TRUST_RATING <= rating <= MAX_TRUST_RATING:
        raise TrustRatingOutOfRangeError(f"rating {rating} outside allowed range [{MIN_TRUST_RATING}, {MAX_TRUST_RATING}]")

    row, _created = ProfileTrust.objects.update_or_create(
        author=author,
        subject=subject,
        defaults={"rating": rating},
    )
    return row


def clear_trust(author: Profile, subject: Profile) -> None:
    """Remove ``author``'s trust rating for ``subject``, if any.

    Args:
        author: The profile whose rating is being cleared.
        subject: The profile it was about."""
    ProfileTrust.objects.for_pair(author, subject).delete()
