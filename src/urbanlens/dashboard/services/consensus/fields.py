"""Per-field-kind strategy registry - the single place a new Consensus field kind is wired in.

Mirrors ``services.spotguessr.modes``'s ``ModeStrategy``/``_STRATEGIES``
registry: adding a 4th field kind later means adding one
``ConsensusFieldStrategy`` entry to ``_STRATEGIES`` below, rather than
editing round generation, answer application, and agreement-checking
separately and hoping they stay in sync.

Each strategy exposes both a "missing" side (for ordinary rounds) and a
"known" side (for trust-check rounds, see ``services.consensus.trust``) -
``WIKI_ALIAS`` and ``PHOTO_COORDINATES`` need the two sides to pick different
things (a wiki with too few aliases vs. one with an existing alias; an image
missing coordinates vs. one that already has them), so the interface always
keeps them separate rather than inferring one from the other.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.consensus.model import ConsensusFieldKind
from urbanlens.dashboard.services.locations.naming import is_meaningful_name, normalize_name_for_comparison
from urbanlens.dashboard.services.wiki.wiki_edits import save_edited_fields

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.consensus.model import ConsensusRound
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

#: How close two submitted photo-coordinate answers must be to count as
#: "agreeing" - see ``ConsensusFieldStrategy.agrees`` for ``PHOTO_COORDINATES``.
AGREEMENT_DISTANCE_METERS = 15.0

_EARTH_RADIUS_METERS = 6_371_000.0


def haversine_distance_meters(a: Point, b: Point) -> float:
    """Great-circle distance between two lon/lat points, in meters.

    Deliberately pure Python (no DB round-trip) - unlike
    ``services.spotguessr.distance.geodesic_distance_meters``, which needs a
    real ``Location`` row to anchor its PostGIS annotation, this is a plain
    point-to-point comparison with no natural "anchor" row to hang it off of
    (``ConsensusFieldStrategy.agrees`` only ever receives the two raw
    values). The haversine formula is accurate to well under a meter at the
    short (tens-of-meters) distances ``AGREEMENT_DISTANCE_METERS`` cares
    about, so the small approximation versus a true geodesic/ellipsoidal
    calculation is immaterial here.
    """
    from urbanlens.dashboard.services.geo.distance import haversine_meters

    return haversine_meters(a.y, a.x, b.y, b.x)


#: A wiki with fewer aliases than this is still worth suggesting more for -
#: aliases are additive, so "missing" here means "could use another," not
#: "has none at all."
ALIAS_SUGGEST_THRESHOLD = 2


@dataclass(frozen=True)
class RoundContent:
    """What a strategy's ``build_round``/``build_check_round`` resolves for one candidate wiki.

    Handed straight to ``ConsensusRound.objects.create()`` by the caller -
    see ``services.consensus.selection``.
    """

    target_image: Image | None = None


@dataclass(frozen=True)
class ConsensusFieldStrategy:
    """Everything field-kind-specific about generating, applying, and comparing a round's answer.

    Attributes:
        kind: The ``ConsensusFieldKind`` value this strategy implements.
        find_missing: Given a pool of candidate wikis, return those still
            missing this field/data - used to generate an ordinary round.
        find_known: Given a pool of candidate wikis, return those that
            already have a confirmed value for this field/data - used to
            generate a trust-check round (see ``services.consensus.trust``).
        build_round: Given a wiki from ``find_missing``'s output, returns
            the round content for it, or None if this particular wiki turns
            out to have nothing usable after all (caller should try another).
        build_check_round: Given a wiki from ``find_known``'s output,
            returns ``(round content, known value)``, or None if this wiki
            turns out to have nothing usable.
        apply_answer: Applies a submitted (and already-validated) answer
            value to the wiki (and/or its target image), saves it, and
            returns a ``WikiEdit.changes``-shaped diff dict for the caller to
            record - or None if this field kind isn't ``WikiEdit``-tracked
            (``PHOTO_COORDINATES``; a submitted-but-duplicate alias also
            returns None, since there's nothing new to record).
        normalize: Canonical form of a submitted value, for agreement/vote
            clustering and (for text kinds) storing on
            ``ConsensusAnswer.normalized_text``.
        agrees: Whether two submitted values count as the same answer.
    """

    kind: str
    find_missing: Callable[[Iterable[Wiki]], list[Wiki]]
    find_known: Callable[[Iterable[Wiki]], list[Wiki]]
    build_round: Callable[[Wiki], RoundContent | None]
    build_check_round: Callable[[Wiki], tuple[RoundContent, Any] | None]
    apply_answer: Callable[[Wiki, Any, Profile, ConsensusRound], dict | None]
    normalize: Callable[[Any], Any]
    agrees: Callable[[Any, Any], bool]


def _text_normalize(value: Any) -> str:
    return normalize_name_for_comparison(str(value)) if value else ""


def _text_agrees(a: Any, b: Any) -> bool:
    normalized_a = _text_normalize(a)
    return normalized_a != "" and normalized_a == _text_normalize(b)


def _choice_normalize(value: Any) -> str:
    return (value or "").strip().lower()


def _choice_agrees(a: Any, b: Any) -> bool:
    normalized_a = _choice_normalize(a)
    return normalized_a != "" and normalized_a == _choice_normalize(b)


def _wiki_field_strategy(
    kind: str,
    field_name: str,
    *,
    current_value: Callable[[Wiki], Any],
    confirmed_value: Callable[[Wiki], Any | None],
    set_value: Callable[[Wiki, Any], None],
    written_fields: tuple[str, ...] = (),
    normalize: Callable[[Any], Any] = _text_normalize,
    agrees: Callable[[Any, Any], bool] = _text_agrees,
) -> ConsensusFieldStrategy:
    """Shared factory for the four plain Wiki-attribute field kinds (name/description/indoor_outdoor/pin_type).

    Each of those four only differs in which attribute it reads/writes and
    what counts as "confirmed" - everything else (round shape, diff
    recording) is identical, so it's expressed once here rather than
    duplicated four times.

    Args:
        written_fields: Columns ``set_value`` actually assigns, when that is not
            just ``field_name``. The pin-type strategy also sets
            ``pin_type_is_user_provided``, and a scoped save that listed only the
            named field would silently drop it.
    """

    def find_missing(pool: Iterable[Wiki]) -> list[Wiki]:
        return [wiki for wiki in pool if confirmed_value(wiki) is None]

    def find_known(pool: Iterable[Wiki]) -> list[Wiki]:
        return [wiki for wiki in pool if confirmed_value(wiki) is not None]

    def build_round(wiki: Wiki) -> RoundContent | None:
        return RoundContent()

    def build_check_round(wiki: Wiki) -> tuple[RoundContent, Any] | None:
        value = confirmed_value(wiki)
        if value is None:
            return None
        return RoundContent(), value

    def apply_answer(wiki: Wiki, value: Any, profile: Profile, round_: ConsensusRound) -> dict | None:
        old_value = current_value(wiki)
        set_value(wiki, value)
        # Scoped, not a bare save: a Wiki is community-editable and has a dozen
        # writers, and this instance was loaded when the round was built. Writing
        # every column would revert whatever was committed while the round ran -
        # the same defect fixed in services.wiki.wiki_edits.
        save_edited_fields(wiki, written_fields or (field_name,))
        return {field_name: {"from": old_value, "to": current_value(wiki)}}

    return ConsensusFieldStrategy(
        kind=kind,
        find_missing=find_missing,
        find_known=find_known,
        build_round=build_round,
        build_check_round=build_check_round,
        apply_answer=apply_answer,
        normalize=normalize,
        agrees=agrees,
    )


def _set_name(wiki: Wiki, value: Any) -> None:
    wiki.name = str(value)


def _set_description(wiki: Wiki, value: Any) -> None:
    wiki.description = str(value)


def _set_indoor_outdoor(wiki: Wiki, value: Any) -> None:
    wiki.indoor_outdoor = value


def _set_pin_type(wiki: Wiki, value: Any) -> None:
    wiki.pin_type = value
    wiki.pin_type_is_user_provided = True


# Named rather than inline lambdas: a lambda's parameter type is inferred from
# the surrounding Callable[[Wiki], Any] annotation, which mypy fails to do
# reliably for these module-level _STRATEGIES entries (whole-project checks
# report "Cannot determine type of ..." even though `wiki: Wiki` here is
# unambiguous) - see the `_set_*` functions just above, which already use this
# same named-function style for the identical reason.
def _current_name(wiki: Wiki) -> str:
    return wiki.name


def _confirmed_name(wiki: Wiki) -> str | None:
    return wiki.name if is_meaningful_name(wiki.name) else None


def _current_description(wiki: Wiki) -> str | None:
    return wiki.description


def _confirmed_description(wiki: Wiki) -> str | None:
    return wiki.description or None


def _current_indoor_outdoor(wiki: Wiki) -> str | None:
    return wiki.indoor_outdoor


def _current_pin_type(wiki: Wiki) -> str:
    return wiki.pin_type


def _confirmed_pin_type(wiki: Wiki) -> str | None:
    return wiki.pin_type if wiki.pin_type_is_user_provided else None


def _valid_indoor_outdoor_choices() -> set[str]:
    from urbanlens.dashboard.models.abstract.choices import IndoorOutdoor

    return set(IndoorOutdoor.values)


def _valid_pin_type_choices() -> set[str]:
    from urbanlens.dashboard.models.pin.model import PinType

    return set(PinType.values)


# ----------------------------------------------------------------------
# WIKI_ALIAS - additive, never routed through agree/vote/tentative (a wiki
# can have several valid aliases at once, unlike the single-value kinds).
# ----------------------------------------------------------------------


def _alias_find_missing(pool: Iterable[Wiki]) -> list[Wiki]:
    # .all() throughout this module, never .count()/.exists()/.filter(): the pool is
    # prefetched by eligibility.eligible_wikis(), and only .all() reads that cache.
    return [wiki for wiki in pool if len(wiki.aliases.all()) < ALIAS_SUGGEST_THRESHOLD]


def _alias_find_known(pool: Iterable[Wiki]) -> list[Wiki]:
    return [wiki for wiki in pool if wiki.aliases.all()]


def _alias_build_round(wiki: Wiki) -> RoundContent | None:
    return RoundContent()


def _alias_build_check_round(wiki: Wiki) -> tuple[RoundContent, Any] | None:
    existing = wiki.aliases.first()
    if existing is None:
        return None
    return RoundContent(), existing.name


def _alias_apply_answer(wiki: Wiki, value: Any, profile: Profile, round_: ConsensusRound) -> dict | None:
    from urbanlens.dashboard.models.aliases.model import AliasSource, WikiAlias
    from urbanlens.dashboard.services.locations.naming import sanitize_name

    name = sanitize_name(str(value)) or ""
    if not name:
        return None
    alias, created = WikiAlias.objects.get_or_create(
        wiki=wiki,
        name__iexact=name,
        defaults={"name": name, "created_by": profile, "source": AliasSource.USER},
    )
    if not created:
        return None  # already had this alias - nothing new to record
    return {"alias_added": {"from": None, "to": alias.name}}


# ----------------------------------------------------------------------
# PHOTO_COORDINATES - the only kind whose round targets a specific Image,
# not the Wiki itself, and whose agreement is distance-based, not string-based.
# ----------------------------------------------------------------------


def _photo_find_missing(pool: Iterable[Wiki]) -> list[Wiki]:
    return [wiki for wiki in pool if any(image.latitude is None and image.longitude is None for image in wiki.images.all())]


def _photo_find_known(pool: Iterable[Wiki]) -> list[Wiki]:
    return [wiki for wiki in pool if any(image.latitude is not None and image.longitude is not None for image in wiki.images.all())]


def _photo_build_round(wiki: Wiki) -> RoundContent | None:
    image = wiki.images.filter(latitude__isnull=True, longitude__isnull=True).order_by("?").first()
    if image is None:
        return None
    return RoundContent(target_image=image)


def _photo_build_check_round(wiki: Wiki) -> tuple[RoundContent, Any] | None:
    image = wiki.images.filter(latitude__isnull=False, longitude__isnull=False).order_by("?").first()
    if image is None:
        return None
    # Guaranteed non-None by the filter() above - Image.latitude/longitude
    # are ordinary nullable fields a queryset filter can't narrow statically.
    if image.latitude is None or image.longitude is None:
        raise ValueError(f"Image {image.pk} matched the non-null coordinate filter but has a null latitude/longitude.")
    return RoundContent(target_image=image), {"latitude": float(image.latitude), "longitude": float(image.longitude)}


def _photo_apply_answer(wiki: Wiki, value: Point, profile: Profile, round_: ConsensusRound) -> dict | None:
    image = round_.target_image
    if image is None:
        return None
    image.latitude = value.y
    image.longitude = value.x
    image.save(update_fields=["latitude", "longitude", "updated"])
    return None  # coordinates were never WikiEdit-tracked (see Wiki's own docstring)


def _photo_normalize(value: Point) -> Point:
    return value


def _photo_agrees(a: Point | None, b: Point | None) -> bool:
    if a is None or b is None:
        return False
    return haversine_distance_meters(a, b) <= AGREEMENT_DISTANCE_METERS


_STRATEGIES: dict[str, ConsensusFieldStrategy] = {
    ConsensusFieldKind.WIKI_NAME: _wiki_field_strategy(
        ConsensusFieldKind.WIKI_NAME,
        "name",
        current_value=_current_name,
        confirmed_value=_confirmed_name,
        set_value=_set_name,
    ),
    ConsensusFieldKind.WIKI_DESCRIPTION: _wiki_field_strategy(
        ConsensusFieldKind.WIKI_DESCRIPTION,
        "description",
        current_value=_current_description,
        confirmed_value=_confirmed_description,
        set_value=_set_description,
    ),
    ConsensusFieldKind.WIKI_INDOOR_OUTDOOR: _wiki_field_strategy(
        ConsensusFieldKind.WIKI_INDOOR_OUTDOOR,
        "indoor_outdoor",
        current_value=_current_indoor_outdoor,
        confirmed_value=_current_indoor_outdoor,
        set_value=_set_indoor_outdoor,
        normalize=_choice_normalize,
        agrees=_choice_agrees,
    ),
    ConsensusFieldKind.WIKI_PIN_TYPE: _wiki_field_strategy(
        ConsensusFieldKind.WIKI_PIN_TYPE,
        "pin_type",
        current_value=_current_pin_type,
        confirmed_value=_confirmed_pin_type,
        set_value=_set_pin_type,
        written_fields=("pin_type", "pin_type_is_user_provided"),
        normalize=_choice_normalize,
        agrees=_choice_agrees,
    ),
    ConsensusFieldKind.WIKI_ALIAS: ConsensusFieldStrategy(
        kind=ConsensusFieldKind.WIKI_ALIAS,
        find_missing=_alias_find_missing,
        find_known=_alias_find_known,
        build_round=_alias_build_round,
        build_check_round=_alias_build_check_round,
        apply_answer=_alias_apply_answer,
        normalize=_text_normalize,
        agrees=_text_agrees,
    ),
    ConsensusFieldKind.PHOTO_COORDINATES: ConsensusFieldStrategy(
        kind=ConsensusFieldKind.PHOTO_COORDINATES,
        find_missing=_photo_find_missing,
        find_known=_photo_find_known,
        build_round=_photo_build_round,
        build_check_round=_photo_build_check_round,
        apply_answer=_photo_apply_answer,
        normalize=_photo_normalize,
        agrees=_photo_agrees,
    ),
}


def get_strategy(kind: str) -> ConsensusFieldStrategy | None:
    """The registered strategy for ``kind``, or None if it has no round-generation logic."""
    return _STRATEGIES.get(kind)


def all_kinds() -> list[str]:
    """Every registered field kind, in ``ConsensusFieldKind`` declaration order."""
    return [kind for kind, _label in ConsensusFieldKind.choices if kind in _STRATEGIES]


def validate_value(kind: str, value: Any) -> bool:
    """Whether ``value`` is a structurally acceptable submission for ``kind`` (before applying it).

    Only checks the closed-vocabulary kinds - free text (name/description/
    alias) and coordinates are validated by their own controller parsing,
    not here.
    """
    if kind == ConsensusFieldKind.WIKI_INDOOR_OUTDOOR:
        return value in _valid_indoor_outdoor_choices()
    if kind == ConsensusFieldKind.WIKI_PIN_TYPE:
        return value in _valid_pin_type_choices()
    return True
