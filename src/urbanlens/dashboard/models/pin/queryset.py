# Generic imports
from __future__ import annotations

import contextlib
import logging
import math
from math import atan2, cos, radians, sin, sqrt
from typing import TYPE_CHECKING, Self

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db.models import F, Q
from django.utils import timezone

# App Imports
from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.services.redact import redact_coordinate

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

    def visited_without_record(self) -> Self:
        """Return top-level pins marked visited that have no dated PinVisit record.

        A pin counts as "visited" when it either has a ``last_visited`` timestamp
        or carries the profile's "Visited" status badge - mirroring the
        ``has_visits`` filter used elsewhere. Such a pin can still lack any
        ``PinVisit`` row (e.g. imported pins, or a status set by hand), leaving a
        gap the Memories page surfaces so the user can log a concrete, dated visit.
        Pins the user dismissed from that queue are excluded.

        Returns:
            Distinct top-level pins that are marked visited but have zero rows in
            their ``visit_history``.
        """
        visited_q = Q(last_visited__isnull=False) | Q(badges__name="Visited", badges__kind="status")
        return self.root_pins().filter(visited_q).filter(visit_history__isnull=True).exclude(unlogged_visit_dismissed=True).distinct()

    def not_visited_this_year(self):
        return self.filter(last_visited__year__lt=timezone.now().year)

    def by_category(self, category):
        return self.filter(badges__name=category, badges__kind="category")

    def by_priority(self, priority):
        return self.filter(priority=priority)

    def by_latitude(self, latitude):
        # A Pin's coordinates live on its Location.
        return self.filter(location__latitude=latitude)

    def by_longitude(self, longitude):
        return self.filter(location__longitude=longitude)

    def by_name(self, name):
        return self.filter(name__icontains=name)

    def by_profile(self, profile):
        return self.filter(profile=profile)

    def by_created_year(self, year):
        return self.filter(created__year=year)

    def by_updated_year(self, year):
        return self.filter(updated__year=year)

    def nearby_pins(self, latitude, longitude, radius):

        R = 6371  # radius of the Earth in km
        lat1 = radians(latitude)
        lon1 = radians(longitude)
        lat2 = radians(F("location__latitude"))
        lon2 = radians(F("location__longitude"))
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        distance = R * c
        return self.filter(distance__lte=distance)

    def by_tag(self, tag_id: int) -> Self:
        """Filter pins that have this tag or any of its descendant tags."""
        from urbanlens.dashboard.models.badges.model import Badge

        tag_ids = Badge.get_badge_and_descendants(tag_id)
        return self.filter(badges__id__in=tag_ids).distinct()

    def apply_badge_groups(self, groups: list[dict]) -> Self:
        """Apply structured badge filter groups returned by ``SearchForm.parse_badge_groups()``.

        Args:
            groups: List of ``{"op": "and"|"or"|"not", "ids": [int, ...]}``.

        Returns:
            Filtered QuerySet (not yet distinct - caller must call ``.distinct()``).
        """
        from urbanlens.dashboard.models.badges.model import Badge as _Badge

        qs = self
        for group in groups:
            op = group.get("op")
            ids = group.get("ids", [])
            if not ids:
                continue
            if op == "and":
                for bid in ids:
                    expanded = _Badge.get_badge_and_descendants(bid)
                    qs = qs.filter(badges__id__in=expanded)
            elif op == "or":
                or_q = Q()
                for bid in ids:
                    expanded = _Badge.get_badge_and_descendants(bid)
                    or_q |= Q(badges__id__in=expanded)
                qs = qs.filter(or_q)
            elif op == "not":
                for bid in ids:
                    expanded = _Badge.get_badge_and_descendants(bid)
                    qs = qs.exclude(badges__id__in=expanded)
        return qs

    def filter_by_criteria(self, criteria) -> Self:
        """Filter pins by the criteria dict produced by SearchForm.cleaned_data.

        Args:
            criteria: Dict with optional keys: name, status (list), tags (QuerySet),
                exclude_tags (QuerySet), badge_groups (list of group dicts from
                ``SearchForm.parse_badge_groups()``), min_rating (int), max_rating (int),
                has_visits ('yes'|'no'|''), min_priority (int), max_priority (int),
                min_danger (int), max_danger (int), min_vulnerability (int), max_vulnerability (int),
                created_after (date), created_before (date),
                visited_after (date), visited_before (date), overlapping_pins (bool).

        Returns:
            Filtered QuerySet (distinct).
        """
        qs = self
        if name := (criteria.get("name") or "").strip():
            qs = qs.filter(
                Q(name__icontains=name) | Q(location__official_name__icontains=name) | Q(location__wiki__name__icontains=name) | Q(aliases__name__icontains=name),
            )
        if badge_statuses := criteria.get("status"):
            qs = qs.filter(badges__id__in=[s.id if hasattr(s, "id") else s for s in badge_statuses])

        # Structured badge_groups (from formula bar) supersedes legacy tags/exclude_tags.
        if badge_groups := criteria.get("badge_groups"):
            qs = qs.apply_badge_groups(badge_groups)
        else:
            if tags := criteria.get("tags"):
                from urbanlens.dashboard.models.badges.model import Badge as _Badge

                for badge in tags:
                    badge_ids = _Badge.get_badge_and_descendants(badge.id)
                    qs = qs.filter(badges__id__in=badge_ids)
            if exclude_tags := criteria.get("exclude_tags"):
                from urbanlens.dashboard.models.badges.model import Badge as _Badge

                for badge in exclude_tags:
                    badge_ids = _Badge.get_badge_and_descendants(badge.id)
                    qs = qs.exclude(badges__id__in=badge_ids)
        if min_rating := criteria.get("min_rating"):
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(reviews__rating__gte=int(min_rating))
        if max_rating := criteria.get("max_rating"):
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(reviews__rating__lte=int(max_rating))
        if has_visits := criteria.get("has_visits"):
            visited_q = Q(last_visited__isnull=False) | Q(badges__name="Visited", badges__kind="status")
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
        if min_danger := criteria.get("min_danger"):
            with contextlib.suppress(ValueError, TypeError):
                qs = qs.filter(danger__gte=int(min_danger))
        if max_danger := criteria.get("max_danger"):
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
        if custom_fields := criteria.get("custom_fields"):
            qs = qs.filter_by_custom_fields(custom_fields)
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
                plus ``contains`` (text), ``min``/``max`` (number), or
                ``after``/``before`` (date).

        Returns:
            Filtered QuerySet.
        """
        qs = self
        for criterion in custom_field_criteria:
            field = criterion.get("field")
            if field is None:
                continue
            lookups: dict = {"custom_field_values__field": field}
            if contains := criterion.get("contains"):
                lookups["custom_field_values__value_text__icontains"] = contains
            if (minimum := criterion.get("min")) is not None:
                lookups["custom_field_values__value_number__gte"] = minimum
            if (maximum := criterion.get("max")) is not None:
                lookups["custom_field_values__value_number__lte"] = maximum
            if (after := criterion.get("after")) is not None:
                lookups["custom_field_values__value_date__gte"] = after
            if (before := criterion.get("before")) is not None:
                lookups["custom_field_values__value_date__lte"] = before
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
        pins = Pin.objects.filter(pk__in=self.values_list("pk", flat=True)).select_related("location")
        footprints = [(pin.pk, polygon) for pin in pins if (polygon := Boundary.objects.effective_polygon_for_pin(pin, BoundaryType.PROPERTY)) is not None]

        overlapping_ids: set[int] = set()
        for i, (pk_a, polygon_a) in enumerate(footprints):
            for pk_b, polygon_b in footprints[i + 1 :]:
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
        for legacy in ("latitude", "longitude", "point"):
            defaults.pop(legacy, None)

        # A Pin no longer stores its own coordinates: it references a shared
        # Location (deduped by coordinates). Callers may pass a specific
        # Location via defaults; otherwise resolve/create one from the
        # coordinates, then find-or-create the profile's root pin for it.
        location = defaults.pop("location", None)
        if location is None:
            from urbanlens.dashboard.models.location.model import Location

            location, _ = Location.objects.get_nearby_or_create(latitude, longitude, threshold_meters=threshold_meters)

        existing_pin = self.filter(
            location=location,
            profile=profile,
            parent_pin__isnull=True,
        ).first()
        if existing_pin is not None:
            return existing_pin, False

        pin = self.create(location=location, profile=profile, **defaults)

        # Return the new pin and True for 'created'
        return pin, True
