from __future__ import annotations

import contextlib
import logging
import math
from typing import TYPE_CHECKING, Self

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.db import IntegrityError
from django.db.models import Count, Exists, F, OuterRef, Q

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.meta import KIND_STATUS
from urbanlens.dashboard.services.security.redact import redact_coordinate

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

logger = logging.getLogger(__name__)


class PinQuerySet(abstract.PublicDashboardQuerySet):
    """Pin filters over per-user data; join via location FK for place attributes."""

    def root_pins(self) -> Self:
        """Return only top-level pins (excludes personal detail pins)."""
        return self.filter(parent_pin__isnull=True)

    def with_cached_photos(self) -> Self:
        """Pins whose location already has a stored photo."""
        from urbanlens.dashboard.models.images.model import Image, MediaKind

        return self.filter(Exists(Image.objects.filter(location_id=OuterRef("location_id"), media_type=MediaKind.PHOTO)))

    def filter_by_security_indicators(self, criteria) -> Self:
        """Filter by exact match on each ``security_<field>`` criterion; ignores unset values."""
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
        """Include the full detail-pin subtree of each pin."""
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
        """Pins marked visited via timestamp or Visited status label."""
        visited_q = Q(last_visited__isnull=False) | Q(labels__name="Visited", labels__kind=KIND_STATUS)
        return self.filter(visited_q).distinct()

    def visited_without_record(self) -> Self:
        """Visited pins with no dated visit row, excluding dismissed ones."""
        return self.root_pins().visited().filter(visit_history__isnull=True).exclude(unlogged_visit_dismissed=True).distinct()

    def by_priority(self, priority):
        return self.filter(priority=priority)

    def by_name(self, name):
        return self.filter(name__icontains=name)

    def with_placeholder_names(self) -> Self:
        """Pins with a stored non-user name; callers check meaningfulness."""
        return self.filter(name_is_user_provided=False).exclude(name__isnull=True).exclude(name="").select_related("location__wiki")

    def by_profile(self, profile):
        return self.filter(profile=profile)

    def modified_since(self, since) -> Self:
        """Pins created or edited at or after ``since``."""
        return self.filter(updated__gte=since)

    def near_point(self, point: Point, radius_km: float) -> Self:
        """Root pins within ``radius_km`` of ``point``, closest first."""
        return self.root_pins().filter(location__point__distance_lte=(point, D(km=radius_km))).annotate(distance=Distance("location__point", point)).order_by("distance")

    def within_bounds(self, south: float, west: float, north: float, east: float) -> Self:
        """Pins whose location falls within a lat/lng box."""
        from django.contrib.gis.geos import Polygon

        from urbanlens.dashboard.services.geo.longitude import normalize_longitude

        west = normalize_longitude(west)
        east = normalize_longitude(east)

        def box(west_edge: float, east_edge: float) -> Polygon:
            bbox = Polygon.from_bbox((west_edge, south, east_edge, north))
            bbox.srid = 4326
            return bbox

        if west > east:
            # Split antimeridian-crossing viewports into two queries.
            return self.filter(Q(location__point__within=box(west, 180.0)) | Q(location__point__within=box(-180.0, east)))
        return self.filter(location__point__within=box(west, east))

    def by_tag(self, tag_id: int) -> Self:
        """Filter pins that have this tag or any of its descendant tags."""
        from urbanlens.dashboard.models.labels.model import Label

        tag_ids = Label.get_label_and_descendants(tag_id)
        return self.filter(labels__id__in=tag_ids).distinct()

    def apply_label_groups(self, groups: list[dict]) -> Self:
        """Apply structured label filter groups; caller must call ``distinct()``."""
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
                # "or" group may also carry a priority threshold.
                if (floor := group.get("min_priority")) is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        or_q |= Q(priority__gte=int(floor))
                qs = qs.filter(or_q)
            elif op == "not":
                for bid in ids:
                    expanded = _Label.get_label_and_descendants(bid)
                    qs = qs.exclude(labels__id__in=expanded)
        return qs

    def filter_by_criteria(self, criteria) -> Self:
        """Filter pins by a ``SearchForm`` criteria dict."""
        qs = self
        if name := (criteria.get("name") or "").strip():
            qs = qs.filter(
                Q(name__icontains=name) | Q(location__official_name__icontains=name) | Q(location__wiki__name__icontains=name) | Q(aliases__name__icontains=name),
            )
        if label_statuses := criteria.get("status"):
            qs = qs.filter(labels__id__in=[s.id if hasattr(s, "id") else s for s in label_statuses])

        # Structured label_groups supersedes legacy tags/exclude_tags.
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
                # 0 means unrated, so only a real minimum restricts the result.
                if min_rating > 0:
                    qs = qs.filter(reviews__rating__gte=min_rating)
        if (max_rating := criteria.get("max_rating")) is not None:
            with contextlib.suppress(ValueError, TypeError):
                max_rating = int(max_rating)
                # 0 means unrated only.
                if max_rating <= 0:
                    qs = qs.filter(reviews__isnull=True)
                else:
                    qs = qs.filter(reviews__rating__lte=max_rating)
        if has_visits := criteria.get("has_visits"):
            visited_q = Q(last_visited__isnull=False) | Q(labels__name="Visited", labels__kind=KIND_STATUS)
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
            qs = qs.filter(location__point__within=split_at_antimeridian(include_regions))
        if exclude_regions := criteria.get("exclude_regions"):
            qs = qs.exclude(location__point__within=split_at_antimeridian(exclude_regions))
        if criteria.get("overlapping_pins"):
            qs = qs.overlapping()
        return qs.distinct()

    def filter_by_custom_fields(self, custom_field_criteria) -> Self:
        """Filter pins by the owner's custom field values."""
        qs = self
        for criterion in custom_field_criteria:
            field = criterion.get("field")
            if field is None:
                continue
            if criterion.get("checked") is False:
                # Unchecked includes rows with no stored value.
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
        """Pins whose footprint overlaps another pin in this queryset."""
        from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
        from urbanlens.dashboard.models.pin.model import Pin

        # Re-queried via the concrete manager so the polygon helper type-checks.
        pins = Pin.objects.filter(pk__in=self.values_list("pk", flat=True)).select_related("location__place", "parent_pin__location", "wiki")
        footprints = [(pin.pk, polygon) for pin in pins if (polygon := Boundary.objects.effective_polygon_for_pin(pin, BoundaryType.PROPERTY)) is not None]

        # Sweep over x-extents to avoid pairwise geometry checks.
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
        return self.filter(reviews__rating=rating).distinct()

    def rated_over(self, rating) -> Self:
        """
        Filters pins by the review.rating field
        """
        return self.filter(reviews__rating__gte=rating).distinct()

    def rated_under(self, rating) -> Self:
        """
        Filters pins by the review.rating field
        """
        return self.filter(reviews__rating__lte=rating).distinct()


class PinManager(abstract.PublicDashboardManager.from_queryset(PinQuerySet)):
    """Manager for Pin."""

    def get_nearby_or_create(self, latitude, longitude, profile, threshold_meters=50, defaults=None):
        """Get or create a pin, treating nearby coordinates as the same place."""
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
        # Coordinates live on Location; drop legacy kwargs to avoid duplicates.
        for redundant in ("latitude", "longitude", "point", "profile"):
            defaults.pop(redundant, None)

        # Pins reference a shared Location; resolve one when not given.
        location = defaults.pop("location", None)
        if location is None:
            from urbanlens.dashboard.models.location.model import Location

            location, _ = Location.objects.get_nearby_or_create(latitude, longitude, threshold_meters=threshold_meters)

        # Include child pins when deduping; prefer root pins as merge target.
        existing_pin = self.filter(location=location, profile=profile).order_by(F("parent_pin_id").asc(nulls_first=True)).first()
        if existing_pin is not None:
            return existing_pin, False

        try:
            pin = self.create(location=location, profile=profile, **defaults)
        except IntegrityError:
            # Concurrent insert won the race; return that row.
            existing_pin = self.filter(location=location, profile=profile).order_by(F("parent_pin_id").asc(nulls_first=True)).first()
            if existing_pin is not None:
                return existing_pin, False
            raise

        return pin, True
