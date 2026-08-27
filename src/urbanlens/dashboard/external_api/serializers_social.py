"""External-API serializers for the social domain's newer surfaces.

Covers the avatar write path and the private profile annotations (nickname and
trust rating). The friendship shapes - ``FriendshipSerializer`` and friends -
predate the per-domain split and stay in ``serializers.py``; the unblock
endpoint reuses ``FriendshipSerializer`` rather than defining a near-identical
twin, so a client parses one relationship shape everywhere.

Two conventions worth knowing before adding to this module:

- **Write serializers validate; they do not decide.** The annotation bounds
  restated here (nickname length, trust range) mirror
  ``services.profile.profile_annotations``' own constants by importing them, so the two
  cannot drift into disagreeing about what is acceptable. The service still
  re-checks, because the HTMX surface reaches it without passing through here.
- **Nothing in this module is a read model for someone else's data.** The
  annotation payload describes what the *caller* recorded about a subject. If a
  field is ever added here that the subject could also see, it belongs in
  ``ProfileDetailSerializer`` instead - see the privacy note on
  ``services.profile.profile_annotations``.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from rest_framework import serializers

from urbanlens.dashboard.models.colors import MaterialColor
from urbanlens.dashboard.services.profile.avatar import AvatarService
from urbanlens.dashboard.services.profile.profile_annotations import (
    MAX_PROFILE_NICKNAME_LENGTH,
    MAX_TRUST_RATING,
    MIN_TRUST_RATING,
)
from urbanlens.dashboard.services.profile.social_links import KNOWN_PLATFORMS, validate_handle

#: Mirrors ``forms.profile_form._DISCORD_HANDLE_RE`` / ``services.profile.profile_settings._DISCORD_USERNAME_RE`` -
#: this is the public-social-link Discord entry (``SocialLink(platform="discord")``), a distinct
#: row from the private ``Profile.discord_username`` contact field, but the same charset rule.
_DISCORD_HANDLE_RE = re.compile(r"^[a-zA-Z0-9._#-]{2,100}$")


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


class SocialLinkSerializer(serializers.Serializer):
    """One entry in a profile's public social-links list, as rendered for display.

    Shape matches ``services.profile.social_links.get_profile_links`` field for field -
    ``url`` is null only for Discord, which has no public profile URL.
    """

    platform = serializers.CharField(read_only=True)
    handle = serializers.CharField(read_only=True)
    url = serializers.CharField(read_only=True, allow_null=True)
    display_name = serializers.CharField(read_only=True)
    icon = serializers.CharField(read_only=True)


class SocialLinksResponseSerializer(serializers.Serializer):
    """The full set of a profile's social links (schema-only)."""

    links = SocialLinkSerializer(many=True, read_only=True)


class SocialLinkWriteSerializer(serializers.Serializer):
    """One social-link entry inside a full-replace submission.

    ``handle`` means different things per platform: a username for the eight
    handle-based platforms - validated by the same
    ``services.profile.social_links.validate_handle`` rules a pasted URL is checked
    against internally, so a directly-submitted handle can never be looser
    than one the web UI would have extracted - Discord's own free-form
    username (it has no public profile URL to parse one from), or the
    complete URL itself for ``website``.
    """

    platform = serializers.ChoiceField(choices=sorted(KNOWN_PLATFORMS))
    handle = serializers.CharField(max_length=500)

    def validate(self, attrs: dict) -> dict:
        """Apply the platform-specific handle/URL rule, normalizing where one exists.

        Args:
            attrs: The already field-validated submission.

        Returns:
            *attrs*, with ``handle`` normalized (stripped of a leading ``@``
            for username platforms; stripped of its fragment for ``website``).

        Raises:
            serializers.ValidationError: The handle is blank, or fails its
                platform's rule.
        """
        platform = attrs["platform"]
        raw = attrs["handle"].strip()
        handle = raw if platform == "website" else raw.lstrip("@")
        if not handle:
            raise serializers.ValidationError({"handle": "This field may not be blank."})

        if platform == "website":
            # Mirrors parse_social_link's own two-step check. The raw scheme
            # must be inspected *before* defaulting a scheme-less submission
            # ("example.com/me") to https: prepending blindly would turn
            # "javascript:alert(1)" into "https://javascript:alert(1)", whose
            # netloc parses to a deceptively harmless-looking hostname
            # ("javascript") - the raw scheme has to be rejected first.
            raw_scheme = urlparse(handle).scheme
            if raw_scheme not in {"", "http", "https"} and ("://" in handle or raw_scheme in {"javascript", "vbscript", "data", "ftp", "file", "mailto"}):
                raise serializers.ValidationError({"handle": "Must be a valid http:// or https:// URL."})
            url_str = handle if "://" in handle else f"https://{handle}"
            parsed = urlparse(url_str)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise serializers.ValidationError({"handle": "Must be a valid http:// or https:// URL."})
            handle = parsed._replace(fragment="").geturl()
            if len(handle) > 500:
                raise serializers.ValidationError({"handle": "URL must be 500 characters or fewer."})
        elif platform == "discord":
            if not _DISCORD_HANDLE_RE.match(handle):
                raise serializers.ValidationError({"handle": "2-100 characters: letters, digits, underscores, dots, hyphens, or #."})
        else:
            error = validate_handle(platform, handle)
            if error:
                raise serializers.ValidationError({"handle": error})

        attrs["handle"] = handle
        return attrs


class SocialLinksReplaceSerializer(serializers.Serializer):
    """Validates a full-replace PUT of the caller's social links.

    PUT rather than PATCH, matching ``SafetyContactDefaultsSerializer``'s own
    precedent: the underlying write deletes and recreates the whole set, and
    there is no per-entry addressing (a platform key inside the body isn't
    one) to PATCH against. Submitting an empty ``links`` list clears every
    platform.
    """

    links = SocialLinkWriteSerializer(many=True)

    def validate_links(self, value: list[dict]) -> list[dict]:
        """Reject a submission naming the same platform twice.

        Args:
            value: The per-entry validated list.

        Returns:
            *value*, unchanged.

        Raises:
            serializers.ValidationError: A platform appears more than once,
                which would make the outcome depend on list order.
        """
        platforms = [link["platform"] for link in value]
        if len(platforms) != len(set(platforms)):
            raise serializers.ValidationError("Each platform may appear at most once.")
        return value
