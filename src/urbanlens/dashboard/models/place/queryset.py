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

    Deliberately offers no public "grant access" helper. Grants are written
    only by the Place backfill migration and by split processing; every other
    caller must go through the computed predicate.
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
