"""Per-mode strategy registry - the single place a new SpotGuessr mode is wired in.

Before this module existed, adding a mode meant editing an if/elif chain in
three separate places that all had to agree on the same mode list: round
generation (``services.spotguessr.session.get_or_create_round``), round
serialization (``services.spotguessr.serializers.serialize_round``), and the
photo-feedback "does this round even have visual content" gate
(``services.spotguessr.relevance``). Adding a fourth mode now means adding one
``ModeStrategy`` entry to ``_STRATEGIES`` below - everything else reads the
registry instead of repeating its own copy of the mode list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.spotguessr.model import SpotGuessrMode

if TYPE_CHECKING:
    from collections.abc import Callable

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.spotguessr.model import GameRound
    from urbanlens.dashboard.services.spotguessr.scoring import RoundTarget
    from urbanlens.dashboard.services.spotguessr.session import GameConfig


@dataclass(frozen=True)
class RoundContent:
    """What ``ModeStrategy.build_round`` resolves for one candidate location.

    Handed straight to ``GameRound.objects.create()`` by the caller - see
    ``services.spotguessr.session.get_or_create_round``.
    """

    target: RoundTarget
    image: Image | None = None
    display_text: str | None = None


@dataclass(frozen=True)
class ModeStrategy:
    """Everything mode-specific about generating, serializing, and reacting to a round.

    Attributes:
        mode: The ``SpotGuessrMode`` value this strategy implements.
        shows_imagery: Whether a round in this mode shows the player real
            photographic content worth reacting to (thumbs up/down/report) -
            Photos and Street View do, Named Place (text-only) doesn't. Read
            by ``services.spotguessr.relevance`` and serialized onto every
            round payload so the frontend never has to hardcode its own copy
            of "which modes show a photo".
        build_round: Given an eligible ``Location``, the session's
            ``GameConfig``, and its joined participants, returns the
            mode-specific ``RoundContent`` for that location, or ``None`` if
            this location has nothing usable for this mode yet (e.g. no
            photo, no name/alias, no Street View coverage) - the caller
            should exclude it and try another candidate rather than treat
            that as a hard error. Most strategies ignore the participant
            list; Photos mode uses it to tell a single-participant session
            apart from a multiplayer one (see ``_build_photos``).
        serialize_round: Mutates a round's outbound JSON payload in place,
            adding whatever fields this mode's ``RoundPayload`` needs
            (``image_url``, ``display_text``, ``street_view_image``, ...).
            Never includes the answer - that's the same rule every mode's
            round payload follows regardless of strategy.
    """

    mode: str
    shows_imagery: bool
    build_round: Callable[[Location, GameConfig, list[Profile]], RoundContent | None]
    serialize_round: Callable[[GameRound, dict[str, Any]], None]


def _build_photos(location: Location, config: GameConfig, participants: list[Profile]) -> RoundContent | None:
    from urbanlens.dashboard.services.spotguessr import photos, scoring

    # A solo player may see their own pin photos for this location (never a
    # wiki-only restriction for them - see photos.candidate_image_for_location's
    # solo_profile arg); anything with 2+ joined participants stays wiki-only.
    solo_profile = participants[0] if len(participants) == 1 else None
    image = photos.candidate_image_for_location(
        location,
        allow_arbitrary_external_photos=config.allow_arbitrary_external_photos,
        solo_profile=solo_profile,
    )
    if image is None:
        return None
    return RoundContent(target=scoring.resolve_target(location, image), image=image)


def _serialize_photos(round_: GameRound, data: dict[str, Any]) -> None:
    # Only the image itself. The photo's caption is deliberately NOT included:
    # it is EXIF/IPTC-derived and routinely reads "Old Mill House, Troy NY",
    # i.e. it names the answer outright. The web client never rendered it, so
    # the leak was invisible from the UI while sitting in the JSON the whole
    # time - and a scriptable JSON API turns that into a lookup table for the
    # whole game. It now rides on the *reveal* payloads instead (see
    # ``services.spotguessr.serializers.serialize_reveal``), where the answer
    # is already public.
    if round_.image_id and round_.image is not None and round_.image.image:
        data["image_url"] = round_.image.image.url


def _build_named_place(location: Location, config: GameConfig, participants: list[Profile]) -> RoundContent | None:
    from urbanlens.dashboard.services.spotguessr import named_place, scoring

    display_text = named_place.candidate_name_for_location(location, use_aliases=config.use_aliases)
    if display_text is None:
        return None
    return RoundContent(target=scoring.resolve_target(location, None), display_text=display_text)


def _serialize_named_place(round_: GameRound, data: dict[str, Any]) -> None:
    data["display_text"] = round_.display_text


def _build_street_view(location: Location, config: GameConfig, participants: list[Profile]) -> RoundContent | None:
    from urbanlens.dashboard.services.spotguessr import scoring, street_view

    if street_view.candidate_street_view_for_location(location) is None:
        return None
    return RoundContent(target=scoring.street_view_target(location))


def _serialize_street_view(round_: GameRound, data: dict[str, Any]) -> None:
    from urbanlens.dashboard.services.spotguessr import street_view

    data["street_view_image"] = street_view.candidate_street_view_for_location(round_.location)


_STRATEGIES: dict[str, ModeStrategy] = {
    SpotGuessrMode.PHOTOS: ModeStrategy(mode=SpotGuessrMode.PHOTOS, shows_imagery=True, build_round=_build_photos, serialize_round=_serialize_photos),
    SpotGuessrMode.NAMED_PLACE: ModeStrategy(
        mode=SpotGuessrMode.NAMED_PLACE,
        shows_imagery=False,
        build_round=_build_named_place,
        serialize_round=_serialize_named_place,
    ),
    SpotGuessrMode.STREET_VIEW: ModeStrategy(
        mode=SpotGuessrMode.STREET_VIEW,
        shows_imagery=True,
        build_round=_build_street_view,
        serialize_round=_serialize_street_view,
    ),
}


def get_strategy(mode: str) -> ModeStrategy | None:
    """The registered strategy for ``mode``, or None if it has no round-generation logic."""
    return _STRATEGIES.get(mode)


def shows_imagery(mode: str) -> bool:
    """Whether rounds in ``mode`` show real photographic content (vs. text-only)."""
    strategy = get_strategy(mode)
    return strategy is not None and strategy.shows_imagery
