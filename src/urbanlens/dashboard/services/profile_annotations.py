"""Private annotations one profile keeps about another: nickname and trust rating.

Extracted from ``controllers.userprofile``'s ``ProfileNicknameView`` and
``ProfileTrustView``, which held the only implementation and returned rendered
HTML - unusable from an API credential. The mobile requirements doc asserted
that "there's no nickname/trust concept anywhere server-side"; both models have
existed and been migrated for some time, so exposing them is plumbing, not a
product decision.

**These rows are private to their author.** ``ProfileNickname`` and
``ProfileTrust`` record what *you* think of someone; the person you wrote them
about must never be able to read them, and neither must anyone else. That is
enforced by never touching the models except through their
``for_pair(author, subject)`` accessors, which pin the author to the viewer.
Any queryset here that filtered on ``subject`` alone would hand the subject
everyone's private opinion of them in one request, so every function in this
module takes ``author`` first and passes it straight through.

Nickname and trust are kept as two singletons rather than merged with notes
into one annotation blob. Their cardinalities differ - a viewer holds at most
one nickname and at most one rating per subject, but any number of notes - so a
combined partial update could not be idempotent: replaying it would either
duplicate notes or silently drop the ones the client did not echo back.
"""

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

    The message is written for the end user and is safe to surface directly.
    Callers map this to HTTP 400.
    """


class SelfAnnotationError(AnnotationError):
    """The author and the subject are the same profile.

    Kept as its own class because it is the one refusal that is about *who* is
    being annotated rather than about the value submitted, and some callers
    word it differently ("Cannot rate your own profile.").
    """


@dataclass(frozen=True, slots=True)
class ProfileAnnotations:
    """Everything one viewer privately records about one subject.

    A read model rather than a row: ``nickname`` and ``trust`` come from two
    different tables and ``note_count`` from a third, and a client rendering a
    profile header wants all three without three round trips.

    ``note_count`` rather than the notes themselves - the notes have their own
    paginated endpoint, and inlining an unbounded collection into a summary
    that is fetched on every profile open is how a summary becomes the slowest
    call in the app.
    """

    #: The private nickname the viewer assigned, or None when they assigned none.
    nickname: str | None
    #: The viewer's private 1-5 trust rating, or None when unrated.
    trust: int | None
    #: How many private notes the viewer holds about this subject.
    note_count: int


def require_distinct(author: Profile, subject: Profile, message: str) -> None:
    """Refuse an annotation a profile is trying to write about itself.

    Public because the HTMX widgets treat a blank submission as "clear this"
    rather than as a value, and must still refuse a self-annotation on that
    path - so they need the check without going through :func:`set_nickname`.

    Args:
        author: The profile writing the annotation.
        subject: The profile being annotated.
        message: The end-user wording for this particular annotation kind.

    Raises:
        SelfAnnotationError: ``author`` and ``subject`` are the same profile.
    """
    if author.pk == subject.pk:
        raise SelfAnnotationError(message)


def get_annotations(author: Profile, subject: Profile) -> ProfileAnnotations:
    """Return everything ``author`` privately records about ``subject``.

    Never raises for a self-lookup: a profile reading its own annotations gets
    the (normally empty) rows it wrote about itself, which is both harmless and
    simpler for a client than a special case.

    Args:
        author: The viewing profile - always the caller, never the subject.
        subject: The profile being looked up.

    Returns:
        The nickname, trust rating and note count, with None for anything unset.
    """
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
        AnnotationError: The nickname is blank or over
            :data:`MAX_PROFILE_NICKNAME_LENGTH`.
    """
    require_distinct(author, subject, "Cannot nickname your own profile.")

    nickname = (nickname or "").strip()
    if not nickname:
        raise AnnotationError("Nickname cannot be empty.")
    if len(nickname) > MAX_PROFILE_NICKNAME_LENGTH:
        raise AnnotationError(f"Nickname cannot be longer than {MAX_PROFILE_NICKNAME_LENGTH} characters.")

    row, _created = ProfileNickname.objects.update_or_create(
        author=author,
        subject=subject,
        defaults={"nickname": nickname},
    )
    return row


def clear_nickname(author: Profile, subject: Profile) -> None:
    """Remove ``author``'s nickname for ``subject``, if any.

    Idempotent, so a retried DELETE is safe. Scoped through ``for_pair`` rather
    than by row id, which is what keeps one author from deleting another's.

    Args:
        author: The profile whose nickname is being cleared.
        subject: The profile it was about.
    """
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
        AnnotationError: The rating is outside the permitted range. Checked
            here rather than left to the field validators, because
            ``update_or_create`` does not run them - an out-of-range value
            would otherwise be written and only fail later, if ever.
    """
    require_distinct(author, subject, "Cannot rate your own profile.")

    if not MIN_TRUST_RATING <= rating <= MAX_TRUST_RATING:
        raise AnnotationError(f"Trust rating must be between {MIN_TRUST_RATING} and {MAX_TRUST_RATING}.")

    row, _created = ProfileTrust.objects.update_or_create(
        author=author,
        subject=subject,
        defaults={"rating": rating},
    )
    return row


def clear_trust(author: Profile, subject: Profile) -> None:
    """Remove ``author``'s trust rating for ``subject``, if any.

    Idempotent, for the same retry-safety reason as :func:`clear_nickname`.

    Args:
        author: The profile whose rating is being cleared.
        subject: The profile it was about.
    """
    ProfileTrust.objects.for_pair(author, subject).delete()
