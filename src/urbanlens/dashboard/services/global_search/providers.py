"""Per-entity search providers for global search.

Each provider owns one result type: it knows how to scope its queryset to
content the requesting user actually has access to, which text fields to
match, how to apply the parsed date/place filters, and how to turn a row into
a rendered :class:`~urbanlens.dashboard.services.global_search.results.SearchResult`.

Typo tolerance comes from PostgreSQL trigram similarity on each provider's
primary name field (pg_trgm, installed by migration 0022), OR-ed with plain
``icontains`` term matching across all searchable fields.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING, ClassVar

from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import Q
from django.urls import reverse

from urbanlens.dashboard.services.global_search.results import SearchResult, excerpt

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.global_search.parser import ParsedQuery

logger = logging.getLogger(__name__)

#: Trigram similarity threshold; below this a fuzzy-only match is noise.
FUZZY_THRESHOLD = 0.25

#: Location sub-fields checked when the query names a place ("in Cincinnati").
_PLACE_FIELDS = (
    "locality",
    "administrative_area_level_1",
    "administrative_area_level_2",
    "administrative_area_level_3",
    "country",
    "route",
    "official_name",
)


def term_filter(terms: list[str], fields: list[str]) -> Q:
    """Build the standard text predicate: every term in at least one field.

    Args:
        terms: Lowercased search terms (AND-ed together).
        fields: ORM field paths each term may appear in (OR-ed together).

    Returns:
        The combined Q object; empty Q when ``terms`` is empty.
    """
    combined = Q()
    for term in terms:
        term_q = Q()
        for field_path in fields:
            term_q |= Q(**{f"{field_path}__icontains": term})
        combined &= term_q
    return combined


def place_filter(location_path: str, place: str) -> Q:
    """Build a predicate matching a place name against Location address fields.

    Args:
        location_path: ORM path prefix to the Location relation (e.g.
            ``"location"`` or ``"pin__location"``).
        place: The place name parsed from the query.

    Returns:
        Q OR-ing the place over locality/state/county/country/street/name.
    """
    combined = Q()
    for field_name in _PLACE_FIELDS:
        combined |= Q(**{f"{location_path}__{field_name}__icontains": place})
    return combined


def date_range_filter(field_path: str, parsed: ParsedQuery) -> Q:
    """Build an inclusive date-range predicate for a DateTimeField.

    Args:
        field_path: ORM path to the datetime field.
        parsed: The parsed query carrying date_start/date_end.

    Returns:
        Q constraining the field's date to the parsed range; empty Q when the
        query has no date range.
    """
    if not (parsed.date_start and parsed.date_end):
        return Q()
    return Q(**{f"{field_path}__date__gte": parsed.date_start, f"{field_path}__date__lte": parsed.date_end})


class SearchProvider(ABC):
    """One result type's search implementation.

    Attributes:
        slug: The RESULT_TYPES slug this provider serves.
        fuzzy_field: Model field trigram similarity is computed against; when
            empty the provider matches with ``icontains`` only.
    """

    slug: ClassVar[str] = ""
    fuzzy_field: ClassVar[str] = ""

    @abstractmethod
    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        """Run this provider's search.

        Args:
            profile: The requesting user's profile; results must be scoped to
                content this profile owns or has direct access to.
            parsed: The structured query.
            limit: Maximum number of results to return.

        Returns:
            Results ordered most relevant first.
        """
        raise NotImplementedError

    def apply_text(self, queryset: QuerySet, parsed: ParsedQuery, fields: list[str]) -> QuerySet:
        """Apply term matching plus fuzzy title matching and relevance ordering.

        With no free-text terms (a purely structured query like "photos from
        last summer") the queryset is left unfiltered, ordered newest-first by
        ``created``.

        Args:
            queryset: The access-scoped queryset.
            parsed: The structured query.
            fields: ORM field paths for exact (icontains) term matching.

        Returns:
            Filtered queryset annotated with ``search_sim`` when fuzzy
            matching applies, ordered most relevant first.
        """
        if not parsed.terms:
            return queryset.order_by("-created")
        text_q = term_filter(parsed.terms, fields)
        if self.fuzzy_field:
            queryset = queryset.annotate(search_sim=TrigramSimilarity(self.fuzzy_field, parsed.text))
            return queryset.filter(text_q | Q(search_sim__gt=FUZZY_THRESHOLD)).order_by("-search_sim", "-created")
        return queryset.filter(text_q).order_by("-created")

    @staticmethod
    def score_of(obj: object) -> float:
        """Relevance score for an ORM row (its trigram annotation when present)."""
        value = getattr(obj, "search_sim", None)
        try:
            return float(value) if value is not None else 0.5
        except (TypeError, ValueError):
            return 0.5


class PinSearchProvider(SearchProvider):
    """The user's own pins: names, aliases, notes, badges, and place names."""

    slug = "pins"
    fuzzy_field = "name"

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.pin import Pin

        queryset = Pin.objects.filter(profile=profile).select_related("location__wiki", "cover_photo")
        if parsed.place:
            queryset = queryset.filter(place_filter("location", parsed.place))
        queryset = queryset.filter(date_range_filter("created", parsed))
        queryset = self.apply_text(
            queryset,
            parsed,
            [
                "name",
                "description",
                "aliases__name",
                "badges__name",
                "notes__text",
                "location__official_name",
                "location__wiki__name",
                "location__wiki__aliases__name",
            ],
        ).distinct()

        results = []
        for pin in queryset[:limit]:
            location = pin.location
            subtitle = location.display_name if location else ""
            image_url = None
            if pin.cover_photo is not None and pin.cover_photo.image:
                image_url = pin.cover_photo.image.url
            results.append(
                SearchResult(
                    type=self.slug,
                    title=pin.effective_name or "Unnamed pin",
                    url=reverse("pin.details", kwargs={"pin_slug": pin.slug or str(pin.uuid)}),
                    subtitle=subtitle,
                    snippet=excerpt(pin.description, parsed.terms),
                    image_url=image_url,
                    date=pin.created,
                    score=self.score_of(pin),
                ),
            )
        return results


class PhotoSearchProvider(SearchProvider):
    """Photos the user can see: own uploads plus images on their pins/pinned places.

    Matches captions, attribution, plugin-generated keywords, and the names of
    the pin/place each photo belongs to. This includes media materialized from
    external providers (Yelp, Wikimedia, ...) for pins and wikis the user has
    access to.
    """

    slug = "photos"
    fuzzy_field = "caption"

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.images import Image

        queryset = (
            Image.objects.filter(
                Q(profile=profile) | Q(pin__profile=profile) | Q(location__pins__profile=profile),
            )
            .select_related("pin", "location__wiki", "profile")
            .exclude(image="")
        )
        if parsed.place:
            queryset = queryset.filter(place_filter("location", parsed.place))
        if parsed.date_start and parsed.date_end:
            # taken_at (EXIF capture time) when known, else upload time.
            queryset = queryset.filter(
                Q(taken_at__date__gte=parsed.date_start, taken_at__date__lte=parsed.date_end) | (Q(taken_at__isnull=True) & date_range_filter("created", parsed)),
            )
        queryset = self.apply_text(
            queryset,
            parsed,
            [
                "caption",
                "author",
                "keywords__keyword",
                "pin__name",
                "location__official_name",
                "location__wiki__name",
                "location__locality",
            ],
        ).distinct()

        results = []
        for image in queryset[:limit]:
            title = image.caption or (image.pin.effective_name if image.pin else None) or (image.location.display_name if image.location else None) or "Photo"
            if image.pin is not None:
                url = reverse("pin.details", kwargs={"pin_slug": image.pin.slug or str(image.pin.uuid)})
            elif image.location is not None and image.location.slug:
                url = reverse("location.wiki", kwargs={"location_slug": image.location.slug})
            else:
                url = reverse("memories.photos")
            subtitle_bits = []
            if image.location is not None and image.location.locality:
                subtitle_bits.append(image.location.locality)
            if image.taken_at:
                subtitle_bits.append(f"taken {image.taken_at:%b %Y}")
            results.append(
                SearchResult(
                    type=self.slug,
                    title=title,
                    url=url,
                    subtitle=" · ".join(subtitle_bits),
                    image_url=image.image.url if image.image else None,
                    date=image.taken_at or image.created,
                    score=self.score_of(image),
                ),
            )
        return results


class WikiSearchProvider(SearchProvider):
    """Community wikis the user has access to (pinned places or wikis they created)."""

    slug = "wikis"
    fuzzy_field = "name"

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.wiki import Wiki

        if not profile.community_enabled:
            return []
        queryset = Wiki.objects.filter(
            Q(location__pins__profile=profile) | Q(created_by=profile),
        ).select_related("location")
        if parsed.place:
            queryset = queryset.filter(place_filter("location", parsed.place))
        queryset = queryset.filter(date_range_filter("updated", parsed))
        queryset = self.apply_text(queryset, parsed, ["name", "description", "aliases__name"]).distinct()

        results = []
        for wiki in queryset[:limit]:
            location = wiki.location
            if location is None or not location.slug:
                continue
            results.append(
                SearchResult(
                    type=self.slug,
                    title=wiki.name or "Unnamed wiki",
                    url=reverse("location.wiki", kwargs={"location_slug": location.slug}),
                    subtitle=location.display_name or "",
                    snippet=excerpt(wiki.description, parsed.terms),
                    date=wiki.updated,
                    score=self.score_of(wiki),
                ),
            )
        return results


class TripSearchProvider(SearchProvider):
    """Trips the user created or is a member of, including activities and comments."""

    slug = "trips"
    fuzzy_field = "name"

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.trips import Trip

        queryset = Trip.objects.filter(Q(profiles=profile) | Q(creator=profile))
        if parsed.date_start and parsed.date_end:
            # A trip matches when its scheduled window overlaps the asked range.
            queryset = queryset.filter(start_date__lte=parsed.date_end).filter(
                Q(end_date__gte=parsed.date_start) | Q(end_date__isnull=True, start_date__gte=parsed.date_start),
            )
        queryset = self.apply_text(
            queryset,
            parsed,
            ["name", "description", "activities__title", "activities__notes", "comments__text"],
        ).distinct()

        results = []
        for trip in queryset[:limit]:
            subtitle = ""
            if trip.start_date:
                subtitle = f"{trip.start_date:%b %d, %Y}"
                if trip.end_date and trip.end_date != trip.start_date:
                    subtitle += f" - {trip.end_date:%b %d, %Y}"
            results.append(
                SearchResult(
                    type=self.slug,
                    title=trip.name,
                    url=reverse("trips.detail", kwargs={"trip_uuid": trip.uuid}),
                    subtitle=subtitle,
                    snippet=excerpt(trip.description, parsed.terms),
                    date=trip.updated,
                    score=self.score_of(trip),
                ),
            )
        return results


class VisitSearchProvider(SearchProvider):
    """The user's logged visits (notes plus the visited pin's name/place)."""

    slug = "visits"
    fuzzy_field = "pin__name"

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.visits import PinVisit

        queryset = PinVisit.objects.filter(pin__profile=profile).select_related("pin__location")
        if parsed.place:
            queryset = queryset.filter(place_filter("pin__location", parsed.place))
        queryset = queryset.filter(date_range_filter("visited_at", parsed))
        queryset = self.apply_text(
            queryset,
            parsed,
            ["notes", "pin__name", "pin__location__official_name", "pin__location__wiki__name"],
        ).distinct()

        results = []
        for visit in queryset.order_by("-visited_at")[:limit] if not parsed.terms else queryset[:limit]:
            pin = visit.pin
            results.append(
                SearchResult(
                    type=self.slug,
                    title=f"Visit to {pin.effective_name or 'a pin'}",
                    url=reverse("pin.details", kwargs={"pin_slug": pin.slug or str(pin.uuid)}),
                    subtitle=f"{visit.visited_at:%b %d, %Y}",
                    snippet=excerpt(visit.notes, parsed.terms),
                    date=visit.visited_at,
                    score=self.score_of(visit),
                ),
            )
        return results


class DirectMessageSearchProvider(SearchProvider):
    """The user's direct messages.

    Only plaintext bodies are searchable: end-to-end encrypted messages never
    reach the server in readable form, so they cannot be matched here.
    """

    slug = "messages"
    fuzzy_field = ""

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.direct_messages import DirectMessage

        if not parsed.terms:
            return []
        queryset = (
            DirectMessage.objects.filter(
                Q(sender=profile, deleted_by_sender_at__isnull=True) | Q(recipient=profile, deleted_by_recipient_at__isnull=True),
            )
            .exclude(body="")
            .select_related("sender__user", "recipient__user")
            .filter(date_range_filter("created", parsed))
        )
        queryset = self.apply_text(queryset, parsed, ["body"])

        results = []
        for message in queryset[:limit]:
            other = message.recipient if message.sender_id == profile.pk else message.sender
            direction = "To" if message.sender_id == profile.pk else "From"
            results.append(
                SearchResult(
                    type=self.slug,
                    title=f"{direction} {other.username}",
                    url=reverse("messages.conversation", kwargs={"profile_slug": other.ensure_slug()}),
                    subtitle=f"{message.created:%b %d, %Y}",
                    snippet=excerpt(message.body, parsed.terms),
                    date=message.created,
                    score=self.score_of(message),
                ),
            )
        return results


class MarkupMapSearchProvider(SearchProvider):
    """The user's markup maps: map titles plus text/labels drawn on any map."""

    slug = "maps"
    fuzzy_field = "title"

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.markup import MarkupMap, PinMarkup

        if not parsed.terms:
            return []
        maps_url = reverse("memories.maps")
        results: list[SearchResult] = []

        map_qs = self.apply_text(MarkupMap.objects.for_profile(profile), parsed, ["title"])
        for markup_map in map_qs[:limit]:
            results.append(
                SearchResult(
                    type=self.slug,
                    title=markup_map.title or "Untitled map",
                    url=maps_url,
                    subtitle=f"Updated {markup_map.updated:%b %d, %Y}",
                    date=markup_map.updated,
                    score=self.score_of(markup_map),
                ),
            )

        # Text drawn on maps (labels/annotations), on any host: standalone
        # markup maps, pin maps, or wiki maps.
        label_q = term_filter(parsed.terms, ["label"])
        markup_qs = PinMarkup.objects.for_profile(profile).exclude(label="").filter(label_q).select_related("parent_map", "parent_pin", "parent_wiki__location")
        seen_map_ids = {result.url for result in results}
        for markup in markup_qs[: limit * 2]:
            if markup.parent_pin is not None:
                url = reverse("pin.details", kwargs={"pin_slug": markup.parent_pin.slug or str(markup.parent_pin.uuid)})
                host = markup.parent_pin.effective_name or "a pin"
            elif markup.parent_wiki is not None and markup.parent_wiki.location is not None and markup.parent_wiki.location.slug:
                url = reverse("location.wiki", kwargs={"location_slug": markup.parent_wiki.location.slug})
                host = markup.parent_wiki.name or "a wiki"
            elif markup.parent_map is not None:
                url = maps_url
                host = markup.parent_map.title or "a markup map"
            else:
                continue
            key = f"{url}:{markup.label}"
            if key in seen_map_ids:
                continue
            seen_map_ids.add(key)
            results.append(
                SearchResult(
                    type=self.slug,
                    title=excerpt(markup.label, parsed.terms, radius=30) or "Map annotation",
                    url=url,
                    subtitle=f"Annotation on {host}",
                    icon="format_shapes",
                    date=markup.updated,
                    score=self.score_of(markup),
                ),
            )
            if len(results) >= limit:
                break
        return results[:limit]


class SafetySearchProvider(SearchProvider):
    """The user's safety check-ins: titles, plans, and their chat messages."""

    slug = "safety"
    fuzzy_field = "title"

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.safety import SafetyCheckin

        queryset = SafetyCheckin.objects.filter(profile=profile).filter(date_range_filter("checkin_by", parsed))
        queryset = self.apply_text(queryset, parsed, ["title", "plan_details", "messages__body"]).distinct()

        results = []
        for checkin in queryset[:limit]:
            results.append(
                SearchResult(
                    type=self.slug,
                    title=checkin.title,
                    url=reverse("safety.checkin.detail", kwargs={"checkin_slug": checkin.slug or str(checkin.uuid)}),
                    subtitle=f"{checkin.get_status_display()} · {checkin.checkin_by:%b %d, %Y}",
                    snippet=excerpt(checkin.plan_details, parsed.terms),
                    date=checkin.checkin_by,
                    score=self.score_of(checkin),
                ),
            )
        return results


class CommentSearchProvider(SearchProvider):
    """Comment threads the user participates in or hosts (pins, wikis, trips)."""

    slug = "comments"
    fuzzy_field = ""

    def search(self, profile: Profile, parsed: ParsedQuery, limit: int) -> list[SearchResult]:
        from urbanlens.dashboard.models.comments import Comment
        from urbanlens.dashboard.models.trips.model import TripComment

        if not parsed.terms:
            return []
        results: list[SearchResult] = []

        comment_qs = (
            Comment.objects.filter(
                Q(profile=profile) | Q(pin__profile=profile) | Q(wiki__location__pins__profile=profile),
            )
            .filter(term_filter(parsed.terms, ["text"]))
            .filter(date_range_filter("created", parsed))
            .select_related("pin", "wiki__location", "profile__user")
            .distinct()
            .order_by("-created")
        )
        for comment in comment_qs[:limit]:
            if comment.pin is not None:
                url = reverse("pin.details", kwargs={"pin_slug": comment.pin.slug or str(comment.pin.uuid)})
                host = comment.pin.effective_name or "a pin"
            elif comment.wiki is not None and comment.wiki.location is not None and comment.wiki.location.slug:
                url = reverse("location.wiki", kwargs={"location_slug": comment.wiki.location.slug})
                host = comment.wiki.name or "a wiki"
            else:
                continue
            results.append(
                SearchResult(
                    type=self.slug,
                    title=f"Comment on {host}",
                    url=url,
                    subtitle=f"{comment.profile.username} · {comment.created:%b %d, %Y}" if comment.profile else f"{comment.created:%b %d, %Y}",
                    snippet=excerpt(comment.text, parsed.terms),
                    date=comment.created,
                    score=self.score_of(comment),
                ),
            )

        trip_comment_qs = TripComment.objects.filter(trip__profiles=profile).filter(term_filter(parsed.terms, ["text"])).filter(date_range_filter("created", parsed)).select_related("trip", "author__user").distinct().order_by("-created")
        for comment in trip_comment_qs[: max(limit - len(results), 0)]:
            results.append(
                SearchResult(
                    type=self.slug,
                    title=f"Comment on {comment.trip.name}",
                    url=reverse("trips.detail", kwargs={"trip_uuid": comment.trip.uuid}),
                    subtitle=f"{comment.author.username} · {comment.created:%b %d, %Y}" if comment.author else f"{comment.created:%b %d, %Y}",
                    snippet=excerpt(comment.text, parsed.terms),
                    date=comment.created,
                    score=self.score_of(comment),
                ),
            )
        return results[:limit]


def default_providers() -> list[SearchProvider]:
    """The full provider chain, in the order sections render.

    Returns:
        Fresh provider instances (they are stateless, but new instances keep
        the engine trivially thread-safe).
    """
    return [
        PinSearchProvider(),
        PhotoSearchProvider(),
        WikiSearchProvider(),
        TripSearchProvider(),
        VisitSearchProvider(),
        DirectMessageSearchProvider(),
        MarkupMapSearchProvider(),
        SafetySearchProvider(),
        CommentSearchProvider(),
    ]
