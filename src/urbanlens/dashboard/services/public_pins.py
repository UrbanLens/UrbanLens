"""Public-pin eligibility engine and vote handling (UL-58).

A location becomes a public-pin candidate only when EVERY criterion in
:class:`PublicPinConfig` holds; the community then votes, and a passed vote
makes the location public - suggested to every account (opt-out). The rules
are deliberately never surfaced to users: the UI shows only the vote buttons
(when a place qualifies) and a plain-language FAQ entry.

Everything here is driven by ``evaluate_public_pin_candidates`` on a Celery
beat schedule. The only request-path entry points are ``public_vote_context``
(render the block) and ``cast_public_vote`` (record a ballot), both cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import math
import re
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q
from django.db.models.functions import Length
from django.utils import timezone

from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.public_pins.model import PublicPinCandidate, PublicPinCandidateStatus, PublicPinVote
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatField, WikiStatVote

if TYPE_CHECKING:
    from datetime import datetime

    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class PublicPinConfig:
    """Every tunable threshold for public-pin eligibility and voting.

    Attributes:
        region_radius_km: Only one public location per circle of this radius
            ("about the size of a city").
        min_vuln_votes: Vulnerability composite must draw from at least this
            many votes.
        max_vuln_avg: Vulnerability average must be strictly below this
            (1-5 scale; low = not vulnerable).
        min_aliases: Aliases required beyond the wiki name itself.
        min_photos: Photos required on the wiki/location.
        min_links: External links required on the wiki.
        min_article_chars: Minimum article length to count as "an article".
        min_markup_or_children: Markup elements plus community child markers
            required on the wiki map.
        top_n_per_state: Rank cutoff among eligible locations per US state
            (ties at the cutoff all qualify).
        pinner_share: Fraction of active users in the pinned-by floor formula.
        pinner_floor_min / pinner_floor_max: Clamp for that formula -
            ``max(min, min(max, ceil(share x active_users)))``.
        active_user_days: A user counts as active if they logged in within
            this many days.
        min_votes_to_pass: Ballots required before a vote can pass.
        min_open_days: Minimum total days a vote must have been open to pass.
        pass_consensus: Yes-share required to pass (inclusive).
        fail_min_votes: Ballots required before the hard-fail rule applies.
        fail_consensus: No-share that closes the vote for good (inclusive).
    """

    region_radius_km: float = 15.0
    min_vuln_votes: int = 3
    max_vuln_avg: float = 2.0
    min_aliases: int = 1
    min_photos: int = 2
    min_links: int = 2
    min_article_chars: int = 280
    min_markup_or_children: int = 1
    top_n_per_state: int = 10
    pinner_share: float = 0.20
    pinner_floor_min: int = 2
    pinner_floor_max: int = 10
    active_user_days: int = 180
    min_votes_to_pass: int = 2
    min_open_days: int = 7
    pass_consensus: float = 0.75
    fail_min_votes: int = 10
    fail_consensus: float = 0.75


CONFIG = PublicPinConfig()

#: Names that are only digits/punctuation/compass letters (bare coordinates)
#: or otherwise carry no meaning are not "meaningful names".
_COORDINATE_LIKE_RE = re.compile(r"^[\s\d\-+.,;:°'\"NSEW]*$", re.IGNORECASE)
_PLACEHOLDER_NAMES = frozenset({"untitled", "unknown", "unnamed", "new location", "new pin", "pin", "location"})


class PublicVoteError(Exception):
    """A ballot was refused (not eligible, not open, or bad input)."""


def is_meaningful_name(name: str | None) -> bool:
    """Whether a wiki name is a real place name rather than a placeholder.

    Args:
        name: The wiki's community name.

    Returns:
        True when the name is long enough, not coordinate-like, and not a
        known placeholder.
    """
    stripped = (name or "").strip()
    if len(stripped) < 4:
        return False
    if _COORDINATE_LIKE_RE.match(stripped):
        return False
    return stripped.casefold() not in _PLACEHOLDER_NAMES


def pinned_by_floor(active_user_count: int, config: PublicPinConfig = CONFIG) -> int:
    """Minimum distinct pinners required, scaled to community size.

    ``max(floor_min, min(floor_max, ceil(share x active_users)))`` - small
    communities need only a couple of pinners; large ones cap out so the bar
    stays reachable.
    """
    scaled = math.ceil(config.pinner_share * active_user_count)
    return max(config.pinner_floor_min, min(config.pinner_floor_max, scaled))


def _km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers (haversine)."""
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _active_user_count(now: datetime, config: PublicPinConfig) -> int:
    """Accounts that logged in within the active window."""
    user_model = get_user_model()
    return user_model.objects.filter(is_active=True, last_login__gte=now - timedelta(days=config.active_user_days)).count()


def _eligible_location_ids(now: datetime, config: PublicPinConfig) -> set[int]:
    """Compute the full set of currently-eligible location ids.

    One aggregate query per aspect (distinct counts, vulnerability composite,
    article length), combined in Python, then ranked per state. Runs on the
    beat schedule only - never in a request.
    """
    floor = pinned_by_floor(_active_user_count(now, config), config)

    public_coords = [
        (float(lat), float(lon))
        for lat, lon in PublicPinCandidate.objects.passed().values_list("location__latitude", "location__longitude")
    ]

    # Distinct counts are safe against join fan-out; averages are not, so the
    # vulnerability composite and article length come from separate queries.
    rows = (
        Location.objects.filter(wiki__isnull=False)
        .exclude(public_candidate__status__in=[PublicPinCandidateStatus.PASSED, PublicPinCandidateStatus.REJECTED])
        .annotate(
            pinners=Count("pins__profile", filter=Q(pins__parent_pin__isnull=True), distinct=True),
            alias_count=Count("wiki__aliases", distinct=True),
            link_count=Count("wiki__links", distinct=True),
            wiki_photo_count=Count("wiki__images", distinct=True),
            loc_photo_count=Count("images", distinct=True),
            markup_count=Count("wiki__markup_items", distinct=True),
            child_marker_count=Count("wiki__child_wikis", distinct=True),
        )
        .filter(
            pinners__gte=floor,
            alias_count__gte=config.min_aliases,
            link_count__gte=config.min_links,
        )
        .values(
            "id",
            "wiki__id",
            "wiki__name",
            "latitude",
            "longitude",
            "administrative_area_level_1",
            "pinners",
            "wiki_photo_count",
            "loc_photo_count",
            "markup_count",
            "child_marker_count",
        )
    )

    survivors: list[dict] = []
    for row in rows:
        if not row["administrative_area_level_1"]:
            continue
        if not is_meaningful_name(row["wiki__name"]):
            continue
        if row["wiki_photo_count"] + row["loc_photo_count"] < config.min_photos:
            continue
        if row["markup_count"] + row["child_marker_count"] < config.min_markup_or_children:
            continue
        lat, lon = float(row["latitude"]), float(row["longitude"])
        if any(_km_between(lat, lon, plat, plon) <= config.region_radius_km for plat, plon in public_coords):
            continue
        survivors.append(row)

    if not survivors:
        return set()

    wiki_ids = [row["wiki__id"] for row in survivors]

    vuln_ok: set[int] = set()
    for agg in (
        WikiStatVote.objects.filter(wiki_id__in=wiki_ids, field=WikiStatField.VULNERABILITY)
        .values("wiki_id")
        .annotate(avg=Avg("value"), cnt=Count("id"))
    ):
        if agg["cnt"] >= config.min_vuln_votes and agg["avg"] is not None and agg["avg"] < config.max_vuln_avg:
            vuln_ok.add(agg["wiki_id"])

    article_ok = {
        wiki_id
        for wiki_id, length in Article.objects.filter(wiki_id__in=wiki_ids)
        .annotate(content_len=Length("content"))
        .values_list("wiki_id", "content_len")
        if (length or 0) >= config.min_article_chars
    }

    survivors = [row for row in survivors if row["wiki__id"] in vuln_ok and row["wiki__id"] in article_ok]

    # Criterion: top-N most commonly pinned per state, computed over the
    # locations that already pass everything else. Ties at the cutoff all
    # qualify (a strict cut on equal counts would be arbitrary).
    eligible: set[int] = set()
    by_state: dict[str, list[dict]] = {}
    for row in survivors:
        by_state.setdefault(row["administrative_area_level_1"], []).append(row)
    for state_rows in by_state.values():
        state_rows.sort(key=lambda r: r["pinners"], reverse=True)
        cutoff_index = min(config.top_n_per_state, len(state_rows)) - 1
        cutoff = state_rows[cutoff_index]["pinners"]
        eligible.update(row["id"] for row in state_rows if row["pinners"] >= cutoff)

    return eligible


def _check_hard_fail(candidate: PublicPinCandidate, now: datetime, config: PublicPinConfig = CONFIG) -> bool:
    """Apply the hard-fail rule; returns True when the candidate was rejected.

    With ``fail_min_votes`` or more ballots and a no-share at or above
    ``fail_consensus``, the vote closes for good and the location is
    permanently ineligible.
    """
    tally = PublicPinVote.objects.tally(candidate)
    if tally.total >= config.fail_min_votes and tally.no_share >= config.fail_consensus:
        candidate.status = PublicPinCandidateStatus.REJECTED
        candidate.decided_at = now
        candidate.save(update_fields=["status", "decided_at", "updated"])
        logger.info("Public-pin candidate %s hard-failed (%s no / %s total)", candidate.pk, tally.no, tally.total)
        return True
    return False


def evaluate_public_pin_candidates(config: PublicPinConfig = CONFIG) -> dict[str, int]:
    """Recompute eligibility, transition candidates, and settle votes.

    Called from the Celery beat task. Idempotent - safe to run at any
    frequency.

    Returns:
        Counters for logging/tests: opened, reopened, suspended, passed,
        rejected.
    """
    now = timezone.now()
    eligible = _eligible_location_ids(now, config)
    counters = {"opened": 0, "reopened": 0, "suspended": 0, "passed": 0, "rejected": 0}

    active = list(PublicPinCandidate.objects.active().select_related("location"))
    by_location = {candidate.location_id: candidate for candidate in active}

    for candidate in active:
        if candidate.location_id in eligible and candidate.status == PublicPinCandidateStatus.SUSPENDED:
            candidate.status = PublicPinCandidateStatus.OPEN
            candidate.save(update_fields=["status", "updated"])
            counters["reopened"] += 1
        elif candidate.location_id not in eligible and candidate.status == PublicPinCandidateStatus.OPEN:
            candidate.status = PublicPinCandidateStatus.SUSPENDED
            candidate.save(update_fields=["status", "updated"])
            counters["suspended"] += 1

    for location_id in eligible - by_location.keys():
        PublicPinCandidate.objects.create(
            location_id=location_id,
            status=PublicPinCandidateStatus.OPEN,
            opened_at=now,
        )
        counters["opened"] += 1

    # Settle open votes. Newly-passed locations join the region-exclusion set
    # immediately, so two candidates in one region can't both pass in a run.
    passed_coords = [
        (float(lat), float(lon))
        for lat, lon in PublicPinCandidate.objects.passed().values_list("location__latitude", "location__longitude")
    ]
    for candidate in PublicPinCandidate.objects.with_status(PublicPinCandidateStatus.OPEN).select_related("location"):
        if _check_hard_fail(candidate, now, config):
            counters["rejected"] += 1
            continue
        if now - candidate.opened_at < timedelta(days=config.min_open_days):
            continue
        tally = PublicPinVote.objects.tally(candidate)
        if tally.total < config.min_votes_to_pass or tally.yes_share < config.pass_consensus:
            continue
        lat, lon = float(candidate.location.latitude), float(candidate.location.longitude)
        if any(_km_between(lat, lon, plat, plon) <= config.region_radius_km for plat, plon in passed_coords):
            candidate.status = PublicPinCandidateStatus.SUSPENDED
            candidate.save(update_fields=["status", "updated"])
            counters["suspended"] += 1
            continue
        candidate.status = PublicPinCandidateStatus.PASSED
        candidate.decided_at = now
        candidate.save(update_fields=["status", "decided_at", "updated"])
        passed_coords.append((lat, lon))
        counters["passed"] += 1
        logger.info("Location %s voted public (%s yes / %s total)", candidate.location_id, tally.yes, tally.total)

    if counters["passed"]:
        sync_public_pin_suggestions()

    return counters


def sync_public_pin_suggestions() -> int:
    """Ensure every opted-in profile has a suggestion for each public location.

    Idempotent backfill: skips profiles that already have a root pin there or
    any prior suggestion for the location (including rejected ones - declining
    a public pin is a decision, not something to re-ask). New accounts are
    picked up on the next beat run.

    Returns:
        Number of suggestions created.
    """
    created = 0
    passed = PublicPinCandidate.objects.passed().select_related("location__wiki")
    for candidate in passed:
        location = candidate.location
        wiki = location.wiki
        already_suggested = set(PinSuggestion.objects.filter(location=location).values_list("profile_id", flat=True))
        has_pin = set(Pin.objects.filter(location=location, parent_pin__isnull=True).values_list("profile_id", flat=True))
        recipients = (
            Profile.objects.filter(community_enabled=True, suggest_public_pins=True)
            .exclude(id__in=already_suggested | has_pin)
            .values_list("id", flat=True)
        )
        new_rows = [
            PinSuggestion(
                profile_id=profile_id,
                location=location,
                latitude=location.latitude,
                longitude=location.longitude,
                origin=PinSuggestionOrigin.COMMUNITY,
                suggested_name=wiki.name,
            )
            for profile_id in recipients
        ]
        if new_rows:
            PinSuggestion.objects.bulk_create(new_rows)
            created += len(new_rows)
    if created:
        logger.info("Created %s public-pin suggestions", created)
    return created


def public_vote_context(location: Location, profile: Profile | None) -> dict | None:
    """Build the template context for the public-vote block on a wiki page.

    Returns None when nothing should render (no candidate, suspended,
    rejected, or the viewer can't vote) - ineligibility is never explained
    in the UI.
    """
    candidate = PublicPinCandidate.objects.filter(location=location).first()
    if candidate is None:
        return None
    if candidate.status == PublicPinCandidateStatus.PASSED:
        return {"is_public": True}
    if not candidate.is_open or profile is None:
        return None
    if not Pin.objects.filter(location=location, profile=profile, parent_pin__isnull=True).exists():
        return None
    vote = PublicPinVote.objects.filter(candidate=candidate, profile=profile).first()
    return {"is_public": False, "my_vote": None if vote is None else vote.make_public}


def cast_public_vote(location: Location, profile: Profile, choice: str, config: PublicPinConfig = CONFIG) -> None:
    """Record, change, or withdraw ``profile``'s ballot for ``location``.

    Args:
        location: The place under vote.
        profile: The voter - must hold a root pin at the location.
        choice: ``"public"``, ``"private"``, or ``"withdraw"``.

    Raises:
        PublicVoteError: When there is no open vote, the profile can't vote
            here, or the choice is unrecognized.
    """
    candidate = PublicPinCandidate.objects.filter(location=location).first()
    if candidate is None or not candidate.is_open:
        raise PublicVoteError("There is no open vote for this location.")
    if not Pin.objects.filter(location=location, profile=profile, parent_pin__isnull=True).exists():
        raise PublicVoteError("Only users with this location pinned can vote.")

    if choice == "withdraw":
        PublicPinVote.objects.filter(candidate=candidate, profile=profile).delete()
        return
    if choice not in ("public", "private"):
        raise PublicVoteError("Unrecognized vote.")

    PublicPinVote.objects.update_or_create(
        candidate=candidate,
        profile=profile,
        defaults={"make_public": choice == "public"},
    )
    # Only the hard-fail rule runs on the write path: passing stays on the
    # beat cadence so the minimum-open-time floor can't be raced.
    _check_hard_fail(candidate, timezone.now(), config)
