"""Background-fetch orchestration for the pin detail page's external-data panels.

Every external-data panel (Wikipedia, media archives, satellite imagery,
default boundaries, ...) used to fetch its upstream data inside the HTTP request
that rendered it, bounded only by a wall-clock deadline. That kept slow
providers from hanging a single request, but the work still happened on the
web worker: a cold pin page fired ~10 upstream fetches through the request
path at once, and CPU-bound steps (gunzipping building-footprint shards,
shapely geometry work) blocked the gevent event loop outright, which no
timeout can prevent.

This module moves all of that off the request path:

* Each panel is described by a :class:`PanelSource` -- it knows how to check
  whether its data has already landed in its backing store (``is_ready``) and
  how to fetch-and-persist that data (``fetch``, run inside a Celery worker).
* Controllers call :func:`schedule_panel_fetch` on a cache miss and return a
  small self-polling placeholder instead of blocking; the HTMX fragment polls
  until the task lands the data (or gives up after
  :data:`MAX_POLL_ATTEMPTS`).
* Scheduling is single-flight per (source, target): an atomic ``cache.add``
  ensures concurrent page loads share one task instead of stampeding the
  upstream API.
* A failed or disabled source sets a short-lived "skip" marker so its panel
  degrades to an immediate 204 (quietly absent) instead of re-polling every
  page load; the source resumes automatically when the marker expires.

Adding a new panel means writing one ``PanelSource`` subclass inside a
plugin (see :mod:`urbanlens.dashboard.plugins`), returning it from the
plugin's ``get_panel_sources``, and pointing a template fragment at a
controller that follows the ready-render-or-schedule pattern -- the task
plumbing, deduplication, and failure handling are shared. The satellite and
street-view carousels similarly assemble their provider chains from plugins'
``get_satellite_providers``/``get_street_view_providers`` contributions.

Reading a panel as JSON
-----------------------

The web UI reads a panel through whatever renders it: ``render_context`` for
the generic info panels, ``media_items`` for the gallery tabs, and a bespoke
controller method plus its own template for the handful that fit neither. None
of that is usable by a native client, which wants the panel's *data*, not its
markup, so :class:`PanelSource` additionally carries a read interface:
:attr:`PanelSource.api_kinds` (which JSON shapes this source can serve, see
:class:`PanelApiKind`) and :meth:`PanelSource.api_payload` (the body itself).

Both default to "nothing". That is deliberate and load-bearing: panel sources
are a *plugin* extension point, so the set of classes reaching this interface
is open-ended and includes code this repository never sees. A default that
guessed at a payload -- dumping the raw ``LocationCache`` row, say -- would
turn "a plugin author forgot to think about the API" into a data leak of
whatever that plugin happened to cache. Failing closed makes the same mistake
produce a panel that is merely absent from the API, which is recoverable.
:class:`InfoPanelSource`, :class:`GalleryMediaSource` and
:class:`BoundaryPanelSource` do opt in on their subclasses' behalf, because
those three base classes define the payload themselves from an already-uniform
contract (a render context, a media-item list, a boundary row) rather than from
anything a subclass can smuggle arbitrary data through.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import timedelta
from enum import StrEnum
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.utils import timezone

from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError, RequestCancelledError, ServiceDisabledError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.subscriptions import SiteFeature
    from urbanlens.dashboard.services.apis.assets.base import MediaProvider
    from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, StreetViewProvider, StreetViewSlide
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary

from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock

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

    A source lists every shape it can serve in :attr:`PanelSource.api_kinds`,
    and :meth:`PanelSource.api_payload` puts each of those shapes under the
    matching top-level key. That pairing is the whole point of the interface:
    a native client branches on the *kind*, never on the source key, so a
    panel contributed by a plugin written long after the client shipped still
    renders instead of being ignored as an unknown string.

    A source may declare more than one kind when it genuinely serves more than
    one (the CRIS plugin is both an information card and a media provider); its
    payload then carries both keys.

    Members:
        INFO: ``{"info": {...}}`` - the information-card contract built by
            :func:`info_card`; the JSON twin of ``_simple_info_panel.html``.
        MEDIA: ``{"media": [{...}, ...]}`` - :class:`MediaItem` dicts, the same
            items the web Media gallery renders as tiles.
        BOUNDARY: ``{"boundary": {...}}`` - GeoJSON geometry plus the
            provenance a client needs to decide whether drawing it is honest.
        BUILDINGS: ``{"buildings": [{...}, ...]}`` - one row per structure
            standing on the pin's parcel, each already paired with the child
            pin (if any) that covers it.
    """

    INFO = "info"
    MEDIA = "media"
    BOUNDARY = "boundary"
    BUILDINGS = "buildings"


#: Keys of an ``InfoPanelSource.render_context`` result that carry panel *data*
#: rather than template plumbing (``nested``, and the ``section_id``/``icon``/
#: ``title``/``pin`` the dispatcher injects afterwards). The API's info card is
#: built by copying this allowlist rather than by passing the context straight
#: through, so a plugin that later stashes something private in its context -
#: a raw upstream response for a template tag to chew on, an internal id - does
#: not silently start publishing it the moment someone edits that plugin.
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

    Every panel that reads as "some facts about this place" answers in this
    exact shape, whether it renders through ``_simple_info_panel.html`` on the
    web or has fully bespoke markup there (NPS, Nominatim, Azure Maps). Having
    one constructor rather than each source hand-rolling a dict is what keeps
    that promise true: a client can lay out the card once and every panel,
    including ones added later, lands in it.

    All keys are always present, including the ones a given source never fills.
    A stable key set costs a few null bytes and saves every consumer from
    guessing whether a missing key means "no value" or "older server".

    Args:
        heading_name: Primary line - usually the place's name at this provider.
        chips: Short category/status pills (e.g. ``["Historic building"]``).
            Falsy entries - including None - are dropped, which is why the
            annotation admits them: a caller can write
            ``chips=[data.get("kind_label")]`` without first proving the key
            exists, and a blank pill never reaches a client.
        facts: Icon-led quick facts, each ``{"icon", "text", "href"?}``.
        meta: Label/value rows, each ``{"label", "value", "href"?}``.
        header_link: ``{"url", "label"}`` for the card's header affordance.
        footer_link: ``{"url", "label"}`` for the card's "view on X" link.
        image_url: A single representative image, for the panels that have one
            (NPS park photos, Nominatim's ``image`` tag). Absolute upstream URL
            - never proxied, so a client must be prepared for it to 404.
        description: A paragraph of prose, for the panels that have one.

    Returns:
        The info-card dict, ready to nest under the payload's ``"info"`` key.
    """
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
        The equivalent :func:`info_card`, carrying only the allowlisted data
        keys - see :data:`_INFO_CONTEXT_DATA_KEYS` for why that is an
        allowlist and not a straight copy.
    """
    return info_card(**{key: context.get(key) for key in _INFO_CONTEXT_DATA_KEYS})


class PanelSource(ABC):
    """One external-data panel: readiness check plus Celery-side fetch.

    Subclasses define where the panel's data lives and how to fill it. The
    scheduling, single-flight, and failure-suppression machinery in this
    module is shared and driven purely through this interface.

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
            Override to ``"celery"`` (the default queue, prefork pool) for a
            source whose fetch does real CPU-bound work (e.g. Overture's
            GeoParquet/Shapely geometry parsing) - many of those running at
            once on a thread pool would cause GIL contention that slows down
            every other panel sharing it, defeating the point of splitting
            the queue in the first place.
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
    queue: ClassVar[str] = "panel_fetch"
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset()
    required_feature: ClassVar[SiteFeature | None] = None

    def scope(self, pin: Pin) -> str:
        """Cache-key scope identifying which rows/entries this pin's fetch fills.

        Location-scoped by default, because most panels cache per shared
        Location (two users pinning the same place share one fetch).

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

        Checked before scheduling a fetch so a source with nothing to work
        with (e.g. no coordinates, no address, no name) degrades to a quiet
        204 instead of polling forever. The default always allows the fetch;
        override when a source needs a precondition beyond "has a Location".

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

    @abstractmethod
    def fetch(self, pin: Pin) -> None:
        """Fetch from the upstream provider(s) and persist to the panel's store.

        Runs inside a Celery worker, never on the request path. Implementations
        persist their own results (LocationCache row, Boundary column, Django
        cache entries) including an explicit empty result when the provider
        genuinely found nothing -- an absent store entry means "not fetched
        yet", and an empty one means "fetched, nothing there".

        Args:
            pin: The pin whose panel data should be fetched.
        """

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """This panel's already-landed data as a JSON body, or None.

        Never fetches. This is a pure read of whatever ``fetch`` previously
        persisted, so it is safe to call on the request path; a source whose
        data hasn't landed yet answers None and the caller schedules a fetch
        the same way the HTMX panels do.

        The default returns None for every source, which - paired with the
        empty default :attr:`api_kinds` - means a panel is invisible to the
        API until someone deliberately writes an override. See the module
        docstring: this interface is reachable by third-party plugin code, so
        the failure mode of forgetting about it has to be an absent panel and
        not an unreviewed dump of whatever that plugin cached.

        Args:
            pin: The pin whose panel is being read. Sources that need the
                viewer's own scope (e.g. which child pins exist) read it from
                here rather than from a request, since this also runs from
                background code paths that have no request.

        Returns:
            A JSON-serializable body whose top-level keys are the source's
            declared :attr:`api_kinds` (see :class:`PanelApiKind`), or None
            when this source is not exposed, has no data yet, or has data that
            isn't worth showing (the JSON equivalent of the web panel's 204).
        """
        return None


class LocationCachePanelSource(PanelSource, ABC):
    """Base for panels whose store is a ``LocationCache`` row.

    Attributes:
        cache_source: The LocationCache ``source`` field value this panel
            reads and writes.
    """

    cache_source: ClassVar[str]

    def is_ready(self, pin: Pin) -> bool:
        """True when a fresh LocationCache row exists for this source."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        return LocationCache.get_fresh(pin.location, self.cache_source) is not None

    def cached_data(self, pin: Pin) -> dict | None:
        """This source's fresh cached payload, or None when nothing has landed.

        The read half of the store this class owns, factored out so the API
        payload builders don't each re-derive "which row, and is it stale?"
        from :attr:`cache_source`.

        Args:
            pin: The pin whose panel is being read.

        Returns:
            The row's ``data`` dict - possibly ``{}``, which means "we
            searched and found nothing", a real answer - or None when no fresh
            row exists (never fetched, or gone stale).
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if pin.location_id is None:
            return None
        row = LocationCache.get_fresh(pin.location, self.cache_source)
        return None if row is None else (row.data or {})


class InfoPanelSource(LocationCachePanelSource, ABC):
    """Base for panels that render through the generic ``_simple_info_panel.html`` template.

    A subclass owns only ``fetch`` (writing to its ``LocationCache`` row,
    inherited from ``LocationCachePanelSource``) and ``render_context``
    (turning that row's cached data into the template's context shape). The
    URL, controller dispatch, readiness/pending polling, and debug-overlay
    wiring are all fully generic (see ``PinController.panel_info``), so a new
    panel of this shape needs only a new ``InfoPanelSource`` subclass in a
    plugin - no new route, controller method, or template block.

    Panels with genuinely bespoke markup (their own JS, a listings grid, a
    map, ...) don't fit this shape and should keep a dedicated controller
    method, route, and template instead of forcing themselves in here.

    Every subclass is exposed on the external API as
    :attr:`~PanelApiKind.INFO` without doing anything, because the payload is
    derived from ``render_context`` - a contract the subclass already had to
    satisfy for the web, and one narrow enough (heading/chips/facts/meta/links)
    that there is nothing for a careless subclass to accidentally publish
    through it. A subclass that genuinely must not reach the API sets
    ``api_kinds = frozenset()`` to opt back out.
    """

    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO})

    @abstractmethod
    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build ``_simple_info_panel.html``'s context from cached data.

        ``section_id``/``icon``/``title`` are filled in by the caller from
        this source's own class attributes - don't include them here.

        Args:
            pin: The pin whose panel is being rendered.
            data: The ``LocationCache`` row's ``data`` dict (``{}`` when the
                fetch found nothing).

        Returns:
            A context dict (may include ``heading_name``, ``chips``,
            ``meta``, ``header_link``, ``footer_link``), or None when there's
            nothing worth showing (renders a 204).
        """

    def debug_count(self, data: dict) -> int:
        """Item count reported in the debug overlay.

        Defaults to 1 (one record found); override for panels whose cached
        data represents a list of distinct results.

        Args:
            data: The ``LocationCache`` row's ``data`` dict.
        """
        return 1

    def api_info(self, pin: Pin, data: dict) -> dict[str, Any] | None:
        """This source's cached data as an :attr:`PanelApiKind.INFO` card.

        Routed through ``render_context`` on purpose rather than reading
        ``data`` again independently: the alternative is two mappings from the
        same cached row to the same facts, which drift the first time someone
        fixes a field name in one of them.

        Args:
            pin: The pin whose panel is being read (``render_context`` may
                branch on it - see the CRIS plugin's site-scope handling).
            data: The ``LocationCache`` row's ``data`` dict.

        Returns:
            The info card, or None when ``render_context`` decided there is
            nothing worth showing.
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
        geo_boundary: Restricts this panel to a geographic region (see
            ``services.geo.geo_boundary``); None means unrestricted.
    """

    geo_boundary: ClassVar[GeoBoundary | None] = None

    def gate(self, pin: Pin) -> bool:
        """Skip scheduling a fetch for a pin with no usable coordinates, or outside ``geo_boundary``."""
        lat, lng = pin.effective_latitude, pin.effective_longitude
        if not (lat and lng):
            return False
        return self.geo_boundary is None or self.geo_boundary.contains(lat, lng)


class GalleryMediaSource(LocationCachePanelSource, ABC):
    """Base for anything that can appear as a source tab in the Media gallery.

    The pin detail page's Media gallery combines results from several
    unrelated providers (archive/media search engines, business directories,
    imagery APIs, ...) behind one uniform per-source loader/tab. Each
    provider needs only its own ``fetch`` (writing to its ``LocationCache``
    row, inherited scheduling/readiness/failure handling from
    ``LocationCachePanelSource``) and ``media_items`` (turning that row's
    ``data`` back into displayable items) - the gallery controller and
    template are otherwise oblivious to which provider it's rendering.

    The external API gets the same deal: every subclass is exposed as
    :attr:`~PanelApiKind.MEDIA` for free, because ``media_items`` already
    normalizes the provider's cached response into a fixed dataclass and there
    is no route through it for a subclass to publish anything else.
    """

    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.MEDIA})

    @abstractmethod
    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn this source's cached ``LocationCache.data`` into gallery items.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source
                (``{}`` when the fetch found nothing).

        Returns:
            The items to render as ``.media-item`` tiles; may be empty.
        """

    def media_is_ready(self, data: dict) -> bool:
        """Whether a cached row's *media* half has actually been filled in.

        Almost always True: a row exists because this source's ``fetch`` wrote
        it, and that fetch produced the items. The exception is a source whose
        ``cache_source`` is shared with a background enrichment source that
        fills only part of the payload - such a row is a legitimate answer for
        the info panel while being no answer at all for the gallery, and
        without this hook the gallery would render it as an authoritative
        "this provider found nothing" for the whole cache window.

        Both gallery surfaces (pin detail and wiki) consult this on the row
        they read, rather than each re-deriving the same condition.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source.

        Returns:
            True when ``media_items`` can be trusted for this row.
        """
        return True

    def api_media(self, data: dict) -> list[dict[str, Any]]:
        """This source's cached data as plain JSON media dicts.

        Args:
            data: The ``LocationCache`` row's ``data`` dict for this source.

        Returns:
            One dict per :class:`MediaItem`, field-for-field. Some providers
            deliberately emit *relative* ``url``/``thumb_url`` values pointing
            at an in-app proxy (CRIS attachments, Immich assets) so an upstream
            API key never reaches a client; those stay relative here too and a
            native client must resolve them against its API base URL rather
            than assuming every media URL is absolute.
        """
        return [asdict(item) for item in self.media_items(data)]

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The provider's cached media as ``{"media": [...]}``, or None.

        An empty list is a real answer ("searched, found nothing") and is
        returned as such; None means the fetch hasn't landed yet, which is the
        distinction the caller needs to decide between showing an empty tab and
        scheduling a fetch.
        """
        data = self.cached_data(pin)
        if data is None:
            return None
        return {PanelApiKind.MEDIA.value: self.api_media(data)}


class MediaPanelSource(GalleryMediaSource):
    """One provider of the combined Media gallery (Smithsonian, Wikimedia, LOC).

    Instantiated once per provider; ``make_gateway`` builds the concrete
    :class:`MediaProvider`, whose ``get_media`` owns the LocationCache write.
    """

    def __init__(self, key: str, cache_source: str, gateway_factory) -> None:
        """Bind this source to one media provider.

        Args:
            key: Registry key, matching the URL's ``source`` segment.
            cache_source: The provider gateway's ``service_key`` (its
                LocationCache source).
            gateway_factory: Zero-argument callable building the gateway.
        """
        # Per-instance rather than ClassVar: three providers share this class.
        self.key = key
        self.cache_source = cache_source
        self._gateway_factory = gateway_factory

    def make_gateway(self) -> MediaProvider:
        """Build this provider's gateway instance."""
        return self._gateway_factory()

    @staticmethod
    def search_terms(pin: Pin, gateway: MediaProvider) -> list[str]:
        """Candidate search queries for this pin, most specific first.

        Some search engines (e.g. Wikimedia Commons) return nothing for an
        overly specific query like a full street address, but do match a
        broader name + city/state query -- multi-query providers get a second,
        narrower candidate to widen recall (see ``MediaProvider.get_media``).

        Args:
            pin: The pin to build search queries for.
            gateway: The provider gateway (controls quoting/country flags).

        Returns:
            Ordered, de-duplicated list of query strings; may be empty.
        """
        if gateway.reject_address_derived_names and pin.location is not None:
            from urbanlens.dashboard.services.locations.naming import is_address_derived_name

            fallback_name = pin.meaningful_official_name or pin.meaningful_name
            # A pin with no real landmark name falls back to its raw street
            # address as the "name" - a query built from that has no genuine
            # narrowing power (just a house number and a generic street-type
            # word), so a provider whose relevance ranking treats query words
            # as independent OR terms is skipped entirely rather than fed a
            # guaranteed-noisy query (see LibraryOfCongressMediaProvider).
            if fallback_name and is_address_derived_name(fallback_name, pin.location):
                return []

        search_term = pin.get_unique_search_name(
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
                include_country=gateway.search_with_country,
                quote_name=gateway.quote_name,
                include_address=False,
                quote_locality=gateway.quote_locality,
            )
            if narrow_term and narrow_term not in terms:
                terms.append(narrow_term)
        return terms

    def fetch(self, pin: Pin) -> None:
        """Fetch this provider's media; ``get_media`` persists to LocationCache."""
        gateway = self.make_gateway()
        terms = self.search_terms(pin, gateway)
        if not terms:
            from urbanlens.dashboard.models.cache.location_cache import LocationCache

            LocationCache.set(pin.location, self.cache_source, {"items": []}, query_key="")
            return
        gateway.get_media(pin.location, terms)

    def gate(self, pin: Pin) -> bool:
        """Geo-restricted providers and pins with no usable search name are skipped."""
        gateway = self.make_gateway()
        if gateway.geo_boundary is not None and not gateway.geo_boundary.contains(pin.effective_latitude, pin.effective_longitude):
            return False
        return bool(self.search_terms(pin, gateway))

    def media_items(self, data: dict) -> list[MediaItem]:
        """Rebuild ``MediaItem``s from this provider's cached ``{"items": [...]}``."""
        return [MediaItem(**item) for item in (data or {}).get("items", [])]


class BoundaryPanelSource(PanelSource):
    """Auto-generated default boundaries stored on the Location's Boundary rows.

    Location-scoped: the generated property/building boundaries are shared
    place data, so one fetch serves every pin (and the wiki page) at that
    Location. This is the lazy path that replaced eager generation on pin
    creation - the provider chain only runs when someone actually views a pin
    detail page (or creates a wiki).
    """

    key = "boundary"
    # Stays on the default (prefork) queue, not the fast thread-pool queue -
    # generate_location_boundaries does real CPU-bound work (gunzipping
    # building-footprint shards, shapely geometry ops), and several of those
    # running concurrently on a thread pool would cause enough GIL contention
    # to slow down every other panel sharing it. See PanelSource.queue.
    queue = "celery"
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.BOUNDARY})

    def scope(self, pin: Pin) -> str:
        """Location-scoped: default boundaries are keyed by Location."""
        return f"loc{pin.location_id}"

    def is_ready(self, pin: Pin) -> bool:
        """True when the provider chain has run for the pin's Location.

        ``place_resolved_at`` is stamped even when nothing was found, so a
        fruitless run doesn't retrigger the chain on every page view.
        """
        if pin.location_id is None:
            return True
        return pin.location.place_resolved_at is not None

    def fetch(self, pin: Pin) -> None:
        """Run the boundary provider chain and persist generated polygons.

        The chain's heavy steps (downloading and gunzipping building-footprint
        shards, shapely geometry work) are exactly why this runs in Celery: on
        the request path that CPU work blocked the entire gevent event loop.
        Persistence uses queryset ``update()`` calls (see
        ``generate_location_boundaries``) so it can never clobber geometry
        saved concurrently by the web request.
        """
        from urbanlens.dashboard.services.locations.boundaries import generate_location_boundaries

        if pin.location_id is None or self.is_ready(pin):
            return
        generate_location_boundaries(pin.location, name=pin.effective_name)

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The pin's effective property and building geometry as GeoJSON.

        Resolution is delegated to ``Boundary.objects.resolve_for_pin``, the
        same chain the map draws from (pin's own row, then an inherited parent
        boundary, then the wiki's, then the location default, then a
        synthesized circle) - reimplementing any part of that ordering here
        would let the API and the map disagree about where a place *is*.

        The ``source`` and ``is_fallback_circle`` flags are not decoration.
        For a property with no real geometry anywhere, ``resolve_for_pin``
        synthesizes a fixed-radius circle around the coordinates so the map has
        something to show; a client that drew that as though it were a surveyed
        parcel boundary would be asserting a property line this app has never
        actually looked up. Both flags are emitted so a client can style it as
        the approximation it is (or drop it), rather than having to infer
        "suspiciously round" from the coordinates.

        Args:
            pin: The pin whose boundaries are being read.

        Returns:
            ``{"boundary": {"property": ..., "building": ...}}`` with each side
            either a ``{"geometry", "source", "is_fallback_circle"}`` dict or
            None, or None overall when the pin has no location or neither side
            resolved to anything.
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
            ``{"geometry", "source", "is_fallback_circle"}``, or None when
            nothing resolved for this type (which for BUILDING is the normal
            case - a missing building boundary means "no known building", and
            unlike PROPERTY it has no circle fallback).
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

    Each provider caches its own slides (24h, keyed by coordinates), so
    running this twice is one round of upstream fetches followed by pure cache
    hits -- the Celery warm-up task and the request-path render share this
    exact function.

    Args:
        lat: WGS-84 latitude.
        lng: WGS-84 longitude.

    Returns:
        Tuple of (all slides in provider order, per-provider outcomes for the
        admin debug overlay).
    """
    slides: list[SatelliteSlide] = []
    results: list[ProviderFetchResult] = []
    for gateway in _satellite_gateways():
        service = gateway.service_key or type(gateway).__name__
        try:
            gateway_slides, from_cache = gateway.get_satellite_slides(lat, lng)
            slides.extend(gateway_slides)
            results.append(ProviderFetchResult(service, from_cache=from_cache, count=len(gateway_slides)))
        except RateLimitExceededError as rle:
            # Recorded as a failed provider, not skipped silently: it contributed
            # nothing *and* may well succeed shortly, which is the difference
            # between "this location has no imagery" and "we did not get to ask".
            # SlidesPanelSource.fetch reads these to decide how long to trust an
            # empty result.
            logger.debug("Satellite view provider %s rate-limited -> %s", service, rle)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
        except RequestCancelledError as rce:
            # A disabled service is a stable state, not a transient one - it is
            # not a reason to keep re-warming this panel every few minutes.
            logger.debug("Satellite view provider %s request cancelled -> %s", service, rce)
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
        Tuple of (all slides in provider order, per-provider outcomes for the
        admin debug overlay).
    """
    slides: list[StreetViewSlide] = []
    results: list[ProviderFetchResult] = []
    for provider in _street_view_gateways():
        service = provider.service_key or type(provider).__name__
        try:
            provider_slides, from_cache = provider.get_street_view_slides(lat, lng)
            slides.extend(provider_slides)
            results.append(ProviderFetchResult(service, from_cache=from_cache, count=len(provider_slides)))
        except RateLimitExceededError as rle:
            # Recorded as a failed provider, not skipped silently: it contributed
            # nothing *and* may well succeed shortly, which is the difference
            # between "this location has no imagery" and "we did not get to ask".
            # SlidesPanelSource.fetch reads these to decide how long to trust an
            # empty result.
            logger.debug("Street view provider %s rate-limited -> %s", service, rle)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
        except RequestCancelledError as rce:
            # A disabled service is a stable state, not a transient one - it is
            # not a reason to keep re-warming this panel every few minutes.
            logger.debug("Street view provider %s request cancelled -> %s", service, rce)
        except Exception:
            # TODO: Catch specific exceptions
            logger.warning("Street view provider %s failed", service, exc_info=True)
            results.append(ProviderFetchResult(service, from_cache=False, count=0, ok=False))
    return slides, results


class SlidesPanelSource(PanelSource, ABC):
    """Base for the satellite/street carousels, whose store is per-provider Django cache.

    The providers each cache their own slides for 24h keyed by coordinates;
    "ready" is tracked with a separate summary marker set after a full
    warm-up pass, whose TTL is deliberately shorter than the slide caches so
    the marker always lapses (triggering a background re-warm) before the
    underlying entries can expire mid-render.
    """

    # Deliberately absent from the external API for now: api_kinds stays empty
    # and api_payload keeps PanelSource's None. These carousels are the one
    # panel family whose "data" is the imagery itself - each slide carries a
    # base64 `data:` URI fetched server-side (so the provider's API key never
    # reaches a client), and several providers x ~5 slides each is plausibly
    # 5-15 MB in a single JSON response. The external API's throttle counts
    # requests, not bytes, so exposing these would hand any key holder a
    # multi-megabyte amplifier that the rate limiter cannot see. The fix is a
    # signed slide-image proxy - the payload becomes a list of short URLs the
    # client fetches individually, which the throttle can then actually count -
    # and that proxy does not exist yet. Until it does, a mobile client renders
    # its own map imagery instead.
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

        ``collect`` reports per-provider outcomes, and they decide the marker's
        lifetime. Every provider answering - even with nothing - is a real
        "there is no imagery here", worth remembering for
        :data:`SLIDES_READY_TTL_SECONDS`. A provider that failed or was
        rate-limited answered nothing at all, and treating that as a settled
        empty result left the panel blank for twelve hours on the strength of one
        transient refusal. Those retry on the ordinary failure cadence instead.
        """
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

    Everything checked here is something the base class lets you omit and that then
    fails *quietly* at render rather than loudly at registration: ``section_id`` and
    ``title`` default to empty strings, so a panel missing them renders a section with
    no DOM id and no heading, and a cache-backed panel with no ``cache_source`` looks
    up the empty key forever and sits in its pending state.

    Args:
        source: The panel source to check.

    Returns:
        A list of problems, empty when the source is well-formed.
    """
    problems: list[str] = []
    if not getattr(source, "key", ""):
        problems.append("key is required (it addresses the panel in URLs, cache keys and Celery arguments)")

    # The two presentation attributes are required only of sources that render a
    # section of their own - which is exactly InfoPanelSource and SlidesPanelSource.
    # The other two shapes legitimately have neither: gallery media providers render as
    # tabs *inside* the combined Media gallery, whose controller supplies the
    # surrounding markup, and a source like BoundaryPanelSource renders nothing at all,
    # fetching data that other surfaces (the map, the external API) consume.
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
    """Every registered panel source, keyed by the source key used in URLs,
    Celery task arguments, and cache keys.

    Combines the core sources with the contributions of every enabled plugin.
    A plugin source whose key collides with an existing one is logged and
    skipped. A source that is registered but misconfigured is logged too - see
    :func:`panel_source_problems` for what that means and why those particular
    mistakes are worth shouting about.

    Returns:
        Mapping of source key to its :class:`PanelSource`.
    """
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


def get_panel_source(source_key: str) -> PanelSource | None:
    """Look up one panel source by key.

    Args:
        source_key: A :func:`panel_sources` key.

    Returns:
        The panel source, or None when no core panel or enabled plugin
        provides that key.
    """
    return panel_sources().get(source_key)


def _fresh_location_cache_sources(pin: Pin) -> set[str]:
    """Every ``LocationCache.source`` that has a non-stale row for this pin's location.

    One query for the whole set, deliberately replacing N calls to
    ``LocationCache.get_fresh``. The staleness rule is the same one
    ``LocationCache.is_stale`` applies (age against
    ``SiteSettings.external_data_cache_days``), just expressed as a cutoff the
    database can filter on instead of a per-row Python comparison - so this
    stays a single query no matter how many panel sources exist.

    Args:
        pin: The pin whose location's cache rows are being examined.

    Returns:
        The set of fresh source names; empty when the pin has no location.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.site_settings import SiteSettings

    if pin.location_id is None:
        return set()
    cutoff = timezone.now() - timedelta(days=SiteSettings.get_current().external_data_cache_days)
    return set(LocationCache.objects.filter(location_id=pin.location_id, updated__gte=cutoff).values_list("source", flat=True))


def panel_readiness(pin: Pin, sources: Iterable[PanelSource] | None = None) -> dict[str, bool]:
    """Whether each panel source already has data for ``pin``, in one pass.

    The bulk form of :meth:`PanelSource.is_ready`. Asking each source
    individually is one ``LocationCache`` query per source - with ~30
    registered sources (three core plus every enabled plugin's contributions)
    that is ~30 round trips to answer a question the database can answer once,
    and it happens on the pin detail page's own render. Anything that needs the
    readiness of more than one source should call this instead of looping.

    Sources are grouped by where their store actually lives, so each group
    costs one lookup:

    * ``LocationCachePanelSource`` - one query for the pin's location's fresh
      cache rows (see :func:`_fresh_location_cache_sources`).
    * ``SlidesPanelSource`` - one ``cache.get_many`` for the warm-up markers.
    * Anything else (``BoundaryPanelSource``, a plugin's bespoke source) falls
      back to its own ``is_ready``. Correctness before cleverness: a source
      this function doesn't recognise still gets the right answer, just not a
      batched one.

    Args:
        pin: The pin whose panels are being checked.
        sources: The sources to report on; defaults to every registered source.
            Pass a subset when only a few panels are in play - the batched
            lookups then cover only what was asked for.

    Returns:
        Mapping of source key to readiness. Keys are exactly the keys of the
        sources passed in (or of every registered source), so a caller can
        index it directly rather than guarding every lookup.
    """
    resolved = list(sources) if sources is not None else list(panel_sources().values())
    readiness: dict[str, bool] = {}

    cache_backed = [source for source in resolved if isinstance(source, LocationCachePanelSource)]
    slide_backed = [source for source in resolved if isinstance(source, SlidesPanelSource)]
    bespoke = [source for source in resolved if not isinstance(source, (LocationCachePanelSource, SlidesPanelSource))]

    if cache_backed:
        fresh_sources = _fresh_location_cache_sources(pin)
        for cache_source in cache_backed:
            readiness[cache_source.key] = cache_source.cache_source in fresh_sources

    if slide_backed:
        ready_keys = {slide_source.key: slide_source.ready_key(pin) for slide_source in slide_backed}
        warm = cache.get_many(list(ready_keys.values()))
        for source_key, ready_key in ready_keys.items():
            readiness[source_key] = bool(warm.get(ready_key))

    for bespoke_source in bespoke:
        # Guarded per source: this map is built for the pin detail page's tab strip, and
        # panels are the plugin extensibility surface, so one plugin's is_ready() raising
        # would otherwise 500 the whole page rather than affecting its own tab. "Not
        # ready" is the safe default - the tab shows its pending state and polls, which
        # is exactly what it does for a panel whose data genuinely hasn't arrived.
        try:
            readiness[bespoke_source.key] = bespoke_source.is_ready(pin)
        except Exception:
            logger.exception("Panel source %s failed its readiness check for pin %s", bespoke_source.key, pin.pk)
            readiness[bespoke_source.key] = False

    return readiness


def panel_visible_to(user: AbstractBaseUser | AnonymousUser, source: PanelSource) -> bool:
    """Whether *user* holds the subscription feature this panel source requires.

    The single place this fact is decided, shared by the web tab strip
    (``controllers.pin._viewer_may_see_panel``, which now just calls this) and
    the external API's panel endpoints - a feature-gated panel must never be
    visible on one surface and hidden on the other.

    Args:
        user: The user asking to see the panel (typically ``request.user``).
        source: The panel source being considered.

    Returns:
        True when the source is unrestricted (the overwhelming majority) or
        the viewer holds the feature it requires.
    """
    feature = source.required_feature
    if feature is None:
        return True
    from urbanlens.dashboard.models.subscriptions import user_has_feature

    return user_has_feature(user, feature)


def schedule_panel_fetch(source_key: str, pin: Pin) -> bool:
    """Ensure a background fetch is in flight for this panel, single-flight.

    Args:
        source_key: A :func:`panel_sources` key.
        pin: The pin whose panel data should be fetched.

    Returns:
        True when a fetch is in flight (newly scheduled or already running) --
        the caller should return a polling placeholder. False when the source
        is unknown (e.g. its plugin was disabled), currently suppressed after
        a failure or disable, or the Celery broker was unreachable -- the
        caller should give up quietly (204).
    """
    source = get_panel_source(source_key)
    if source is None:
        logger.warning("schedule_panel_fetch: unknown source '%s' for pin %s", source_key, getattr(pin, "pk", None))
        return False
    if not pin.profile.external_apis_enabled:
        return False
    if cache.get(source.skip_key(pin)):
        logger.debug("schedule_panel_fetch: %s for pin %s is suppressed, skipping", source_key, pin.pk)
        return False
    # The marker's TTL covers queue wait *and* execution, so on a backed-up
    # panel_fetch queue it can lapse before the task even starts. The worker
    # therefore releases by token: without one, a fetch that outlived its marker
    # deletes the *next* schedule's marker on the way out, and the poll after
    # that dispatches a third fetch - duplicate paid API calls for one panel.
    flight_token = acquire_lock(source.flight_key(pin), FLIGHT_TTL_SECONDS)
    if flight_token is not None:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import fetch_panel_source

        logger.debug("schedule_panel_fetch: dispatching %s for pin %s to queue '%s'", source_key, pin.pk, source.queue)
        if safely_enqueue_task(fetch_panel_source, source_key, pin.pk, flight_token, queue=source.queue) is None:
            # Broker down: a raised error here would 500 every panel on the pin
            # detail page at once. Release the just-claimed single-flight marker
            # so the next poll retries the enqueue instead of waiting out
            # FLIGHT_TTL_SECONDS behind a task that was never queued.
            release_lock(source.flight_key(pin), flight_token)
            return False
    return True


def _release_flight(source, pin: Pin, flight_token: str | None) -> None:
    """Drop the single-flight marker, if it is still this fetch's.

    Args:
        source: The panel source being fetched.
        pin: The pin whose panel was fetched.
        flight_token: Token from the scheduling call, or None for a task enqueued
            before tokens existed - those release unconditionally, as they did
            before, rather than leaking the marker until its TTL.
    """
    if flight_token is None:
        cache.delete(source.flight_key(pin))
    else:
        release_lock(source.flight_key(pin), flight_token)


def run_panel_fetch(source_key: str, pin: Pin, flight_token: str | None = None) -> None:
    """Execute one panel fetch inside the Celery worker.

    Owns the failure policy so individual sources don't have to:

    * Success clears the single-flight marker; the next poll renders.
    * A rate-limit or service-disabled signal suppresses the source for
      :data:`DISABLED_SKIP_TTL_SECONDS` -- polls stop immediately and the
      panel stays quietly absent until the marker lapses.
    * Any other failure suppresses for :data:`FAILURE_SKIP_TTL_SECONDS`, so a
      broken provider degrades to an absent panel instead of being retried by
      every page load's poll cycle.

    Args:
        source_key: A :func:`panel_sources` key.
        pin: The pin whose panel data should be fetched.
    """
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
        source.fetch(pin)
    except (RateLimitExceededError, ServiceDisabledError) as exc:
        logger.debug("Panel fetch %s for pin %s skipped: %s", source_key, pin.pk, exc)
        cache.set(source.skip_key(pin), 1, DISABLED_SKIP_TTL_SECONDS)
    except SoftTimeLimitExceeded:
        # Celery's own worker log already recorded the soft time limit at
        # WARNING with full task context; a second ERROR-level traceback here
        # would just be noise for the same event. Suppress like any other
        # failure and let the task end - re-raising would still hit the hard
        # time limit before doing anything useful with the remaining budget.
        logger.warning(
            "Panel fetch %s for pin %s hit its soft time limit after %.1fs; suppressing for %ss",
            source_key,
            pin.pk,
            time.monotonic() - started,
            FAILURE_SKIP_TTL_SECONDS,
        )
        cache.set(source.skip_key(pin), 1, FAILURE_SKIP_TTL_SECONDS)
    except Exception:
        logger.exception("Panel fetch %s for pin %s failed after %.1fs", source_key, pin.pk, time.monotonic() - started)
        cache.set(source.skip_key(pin), 1, FAILURE_SKIP_TTL_SECONDS)
    else:
        logger.debug("Panel fetch %s for pin %s finished in %.1fs", source_key, pin.pk, time.monotonic() - started)
    finally:
        _release_flight(source, pin, flight_token)
