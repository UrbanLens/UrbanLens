"""External-API serializers for the social domain's newer surfaces.

Covers the avatar write path and the private profile annotations (nickname and
trust rating). The friendship shapes - ``FriendshipSerializer`` and friends -
predate the per-domain split and stay in ``serializers.py``; the unblock
endpoint reuses ``FriendshipSerializer`` rather than defining a near-identical
twin, so a client parses one relationship shape everywhere.

Two conventions worth knowing before adding to this module:

- **Write serializers validate; they do not decide.** The annotation bounds
  restated here (nickname length, trust range) mirror
  ``services.profile_annotations``' own constants by importing them, so the two
  cannot drift into disagreeing about what is acceptable. The service still
  re-checks, because the HTMX surface reaches it without passing through here.
- **Nothing in this module is a read model for someone else's data.** The
  annotation payload describes what the *caller* recorded about a subject. If a
  field is ever added here that the subject could also see, it belongs in
  ``ProfileDetailSerializer`` instead - see the privacy note on
  ``services.profile_annotations``.
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.colors import MaterialColor
from urbanlens.dashboard.services.avatar import AvatarService
from urbanlens.dashboard.services.profile_annotations import (
    MAX_PROFILE_NICKNAME_LENGTH,
    MAX_TRUST_RATING,
    MIN_TRUST_RATING,
)


class AvatarEmojiSerializer(serializers.Serializer):
    """Validates a generated-emoji avatar request.

    Both fields are constrained to closed sets. ``animal`` selects an emoji from
    a fixed table, and ``color`` is interpolated straight into the generated SVG
    - an arbitrary string there would be markup injection into a file the site
    subsequently serves to every viewer of that profile.

    Unlike the site's own picker, which silently substitutes a default for an
    unrecognized value, this refuses: an API client that sent ``"purpel"``
    should be told, not handed a grey fox and left to wonder.
    """

    animal = serializers.ChoiceField(choices=sorted(AvatarService.ANIMAL_EMOJIS))
    color = serializers.CharField()

    def validate_color(self, value: str) -> str:
        """Normalize a palette colour to its canonical hex form.

        Accepts any case (``#4caf50`` and ``#4CAF50`` both work) because hex
        colours are routinely lowercased by client-side pickers, and returns
        the enum's own spelling so the stored SVG is byte-identical whichever
        form arrived.

        Args:
            value: The submitted colour string.

        Returns:
            The matching ``MaterialColor`` value.

        Raises:
            serializers.ValidationError: The colour is not in the palette.
        """
        by_lowercase = {choice.lower(): choice for choice in MaterialColor.values}
        canonical = by_lowercase.get((value or "").strip().lower())
        if canonical is None:
            raise serializers.ValidationError("Choose one of the site's avatar palette colours.")
        return canonical


class ProfileAnnotationsSerializer(serializers.Serializer):
    """Everything the caller privately records about one other profile (read-only).

    Every field describes the *caller's* own rows. The subject of an annotation
    can never read it - asking for your own annotations returns nulls, not the
    opinions other people hold about you.

    ``note_count`` rather than the notes themselves: notes are an unbounded
    collection with their own endpoint (``/profiles/{slug}/notes/``), and this
    payload is fetched every time a profile screen opens.
    """

    #: Null when the caller assigned no nickname.
    nickname = serializers.CharField(read_only=True, allow_null=True)
    #: Null when the caller gave no trust rating; otherwise 1-5.
    trust = serializers.IntegerField(read_only=True, allow_null=True)
    note_count = serializers.IntegerField(read_only=True)


class ProfileNicknameWriteSerializer(serializers.Serializer):
    """Validates a nickname assignment.

    ``allow_blank`` is off deliberately. Blank is how the site's HTMX widget
    signals "clear this", but an API with a real ``DELETE`` on the same route
    does not need a second, ambiguous way to say it - and accepting blank here
    would make ``PUT`` sometimes a delete, which no client would expect.
    """

    nickname = serializers.CharField(max_length=MAX_PROFILE_NICKNAME_LENGTH)


class ProfileTrustWriteSerializer(serializers.Serializer):
    """Validates a trust rating.

    Bounded to the same range the model's validators enforce. The bound is
    checked here as well as in the service because ``update_or_create`` does not
    run field validators - without one of the two, an out-of-range rating would
    be stored and only surface as a rendering oddity much later.
    """

    rating = serializers.IntegerField(min_value=MIN_TRUST_RATING, max_value=MAX_TRUST_RATING)
