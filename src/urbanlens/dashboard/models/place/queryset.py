"""Place querysets and managers - geometric resolution and domain lookups."""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import TYPE_CHECKING, Self

from django.contrib.gis.geos import Point

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.place.external_tag import PlaceExternalTag  # noqa: F401 - mypy needs these; ruff does not
    from urbanlens.dashboard.models.place.external_tag_group import ExternalTagGroup, ExternalTagVocabularyEntry  # noqa: F401 - mypy needs these; ruff does not
    from urbanlens.dashboard.models.place.model import (
        Place,
        PlaceAccessGrant,  # noqa: F401 - mypy needs these; ruff does not
    )

#: A WGS-84 coordinate as any of the forms this codebase stores or parses one
#: in. ``Location`` holds fixed-precision decimals; parsed input arrives as
#: floats or strings.
Coordinate = float | Decimal | str | None

logger = logging.getLogger(__name__)


class PlaceQuerySet(abstract.DashboardQuerySet["Place"]):
    """QuerySet for Place - the real-world parcels and buildings pins resolve onto."""

    def current(self) -> Self:
        """Places that still describe the ground as it is today."""
        from urbanlens.dashboard.models.place.model import PlaceStatus

        return self.filter(status=PlaceStatus.CURRENT)

    def of_kind(self, kind: str) -> Self:
        """Places of one kind (parcel, building, site)."""
        return self.filter(kind=kind)

    def implausible(self) -> Self:
        """
        Places larger than their kind can be, and every place in a domain rooted on one.

        Such a place is not the thing it claims to be, so it must not share access between the pins on it.
        """
        from urbanlens.dashboard.models.place.model import implausible_area_q

        return self.filter(implausible_area_q() | implausible_area_q("domain_root__"))

    def resolvable(self) -> Self:
        """
        Places a coordinate is allowed to resolve onto.
        Four exclusions, each load-bearing: - **Superseded** places keep their geometry for display and history, but a historical campus boundary still geometrically contains every post-split pin, so containment against it must never resolve. - **Aggregates** (anything with ``MEMBER_OF`` children) exist to be *earned* by holding every member, never to be pinned into directly - see ``services.wiki.wiki_access``. - **Implausible** places (see :meth:`implausible`) would join strangers a county apart.
        """
        from urbanlens.dashboard.models.place.model import Place

        return self.current().filter(is_aggregate=False, geometry__isnull=False).exclude(pk__in=Place.objects.implausible().values("pk"))

    def containing_point(self, point: Point) -> Self:
        """
        Resolvable places whose official geometry contains a coordinate.
        Ordered most-specific first: an official or footprint place beats any synthetic one whatever its size, and within each of those two groups the smallest area wins.
        A building footprint is always smaller than the parcel enclosing it, so area ordering subsumes "deepest in the containment tree" without needing to walk it, and it stays deterministic for two unrelated parcels that overlap through bad county geometry.
        """
        return self.resolvable().filter(geometry__contains=point).most_specific_first()

    def most_specific_first(self) -> Self:
        """Order strong evidence (official, footprint) before synthetic, then smallest area, then pk."""
        from django.db.models import Case, IntegerField, Value, When

        from urbanlens.dashboard.models.place.model import STRONG_EVIDENCE

        return self.annotate(evidence_rank=Case(When(evidence__in=STRONG_EVIDENCE, then=Value(0)), default=Value(1), output_field=IntegerField())).order_by("evidence_rank", "area_sqm", "pk")


_PlaceManagerBase = abstract.DashboardManager.from_queryset(PlaceQuerySet)


class PlaceManager(_PlaceManagerBase["Place"]):
    """
    Manager for Place.

    ``resolve_for_point`` is the single answer to "which real-world thing is
    this coordinate on?" - every creation, import, move, and access check goes
    through it rather than running its own containment query.
    """

    def resolve_for_point(self, latitude: Coordinate, longitude: Coordinate) -> Place | None:
        """
        The most specific current place containing a coordinate, or None.

        Args:
            latitude: WGS-84 latitude; None is tolerated.
            longitude: WGS-84 longitude; None is tolerated.

        Returns:
            The smallest resolvable Place containing the point, or None when
            the coordinate is on no known parcel or building.
        """
        point = point_for_coordinates(latitude, longitude)
        if point is None:
            return None
        return self.containing_point(point).first()

    def competing_for_point(self, latitude: Coordinate, longitude: Coordinate, *, resolved: Place | None) -> PlaceQuerySet:
        """
        Places that genuinely compete with ``resolved`` for a coordinate.
        A competitor is a resolvable place containing the same point that is in a *different access domain*.
        Everything inside one property - buildings under their parcel - shares a domain and is therefore never a competitor, which is what stops a 124-building campus from telling every visitor that 124 other places cover their pin.

        Args:
            latitude: WGS-84 latitude; None is tolerated.
            longitude: WGS-84 longitude; None is tolerated.
            resolved: The place the coordinate resolved onto, if any.

        Returns:
            QuerySet of competing places, most specific first (usually empty).
        """
        point = point_for_coordinates(latitude, longitude)
        if point is None:
            return self.none()
        candidates = self.containing_point(point)
        if resolved is not None:
            candidates = candidates.exclude(domain_root_id=resolved.domain_root_id)
        return candidates


def point_for_coordinates(latitude: Coordinate, longitude: Coordinate) -> Point | None:
    """
    Build a WGS-84 GEOS point, tolerating missing coordinates.

    Args:
        latitude: WGS-84 latitude, or None.
        longitude: WGS-84 longitude, or None.

    Returns:
        The point, or None when either coordinate is missing.
    """
    if latitude is None or longitude is None:
        return None
    return Point(float(longitude), float(latitude), srid=4326)


class PlaceAccessGrantQuerySet(abstract.DashboardQuerySet["PlaceAccessGrant"]):
    """QuerySet for PlaceAccessGrant."""

    def for_profile(self, profile) -> Self:
        """Grants held by one profile."""
        return self.filter(profile=profile)


_PlaceAccessGrantManagerBase = abstract.DashboardManager.from_queryset(PlaceAccessGrantQuerySet)


class PlaceAccessGrantManager(_PlaceAccessGrantManagerBase["PlaceAccessGrant"]):
    """
    Manager for PlaceAccessGrant.
    Every other caller must go through the computed predicate in ``services.wiki.wiki_access``.
    """

    def granted_domain_ids(self, profile) -> set[int]:
        """
        Domain roots this profile holds an explicit grant for.

        Args:
            profile: The Profile to look up.

        Returns:
            Set of ``Place.domain_root_id`` values.
        """
        if profile is None or profile.pk is None:
            return set()
        return {root for root in self.for_profile(profile).values_list("place__domain_root_id", flat=True) if root is not None}

    def grant_places(self, profile_ids: Iterable[int], places: Iterable[Place], *, reason: str) -> int:
        """
        Permanently grant each profile each place's domain.
        Never removes anything. Repeat grants are no-ops.

        Args:
            profile_ids: Profiles to grant. A no-op for an empty iterable.
            places: Places to grant on.
            reason: Why, as a :class:`GrantReason` value.

        Returns:
            How many grants were requested (existing ones included).
        """
        ids = list(profile_ids)
        targets = list({place.pk: place for place in places}.values())
        if not ids or not targets:
            return 0
        grants = [self.model(profile_id=profile_id, place=place, reason=reason) for profile_id in ids for place in targets]
        # ignore_conflicts: a repeat call (a profile who already holds part of
        # the family, or two overlapping snapshot triggers) must stay a no-op,
        # not an IntegrityError on the (profile, place) unique constraint.
        self.bulk_create(grants, ignore_conflicts=True)
        return len(grants)

    def snapshot_family(self, profile_ids: Iterable[int], aggregate: Place, *, reason: str | None = None) -> None:
        """
        Permanently grant a split-derived aggregate and all its current members.
        For anyone who independently earns the aggregate by pinning every one of its current successors.
        Grants made when the split happens go through ``services.places.lineage_events``.

        Args:
            profile_ids: Profiles to grant. A no-op for an empty iterable.
            aggregate: The split-derived aggregate place. Its own row and
                every current ``MEMBER_OF`` child are granted together.
            reason: Defaults to :attr:`GrantReason.EARNED_FAMILY`.
        """
        from urbanlens.dashboard.models.place.model import GrantReason

        resolved_reason = reason if reason is not None else GrantReason.EARNED_FAMILY
        self.grant_places(profile_ids, self.family_of(aggregate), reason=resolved_reason)

    @staticmethod
    def family_of(aggregate: Place) -> list[Place]:
        """The aggregate plus every current ``MEMBER_OF`` child."""
        from urbanlens.dashboard.models.place.model import PlaceRelation

        return [aggregate, *aggregate.children.filter(parent_relation=PlaceRelation.MEMBER_OF)]

    def record_engagement(self, profile, place: Place | None) -> None:
        """
        Permanently grant *profile* the domain *place* sits in, for engaging with it.
        A profile who views a wiki or shares content to it while they hold access keeps that access even after every qualifying pin is later moved or deleted - see :class:`GrantReason.GRANDFATHERED_ENGAGEMENT`.

        Args:
            profile: The profile engaging with the wiki. A no-op for None or
                an unsaved profile.
            place: The place tied to the wiki's Location. A no-op for None or
                an unsaved place - a placeless location has no domain to
                grant, and its exact-pin-match access needs no grandfathering.
        """
        from urbanlens.dashboard.models.place.model import GrantReason

        if profile is None or profile.pk is None or place is None or place.pk is None:
            return
        self.get_or_create(profile=profile, place=place, defaults={"reason": GrantReason.GRANDFATHERED_ENGAGEMENT})


class PlaceExternalTagQuerySet(abstract.DashboardQuerySet["PlaceExternalTag"]):
    """QuerySet for PlaceExternalTag - raw provider classification data."""


_PlaceExternalTagManagerBase = abstract.DashboardManager.from_queryset(PlaceExternalTagQuerySet)


class PlaceExternalTagManager(_PlaceExternalTagManagerBase):
    """Manager for PlaceExternalTag rows."""


class ExternalTagGroupQuerySet(abstract.DashboardQuerySet["ExternalTagGroup"]):
    """QuerySet for ExternalTagGroup."""

    def non_empty(self) -> Self:
        """Groups that still have at least one member."""
        return self.filter(members__isnull=False).distinct()


_ExternalTagGroupManagerBase = abstract.DashboardManager.from_queryset(ExternalTagGroupQuerySet)


class ExternalTagGroupManager(_ExternalTagGroupManagerBase):
    """Manager for ExternalTagGroup."""


class ExternalTagVocabularyEntryQuerySet(abstract.DashboardQuerySet["ExternalTagVocabularyEntry"]):
    """QuerySet for ExternalTagVocabularyEntry."""

    def ungrouped(self) -> Self:
        """Entries with no explicit group - eligible for default same-text matching."""
        return self.filter(group__isnull=True)


_ExternalTagVocabularyEntryManagerBase = abstract.DashboardManager.from_queryset(ExternalTagVocabularyEntryQuerySet)


class ExternalTagVocabularyEntryManager(_ExternalTagVocabularyEntryManagerBase):
    """Manager for ExternalTagVocabularyEntry."""
