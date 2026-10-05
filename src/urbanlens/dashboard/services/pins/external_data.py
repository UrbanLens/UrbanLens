"""Background-fetch orchestration for the Private Pin page's external-data panels. * Each panel is described by a :class:`PanelSource` -- it knows how to check whether its data has already landed in its backing store (``is_ready``) and how to..."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import json
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.cache import cache

from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.core.gateway import UpstreamBusyError, is_source_outage
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError, RequestCancelledError, ServiceDisabledError
from urbanlens.dashboard.services.core.task_limits import SOFT_TIME_LIMIT_ERRORS
from urbanlens.dashboard.services.pins.search_names import SHARED_SCOPE, search_names

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence
    from datetime import datetime, timedelta

    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser

    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.subscriptions import SiteFeature
    from urbanlens.dashboard.services.apis.assets.base import MediaProvider
    from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, StreetViewProvider, StreetViewSlide
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.media.previews import GalleryUrls
    from urbanlens.dashboard.services.media.subject_relevance import MediaSubject
    from urbanlens.dashboard.services.pins.search_names import SearchNames, SearchScope

from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock
from urbanlens.dashboard.services.sandbox.queues import Queue

logger = logging.getLogger(__name__)

#: Seconds between HTMX/JS poll requests while a fetch task is in flight.
POLL_INTERVAL_SECONDS = 2
#: Polls before a panel gives up and disappears for this page view. The next
#: full page load starts a fresh poll cycle, so this only bounds how long one
#: page keeps asking, not how long the data takes to eventually land.
MAX_POLL_ATTEMPTS = 30
#: TTL for the single-flight marker. Must comfortably exceed the Celery task's
#: hard time limit so a killed task's marker expires right after the task does,
#: and a crashed worker can't wedge a panel for longer than this.
FLIGHT_TTL_SECONDS = 150
#: How long a source stays suppressed after its fetch failed unexpectedly.
FAILURE_SKIP_TTL_SECONDS = 300
#: How long a source stays suppressed after reporting itself rate-limited or
#: administratively disabled. Longer than the failure TTL: these are explicit
#: signals, not transient flakes.
DISABLED_SKIP_TTL_SECONDS = 1800
#: TTL for the satellite/street "caches are warm" marker. Deliberately shorter
#: than the 24h per-provider slide caches it summarises, so the marker always
#: expires (and re-warms via a task) before the underlying entries do.
SLIDES_READY_TTL_SECONDS = 12 * 3600


@dataclass(frozen=True, slots=True)
class ProviderFetchResult:
    """Outcome of one imagery provider inside a slide collector run.

    Attributes:
        service: The provider's service key (or class name when keyless).
        from_cache: Whether the provider's slides came from its Django cache.
        count: Number of slides the provider contributed.
        ok: False when the provider raised instead of returning slides.
    """

    service: str
    from_cache: bool
    count: int
    ok: bool = True


class PanelApiKind(StrEnum):
    """A read shape a panel's JSON body can take on the external API.
    That pairing is the whole point of the interface: a native client branches on the *kind*, never on the source key, so a panel contributed by a plugin written long after the client shipped still renders instead of being ignored as an unknown string."""

    INFO = "info"
    MEDIA = "media"
    BOUNDARY = "boundary"
    BUILDINGS = "buildings"


class PanelPlacement(StrEnum):
    """Where the Private Pin page renders an info panel."""

    #: A card of its own, loaded as the page scrolls to it.
    STANDALONE = "standalone"
    #: A tab in the Regional Data card: data about the area (county, watershed, air shed) rather than the site.
    REGIONAL = "regional"
    #: A tab in the Location Data card: data about this place itself.
    LOCATION = "location"
    #: A tab in the Property Records card: records of the parcel or building.
    PROPERTY = "property"


@dataclass(frozen=True, slots=True)
class OverviewSummary:
    """What one source contributes to its card's merged Overview tab.

    Attributes:
        heading_name: A name for the place; the first source to offer one wins.
        chips: Short kind labels, deduplicated across sources.
        fields: ``{"label", "value", "href"?}`` facts, merged without attribution.
        footer_link: ``{"url", "label"}`` for an external page about the place.
        notes: Sentences that stand on their own; each links to the source's own tab.
    """

    heading_name: str | None = None
    chips: list[str] = field(default_factory=list)
    fields: list[dict[str, str]] = field(default_factory=list)
    footer_link: dict[str, str] | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CachedEntry:
    """What a pin reads from one cache-backed source: its rows' payloads, combined.

    Attributes:
        data: The combined payload; ``{}`` means "we searched and found nothing".
        query_key: The queries that filled it, joined.
        rows: The rows it was combined from, the shared row first, for readers that judge each search on its own.
    """

    data: dict
    query_key: str
    rows: tuple[LocationCache, ...]


#: Keys of an ``InfoPanelSource.render_context`` result that carry panel *data* rather than template
#: plumbing (``nested``, and the ``section_id``/``icon``/ ``title``/``pin`` the dispatcher injects
#: afterwards).
#: The API's info card is built by copying this allowlist rather than by passing the context
_INFO_CONTEXT_DATA_KEYS = ("heading_name", "chips", "facts", "meta", "header_link", "footer_link")


def info_card(
    *,
    heading_name: str | None = None,
    chips: Sequence[str | None] | None = None,
    facts: Sequence[dict] | None = None,
    meta: Sequence[dict] | None = None,
    header_link: dict | None = None,
    footer_link: dict | None = None,
    image_url: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Build the :attr:`PanelApiKind.INFO` body, the one information-card contract.
    Having one constructor rather than each source hand-rolling a dict is what keeps that promise true: a client can lay out the card once and every panel, including ones added later, lands in it.

    Args:
        heading_name: Primary line - usually the place's name at this provider.
        chips: Short category/status pills (e.g. ``["Historic building"]``).
        facts: Icon-led quick facts, each ``{"icon", "text", "href"?}``.
        meta: Label/value rows, each ``{"label", "value", "href"?}``.
        header_link: ``{"url", "label"}`` for the card's header affordance.
        footer_link: ``{"url", "label"}`` for the card's "view on X" link.
        image_url: A single representative image, for the panels that have one (NPS park photos, Nominatim's ``image`` tag).
        description: A paragraph of prose, for the panels that have one.

    Returns:
        The info-card dict, ready to nest under the payload's ``"info"`` key."""
    return {
        "heading_name": heading_name or None,
        "chips": [chip for chip in (chips or []) if chip],
        "facts": list(facts or []),
        "meta": list(meta or []),
        "header_link": header_link or None,
        "footer_link": footer_link or None,
        "image_url": image_url or None,
        "description": description or None,
    }


def info_card_from_render_context(context: dict) -> dict[str, Any]:
    """Project an ``InfoPanelSource.render_context`` result onto the info-card contract.

    Args:
        context: A render context (see ``InfoPanelSource.render_context``).

    Returns:
        The equivalent :func:`info_card`, carrying only the allowlisted data keys - see :data:`_INFO_CONTEXT_DATA_KEYS` for why that is an allowlist and not a straight copy."""
    return info_card(**{key: context.get(key) for key in _INFO_CONTEXT_DATA_KEYS})


class PanelSource(ABC):
    """One external-data panel: readiness check plus Celery-side fetch.

    Attributes:
        key: Registry key; also the Celery task argument and log label.
        section_id: DOM id of the panel's section element (HTMX panels only).
        icon: Material symbol name for the pending placeholder's header.
        title: Heading text for the pending placeholder's header.
        outer_class: CSS classes for the pending placeholder's outer element.
        outer_is_card: True when the section element is itself the card (the
            satellite/street layout) rather than wrapping an inner card div.
        queue: Celery queue this source's fetch is dispatched to. Defaults to
            the dedicated ``panel_fetch`` queue (a high-concurrency thread
            pool - see docker-compose.yml's celery-worker-panels service),
            appropriate for the common case of "one or two small HTTP calls."
            Override to :attr:`Queue.INTERACTIVE` (prefork pool) for a source
            whose fetch does real CPU-bound work (e.g. Overture's
            GeoParquet/Shapely geometry parsing) - many of those running at
            once on a thread pool would cause GIL contention that slows down
            every other panel sharing it, defeating the point of splitting
            the queue in the first place. Not the bulk pool: somebody is
            looking at the panel while it loads.
        api_kinds: Which :class:`PanelApiKind` shapes this source can serve as
            JSON. Empty - the default - means "this panel is not exposed on
            the external API at all", and is the authoritative signal for
            that: a caller asking whether to advertise a panel checks
            ``api_kinds``, not whether ``api_payload`` happens to return None
            right now (which it also does whenever the data simply hasn't
            landed yet). See the module docstring for why the default is
            closed rather than a guess.
        required_feature: The :class:`SiteFeature` a viewer must hold for this
            source's data to be shown to them, or None when it is unrestricted
            (the overwhelming majority). Declared on the source rather than
            only at each call site so every surface - web tab strip, external
            API, anything added later - gates on the same fact instead of each
            re-deciding it and eventually disagreeing.
    """

    key: ClassVar[str]
    section_id: ClassVar[str] = ""
    icon: ClassVar[str] = "public"
    title: ClassVar[str] = ""
    outer_class: ClassVar[str] = ""
    outer_is_card: ClassVar[bool] = False
    queue: ClassVar[str] = Queue.PANEL_FETCH
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset()
    required_feature: ClassVar[SiteFeature | None] = None

    def scope(self, pin: Pin) -> str:
        """Cache-key scope identifying which rows/entries this pin's fetch fills.
        Location-scoped by default, because most panels cache per shared Location (two users pinning the same place share one fetch).

        Args:
            pin: The pin whose panel is being fetched.

        Returns:
            A short string unique to the fetch target.
        """
        return f"loc{pin.location_id}"

    def flight_key(self, pin: Pin) -> str:
        """Single-flight cache key for this source and pin's fetch target."""
        return f"ulfetch:flight:{self.key}:{self.scope(pin)}"

    def skip_key(self, pin: Pin) -> str:
        """Suppression cache key set after a failed/disabled fetch."""
        return f"ulfetch:skip:{self.key}:{self.scope(pin)}"

    def gate(self, pin: Pin) -> bool:
        """Whether this source has enough information to fetch for ``pin``.
        Checked before scheduling a fetch so a source with nothing to work with (e.g. no coordinates, no address, no name) degrades to a quiet 204 instead of polling forever.

        Args:
            pin: The pin whose panel is being rendered.

        Returns:
            True when a fetch is worth scheduling.
        """
        return True

    @abstractmethod
    def is_ready(self, pin: Pin) -> bool:
        """Whether the panel's data has already been fetched and persisted.

        Args:
            pin: The pin whose panel is being rendered.

        Returns:
            True when the controller can render directly from the store.
        """

    def has_landed(self, pin: Pin) -> bool:
        """Whether a fetch has answered for ``pin``, whether or not the answer has anything to show.

        Args:
            pin: The pin whose panel is being read.

        Returns:
            True when fetching again would only repeat the answer.
        """
        return self.is_ready(pin)

    @abstractmethod
    def fetch(self, pin: Pin) -> None:
        """Fetch from the upstream provider(s) and persist to the panel's store.
        Runs inside a Celery worker, never on the request path.

        Args:
            pin: The pin whose panel data should be fetched.
        """

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """This panel's already-landed data as a JSON body, or None.
        See the module docstring: this interface is reachable by third-party plugin code, so the failure mode of forgetting about it has to be an absent panel and not an unreviewed dump of whatever that plugin cached.

        Args:
            pin: The pin whose panel is being read. Sources that need the
                viewer's own scope (e.g. which child pins exist) read it from
                here rather than from a request, since this also runs from
                background code paths that have no request.

        Returns:
            A JSON-serializable body whose top-level keys are the source's declared :attr:`api_kinds` (see :class:`PanelApiKind`), or None when this source is not exposed, has no data yet, or has data that isn't worth showing (the JSON equivalent of the web panel's 204).
        """
        return None


class LocationCachePanelSource(PanelSource, ABC):
    """Base for panels whose store is a ``LocationCache`` row.

    Attributes:
        cache_source: The LocationCache ``source`` field value this panel reads and writes."""

    cache_source: ClassVar[str]

    #: When True, a fresh cache row is not enough to call this panel ready - its payload is inspected
    #: with :meth:`has_content` as well.
    #: Off by default because it costs bytes: the batched readiness query otherwise fetches only
    #: source names, and some payloads (boundary geometry, image lists) are large.
    inspects_content: ClassVar[bool] = False

    #: When True, the panel describes the site rather than the building: a pin nested under a site pin on
    #: another location takes the site's answer instead of asking the upstream about a point on the same property.
    site_level: ClassVar[bool] = False
    #: A nested pin farther than this from its site is not on it, whatever the user filed it under.
    SITE_RADIUS_METERS: ClassVar[float] = 1000.0
    #: How long this source's answer stays current, when that is shorter than the site-wide
    #: ``external_data_cache_days``: for data its upstream itself keeps for hours. None keeps the site-wide window.
    cache_max_age: ClassVar[timedelta | None] = None

    def fresh_since(self, since: datetime) -> datetime:
        """The oldest ``updated`` this source's rows may have and still be fresh.

        Args:
            since: The site-wide cutoff, ``LocationCache.fresh_since()``.

        Returns:
            The later of the two cutoffs.
        """
        if self.cache_max_age is None:
            return since
        from django.utils import timezone

        return max(since, timezone.now() - self.cache_max_age)

    def site_pin(self, pin: Pin) -> Pin | None:
        """The pin whose answer this one should share, when this panel is site-level and ``pin`` is nested.

        Args:
            pin: The pin whose panel is being fetched.

        Returns:
            The outermost ancestor, when it stands on another location; otherwise None.
        """
        return self.nesting_site(pin) if self.site_level else None

    def nesting_site(self, pin: Pin) -> Pin | None:
        """The outermost pin ``pin`` is nested under, when it stands on another location close enough to be the same site.

        Args:
            pin: A possibly nested pin.

        Returns:
            The root ancestor, or None for a root pin, one sharing its root's location, or one filed under a far-off root.
        """
        chain = pin.ancestor_chain()
        site = chain[-1] if chain else None
        if site is None or site.location_id is None or site.location_id == pin.location_id:
            return None
        from urbanlens.dashboard.services.geo.distance import haversine_meters

        apart = haversine_meters(float(pin.effective_latitude or 0), float(pin.effective_longitude or 0), float(site.effective_latitude or 0), float(site.effective_longitude or 0))
        return site if apart <= self.SITE_RADIUS_METERS else None

    def site_answer_covers(self, pin: Pin, data: dict) -> bool:
        """Whether the site's answer is also the answer for ``pin``.

        Args:
            pin: The nested pin.
            data: The site's payload.

        Returns:
            True by default: a site-level panel's answer is about the area.
        """
        return True

    def adopt_site_answer(self, pin: Pin) -> bool:
        """Copy the site's answer to ``pin``'s location, fetching it for the site first when there is none.

        Args:
            pin: The pin whose panel is being fetched.

        Returns:
            Whether this handled the fetch; False leaves ``pin`` to fetch for itself.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.core.coalesce import coalesced

        site = self.site_pin(pin)
        if site is None or pin.location is None or not self.gate(site):
            return False
        row = LocationCache.get_fresh(site.location, self.cache_source, max_age=self.cache_max_age)
        if row is None:
            # Every building on the site opens at once; one of them asks for the site.
            coalesced(f"ulfetch:site:{self.key}:loc{site.location_id}", lambda: self.fetch(site), ttl=FAILURE_SKIP_TTL_SECONDS)
            row = LocationCache.get_fresh(site.location, self.cache_source, max_age=self.cache_max_age)
        if row is None:
            return True
        if not self.site_answer_covers(pin, row.data):
            return False
        entry = LocationCache.set(pin.location, self.cache_source, row.data, query_key=row.query_key)
        # The copy is as old as the site's answer, so it goes stale when that does.
        LocationCache.objects.filter(pk=entry.pk).update(updated=row.updated)
        self.adopted(pin, row.data)
        return True

    def adopted(self, pin: Pin, data: dict) -> None:
        """Do whatever :meth:`fetch` does besides caching, for a pin that took its site's answer.

        Args:
            pin: The pin that took it.
            data: The site's payload.
        """

    def seed_descendants(self, site: Pin) -> int:
        """Write the answers ``site``'s cached payload already holds for the markers nested under it.

        Args:
            site: A pin whose nested markers may have just been created.

        Returns:
            How many nested locations were answered; 0 for a source whose site answer says nothing about its buildings.
        """
        return 0

    def has_content(self, data: dict | None) -> bool:
        """Whether a fetched payload has anything worth showing a tab for.
        Only consulted when :attr:`inspects_content` is set.

        Args:
            data: The cached payload, or None.

        Returns:
            True when a tab for this panel would render something.
        """
        return bool(data)

    def shows(self, pin: Pin, data: dict) -> bool:
        """Whether the panel's card has anything to show ``pin`` from a payload that has landed.

        Its view answers 204 when this is False, and the Private Pin page leaves out a card it already knows would
        (``services.pins.panel_probe``).

        Args:
            pin: The pin being viewed.
            data: The cached payload; ``{}`` when the fetch found nothing.

        Returns:
            :meth:`has_content`, by default.
        """
        return self.has_content(data)

    def always_shows(self, pin: Pin) -> bool:
        """Whether the card has something to show ``pin`` whatever this source's fetch finds, or whether it applies.

        Args:
            pin: The pin being viewed.

        Returns:
            False by default.
        """
        return False

    def overview_summary(self, pin: Pin, data: dict) -> OverviewSummary | None:
        """This source's cached data, summarized for the Overview tab of the card it is a tab in.

        Args:
            pin: The pin being viewed.
            data: The ``LocationCache`` row's ``data`` dict.

        Returns:
            The summary, or None when this source adds nothing to the Overview.
        """
        return None

    def search_scopes(self, pin: Pin, names: SearchNames | None = None) -> tuple[SearchScope, ...]:
        """The cached searches ``pin`` reads from this source, the shared one first.

        Args:
            pin: The pin whose panel is being read or fetched.
            names: ``search_names(pin)``, when the caller already has it.

        Returns:
            The one shared row, for a source whose fetch uses no names.
        """
        return (SHARED_SCOPE,)

    def merge_cached(self, payloads: Sequence[dict]) -> dict:
        """Combine the payloads of the rows a pin reads into one, the shared row's first.

        Args:
            payloads: One payload per :meth:`search_scopes` entry, in that order.

        Returns:
            The combined payload; for a single-row source, that row's.
        """
        return payloads[0] if payloads else {}

    def cached_entry(self, pin: Pin) -> CachedEntry | None:
        """What ``pin`` reads from this source, or None while any of its rows is missing or stale.

        Args:
            pin: The pin whose panel is being read.

        Returns:
            The combined entry.
        """
        return cached_entries(pin, [self])[self.key]

    def is_ready(self, pin: Pin) -> bool:
        """True when this source has something to show for ``pin``."""
        entry = self.cached_entry(pin)
        if entry is None:
            return False
        return self.has_content(entry.data) if self.inspects_content else True

    def has_landed(self, pin: Pin) -> bool:
        """True when a fresh answer is stored for ``pin``, including one with nothing to show."""
        return self.cached_entry(pin) is not None

    def cached_data(self, pin: Pin) -> dict | None:
        """This source's fresh cached payload, or None when nothing has landed.

        Args:
            pin: The pin whose panel is being read.

        Returns:
            The rows' combined ``data`` - possibly ``{}``, which means "we searched and found nothing", a real answer - or None when a row is missing (never fetched, or gone stale).
        """
        entry = self.cached_entry(pin)
        return None if entry is None else entry.data


class InfoPanelSource(LocationCachePanelSource, ABC):
    """Base for panels that render through the generic ``_simple_info_panel.html`` template.

    Attributes:
        placement: Where the Private Pin page renders the panel - see :class:`PanelPlacement`.
        tab_label: The tab's label when the panel is placed in a tabbed card; empty uses :attr:`title`.
        tab_order: Sort key among one card's tabs; ties keep plugin order.
        building_level: The panel describes one structure rather than the area, so a property page with the
            child-details toggle on shows it again inside each building child's card, for that child.
        shown_in: The key of another info panel that shows this one inside it, under this panel's title, in place
            of a tab or card of its own. Ignored while that panel is not registered or not visible to the viewer.
    """

    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO})
    placement: ClassVar[PanelPlacement] = PanelPlacement.STANDALONE
    tab_label: ClassVar[str] = ""
    tab_order: ClassVar[int] = 100
    building_level: ClassVar[bool] = False
    shown_in: ClassVar[str] = ""

    @property
    def label(self) -> str:
        """The panel's tab label."""
        return self.tab_label or self.title

    @abstractmethod
    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build ``_simple_info_panel.html``'s context from cached data.

        Args:
            pin: The pin whose panel is being rendered.
            data: The ``LocationCache`` row's ``data`` dict (``{}`` when the
                fetch found nothing).

        Returns:
            A context dict (may include ``heading_name``, ``chips``, ``meta``, ``header_link``, ``footer_link``), or None when there's nothing worth showing (renders a 204).
        """

    def shows(self, pin: Pin, data: dict) -> bool:
        """Whether :meth:`render_context` has anything for ``pin``, which is what the panel's view renders or 204s on."""
        return self.render_context(pin, data) is not None

    def debug_count(self, data: dict) -> int:
        """Item count reported in the debug overlay.

        Args:
            data: The ``LocationCache`` row's ``data`` dict.
        """
        return 1

    def api_info(self, pin: Pin, data: dict) -> dict[str, Any] | None:
        """This source's cached data as an :attr:`PanelApiKind.INFO` card.

        Args:
            pin: The pin whose panel is being read (``render_context`` may
                branch on it - see the CRIS plugin's site-scope handling).
            data: The ``LocationCache`` row's ``data`` dict.

        Returns:
            The info card, or None when ``render_context`` decided there is nothing worth showing.
        """
        context = self.render_context(pin, data)
        return None if context is None else info_card_from_render_context(context)

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The panel's cached data as ``{"info": {...}}``, or None."""
        data = self.cached_data(pin)
        if data is None:
            return None
        card = self.api_info(pin, data)
        return None if card is None else {PanelApiKind.INFO.value: card}


class CoordinateGatedInfoPanelSource(InfoPanelSource, ABC):
    """An ``InfoPanelSource`` that only makes sense when the pin has coordinates.

    Attributes:
        geo_boundary: Restricts this panel to a geographic region (see ``services.geo.geo_boundary``); None means unrestricted."""

    geo_boundary: ClassVar[GeoBoundary | None] = None

    def gate(self, pin: Pin) -> bool:
        """Skip scheduling a fetch for a pin with no usable coordinates, or outside ``geo_boundary``."""
        lat, lng = pin.effective_latitude, pin.effective_longitude
        if not (lat and lng):
            return False
        return self.geo_boundary is None or self.geo_boundary.contains(lat, lng)


class GalleryMediaSource(LocationCachePanelSource, ABC):
    """Base for anything that can appear as a source tab in the Media gallery.

    Attributes:
        judges_relevance: Whether an item is shown only when it is about the place (``services.media.subject_relevance``),
            for a source whose results come from a search rather than from the place itself.
    """

    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.MEDIA})
    judges_relevance: ClassVar[bool] = False
    #: Whether the tiles are members' own uploads rather than a provider's media: they are read where they are, never
    #: saved to a pin from the gallery (a member's photo is copied through its wiki, with its provenance).
    members_media: ClassVar[bool] = False
    #: The payload key :meth:`media_items` reads its results from.
    media_results_key: ClassVar[str] = "items"

    @abstractmethod
    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn this source's cached ``LocationCache.data`` into gallery items.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source
                (``{}`` when the fetch found nothing).

        Returns:
            The items to render as ``.media-item`` tiles; may be empty.
        """

    def for_viewer(self, data: dict, viewer: Profile, location: Location) -> dict:
        """``data`` as ``viewer`` may see it on ``location``'s pages, which every reader applies before reading tiles.

        A provider's cached rows describe public media and are anyone's to see as they are. A source whose rows name
        members' own uploads overrides this, and stores rows that yield no tile until narrowed here: a reader that
        forgot to call this then shows nothing, rather than a photo its uploader never shared.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source.
            viewer: The profile looking.
            location: The place whose page is being viewed.

        Returns:
            The payload :meth:`media_items` reads.
        """
        return data

    def pictures(self, items: Sequence[MediaItem]) -> list[GalleryUrls]:
        """Where each tile gets its pictures: this site's copy of a provider's image, or a member's upload where it is.

        Args:
            items: The tiles, from :meth:`gallery_items`.

        Returns:
            One :class:`~urbanlens.dashboard.services.media.previews.GalleryUrls` per item.
        """
        from urbanlens.dashboard.services.media.previews import GalleryUrls, gallery_urls

        if self.members_media:
            # This site's own media is served under its own access checks; copying it as a remote image would not be.
            return [GalleryUrls(thumb=item.thumb_url, view="") for item in items]
        return gallery_urls(items, provider=self.key)

    def media_is_ready(self, data: dict) -> bool:
        """Whether a cached row's *media* half has actually been filled in.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source.

        Returns:
            True when ``media_items`` can be trusted for this row.
        """
        return True

    def relevant_media_items(self, data: dict, subject: MediaSubject, *, kept: Collection[str] = ()) -> list[MediaItem]:
        """:meth:`media_items`, less any that are not about ``subject`` when this source judges relevance.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source.
            subject: The place the items were found for.
            kept: Keys (``media_item_key``) of items someone marked relevant, kept whatever the judgement.

        Returns:
            The items, in cached order.
        """
        items = self.media_items(data)
        if not self.judges_relevance:
            return items
        from urbanlens.dashboard.models.images.relevance import media_item_key

        return [item for item in items if (kept and media_item_key(item.url) in kept) or subject.matches(item)]

    def gallery_items(self, data: dict, subject: MediaSubject, *, kept: Collection[str] = ()) -> list[MediaItem]:
        """The items a gallery shows as tiles: :meth:`relevant_media_items` less documents, which belong on Article > Sources.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source.
            subject: The place the items were found for.
            kept: Keys (``media_item_key``) of items someone marked relevant.

        Returns:
            The items, in cached order.
        """
        return [item for item in self.relevant_media_items(data, subject, kept=kept) if not item.is_document]

    def without_irrelevant(self, data: dict, subjects: Sequence[MediaSubject], *, kept: Collection[str]) -> dict | None:
        """``data`` less each cached result that :meth:`relevant_media_items` keeps for none of ``subjects``.

        A result that does not read as exactly one item is never judged, so it stays.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source.
            subjects: Every subject a reader of the row judges it against.
            kept: Keys (``media_item_key``) of items to keep whatever the judgement.

        Returns:
            The payload without those results, or None when it would lose none.
        """
        results = data.get(self.media_results_key)
        if not self.judges_relevance or not subjects or not isinstance(results, list):
            return None
        survivors = [result for result in results if not self._judged_irrelevant(result, subjects, kept)]
        if len(survivors) == len(results):
            return None
        return {**data, self.media_results_key: survivors}

    def _judged_irrelevant(self, result: object, subjects: Sequence[MediaSubject], kept: Collection[str]) -> bool:
        if not isinstance(result, dict):
            return False
        single = {self.media_results_key: [result]}
        try:
            if len(self.media_items(single)) != 1:
                return False
        except (TypeError, KeyError, ValueError):
            return False
        return not any(self.relevant_media_items(single, subject, kept=kept) for subject in subjects)

    def api_media(self, data: dict) -> list[dict[str, Any]]:
        """This source's cached data as plain JSON media dicts.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source.

        Returns:
            One dict per :class:`MediaItem`, field-for-field.
        """
        return [asdict(item) for item in self.media_items(data)]

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The provider's cached media as ``{"media": [...]}``, or None; only what is about the place, for a source that judges relevance."""
        data = self.cached_data(pin)
        if data is None or pin.location is None:
            return None
        data = self.for_viewer(data, pin.profile, pin.location)
        if not self.judges_relevance:
            return {PanelApiKind.MEDIA.value: self.api_media(data)}
        from urbanlens.dashboard.models.images.relevance import MediaRelevance
        from urbanlens.dashboard.services.media.subject_relevance import subject_for_pin

        kept = set(MediaRelevance.objects.for_gallery(pin.profile, pin.location_id, self.key).filter(is_relevant=True).values_list("item_key", flat=True))
        return {PanelApiKind.MEDIA.value: [asdict(item) for item in self.relevant_media_items(data, subject_for_pin(pin), kept=kept)]}


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """One document a panel lists under Article > Sources and serves through its scoped proxy.

    Attributes:
        document_id: Identifies the document within its source's cached payload; the proxy URL's last segment.
        title: Visible title.
        content_type: What the proxy serves it as.
        subject: What it documents, e.g. one building's name.
        subject_kind: ``"building"``, ``"site"``, or ``""`` when unknown.
        page_url: The provider's own page for it, when the document opens there rather than through the proxy.
    """

    document_id: str
    title: str
    content_type: str = "application/pdf"
    subject: str = ""
    subject_kind: str = ""
    page_url: str = ""


class DocumentUnavailableError(Exception):
    """A listed document's bytes could not be fetched right now."""


class DocumentPanelSource(LocationCachePanelSource, ABC):
    """A cache-backed panel whose payload names documents for the Article > Sources tab.

    Attributes:
        documents_depend_on_site_scope: Whether a site-scope page lists different documents from a building's, so a
            pin that becomes a site has its documents fetched again.
        documents_judged: Whether :meth:`source_documents` lists a document only when it is about the subject it is given.
        fetched_for_sources: Whether the Sources tab fetches this source when nothing ready is cached. When False the
            tab lists only what another reader, such as the source's Media gallery, has already cached.
    """

    documents_depend_on_site_scope: ClassVar[bool] = True
    documents_judged: ClassVar[bool] = False
    fetched_for_sources: bool = True

    def may_list_documents(self, data: dict) -> bool:
        """Whether a cached payload holds any document at all, before any is judged against a subject.

        Args:
            data: The ``LocationCache`` row's ``data`` dict.

        Returns:
            False only when :meth:`source_documents` would list nothing whatever the subject.
        """
        return True

    def documents_ready(self, data: dict, *, site_scope: bool) -> bool:
        """Whether a cached payload can answer the Sources tab for a viewer of this scope.

        Args:
            data: The ``LocationCache`` row's ``data`` dict.
            site_scope: Whether the page describes a parcel/site rather than one building.

        Returns:
            True when :meth:`source_documents` can be trusted for this row.
        """
        return True

    @abstractmethod
    def source_documents(self, data: dict, *, site_scope: bool, subject: MediaSubject | None = None) -> list[SourceDocument]:
        """The documents a cached payload lists, in display order.

        Args:
            data: The ``LocationCache`` row's ``data`` dict.
            site_scope: Whether the page describes a parcel/site rather than one building.
            subject: The place, for a source whose documents were found by searching and must be about it.

        Returns:
            The documents, each with an id unique within this payload.
        """

    @abstractmethod
    def download_document(self, document: SourceDocument) -> tuple[bytes, str]:
        """Fetch one listed document's bytes.

        Args:
            document: A document :meth:`source_documents` listed.

        Returns:
            ``(content, content_type)`` as the upstream reported them.

        Raises:
            DocumentUnavailableError: The upstream could not supply it.
        """

    def document_cache_key(self, document: SourceDocument) -> str:
        """Cache key for one document's bytes.

        Args:
            document: A listed document.

        Returns:
            The key.
        """
        return f"ul_source_document_{self.key}_{document.document_id}"

    def find_document(self, data: dict, document_id: str, *, site_scope: bool, subject: MediaSubject | None = None) -> SourceDocument | None:
        """The listed document with this id, or None when the payload does not list it.

        Args:
            data: The ``LocationCache`` row's ``data`` dict.
            document_id: The id from the proxy URL.
            site_scope: Whether the page describes a parcel/site rather than one building.
            subject: The place, as for :meth:`source_documents`.

        Returns:
            The document, or None.
        """
        return next((document for document in self.source_documents(data, site_scope=site_scope, subject=subject) if document.document_id == document_id), None)


#: The key of a name-built row's payload naming the names its search was built from.
SEARCH_NAMES_KEY = "search_names"


class NameSearchSource(LocationCachePanelSource, ABC):
    """A cache-backed panel whose upstream query is built from the place's names.

    The shared row is built from shared names only. A pin with custom names also reads a row cached for exactly that
    set (see ``services.pins.search_names``), fetched once for every pin holding it. Each row keeps the names its
    search was built from under :data:`SEARCH_NAMES_KEY`.

    Attributes:
        results_key: The payload key holding the results the rows are combined on.
        results_per_row: How many of each row's results the combined payload keeps; None keeps them all.
    """

    results_key: ClassVar[str] = "items"
    results_per_row: ClassVar[int | None] = None

    def search_scopes(self, pin: Pin, names: SearchNames | None = None) -> tuple[SearchScope, ...]:
        """The shared search, then the search for ``pin``'s custom names when it has any."""
        return (names or search_names(pin)).scopes

    def scope(self, pin: Pin) -> str:
        """Location- and audience-scoped, so each name set's fetch is single-flight and suppressed on its own."""
        audience = search_names(pin).audience
        return f"loc{pin.location_id}:{audience}" if audience else f"loc{pin.location_id}"

    def result_identity(self, result: object) -> str:
        """What makes two results the same one, for combining rows.

        Args:
            result: One entry of a payload's :attr:`results_key` list.

        Returns:
            Its URL when it has one, else its JSON.
        """
        if isinstance(result, dict):
            for key in ("url", "link", "thumbnail"):
                if value := result.get(key):
                    return str(value)
        return json.dumps(result, sort_keys=True, default=str)

    def merge_cached(self, payloads: Sequence[dict]) -> dict:
        """The shared row's payload with every row's results, in row order, each result once."""
        merged = dict(payloads[0]) if payloads else {}
        merged.pop(SEARCH_NAMES_KEY, None)
        seen: set[str] = set()
        results: list = []
        for payload in payloads:
            for result in (payload.get(self.results_key) or [])[: self.results_per_row]:
                identity = self.result_identity(result)
                if identity not in seen:
                    seen.add(identity)
                    results.append(result)
        merged[self.results_key] = results
        return merged

    def fetch(self, pin: Pin) -> None:
        """Fetch each of ``pin``'s searches that has no fresh row, each once however many pins ask at a time."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.core.coalesce import coalesced

        if pin.location is None:
            return
        for scope in self.search_scopes(pin):
            if LocationCache.get_fresh(pin.location, self.cache_source, scope.audience, max_age=self.cache_max_age) is not None:
                continue
            key = f"ulfetch:search:{self.key}:loc{pin.location_id}:{scope.audience or 'shared'}"
            coalesced(key, lambda scope=scope: self.fetch_scope(pin, scope), ttl=FAILURE_SKIP_TTL_SECONDS)

    @abstractmethod
    def fetch_scope(self, pin: Pin, scope: SearchScope) -> None:
        """Run one search and cache it under ``scope.audience``, through :meth:`store`.

        A scope whose names build no query is cached as an empty answer without asking the upstream; an outage is
        not cached at all.

        Args:
            pin: The pin whose panel is being fetched; its address and locality are shared context.
            scope: The search to run.
        """

    def store(self, pin: Pin, scope: SearchScope, data: dict, query_key: str) -> None:
        """Cache one search's payload under its audience, with the names that built it.

        Args:
            pin: The pin whose Location the row belongs to.
            scope: The search that produced ``data``.
            data: The payload.
            query_key: The query sent.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(pin.location, self.cache_source, {**data, SEARCH_NAMES_KEY: scope.provenance()}, query_key=query_key[:255], audience=scope.audience)


class GatewayMediaPanelSource(GalleryMediaSource):
    """A Media gallery provider backed by one :class:`MediaProvider` gateway."""

    def __init__(self, key: str, cache_source: str, gateway_factory) -> None:
        """Bind this source to one media provider.

        Args:
            key: Registry key, matching the URL's ``source`` segment.
            cache_source: The provider gateway's ``service_key`` (its
                LocationCache source).
            gateway_factory: Zero-argument callable building the gateway.
        """
        # Per-instance rather than ClassVar: several providers share this class.
        self.key = key
        self.cache_source = cache_source
        self._gateway_factory = gateway_factory

    def make_gateway(self) -> MediaProvider:
        """Build this provider's gateway instance."""
        return self._gateway_factory()

    def media_items(self, data: dict) -> list[MediaItem]:
        """Rebuild ``MediaItem``s from this provider's cached ``{"items": [...]}``."""
        return [MediaItem(**item) for item in (data or {}).get("items", [])]


class MediaPanelSource(NameSearchSource, GatewayMediaPanelSource):
    """One provider of the combined Media gallery (Smithsonian, Wikimedia, LOC), searched by the place's names."""

    judges_relevance: ClassVar[bool] = True

    @staticmethod
    def search_terms(pin: Pin, gateway: MediaProvider, scope: SearchScope) -> list[str]:
        """Candidate search queries for one of this pin's searches, most specific first.

        Args:
            pin: The pin to build search queries for.
            gateway: The provider gateway (controls quoting/country flags).
            scope: The search, whose names the queries use.

        Returns:
            Ordered, de-duplicated list of query strings; may be empty.
        """
        if gateway.reject_address_derived_names and pin.location is not None and scope.names:
            from urbanlens.dashboard.services.locations.naming import is_address_derived_name

            # A query built from a raw street address has no narrowing power for a provider whose relevance
            # ranking treats query words as independent OR terms, so such a provider is skipped instead.
            if is_address_derived_name(scope.names[0], pin.location):
                return []

        search_term = pin.get_unique_search_name(
            scope,
            include_country=gateway.search_with_country,
            quote_name=gateway.quote_name,
            include_address=gateway.include_address,
            quote_locality=gateway.quote_locality,
        )
        if not search_term:
            return []
        terms = [search_term]
        if gateway.multi_query:
            narrow_term = pin.get_unique_search_name(
                scope,
                include_country=gateway.search_with_country,
                quote_name=gateway.quote_name,
                include_address=False,
                quote_locality=gateway.quote_locality,
            )
            if narrow_term and narrow_term not in terms:
                terms.append(narrow_term)
        return terms

    def fetch_scope(self, pin: Pin, scope: SearchScope) -> None:
        """Fetch this provider's media for one search; ``get_media`` persists to LocationCache."""
        gateway = self.make_gateway()
        terms = self.search_terms(pin, gateway, scope)
        if not terms:
            self.store(pin, scope, {"items": []}, query_key="")
            return
        gateway.get_media(pin.location, terms, audience=scope.audience, search_names=scope.provenance())

    def gate(self, pin: Pin) -> bool:
        """Unavailable and geo-restricted providers, and pins with no usable search name, are skipped."""
        gateway = self.make_gateway()
        if not gateway.available():
            return False
        if gateway.geo_boundary is not None and not gateway.geo_boundary.contains(pin.effective_latitude, pin.effective_longitude):
            return False
        return any(self.search_terms(pin, gateway, scope) for scope in self.search_scopes(pin))


class DocumentMediaPanelSource(MediaPanelSource, DocumentPanelSource):
    """A Media gallery provider whose results may include books and scans, which are listed under Article > Sources.

    A document opens on the provider's own page: a scanned book runs to tens of megabytes, more than the Sources proxy holds.
    """

    documents_depend_on_site_scope: ClassVar[bool] = False
    documents_judged: ClassVar[bool] = True

    def __init__(self, key: str, cache_source: str, gateway_factory, *, fetched_for_sources: bool = True) -> None:
        """Bind this source to one media provider.

        Args:
            key: Registry key, matching the URL's ``source`` segment.
            cache_source: The provider gateway's ``service_key`` (its LocationCache source).
            gateway_factory: Zero-argument callable building the gateway.
            fetched_for_sources: False to list only the documents the Media gallery's own search has cached, so the
                Sources tab never searches the provider itself.
        """
        super().__init__(key, cache_source, gateway_factory)
        self.fetched_for_sources = fetched_for_sources

    def may_list_documents(self, data: dict) -> bool:
        """Whether any of this provider's cached results is a document."""
        return any(item.is_document for item in self.media_items(data))

    def source_documents(self, data: dict, *, site_scope: bool, subject: MediaSubject | None = None) -> list[SourceDocument]:
        """The documents among this provider's results that are about ``subject``; none without one."""
        from urbanlens.dashboard.models.images.relevance import media_item_key

        if subject is None:
            return []
        return [
            SourceDocument(
                document_id=media_item_key(item.url),
                title=item.title or item.caption or "Document",
                content_type=item.content_type,
                page_url=item.page_url or item.url,
            )
            for item in self.relevant_media_items(data, subject)
            if item.is_document
        ]

    def download_document(self, document: SourceDocument) -> tuple[bytes, str]:
        """Never proxied: the Sources tab links to the provider's page instead.

        Raises:
            DocumentUnavailableError: Always.
        """
        raise DocumentUnavailableError(document.document_id)


class BoundaryPanelSource(PanelSource):
    """Auto-generated default boundaries stored on the Location's Boundary rows.
    Location-scoped: the generated property/building boundaries are shared place data, so one fetch serves every pin (and the wiki page) at that Location."""

    key = "boundary"
    # The prefork pool, not the fast thread-pool queue - generate_location_boundaries
    # does real CPU-bound work (gunzipping building-footprint shards, shapely
    # geometry ops), and several of those running concurrently on a thread pool
    # would cause enough GIL contention to slow down every other panel sharing
    # it. See PanelSource.queue.
    queue = Queue.INTERACTIVE
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.BOUNDARY})

    def scope(self, pin: Pin) -> str:
        """Location-scoped: default boundaries are keyed by Location."""
        return f"loc{pin.location_id}"

    @classmethod
    def location_flight_key(cls, location_id: int | None) -> str:
        """The flight marker of a location's boundary fetch, for a caller generating it with no pin in hand.

        Args:
            location_id: The location.

        Returns:
            The key :meth:`flight_key` gives for any pin standing there.
        """
        return f"ulfetch:flight:{cls.key}:loc{location_id}"

    def flight_key(self, pin: Pin) -> str:
        """Single-flight cache key for this location's boundary fetch."""
        return self.location_flight_key(pin.location_id)

    def is_ready(self, pin: Pin) -> bool:
        """True when the provider chain has a fresh answer for the pin's Location.

        A run that found no place is only fresh for :data:`CIRCLE_RETRY_AFTER`. After that the
        fallback circle is asked again, so a refusal stamped before the parcel was available
        does not stay a circle for the whole cache window.
        """
        if pin.location_id is None:
            return True
        from urbanlens.dashboard.services.locations.boundaries import generation_status

        ran, stale = generation_status(pin.location)
        return ran and not stale

    def fetch(self, pin: Pin) -> None:
        """Run the boundary provider chain and persist generated polygons.
        Persistence uses queryset ``update()`` calls (see ``generate_location_boundaries``) so it can never clobber geometry saved concurrently by the web request."""
        from urbanlens.dashboard.services.locations.boundaries import generate_location_boundaries

        if pin.location_id is None or self.is_ready(pin):
            return
        generate_location_boundaries(pin.location)

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The pin's effective property and building geometry as GeoJSON.

        Args:
            pin: The pin whose boundaries are being read.

        Returns:
            ``{"boundary": {"property": ..., "building": ...}}`` with each side either a ``{"geometry", "source", "is_fallback_circle"}`` dict or None, or None overall when the pin has no location or neither side resolved to anything.
        """
        if pin.location_id is None:
            return None

        from urbanlens.dashboard.models.boundary.model import BoundaryType

        property_side = self._boundary_side(pin, BoundaryType.PROPERTY)
        building_side = self._boundary_side(pin, BoundaryType.BUILDING)
        if property_side is None and building_side is None:
            return None
        return {PanelApiKind.BOUNDARY.value: {"property": property_side, "building": building_side}}

    @staticmethod
    def _boundary_side(pin: Pin, boundary_type: str) -> dict[str, Any] | None:
        """One boundary type's resolved geometry plus its provenance.

        Args:
            pin: The pin to resolve for.
            boundary_type: A :class:`BoundaryType` value.

        Returns:
            ``{"geometry", "source", "is_fallback_circle"}``, or None when nothing resolved for this type (which for BUILDING is the normal case - a missing building boundary means "no known building", and unlike PROPERTY it has no circle fallback).
        """
        from urbanlens.dashboard.models.boundary.model import Boundary
        from urbanlens.dashboard.services.geo.geo import geometry_to_geojson

        polygon, source = Boundary.objects.resolve_for_pin(pin, boundary_type)
        if polygon is None:
            return None
        return {"geometry": geometry_to_geojson(polygon), "source": source, "is_fallback_circle": source == "circle"}


def _satellite_gateways() -> list[SatelliteViewProvider]:
    """The plugin-contributed satellite imagery provider chain, in display order."""
    from urbanlens.dashboard.plugins import plugin_registry

    return plugin_registry.satellite_providers()


def _street_view_gateways() -> list[StreetViewProvider]:
    """The plugin-contributed street-level imagery provider chain, in display order."""
    from urbanlens.dashboard.plugins import plugin_registry

    return plugin_registry.street_view_providers()


def collect_satellite_slides(lat: float, lng: float) -> tuple[list[SatelliteSlide], list[ProviderFetchResult]]:
    """Gather satellite slides from every provider, tolerating per-provider failure.
    Each provider caches its own slides (24h, keyed by coordinates), so running this twice is one round of upstream fetches followed by pure cache hits -- the Celery warm-up task and the request-path render share this exact function.

    Args:
        lat: WGS-84 latitude.
        lng: WGS-84 longitude.

    Returns:
        Tuple of (all slides in provider order, per-provider outcomes for the admin debug overlay)."""
    slides: list[SatelliteSlide] = []
    results: list[ProviderFetchResult] = []
    for gateway in _satellite_gateways():
        service = gateway.service_key or type(gateway).__name__
        try:
            fetched = gateway.get_satellite_slides(lat, lng)
            slides.extend(fetched.slides)
            results.append(ProviderFetchResult(service, from_cache=fetched.from_cache, count=len(fetched.slides), ok=not fetched.degraded))
        except RateLimitExceededError as rle:
            # Recorded as a failed provider, not skipped silently: it contributed nothing *and* may
            # well succeed shortly, which is the difference between "this location has no imagery"
            # and "we did not get to ask".
            # SlidesPanelSource.fetch reads these to decide how long to trust an empty result.
            logger.debug("Satellite view provider %s rate-limited -> %s", service, rle)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
        except RequestCancelledError as rce:
            # A disabled service is settled, not a reason to re-warm the panel every few minutes;
            # an unreadable limiter is transient and must not mark the carousel ready for 12 hours.
            logger.debug("Satellite view provider %s request cancelled -> %s", service, rce)
            if rce.transient:
                results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
        except Exception as e:
            # TODO: Catch specific exceptions
            logger.warning("Satellite view provider %s failed -> %s", service, e)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
    return slides, results


def collect_street_view_slides(lat: float, lng: float) -> tuple[list[StreetViewSlide], list[ProviderFetchResult]]:
    """Gather street-level slides from every provider, tolerating per-provider failure.

    Args:
        lat: WGS-84 latitude.
        lng: WGS-84 longitude.

    Returns:
        Tuple of (all slides in provider order, per-provider outcomes for the admin debug overlay)."""
    slides: list[StreetViewSlide] = []
    results: list[ProviderFetchResult] = []
    for provider in _street_view_gateways():
        service = provider.service_key or type(provider).__name__
        try:
            fetched = provider.get_street_view_slides(lat, lng)
            slides.extend(fetched.slides)
            results.append(ProviderFetchResult(service, from_cache=fetched.from_cache, count=len(fetched.slides), ok=not fetched.degraded))
        except RateLimitExceededError as rle:
            # Recorded as a failed provider, not skipped silently: it contributed nothing *and* may
            # well succeed shortly, which is the difference between "this location has no imagery"
            # and "we did not get to ask".
            # SlidesPanelSource.fetch reads these to decide how long to trust an empty result.
            logger.debug("Street view provider %s rate-limited -> %s", service, rle)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
        except RequestCancelledError as rce:
            # See collect_satellite_slides.
            logger.debug("Street view provider %s request cancelled -> %s", service, rce)
            if rce.transient:
                results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
        except Exception:
            # TODO: Catch specific exceptions
            logger.warning("Street view provider %s failed", service, exc_info=True)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
    return slides, results


class SlidesPanelSource(PanelSource, ABC):
    """Base for the satellite/street carousels, whose store is per-provider Django cache."""

    # Deliberately absent from the external API for now: api_kinds stays empty and api_payload keeps
    # PanelSource's None.
    # The fix is a signed slide-image proxy - the payload becomes a list of short URLs the client
    # fetches individually, which the throttle can then actually count - and that proxy does not
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset()

    def scope(self, pin: Pin) -> str:
        """Coordinate-scoped, matching the providers' own cache keys."""
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        return f"{lat:.5f},{lng:.5f}"

    def ready_key(self, pin: Pin) -> str:
        """Cache key of the "provider caches are warm" summary marker."""
        return f"ulfetch:ready:{self.key}:{self.scope(pin)}"

    def is_ready(self, pin: Pin) -> bool:
        """True when a warm-up pass has completed for these coordinates."""
        return bool(cache.get(self.ready_key(pin)))

    @abstractmethod
    def collect(self, lat: float, lng: float) -> tuple[list, list[ProviderFetchResult]]:
        """Run this carousel's provider chain (see the module-level collectors)."""

    def fetch(self, pin: Pin) -> None:
        """Warm every provider's slide cache, then mark how far to trust the result.
        Those retry on the ordinary failure cadence instead."""
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        _, results = self.collect(lat, lng)
        complete = all(result.ok for result in results)
        cache.set(self.ready_key(pin), 1, SLIDES_READY_TTL_SECONDS if complete else FAILURE_SKIP_TTL_SECONDS)


class SatellitePanelSource(SlidesPanelSource):
    """Multi-provider satellite imagery carousel."""

    key = "satellite"
    section_id = "satellite-view-section"
    icon = "globe"
    title = "Satellite View"
    outer_class = "satellite-view card card--primary"
    outer_is_card = True

    def collect(self, lat: float, lng: float) -> tuple[list[SatelliteSlide], list[ProviderFetchResult]]:
        """Run the satellite provider chain."""
        return collect_satellite_slides(lat, lng)


class StreetViewPanelSource(SlidesPanelSource):
    """Multi-provider street-level imagery carousel."""

    key = "street_view"
    section_id = "street-view-section"
    icon = "streetview"
    title = "Street View"
    outer_class = "street-view card card--primary"
    outer_is_card = True

    def collect(self, lat: float, lng: float) -> tuple[list[StreetViewSlide], list[ProviderFetchResult]]:
        """Run the street-view provider chain."""
        return collect_street_view_slides(lat, lng)


#: Panels that belong to the core application rather than any one plugin:
#: the default boundaries and the two imagery carousels (which aggregate
#: plugin-contributed providers but are themselves core features).
_CORE_PANEL_SOURCES: tuple[PanelSource, ...] = (
    BoundaryPanelSource(),
    SatellitePanelSource(),
    StreetViewPanelSource(),
)


def panel_source_problems(source: PanelSource) -> list[str]:
    """Return the ways ``source`` is misconfigured, as human-readable strings.

    Args:
        source: The panel source to check.

    Returns:
        A list of problems, empty when the source is well-formed."""
    problems: list[str] = []
    if not getattr(source, "key", ""):
        problems.append("key is required (it addresses the panel in URLs, cache keys and Celery arguments)")

    # The two presentation attributes are required only of sources that render a section of their
    # own - which is exactly InfoPanelSource and SlidesPanelSource.
    # The other two shapes legitimately have neither: gallery media providers render as tabs
    # *inside* the combined Media gallery, whose controller supplies the surrounding markup, and a
    if isinstance(source, (InfoPanelSource, SlidesPanelSource)):
        if not source.title:
            problems.append("title is required (it is the panel's heading, and the pending placeholder's)")
        if not source.section_id:
            problems.append("section_id is required (the panel's DOM id, which HTMX swaps against)")

    if isinstance(source, LocationCachePanelSource) and not getattr(source, "cache_source", ""):
        problems.append("cache_source is required for a cache-backed panel (it keys the LocationCache rows its fetch writes)")
    return problems


#: Keys already reported by :func:`panel_sources`, so a misconfigured plugin is
#: reported once rather than on every request that builds the registry.
_REPORTED_PANEL_PROBLEMS: set[str] = set()


def panel_sources() -> dict[str, PanelSource]:
    """Every registered panel source, keyed by the source key used in URLs, Celery task arguments, and cache keys.

    Returns:
        Mapping of source key to its :class:`PanelSource`."""
    from urbanlens.dashboard.plugins import plugin_registry

    sources: dict[str, PanelSource] = {source.key: source for source in _CORE_PANEL_SOURCES}
    for source in plugin_registry.panel_sources():
        if source.key in sources:
            logger.warning("Ignoring duplicate panel source '%s' from plugins", source.key)
            continue
        sources[source.key] = source

    for key, source in sources.items():
        problems = panel_source_problems(source)
        if problems and key not in _REPORTED_PANEL_PROBLEMS:
            _REPORTED_PANEL_PROBLEMS.add(key)
            logger.error("Panel source '%s' (%s) is misconfigured: %s", key, type(source).__name__, "; ".join(problems))
    return sources


def tabbed_panels(sources: Iterable[PanelSource], placement: PanelPlacement) -> list[InfoPanelSource]:
    """The info panels placed in one tabbed card, in tab order.

    Args:
        sources: Candidate sources, in registry order.
        placement: The card whose tabs are wanted.

    Returns:
        The matching sources, sorted by ``tab_order``; the sort is stable, so ties keep registry order. A source shown
        inside another of ``sources`` is not a tab of its own.
    """
    placed = [source for source in own_panels(sources) if source.placement == placement]
    return sorted(placed, key=lambda source: source.tab_order)


def own_panels(sources: Iterable[PanelSource]) -> list[InfoPanelSource]:
    """The info panels among ``sources`` that are not shown inside another of them.

    Args:
        sources: Candidate sources, in registry order.

    Returns:
        The info panels with no :attr:`~InfoPanelSource.shown_in` host among ``sources``, in the order given.
    """
    panels = [source for source in sources if isinstance(source, InfoPanelSource)]
    keys = {source.key for source in panels}
    return [source for source in panels if source.shown_in not in keys]


def panels_shown_in(host: InfoPanelSource, sources: Iterable[PanelSource]) -> list[InfoPanelSource]:
    """The info panels among ``sources`` that ``host`` shows inside it.

    Args:
        host: The panel whose tab or card holds them.
        sources: Candidate sources, in registry order.

    Returns:
        The panels declaring ``host`` as their :attr:`~InfoPanelSource.shown_in`, in the order given.
    """
    return [source for source in sources if isinstance(source, InfoPanelSource) and source is not host and source.shown_in == host.key]


def without_repeats(contexts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Several panels' render contexts, each dropping what an earlier one already said.

    A fact is repeated when it links somewhere an earlier panel already linked, or gives a label and value an earlier
    panel already gave. The first panel is kept whole; a later one left with nothing to show is dropped.

    Args:
        contexts: ``render_context`` results, in display order.

    Returns:
        The contexts to show, in the same order.
    """
    links: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    texts: set[str] = set()
    kept: list[dict[str, Any]] = []
    for index, context in enumerate(contexts):
        meta = [entry for entry in context.get("meta") or [] if index == 0 or not (entry.get("href") in links or (str(entry.get("label")), str(entry.get("value"))) in pairs)]
        facts = [fact for fact in context.get("facts") or [] if index == 0 or not (fact.get("href") in links or str(fact.get("text")) in texts)]
        footer_link = context.get("footer_link")
        if index and footer_link and footer_link.get("url") in links:
            footer_link = None
        trimmed = {**context, "meta": meta, "facts": facts, "footer_link": footer_link}
        links.update(link for link in [*(entry.get("href") for entry in meta), *(fact.get("href") for fact in facts), (footer_link or {}).get("url")] if link)
        pairs.update((str(entry.get("label")), str(entry.get("value"))) for entry in meta)
        texts.update(str(fact.get("text")) for fact in facts)
        if index == 0 or any(trimmed.get(key) for key in ("heading_name", "chips", "facts", "meta", "footer_link")):
            kept.append(trimmed)
    return kept


def get_panel_source(source_key: str) -> PanelSource | None:
    """Look up one panel source by key.

    Args:
        source_key: A :func:`panel_sources` key.

    Returns:
        The panel source, or None when no core panel or enabled plugin provides that key."""
    return panel_sources().get(source_key)


def shows_members_media(source_key: str) -> bool:
    """Whether a gallery source's tiles are other members' photos from this site, which no gallery action copies.

    Args:
        source_key: A :func:`panel_sources` key, as a gallery request names it.

    Returns:
        True for a :attr:`GalleryMediaSource.members_media` source.
    """
    panel = get_panel_source(source_key)
    return isinstance(panel, GalleryMediaSource) and panel.members_media


def document_panel_sources() -> list[DocumentPanelSource]:
    """Every registered panel source that lists documents for Article > Sources.

    Returns:
        The sources, in registry order.
    """
    return [source for source in panel_sources().values() if isinstance(source, DocumentPanelSource)]


def _scopes_by_source(pin: Pin, sources: Sequence[LocationCachePanelSource]) -> dict[str, tuple[SearchScope, ...]]:
    """Each source's cached searches for ``pin``, splitting the pin's names at most once."""
    names = search_names(pin) if any(isinstance(source, NameSearchSource) for source in sources) else None
    return {source.key: source.search_scopes(pin, names) for source in sources}


def _fresh_location_cache_keys(pin: Pin, audiences: Iterable[str], since: datetime) -> dict[tuple[str, str], datetime]:
    """Every ``(source, audience)`` of these audiences with a row at this pin's location fresh by the site-wide window.

    Args:
        pin: The pin whose location's cache rows are being examined.
        audiences: The audiences to look at.
        since: ``LocationCache.fresh_since()``.

    Returns:
        Each fresh key's ``updated``, for a source with a shorter window of its own to judge; empty when the pin has
        no location."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    if pin.location_id is None:
        return {}
    rows = LocationCache.objects.filter(location_id=pin.location_id, audience__in=list(audiences), updated__gte=since)
    return {(source, audience): updated for source, audience, updated in rows.values_list("source", "audience", "updated")}


def _entries(pin: Pin, sources: Sequence[LocationCachePanelSource], scopes: dict[str, tuple[SearchScope, ...]], since: datetime | None = None) -> dict[str, CachedEntry | None]:
    """:func:`cached_entries`, for a caller that has already worked out each source's scopes."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    if pin.location_id is None:
        return {source.key: None for source in sources}
    audiences = {scope.audience for source in sources for scope in scopes[source.key]}
    since = since if since is not None else LocationCache.fresh_since()
    rows = LocationCache.fresh_rows(pin.location_id, {source.cache_source for source in sources}, audiences, since)
    entries: dict[str, CachedEntry | None] = {}
    for source in sources:
        cutoff = source.fresh_since(since)
        found = [row if (row := rows.get((source.cache_source, scope.audience))) is not None and row.updated >= cutoff else None for scope in scopes[source.key]]
        present = [row for row in found if row is not None]
        if not present or len(present) < len(found):
            entries[source.key] = None
            continue
        entries[source.key] = CachedEntry(
            data=source.merge_cached([row.data or {} for row in present]),
            query_key=" || ".join(row.query_key for row in present if row.query_key),
            rows=tuple(present),
        )
    return entries


def cached_entries(pin: Pin, sources: Sequence[LocationCachePanelSource]) -> dict[str, CachedEntry | None]:
    """What ``pin`` reads from each source, in one query.

    A pin reads the shared row and, for a name-built source, the row of its own name set, never another's.

    Args:
        pin: The pin whose panels are being read.
        sources: The cache-backed sources to read.

    Returns:
        Source key to its combined entry, or to None while any row it needs is missing or stale.
    """
    if pin.location_id is None:
        return {source.key: None for source in sources}
    return _entries(pin, sources, _scopes_by_source(pin, sources))


def gate_allows(source: PanelSource, pin: Pin) -> bool:
    """Whether *source* applies to *pin*, treating a raising gate as "no".
    Per-panel surfaces survive that on their own (one HTMX request, one panel), but any surface that evaluates *every* source in one pass would answer with no panels at all rather than one fewer.

    Args:
        source: The panel source to test.
        pin: The pin being rendered.

    Returns:
        The source's own ``gate`` result, or False when it raised."""
    try:
        return bool(source.gate(pin))
    except Exception:
        logger.exception("Panel source %s raised from gate(); treating as not applicable", getattr(source, "key", source))
        return False


def panel_readiness(pin: Pin, sources: Iterable[PanelSource] | None = None, *, require_content: bool = True) -> dict[str, bool]:
    """Whether each panel source already has data for ``pin``, in one pass.
    Anything that needs the readiness of more than one source should call this instead of looping.

    Args:
        pin: The pin whose panels are being checked.
        sources: The sources to report on; defaults to every registered source.
        require_content: Count a source that judges its payload (``inspects_content``) ready only when the payload has
            something to show, as :meth:`PanelSource.is_ready` does; False counts any stored answer, as
            :meth:`PanelSource.has_landed` does.

    Returns:
        Mapping of source key to readiness."""
    resolved = list(sources) if sources is not None else list(panel_sources().values())
    readiness: dict[str, bool] = {}

    cache_backed = [source for source in resolved if isinstance(source, LocationCachePanelSource)]
    slide_backed = [source for source in resolved if isinstance(source, SlidesPanelSource)]
    bespoke = [source for source in resolved if not isinstance(source, (LocationCachePanelSource, SlidesPanelSource))]

    if cache_backed:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        scopes = _scopes_by_source(pin, cache_backed)
        since = LocationCache.fresh_since()
        fresh = _fresh_location_cache_keys(pin, {scope.audience for source_scopes in scopes.values() for scope in source_scopes}, since)
        landed = {source.key: all((updated := fresh.get((source.cache_source, scope.audience))) is not None and updated >= source.fresh_since(since) for scope in scopes[source.key]) for source in cache_backed}
        # Panels that opt into a content check need their payload, which the key query above deliberately does not
        # carry; fetched in one extra query covering only those sources.
        inspecting = [source for source in cache_backed if require_content and source.inspects_content and landed[source.key]]
        entries = _entries(pin, inspecting, scopes, since) if inspecting else {}
        for cache_source in cache_backed:
            fresh_enough = landed[cache_source.key]
            if fresh_enough and require_content and cache_source.inspects_content:
                entry = entries.get(cache_source.key)
                fresh_enough = cache_source.has_content(entry.data if entry is not None else None)
            readiness[cache_source.key] = fresh_enough

    if slide_backed:
        ready_keys = {slide_source.key: slide_source.ready_key(pin) for slide_source in slide_backed}
        warm = cache.get_many(list(ready_keys.values()))
        for source_key, ready_key in ready_keys.items():
            readiness[source_key] = bool(warm.get(ready_key))

    for bespoke_source in bespoke:
        # Guarded per source: this map is built for the Private Pin page's tab strip, and panels are
        # the plugin extensibility surface, so one plugin's is_ready() raising would otherwise 500
        # the whole page rather than affecting its own tab.
        # "Not ready" is the safe default - the tab shows its pending state and polls, which is
        try:
            readiness[bespoke_source.key] = bespoke_source.is_ready(pin)
        except Exception:
            logger.exception("Panel source %s failed its readiness check for pin %s", bespoke_source.key, pin.pk)
            readiness[bespoke_source.key] = False

    return readiness


def panel_visible_to(user: AbstractBaseUser | AnonymousUser, source: PanelSource) -> bool:
    """Whether *user* holds the subscription feature this panel source requires.

    Args:
        user: The user asking to see the panel (typically ``request.user``).
        source: The panel source being considered.

    Returns:
        True when the source is unrestricted (the overwhelming majority) or the viewer holds the feature it requires."""
    feature = source.required_feature
    if feature is None:
        return True
    from urbanlens.dashboard.models.subscriptions import user_has_feature

    return user_has_feature(user, feature)


def seed_site_descendants(site: Pin) -> int:
    """Answer the markers nested under ``site`` from every panel's cached site payload, without asking any upstream.

    Args:
        site: The site pin.

    Returns:
        How many nested cache rows were written, across every source.
    """
    written = 0
    for source in panel_sources().values():
        if not isinstance(source, LocationCachePanelSource):
            continue
        try:
            written += source.seed_descendants(site)
        except Exception:
            logger.exception("Panel source %s failed to seed the markers nested under pin %s", source.key, site.pk)
    return written


def fetch_blocked(source: PanelSource, pin: Pin) -> bool:
    """Whether :func:`schedule_panel_fetch` would refuse to fetch ``source`` for ``pin``.

    Args:
        source: The panel source.
        pin: The pin whose panel would be fetched.

    Returns:
        True when the pin's owner turned external services off, or the source is suppressed after a failed fetch.
    """
    if not pin.profile.external_apis_enabled:
        return True
    if cache.get(source.skip_key(pin)):
        logger.debug("%s for pin %s is suppressed", source.key, pin.pk)
        return True
    return False


def schedule_panel_fetch(source_key: str, pin: Pin) -> bool:
    """Ensure a background fetch is in flight for this panel, single-flight.

    Args:
        source_key: A :func:`panel_sources` key.
        pin: The pin whose panel data should be fetched.

    Returns:
        True when a fetch is in flight (newly scheduled or already running) -- the caller should return a polling placeholder."""
    source = get_panel_source(source_key)
    if source is None:
        logger.warning("schedule_panel_fetch: unknown source '%s' for pin %s", source_key, getattr(pin, "pk", None))
        return False
    if fetch_blocked(source, pin):
        return False
    # The marker's TTL covers queue wait *and* execution, so on a backed-up panel_fetch queue it can
    # lapse before the task even starts.
    # The worker therefore releases by token: without one, a fetch that outlived its marker deletes
    # the *next* schedule's marker on the way out, and the poll after that dispatches a third fetch
    flight_token = acquire_lock(source.flight_key(pin), FLIGHT_TTL_SECONDS)
    if flight_token is not None:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import fetch_panel_source

        logger.debug("schedule_panel_fetch: dispatching %s for pin %s to queue '%s'", source_key, pin.pk, source.queue)
        if safely_enqueue_task(fetch_panel_source, source_key, pin.pk, flight_token, queue=source.queue, durable=False) is None:
            # Broker down: a raised error here would 500 every panel on the pin detail page at once.
            # Release the just-claimed single-flight marker so the next poll retries the enqueue
            # instead of waiting out FLIGHT_TTL_SECONDS behind a task that was never queued.
            release_lock(source.flight_key(pin), flight_token)
            return False
    return True


def _release_flight(source, pin: Pin, flight_token: str | None) -> None:
    """Drop the single-flight marker, if it is still this fetch's.

    Args:
        source: The panel source being fetched.
        pin: The pin whose panel was fetched.
        flight_token: Token from the scheduling call, or None for a task enqueued before tokens existed - those release unconditionally, as they did before, rather than leaking the marker until its TTL."""
    if flight_token is None:
        cache.delete(source.flight_key(pin))
    else:
        release_lock(source.flight_key(pin), flight_token)


def run_panel_fetch(source_key: str, pin: Pin, flight_token: str | None = None) -> None:
    """Execute one panel fetch inside the Celery worker. Owns the failure policy so individual sources don't have to:

    Args:
        source_key: A :func:`panel_sources` key.
        pin: The pin whose panel data should be fetched."""
    source = get_panel_source(source_key)
    if source is None:
        logger.warning("Panel fetch for unknown source '%s' skipped (plugin removed or disabled?)", source_key)
        return
    if not pin.profile.external_apis_enabled:
        # External APIs may have been turned off after this task was enqueued;
        # skip without recording a failure so the panel just stays absent.
        _release_flight(source, pin, flight_token)
        return

    started = time.monotonic()
    logger.debug("Panel fetch %s for pin %s starting on queue '%s'", source_key, pin.pk, source.queue)
    try:
        if not (isinstance(source, LocationCachePanelSource) and source.adopt_site_answer(pin)):
            source.fetch(pin)
    except UpstreamBusyError as exc:
        logger.info("Panel fetch %s for pin %s deferred %ss: %s", source_key, pin.pk, exc.retry_after, exc)
        cache.set(source.skip_key(pin), 1, exc.retry_after)
    except (RateLimitExceededError, ServiceDisabledError) as exc:
        logger.debug("Panel fetch %s for pin %s skipped: %s", source_key, pin.pk, exc)
        cache.set(source.skip_key(pin), 1, DISABLED_SKIP_TTL_SECONDS)
    except SOFT_TIME_LIMIT_ERRORS:
        # Celery's own worker log already recorded the soft time limit at WARNING with full task
        # context; a second ERROR-level traceback here would just be noise for the same event.
        # Suppress like any other failure and let the task end - re-raising would still hit the hard
        # time limit before doing anything useful with the remaining budget.
        logger.warning(
            "Panel fetch %s for pin %s hit its soft time limit after %.1fs; suppressing for %ss",
            source_key,
            pin.pk,
            time.monotonic() - started,
            FAILURE_SKIP_TTL_SECONDS,
        )
        cache.set(source.skip_key(pin), 1, FAILURE_SKIP_TTL_SECONDS)
    except Exception as exc:
        if is_source_outage(exc):
            logger.warning("Panel fetch %s for pin %s could not reach its upstream: %s", source_key, pin.pk, exc)
        else:
            logger.exception("Panel fetch %s for pin %s failed after %.1fs", source_key, pin.pk, time.monotonic() - started)
        cache.set(source.skip_key(pin), 1, FAILURE_SKIP_TTL_SECONDS)
    else:
        logger.debug("Panel fetch %s for pin %s finished in %.1fs", source_key, pin.pk, time.monotonic() - started)
        if not source.has_landed(pin):
            # A source that met an outage returns without writing it down as "nothing here"; left
            # unsuppressed, every poll of every open page would dispatch the same call again.
            logger.info("Panel fetch %s for pin %s landed nothing; suppressing for %ss", source_key, pin.pk, FAILURE_SKIP_TTL_SECONDS)
            cache.set(source.skip_key(pin), 1, FAILURE_SKIP_TTL_SECONDS)
    finally:
        _release_flight(source, pin, flight_token)
