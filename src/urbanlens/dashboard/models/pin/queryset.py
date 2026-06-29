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

logger = logging.getLogger(__name__)


class PinQuerySet(abstract.QuerySet):
    """QuerySet for Pin - the user-specific half of the place model.

    Filters here operate on per-user data (profile, visit history, status, priority).
    For filtering by place attributes (address, CID, canonical name) use LocationQuerySet
    or join through the location FK: Pin.objects.filter(location__name__icontains=...).
    """

    def root_pins(self) -> Self:
        """Return only top-level pins (excludes both personal and community detail pins)."""
        return self.filter(parent_pin__isnull=True, parent_location__isnull=True)

    def detail_pins(self) -> Self:
        """Return only personal detail pins (sub-markers owned by a user's pin)."""
        return self.filter(parent_pin__isnull=False)

    def location_detail_pins(self) -> Self:
        """Return only community detail pins (attached directly to a Location for the wiki)."""
        return self.filter(parent_location__isnull=False, parent_pin__isnull=True)

    def never_visited(self):
        return self.filter(last_visited__isnull=True)

    def not_visited_this_year(self):
        return self.filter(last_visited__year__lt=timezone.now().year)

    def by_category(self, category):
        return self.filter(badges__name=category, badges__kind="category")

    def by_priority(self, priority):
        return self.filter(priority=priority)

    def by_latitude(self, latitude):
        return self.filter(latitude=latitude)

    def by_longitude(self, longitude):
        return self.filter(longitude=longitude)

    def by_name(self, name):
        return self.filter(name__icontains=name)

    def by_profile(self, profile):
        return self.filter(profile=profile)

    def by_user(self, user):
        return self.filter(user=user)

    def by_created_year(self, year):
        return self.filter(created__year=year)

    def by_updated_year(self, year):
        return self.filter(updated__year=year)

    def nearby_pins(self, latitude, longitude, radius):

        R = 6371  # radius of the Earth in km
        lat1 = radians(latitude)
        lon1 = radians(longitude)
        lat2 = radians(F("latitude"))
        lon2 = radians(F("longitude"))
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
            Filtered QuerySet (not yet distinct — caller must call ``.distinct()``).
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
                visited_after (date), visited_before (date).

        Returns:
            Filtered QuerySet (distinct).
        """
        qs = self
        if name := (criteria.get("name") or "").strip():
            qs = qs.filter(
                Q(name__icontains=name) | Q(location__name__icontains=name) | Q(aliases__name__icontains=name),
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
        return qs.distinct()

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


class PinManager(abstract.Manager.from_queryset(PinQuerySet)):
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
                "get_nearby_or_create called with non-numeric coordinates (%s, %s), skipping.",
                latitude,
                longitude,
            )
            return None, False

        if math.isnan(lat_f) or math.isnan(lon_f) or math.isinf(lat_f) or math.isinf(lon_f):
            logger.warning(
                "get_nearby_or_create called with invalid coordinates (%s, %s), skipping.",
                latitude,
                longitude,
            )
            return None, False

        latitude, longitude = lat_f, lon_f

        point = Point(float(longitude), float(latitude), srid=4326)

        # Find existing pins within the threshold distance
        existing_pins = self.filter(
            point__distance_lte=(point, D(m=threshold_meters)),
            profile=profile,
        )

        if existing_pins.exists():
            # Return the first close enough pin and False for 'created'
            return existing_pins.first(), False

        # No existing pin found within the threshold, create a new one
        pin_data = {
            "latitude": latitude,
            "longitude": longitude,
            "profile": profile,
            "point": point,
            **(defaults or {}),
        }
        pin = self.create(**pin_data)

        # Return the new pin and True for 'created'
        return pin, True
