# Generic imports
from __future__ import annotations

import contextlib
import logging
import math
from typing import TYPE_CHECKING, Self

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.db import IntegrityError
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

# App Imports
from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.services.security.redact import redact_coordinate

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

logger = logging.getLogger(__name__)


class PinQuerySet(abstract.PublicDashboardQuerySet):
    """QuerySet for Pin - the user-specific half of the place model.

    Filters here operate on per-user data (profile, visit history, status, priority).
    For filtering by place attributes (address, CID, official name) use LocationQuerySet
    or join through the location FK: Pin.objects.filter(location__official_name__icontains=...).
    """

    def root_pins(self) -> Self:
        """Return only top-level pins (excludes personal detail pins)."""
        return self.filter(parent_pin__isnull=True)

    def with_cached_photos(self) -> Self:
        """Pins whose location already has at least one stored Photo Image.

        A pure DB lookup - no external API call is made or needed. Deliberately
        not scoped to ``wiki__isnull=False``: this answers "is there already a
        photo to show" (pin detail display), a superset of "eligible for
        SpotGuessr Photos mode" (that stricter wiki+relevance filter lives in
        ``services.spotguessr.photos`` and isn't duplicated here).
        """
        from urbanlens.dashboard.models.images.model import Image, MediaKind

        return self.filter(Exists(Image.objects.filter(location_id=OuterRef("location_id"), media_type=MediaKind.PHOTO)))

    def filter_by_security_indicators(self, criteria) -> Self:
        """Filter by exact match on each ``security_<field>`` criterion.

        Each of the 8 :data:`SECURITY_FIELDS` gets its own optional exact-match
        filter (e.g. ``security_fences="everywhere"``); unset/invalid values
        are ignored rather than raising, matching the rest of
        ``filter_by_criteria``'s tolerance for a hand-edited/stale criteria dict.

        Args:
            criteria: The same criteria dict ``filter_by_criteria`` receives.

        Returns:
            Filtered QuerySet.
        """
        from urbanlens.dashboard.models.abstract.choices import SecurityLevel
        from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS

        valid_levels = {value for value, _ in SecurityLevel.choices}
        qs = self
        for field_key, _label in SECURITY_FIELDS:
            value = criteria.get(f"security_{field_key}")
            if value and value in valid_levels:
                qs = qs.filter(**{field_key: value})
        return qs

    def detail_pins(self) -> Self:
        """Return only personal detail pins (sub-markers owned by a user's pin)."""
        return self.filter(parent_pin__isnull=False)

    def with_descendants(self) -> Self:
        """Expand this queryset to include the full personal detail-pin subtree of each pin.

        Walks ``parent_pin`` children level by level (BFS) until no new
        descendants are found, so pins of any nesting depth are included -
        needed because deleting a pin cascades to its entire subtree
        (``Pin.parent_pin`` is ``on_delete=CASCADE``).

        Returns:
            A fresh QuerySet over this queryset's pins plus every descendant.
        """
        from urbanlens.dashboard.models.pin.model import Pin

        root_ids = set(self.values_list("pk", flat=True))
        all_ids = set(root_ids)
        frontier = root_ids
        while frontier:
            children = set(Pin.objects.filter(parent_pin_id__in=frontier).values_list("pk", flat=True))
            frontier = children - all_ids
            all_ids |= frontier
        return Pin.objects.filter(pk__in=all_ids)

    def never_visited(self):
        return self.filter(last_visited__isnull=True)

    def visited(self) -> Self:
        """Return pins marked visited, however that was recorded.

        A pin counts as "visited" when it either has a ``last_visited``
        timestamp or carries the profile's "Visited" status label - the same
        predicate used inline by ``visited_without_record`` and
        ``filter_by_criteria``'s ``has_visits`` filter, named here so other
        callers (e.g. Consensus's eligibility rule - only visited pins'
        wikis are ever surfaced as rounds) can build on it directly instead
        of re-deriving the ``Q``.
        """
        visited_q = Q(last_visited__isnull=False) | Q(labels__name="Visited", labels__kind="status")
        return self.filter(visited_q).distinct()

    def visited_without_record(self) -> Self:
        """Return top-level pins marked visited that have no dated PinVisit record.

        Such a pin can still lack any ``PinVisit`` row (e.g. imported pins, or a
        status set by hand), leaving a gap the Memories page surfaces so the user
        can log a concrete, dated visit. Pins the user dismissed from that queue
        are excluded.

        Returns:
            Distinct top-level pins that are marked visited but have zero rows in
            their ``visit_history``.
        """
        return self.root_pins().visited().filter(visit_history__isnull=True).exclude(unlogged_visit_dismissed=True).distinct()

    def not_visited_this_year(self):
        return self.filter(last_visited__year__lt=timezone.now().year)

    def by_category(self, category):
        return self.filter(labels__name=category, labels__kind="category")

    def by_priority(self, priority):
        return self.filter(priority=priority)

    def by_latitude(self, latitude):
        # A Pin's coordinates live on its Location.
        return self.filter(location__latitude=latitude)

    def by_longitude(self, longitude):
        return self.filter(location__longitude=longitude)

    def by_name(self, name):
        return self.filter(name__icontains=name)

    def with_placeholder_names(self) -> Self:
        """Pins carrying a stored, non-user-provided name (candidates for the name-upgrade sweep).

        Narrows to the SQL-cheap part of the check (a name is stored at all,
        and the user didn't type it); callers must still test each name with
        ``is_meaningful_name`` themselves, since "meaningful" isn't expressible
        as a query filter.

        Returns:
            Filtered queryset, with ``location`` (and its wiki) preselected.
        """
        return self.filter(name_is_user_provided=False).exclude(name__isnull=True).exclude(name="").select_related("location__wiki")

    def by_profile(self, profile):
        return self.filter(profile=profile)

    def by_created_year(self, year):
        return self.filter(created__year=year)

    def by_updated_year(self, year):
        return self.filter(updated__year=year)

    def modified_since(self, since) -> Self:
        """Return pins created or edited at or after ``since``.

        ``updated`` is ``auto_now``, so any save - create or edit - moves a pin
        into this window. Deletions never appear here; delta-sync callers pair
        this with ``PinTombstone.objects.deleted_since`` to learn about those.
        Rides the ``idxdb_profile_update`` index when combined with a profile
        filter.

        Args:
            since: Inclusive lower bound on the last-modified time.

        Returns:
            This queryset filtered to pins modified at or after ``since``.
        """
        return self.filter(updated__gte=since)

    def near_point(self, point: Point, radius_km: float) -> Self:
        """Return root pins whose location falls within ``radius_km`` of ``point``, closest first.

        Used by the pin detail page's "Nearby Pins" map layer to find a
        profile's other pins around the one being viewed.

        Args:
            point: PostGIS point to measure distance from.
            radius_km: Search radius in kilometers.

        Returns:
            Root pins ordered nearest-first, annotated with ``distance`` (a
            ``django.contrib.gis.measure.Distance``).
        """
        return self.root_pins().filter(location__point__distance_lte=(point, D(km=radius_km))).annotate(distance=Distance("location__point", point)).order_by("distance")

    def within_bounds(self, south: float, west: float, north: float, east: float) -> Self:
        """Return pins whose location falls within a lat/lng bounding box.

        Used by the pin-list sidebar to scope its results to the map's
        current viewport. Same ``Polygon.from_bbox`` + ``location__point__within``
        idiom as ``MapController.map_pins_json``'s inline bbox handling and
        ``filter_by_criteria``'s ``include_regions`` support.

        Args:
            south: Southern (minimum) latitude.
            west: Western edge longitude. May exceed +/-180 (Leaflet reports
                unwrapped bounds when the map is panned across the date line)
                and may be greater than ``east`` when the viewport crosses it.
            north: Northern (maximum) latitude.
            east: Eastern edge longitude, same caveats as ``west``.

        Returns:
            This queryset filtered to pins within the box.
        """
        from django.contrib.gis.geos import Polygon

        from urbanlens.dashboard.services.geo.longitude import normalize_longitude

        west = normalize_longitude(west)
        east = normalize_longitude(east)

        def box(west_edge: float, east_edge: float) -> Polygon:
            bbox = Polygon.from_bbox((west_edge, south, east_edge, north))
            bbox.srid = 4326
            return bbox

        if west > east:
            # The viewport crosses the antimeridian. One box from these edges
            # spans the *long* way round - 358 degrees for a 2-degree window -
            # so it excludes everything on screen and includes everything else.
            # Query the two real halves instead.
            return self.filter(Q(location__point__within=box(west, 180.0)) | Q(location__point__within=box(-180.0, east)))
        return self.filter(location__point__within=box(west, east))

    def by_tag(self, tag_id: int) -> Self:
        """Filter pins that have this tag or any of its descendant tags."""
        from urbanlens.dashboard.models.labels.model import Label

        tag_ids = Label.get_label_and_descendants(tag_id)
        return self.filter(labels__id__in=tag_ids).distinct()

    def apply_label_groups(self, groups: list[dict]) -> Self:
        """Apply structured label filter groups returned by ``SearchForm.parse_label_groups()``.

        Args:
            groups: List of ``{"op": "and"|"or"|"not", "ids": [int, ...]}``.

        Returns:
            Filtered QuerySet (not yet distinct - caller must call ``.distinct()``).
        """
        from urbanlens.dashboard.models.labels.model import Label as _Label

        qs = self
        for group in groups:
            op = group.get("op")
            ids = group.get("ids", [])
            if not ids:
                continue
            if op == "and":
                for bid in ids:
                    expanded = _Label.get_label_and_descendants(bid)
                    qs = qs.filter(labels__id__in=expanded)
            elif op == "or":
                or_q = Q()
                for bid in ids:
                    expanded = _Label.get_label_and_descendants(bid)
                    or_q |= Q(labels__id__in=expanded)
                qs = qs.filter(or_q)
            elif op == "not":
                for bid in ids:
                    expanded = _Label.get_label_and_descendants(bid)
                    qs = qs.exclude(labels__id__in=expanded)
        return qs

    def filter_by_criteria(self, criteria) -> Self:
        """Filter pins by the criteria dict produced by SearchForm.cleaned_data.

        Args:
            criteria: Dict with optional keys: name, status (list), tags (QuerySet),
                exclude_tags (QuerySet), label_groups (list of group dicts from
                ``SearchForm.parse_label_groups()``), min_rating (int), max_rating (int),
                has_visits ('yes'|'no'|''), min_priority (int), max_priority (int),
                min_danger (int), max_danger (int), min_vulnerability (int), max_vulnerability (int),
                created_after (date), created_before (date),
                visited_after (date), visited_before (date), overlapping_pins (bool),
                date_built_after (date), date_built_before (date),
                date_abandoned_after (date), date_abandoned_before (date),
                last_viewed_after (date), last_viewed_before (date),
                security_<field> (SecurityLevel value, one per SECURITY_FIELDS entry),
                has_links ('yes'|'no'|''), min_detail_pins (int), max_detail_pins (int),
                include_regions (MultiPolygon | None), exclude_regions (MultiPolygon | None).

        Returns:
            Filtered QuerySet (distinct).
        """
        qs = self
        if name := (criteria.get("name") or "").strip():
            qs = qs.filter(
                Q(name__icontains=name) | Q(location__official_name__icontains=name) | Q(location__wiki__name__icontains=name) | Q(aliases__name__icontains=name),
            )
        if label_statuses := criteria.get("status"):
            qs = qs.filter(labels__id__in=[s.id if hasattr(s, "id") else s for s in label_statuses])

        # Structured label_groups (from formula bar) supersedes legacy tags/exclude_tags.
        if label_groups := criteria.get("label_groups"):
            qs = qs.apply_label_groups(label_groups)
        else:
            if tags := criteria.get("tags"):
                from urbanlens.dashboard.models.labels.model import Label as _Label

                for label in tags:
                    label_ids = _Label.get_label_and_descendants(label.id)
                    qs = qs.filter(labels__id__in=label_ids)
            if exclude_tags := criteria.get("exclude_tags"):
                from urbanlens.dashboard.models.labels.model import Label as _Label

                for label in exclude_tags:
                    label_ids = _Label.get_label_and_descendants(label.id)
                    qs = qs.exclude(labels__id__in=label_ids)
        if (min_rating := criteria.get("min_rating")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                min_rating = int(min_rating)
                # 0 on the rating slider means "unrated" (there is no stored
                # Review with rating=0 - pin_edit.py deletes the review
                # instead of ever writing one), not a real floor to compare
                # against. Every pin, rated or not, already satisfies "0 or
                # unrated", so this is a no-op; only a real minimum (1-5)
                # actually restricts the result.
                if min_rating > 0:
                    qs = qs.filter(reviews__rating__gte=min_rating)
        if (max_rating := criteria.get("max_rating")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                max_rating = int(max_rating)
                # max_rating=0 asks for "unrated only" - reviews__rating__lte=0
                # would silently match nothing (no Review row ever has
                # rating=0), so that specific case needs isnull instead.
                if max_rating <= 0:
                    qs = qs.filter(reviews__isnull=True)
                else:
                    qs = qs.filter(reviews__rating__lte=max_rating)
        if has_visits := criteria.get("has_visits"):
            visited_q = Q(last_visited__isnull=False) | Q(labels__name="Visited", labels__kind="status")
            if has_visits == "yes":
                qs = qs.filter(visited_q)
            elif has_visits == "no":
                qs = qs.exclude(visited_q)
        if visited_after := criteria.get("visited_after"):
            qs = qs.filter(last_visited__date__gte=visited_after)
        if visited_before := criteria.get("visited_before"):
            qs = qs.filter(last_visited__date__lte=visited_before)
        if (min_priority := criteria.get("min_priority")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(priority__gte=int(min_priority))
        if (max_priority := criteria.get("max_priority")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(priority__lte=int(max_priority))
        if (min_danger := criteria.get("min_danger")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(danger__gte=int(min_danger))
        if (max_danger := criteria.get("max_danger")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(danger__lte=int(max_danger))
        if (min_vulnerability := criteria.get("min_vulnerability")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(vulnerability__gte=int(min_vulnerability))
        if (max_vulnerability := criteria.get("max_vulnerability")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(vulnerability__lte=int(max_vulnerability))
        if created_after := criteria.get("created_after"):
            qs = qs.filter(created__date__gte=created_after)
        if created_before := criteria.get("created_before"):
            qs = qs.filter(created__date__lte=created_before)
        if date_built_after := criteria.get("date_built_after"):
            qs = qs.filter(date_built__gte=date_built_after)
        if date_built_before := criteria.get("date_built_before"):
            qs = qs.filter(date_built__lte=date_built_before)
        if date_abandoned_after := criteria.get("date_abandoned_after"):
            qs = qs.filter(date_abandoned__gte=date_abandoned_after)
        if date_abandoned_before := criteria.get("date_abandoned_before"):
            qs = qs.filter(date_abandoned__lte=date_abandoned_before)
        if last_viewed_after := criteria.get("last_viewed_after"):
            qs = qs.filter(last_viewed_at__date__gte=last_viewed_after)
        if last_viewed_before := criteria.get("last_viewed_before"):
            qs = qs.filter(last_viewed_at__date__lte=last_viewed_before)
        qs = qs.filter_by_security_indicators(criteria)
        if has_links := criteria.get("has_links"):
            from urbanlens.dashboard.models.links.model import PinLink

            link_exists = Exists(PinLink.objects.filter(pin=OuterRef("pk")))
            if has_links == "yes":
                qs = qs.filter(link_exists)
            elif has_links == "no":
                qs = qs.filter(~link_exists)
        min_detail_pins = criteria.get("min_detail_pins")
        max_detail_pins = criteria.get("max_detail_pins")
        if min_detail_pins is not None or max_detail_pins is not None:
            qs = qs.annotate(detail_pin_count=Count("detail_pins", distinct=True))
            if min_detail_pins is not None:
                with contextlib.suppress(ValueError, TypeError):
                    qs = qs.filter(detail_pin_count__gte=int(min_detail_pins))
            if max_detail_pins is not None:
                with contextlib.suppress(ValueError, TypeError):
                    qs = qs.filter(detail_pin_count__lte=int(max_detail_pins))
        if custom_fields := criteria.get("custom_fields"):
            qs = qs.filter_by_custom_fields(custom_fields)
        from urbanlens.dashboard.services.geo.longitude import split_at_antimeridian

        if include_regions := criteria.get("include_regions"):
            # Regions drawn across the date line arrive unwrapped (Leaflet gives
            # 179..181); stored points are folded, so a planar __within misses
            # everything east of the line until the region is split.
            qs = qs.filter(location__point__within=split_at_antimeridian(include_regions))
        if exclude_regions := criteria.get("exclude_regions"):
            qs = qs.exclude(location__point__within=split_at_antimeridian(exclude_regions))
        if criteria.get("overlapping_pins"):
            qs = qs.overlapping()
        return qs.distinct()

    def filter_by_custom_fields(self, custom_field_criteria) -> Self:
        """Filter pins by the owner's custom field values.

        Each criterion gets its own ``filter()`` call so it joins its own
        CustomFieldValue row (AND semantics across fields), while a number/date
        range stays within a single join so both bounds apply to the same value.

        Args:
            custom_field_criteria: List of dicts from
                ``SearchForm.parse_custom_field_criteria()``: each has ``field``
                plus ``contains`` (text/url), ``min``/``max`` (number),
                ``after``/``before`` (date), ``after_time``/``before_time``
                (time), ``equals`` (select), or ``checked`` (checkbox).

        Returns:
            Filtered QuerySet.
        """
        qs = self
        for criterion in custom_field_criteria:
            field = criterion.get("field")
            if field is None:
                continue
            if criterion.get("checked") is False:
                # "Unchecked" means not affirmatively checked: no stored value counts too.
                qs = qs.exclude(custom_field_values__field=field, custom_field_values__value_boolean=True)
                continue
            lookups: dict = {"custom_field_values__field": field}
            if contains := criterion.get("contains"):
                lookups["custom_field_values__value_text__icontains"] = contains
            if equals := criterion.get("equals"):
                lookups["custom_field_values__value_text"] = equals
            if criterion.get("checked") is True:
                lookups["custom_field_values__value_boolean"] = True
            if (minimum := criterion.get("min")) is not None:
                lookups["custom_field_values__value_number__gte"] = minimum
            if (maximum := criterion.get("max")) is not None:
                lookups["custom_field_values__value_number__lte"] = maximum
            if (after := criterion.get("after")) is not None:
                lookups["custom_field_values__value_date__gte"] = after
            if (before := criterion.get("before")) is not None:
                lookups["custom_field_values__value_date__lte"] = before
            if (after_time := criterion.get("after_time")) is not None:
                lookups["custom_field_values__value_time__gte"] = after_time
            if (before_time := criterion.get("before_time")) is not None:
                lookups["custom_field_values__value_time__lte"] = before_time
            if (ref_id := criterion.get("ref_id")) is not None:
                from urbanlens.dashboard.models.custom_fields.model import CustomFieldValue

                ref_attr = CustomFieldValue.REF_FIELD_BY_KIND.get(getattr(field, "reference_kind", ""))
                if ref_attr:
                    lookups[f"custom_field_values__{ref_attr}_id"] = ref_id
            if len(lookups) > 1:
                qs = qs.filter(**lookups)
        return qs

    def overlapping(self) -> Self:
        """Pins whose footprint overlaps another pin's footprint.

        A pin's "footprint" is its effective property boundary - a drawn or
        API-generated polygon when one exists, else a default circle centered
        on its coordinates (see ``BoundaryManager.effective_polygon_for_pin``).
        Every pin resolves to *some* footprint this way, so this doubles as a
        duplicate/stacked-pin detector (e.g. two pins left at the same
        coordinates by a bug get identical, fully-overlapping circles) as well
        as a real drawn-boundary overlap check.

        Both pins in every overlapping pair are included in the result.

        Returns:
            Pins (from this queryset) that overlap at least one other pin also
            in this queryset.
        """
        from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
        from urbanlens.dashboard.models.pin.model import Pin

        # Re-queried via the concrete Pin manager (rather than iterating `self`
        # directly) so effective_polygon_for_pin, which takes a concrete Pin,
        # type-checks - `self` here is still the generic PinQuerySet[_ModelT].
        # The select_related covers every relation resolve_for_pin's fallback
        # chain touches (own row -> place polygon -> parent -> wiki -> circle),
        # so resolving N pins doesn't lazily re-fetch each pin's location,
        # place, parent, and wiki as four extra queries per pin.
        pins = Pin.objects.filter(pk__in=self.values_list("pk", flat=True)).select_related("location__place", "parent_pin__location", "wiki")
        footprints = [(pin.pk, polygon) for pin in pins if (polygon := Boundary.objects.effective_polygon_for_pin(pin, BoundaryType.PROPERTY)) is not None]

        # Sweep over x-extents instead of comparing all pairs: real
        # collections are geographically spread out, and most footprints are
        # the default ~50m circles, so nearly every pair's bounding boxes are
        # disjoint - the O(n^2) polygon intersects this replaced spent almost
        # all its time proving that. Sorting by min-x and breaking when the
        # next candidate starts past this one's max-x keeps only genuinely
        # x-overlapping pairs; the y-extent check then filters the rest before
        # any real geometry runs.
        entries = sorted(((polygon.extent, pk, polygon) for pk, polygon in footprints), key=lambda entry: entry[0][0])
        overlapping_ids: set[int] = set()
        for i, ((_min_x, min_y, max_x, max_y), pk_a, polygon_a) in enumerate(entries):
            for (other_min_x, other_min_y, _other_max_x, other_max_y), pk_b, polygon_b in entries[i + 1 :]:
                if other_min_x > max_x:
                    break
                if other_min_y > max_y or other_max_y < min_y:
                    continue
                if polygon_a.intersects(polygon_b):
                    overlapping_ids.add(pk_a)
                    overlapping_ids.add(pk_b)

        return self.filter(pk__in=overlapping_ids)

    def rated(self, rating) -> Self:
        """
        Filters pins by the review.rating field
        """
        return self.filter(reviews__rating=rating)

    def rated_over(self, rating) -> Self:
        """
        Filters pins by the review.rating field
        """
        return self.filter(reviews__rating__gte=rating)

    def rated_under(self, rating) -> Self:
        """
        Filters pins by the review.rating field
        """
        return self.filter(reviews__rating__lte=rating)


class PinManager(abstract.PublicDashboardManager.from_queryset(PinQuerySet)):
    """Manager for Pin. Use get_nearby_or_create to avoid duplicate pins for the same profile+location."""

    def get_nearby_or_create(self, latitude, longitude, profile, threshold_meters=50, defaults=None):
        """
        Get or create a Pin instance, considering two pins the same if they are within a certain distance threshold.

        Args:
            latitude (float): Latitude of the pin.
            longitude (float): Longitude of the pin.
            profile (Profile): The profile associated with the pin.
            threshold_meters (float): Distance threshold in meters for considering pins as the same.
            defaults (dict, optional): Defaults to use for object creation.

        Returns:
            (Pin, bool): Tuple of (Pin instance, created boolean)

        """
        if latitude is None or longitude is None:
            logger.warning("get_nearby_or_create called with None coordinates, skipping.")
            return None, False

        try:
            lat_f, lon_f = float(latitude), float(longitude)
        except (TypeError, ValueError):
            logger.warning(
                "get_nearby_or_create called with non-numeric coordinates (type %s, %s -> %s, %s), skipping.",
                type(latitude),
                type(longitude),
                redact_coordinate(latitude),
                redact_coordinate(longitude),
            )
            return None, False

        if math.isnan(lat_f) or math.isnan(lon_f) or math.isinf(lat_f) or math.isinf(lon_f):
            logger.warning(
                "get_nearby_or_create called with invalid coordinates (type %s, %s -> %s, %s), skipping.",
                type(latitude),
                type(longitude),
                redact_coordinate(latitude),
                redact_coordinate(longitude),
            )
            return None, False

        latitude, longitude = lat_f, lon_f

        defaults = dict(defaults or {})
        # Coordinates live on the Location; drop any legacy coord kwargs.
        # `profile` is already an explicit argument, but the import parsers
        # embed it in their pin dicts too - drop it so create() below doesn't
        # receive it twice.
        for redundant in ("latitude", "longitude", "point", "profile"):
            defaults.pop(redundant, None)

        # A Pin no longer stores its own coordinates: it references a shared
        # Location (deduped by coordinates). Callers may pass a specific
        # Location via defaults; otherwise resolve/create one from the
        # coordinates, then find-or-create the profile's root pin for it.
        location = defaults.pop("location", None)
        if location is None:
            from urbanlens.dashboard.models.location.model import Location

            location, _ = Location.objects.get_nearby_or_create(latitude, longitude, threshold_meters=threshold_meters)

        # Not scoped to root pins: the profile may already have a child (sub) pin
        # at this exact Location (e.g. one merged under another pin earlier), and
        # that must dedupe too, not just root pins. Root pins sort first when both
        # exist, since that's the more useful merge target and matches prior
        # behavior for the common case.
        existing_pin = self.filter(location=location, profile=profile).order_by(F("parent_pin_id").asc(nulls_first=True)).first()
        if existing_pin is not None:
            return existing_pin, False

        try:
            pin = self.create(location=location, profile=profile, **defaults)
        except IntegrityError:
            # A concurrent request created this profile's pin at this Location between
            # the existence check above and this insert (db_pin_unique_location_per_profile)
            # - return that row instead of letting the race surface as an unhandled 500.
            existing_pin = self.filter(location=location, profile=profile).order_by(F("parent_pin_id").asc(nulls_first=True)).first()
            if existing_pin is not None:
                return existing_pin, False
            raise

        # Return the new pin and True for 'created'
        return pin, True
