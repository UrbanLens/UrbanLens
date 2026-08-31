"""Place querysets and managers - geometric resolution and domain lookups."""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import TYPE_CHECKING, Self

from django.contrib.gis.geos import Point

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.place.model import Place

#: A WGS-84 coordinate as any of the forms this codebase stores or parses one
#: in. ``Location`` holds fixed-precision decimals; parsed input arrives as
#: floats or strings.
Coordinate = float | Decimal | str | None

logger = logging.getLogger(__name__)


class PlaceQuerySet(abstract.DashboardQuerySet):
    """QuerySet for Place - the real-world parcels and buildings pins resolve onto."""

    def current(self) -> Self:
        """Places that still describe the ground as it is today."""
        from urbanlens.dashboard.models.place.model import PlaceStatus

        return self.filter(status=PlaceStatus.CURRENT)

    def of_kind(self, kind: str) -> Self:
        """Places of one kind (parcel, building, site)."""
        return self.filter(kind=kind)

    def resolvable(self) -> Self:
        """Places a coordinate is allowed to resolve onto.

        Three exclusions, each load-bearing:

        - **Superseded** places keep their geometry for display and history,
          but a historical campus boundary still geometrically contains every
          post-split pin, so containment against it must never resolve.
        - **Aggregates** (anything with ``MEMBER_OF`` children) exist to be
          *earned* by holding every member, never to be pinned into directly -
          see ``services.wiki.wiki_access``. Their geometry is the union of
          their members, so excluding them here is what makes the strict rule
          unbypassable rather than merely unlikely.
        - **Geometry-less** places (a building nobody has a footprint for)
          have nothing to test containment against; they stay reachable
          through their parent's domain instead.
        """
        return self.current().filter(is_aggregate=False, geometry__isnull=False)

    def containing_point(self, point: Point) -> Self:
        """Resolvable places whose official geometry contains a coordinate.

        Ordered most-specific first: smallest area wins. A building footprint
        is always smaller than the parcel enclosing it, so area ordering
        subsumes "deepest in the containment tree" without needing to walk it,
        and it stays deterministic for two unrelated parcels that overlap
        through bad county geometry.
        """
        return self.resolvable().filter(geometry__contains=point).order_by("area_sqm", "pk")

    def in_domain(self, domain_root_id: int) -> Self:
        """Every place sharing one access domain."""
        return self.filter(domain_root_id=domain_root_id)

    def in_domains(self, domain_root_ids: Iterable[int]) -> Self:
        """Every place in any of the given access domains."""
        return self.filter(domain_root_id__in=list(domain_root_ids))

    def part_of_children(self) -> Self:
        """Restrict to places attached to their parent by a ``PART_OF`` edge."""
        from urbanlens.dashboard.models.place.model import PlaceRelation

        return self.filter(parent__isnull=False, parent_relation=PlaceRelation.PART_OF)

    def member_of_children(self) -> Self:
        """Restrict to places attached to their parent by a ``MEMBER_OF`` edge."""
        from urbanlens.dashboard.models.place.model import PlaceRelation

        return self.filter(parent__isnull=False, parent_relation=PlaceRelation.MEMBER_OF)


class PlaceManager(abstract.DashboardManager.from_queryset(PlaceQuerySet)):
    """Manager for Place.

    ``resolve_for_point`` is the single answer to "which real-world thing is
    this coordinate on?" - every creation, import, move, and access check goes
    through it rather than running its own containment query.
    """

    def resolve_for_point(self, latitude: Coordinate, longitude: Coordinate) -> Place | None:
        """The most specific current place containing a coordinate, or None.

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
        """Places that genuinely compete with ``resolved`` for a coordinate.

        A competitor is a resolvable place containing the same point that is
        in a *different access domain*. Everything inside one property -
        buildings under their parcel - shares a domain and is therefore never
        a competitor, which is what stops a 124-building campus from telling
        every visitor that 124 other places cover their pin. What survives is
        the real case: two unrelated parcels whose county geometry overlaps.

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
    """Build a WGS-84 GEOS point, tolerating missing coordinates.

    Args:
        latitude: WGS-84 latitude, or None.
        longitude: WGS-84 longitude, or None.

    Returns:
        The point, or None when either coordinate is missing.
    """
    if latitude is None or longitude is None:
        return None
    return Point(float(longitude), float(latitude), srid=4326)


class PlaceAccessGrantQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PlaceAccessGrant."""

    def for_profile(self, profile) -> Self:
        """Grants held by one profile."""
        return self.filter(profile=profile)


class PlaceAccessGrantManager(abstract.DashboardManager.from_queryset(PlaceAccessGrantQuerySet)):
    """Manager for PlaceAccessGrant.

    Deliberately offers no general-purpose "grant access" helper - the two
    methods below are narrow and named for the one structural event each
    covers. Every other caller must go through the computed predicate in
    ``services.wiki.wiki_access``.
    """

    def granted_domain_ids(self, profile) -> set[int]:
        """Domain roots this profile holds an explicit grant for.

        Args:
            profile: The Profile to look up.

        Returns:
            Set of ``Place.domain_root_id`` values.
        """
        if profile is None or profile.pk is None:
            return set()
        return set(self.for_profile(profile).values_list("place__domain_root_id", flat=True))

    def snapshot_family(self, profile_ids: Iterable[int], aggregate: Place, *, reason: str | None = None) -> None:
        """Permanently grant a split-derived aggregate and all its current members.

        Used both at the moment a parcel is split (for everyone who held the
        undivided parcel) and, later, for anyone who independently earns the
        same aggregate by pinning every one of its current successors - both
        are "proved full knowledge of this split family", just at different
        times, and both deserve the identical permanent record.

        Args:
            profile_ids: Profiles to grant. A no-op for an empty iterable.
            aggregate: The split-derived aggregate place. Its own row and
                every current ``MEMBER_OF`` child are granted together.
            reason: Defaults to :attr:`GrantReason.GRANDFATHERED_SPLIT`.
        """
        from urbanlens.dashboard.models.place.model import GrantReason, PlaceRelation

        resolved_reason = reason if reason is not None else GrantReason.GRANDFATHERED_SPLIT
        ids = list(profile_ids)
        if not ids:
            return
        family = [aggregate, *aggregate.children.filter(parent_relation=PlaceRelation.MEMBER_OF)]
        grants = [self.model(profile_id=profile_id, place=place, reason=resolved_reason) for profile_id in ids for place in family]
        # ignore_conflicts: a repeat call (a profile who already holds part of
        # the family, or two overlapping snapshot triggers) must stay a no-op,
        # not an IntegrityError on the (profile, place) unique constraint.
        self.bulk_create(grants, ignore_conflicts=True)

    def record_engagement(self, profile, place: Place | None) -> None:
        """Permanently grant *profile* the domain *place* sits in, for engaging with it.

        A profile who views a wiki or shares content to it while they hold
        access keeps that access even after every qualifying pin is later
        moved or deleted - see :class:`GrantReason.GRANDFATHERED_ENGAGEMENT`.
        Idempotent: a repeat view is a cheap no-op once the grant exists.

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


class PlaceExternalTagQuerySet(abstract.DashboardQuerySet):
    """QuerySet for PlaceExternalTag - raw provider classification data."""

    def for_place(self, place) -> Self:
        """Tags belonging to one place."""
        return self.filter(place=place)

    def for_source(self, source: str) -> Self:
        """Tags reported by one provider."""
        return self.filter(source=source)

    def matching(self, key: str, value: str | None = None) -> Self:
        """Tags with a given key, optionally narrowed to one value."""
        qs = self.filter(key=key)
        return qs.filter(value=value) if value is not None else qs


class PlaceExternalTagManager(abstract.DashboardManager.from_queryset(PlaceExternalTagQuerySet)):
    """Manager for PlaceExternalTag rows."""


class ExternalTagGroupQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ExternalTagGroup."""

    def non_empty(self) -> Self:
        """Groups that still have at least one member."""
        return self.filter(members__isnull=False).distinct()


class ExternalTagGroupManager(abstract.DashboardManager.from_queryset(ExternalTagGroupQuerySet)):
    """Manager for ExternalTagGroup."""


class ExternalTagVocabularyEntryQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ExternalTagVocabularyEntry."""

    def ungrouped(self) -> Self:
        """Entries with no explicit group - eligible for default same-text matching."""
        return self.filter(group__isnull=True)

    def in_group(self, group) -> Self:
        """Entries belonging to one explicit group."""
        return self.filter(group=group)

    def for_tag(self, source: str, key: str, value: str) -> Self:
        """The (at most one) entry for one exact tag tuple."""
        return self.filter(source=source, key=key, value=value)


class ExternalTagVocabularyEntryManager(abstract.DashboardManager.from_queryset(ExternalTagVocabularyEntryQuerySet)):
    """Manager for ExternalTagVocabularyEntry."""
