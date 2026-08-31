"""Place - the real-world parcel or building a coordinate resolves onto.

``Location`` is an exact coordinate. ``Place`` is the *thing* that coordinate
is on, and it is the unit everything shared hangs off: official geometry, the
community wiki, and access.

Before Place, official geometry hung off Location, and it was fetched by point
lookup - so importing 124 building pins onto one campus created 124 Locations
each holding its own copy of the *same* parcel polygon. Every point on the
campus then sat inside 125 boundaries at once, which is why a visitor was told
124 other places covered their pin, why a building's page drew the parcel, and
why one property could accumulate 125 wikis. One row per real-world thing
removes all of that at the source.

Two relationships, and the difference between them is the entire access model:

``PART_OF``
    The child is a component of the parent, and the parent's identity is fully
    implied by it: a building on a parcel. Knowing the building *is* knowing
    the parcel it stands on. Carries **no** access semantics - see
    :class:`PlaceRelation` and the access domain notion below.

``MEMBER_OF``
    The child is one of several independent peers that make up the parent: the
    parcels a campus was split into, or the several tax parcels one site spans.
    The parent's knowledge genuinely exceeds any single child's, so it is
    earned only by holding every member.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models import MultiPolygonField
from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, DateTimeField, FloatField, ForeignKey, Index, IntegerField, Q, TextChoices, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.place.queryset import PlaceAccessGrantManager, PlaceManager

logger = logging.getLogger(__name__)


class PlaceKind(TextChoices):
    """What sort of real-world thing a Place describes.

    Extensible by design: the containment FK does the modelling work, and kind
    is closer to a label on hierarchy depth than a behavioural switch, so a new
    kind is an enum value rather than a schema migration.
    """

    PARCEL = "parcel", "Parcel"
    BUILDING = "building", "Building"
    SITE = "site", "Site"


class PlaceRelation(TextChoices):
    """How a Place is attached to its parent - the only access boundary there is.

    ``PART_OF`` is pure organisation. Splitting a property into its buildings
    must not change who can see what: a parcel and everything ``PART_OF`` it
    form one **access domain**, and holding a pin anywhere in that domain
    grants every wiki in it, in both directions.

    ``MEMBER_OF`` is the boundary. Its parent is reachable only by holding
    access to every one of its members (see
    ``services.wiki.wiki_access.accessible_domain_ids``).
    """

    PART_OF = "part_of", "Part of"
    MEMBER_OF = "member_of", "Member of"


class PlaceStatus(TextChoices):
    """Whether a Place still describes the ground as it is today.

    Deliberately *not* overloaded to carry access semantics - that is
    :class:`PlaceRelation`'s job. Status controls one thing: whether a
    coordinate may resolve onto this geometry. A superseded campus boundary
    still contains every pin dropped inside its former footprint, so it must
    never resolve, but it stays for display and history.
    """

    CURRENT = "current", "Current"
    SUPERSEDED = "superseded", "Superseded"


class GrantReason(TextChoices):
    """Why a profile holds access that containment can no longer prove."""

    GRANDFATHERED_BACKFILL = "backfill", "Held before places existed"
    GRANDFATHERED_SPLIT = "split", "Held the split family - originally, or by earning every current member"
    GRANDFATHERED_ENGAGEMENT = "engagement", "Viewed the wiki or shared content to it while access was held"


class Place(abstract.DashboardModel):
    """One real-world parcel, building, or multi-parcel site.

    Attributes:
        kind: What sort of thing this is (see :class:`PlaceKind`).
        parent: The containing or aggregating place, if any.
        parent_relation: How it is attached (see :class:`PlaceRelation`).
        status: Whether it still describes the ground (see :class:`PlaceStatus`).
        geometry: Official boundary. Written **only** by the provider chain and
            boundary voting - there is no user-writable path, which is what
            makes "user-drawn shapes can never widen access" structural rather
            than a filter every query has to remember.
        area_sqm: Cached area, so most-specific resolution is an indexed sort
            instead of a PostGIS area computation per candidate per request.
        domain_root: Denormalised root of this place's access domain (see
            :class:`PlaceRelation`); a domain root points at itself. Maintained
            by ``services.places.lineage``.
        is_aggregate: True once this place has any ``MEMBER_OF`` child. Such a
            place is earned, never resolved onto (see
            ``PlaceQuerySet.resolvable``).
        provider / provider_key: The external record this place came from, for
            idempotent re-matching across provider runs.
    """

    kind = CharField(max_length=20, choices=PlaceKind.choices, default=PlaceKind.PARCEL)
    status = CharField(max_length=20, choices=PlaceStatus.choices, default=PlaceStatus.CURRENT)

    # Human-readable label for admin and logs only. The community-facing name
    # lives on Wiki; this is never rendered to users.
    name = CharField(max_length=255, blank=True, default="")

    parent = ForeignKey(
        "self",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    # Meaningless when parent is null; every reader checks parent_id first.
    parent_relation = CharField(max_length=20, choices=PlaceRelation.choices, default=PlaceRelation.PART_OF)

    # Root of this place's access domain. Never null after save (a domain root
    # points at itself), so access lookups are one indexed equality test rather
    # than a recursive walk.
    domain_root = ForeignKey(
        "self",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="domain_members",
    )
    is_aggregate = BooleanField(default=False)
    # How many BUILDING children this place has. Cached because it decides
    # scope on every render: a parcel with several buildings is describing the
    # grounds, while a parcel with one is the ordinary case where "the parcel"
    # and "the building" are the same thing and neither should be singled out.
    building_child_count = IntegerField(default=0)

    geometry = MultiPolygonField(geography=True, srid=4326, null=True, blank=True)
    area_sqm = FloatField(null=True, blank=True)
    # A non-null value with null geometry means "the providers were asked and
    # had nothing" - don't refetch on every page view.
    geometry_generated_at = DateTimeField(null=True, blank=True)

    provider = CharField(max_length=20, blank=True, default="")
    provider_key = CharField(max_length=255, blank=True, default="")

    objects = PlaceManager()

    if TYPE_CHECKING:
        id: int
        parent_id: int | None
        domain_root_id: int | None

    @property
    def is_domain_root(self) -> bool:
        """True when this place roots its own access domain."""
        return self.domain_root_id == self.pk

    @property
    def parcel(self) -> Place | None:
        """The parcel this place is on: its ``PART_OF`` parent, or itself.

        Returns None for a building whose parcel is unknown.
        """
        if self.kind != PlaceKind.BUILDING:
            return self
        if self.parent_id and self.parent_relation == PlaceRelation.PART_OF:
            return self.parent
        return None

    @property
    def is_multi_building(self) -> bool:
        """Whether the parcel this place belongs to holds several buildings.

        The distinction the whole parcel/building split exists for. An ordinary
        house is one building on one parcel, and calling either of those "the
        building" or "the grounds" would be drawing a line where there isn't
        one - so scope stays neutral and both outlines are drawn. A campus with
        124 structures is the opposite case, and every marker on it has to
        commit to describing one or the other.
        """
        from urbanlens.dashboard.services.locations.site_scope import MULTI_BUILDING_THRESHOLD

        parcel = self.parcel
        return parcel is not None and parcel.building_child_count >= MULTI_BUILDING_THRESHOLD

    @property
    def expected_domain_root_id(self) -> int | None:
        """The domain root this place's own edge implies.

        A place roots its own domain unless it is ``PART_OF`` a parent, in
        which case it inherits the parent's. Returns None only for an unsaved
        instance whose parent is also unsaved.
        """
        if self.parent_id is None or self.parent_relation != PlaceRelation.PART_OF:
            return self.pk
        return self.parent.domain_root_id if self.parent is not None else None

    def save(self, *args, **kwargs) -> None:
        """Persist, then self-anchor the domain root for a newly created root place.

        A domain root points at itself, which cannot be expressed before the
        row has a primary key. The follow-up write happens only on the insert
        that creates a root, and uses ``queryset.update`` so it can't recurse.
        """
        super().save(*args, **kwargs)
        if self.domain_root_id is None and self.parent_id is None:
            type(self).objects.filter(pk=self.pk).update(domain_root=self.pk)
            self.domain_root_id = self.pk

    def __str__(self) -> str:
        label = self.name or f"place {self.pk}"
        return f"{self.get_kind_display()} ({label})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_places"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["status", "is_aggregate"], name="idxdb_place_resolvable"),
        ]
        constraints = [
            UniqueConstraint(
                fields=["provider", "provider_key", "kind"],
                condition=~Q(provider_key=""),
                name="place_unique_provider_record",
            ),
        ]


class PlaceAccessGrant(abstract.DashboardModel):
    """Materialised access that containment can no longer prove.

    Written by four callers, all through ``PlaceAccessGrantManager`` rather
    than a public "grant access" API surface:

    - The Place backfill migration, for access held before places existed.
    - :func:`services.places.splits.process_split`, for every profile who
      held the *undivided* parcel at the moment it was split - snapshotted
      for the parcel and every one of its new successors together
      (:meth:`PlaceAccessGrantManager.snapshot_family`), so a structural
      reorganisation a profile had no part in never costs them anything.
    - :func:`services.wiki.wiki_access._snapshot_earned_split_families`, the
      symmetric case for a profile who was never a prior holder but later
      earns a split-derived aggregate the ordinary way (holding every current
      successor at once) - the moment that happens, it is snapshotted exactly
      like a prior holder's, because proving full knowledge of a
      split-derived family is proving full knowledge of it regardless of when.
      An *organic* multi-parcel site (never split, ``PlaceStatus.CURRENT``) is
      deliberately excluded - see :class:`PlaceStatus` - and stays governed by
      the strict recompute-every-time rule in ``services.wiki.wiki_access``.
    - :meth:`PlaceAccessGrantManager.record_engagement`, called from
      :func:`services.wiki.wiki_access.resolve_visible_wiki` and
      :meth:`services.wiki.wiki_share.WikiShareService.share_from_pin` -
      a profile who actually viewed a wiki or shared content to it while they
      held access keeps that access even after every qualifying pin is gone;
      one who never engaged with it does not.

    Grants are permanent and are never revoked by pin churn, unlike computed
    access.

    Attributes:
        profile: Who holds the access.
        place: The place it applies to. Access is evaluated per domain, so a
            grant on any member of a domain grants the whole domain.
        reason: Which structural change created it.
    """

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="place_access_grants",
    )
    place = ForeignKey(
        "dashboard.Place",
        on_delete=CASCADE,
        related_name="access_grants",
    )
    reason = CharField(max_length=20, choices=GrantReason.choices)

    objects = PlaceAccessGrantManager()

    if TYPE_CHECKING:
        profile_id: int
        place_id: int

    def __str__(self) -> str:
        return f"Grant({self.get_reason_display()}) for profile {self.profile_id} on place {self.place_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_place_access_grants"
        constraints = [
            UniqueConstraint(fields=["profile", "place"], name="place_access_grant_unique"),
        ]
        indexes = []
