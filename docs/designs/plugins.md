# UrbanLens Plugin System

External integrations — third-party APIs and the features built on them — are packaged as
**plugins** so each UrbanLens install can add, remove, and disable them independently.
The framework lives in `urbanlens.dashboard.plugins`.

## What a plugin is

A plugin is a single class subclassing `UrbanLensPlugin` that bundles everything one
integration needs:

```python
from urbanlens.dashboard.plugins import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults


class NpsPlugin(UrbanLensPlugin):
    """National Park Service information for pinned locations."""

    name = "nps"                        # unique slug (required)
    verbose_name = "National Park Service"
    description = "Shows nearby US national park information on the pin detail page."
    author = "UrbanLens"
    version = "1.0"
    order = 100                         # sort key for aggregated contributions

    def get_service_defaults(self):
        """Rate-limit defaults; an ApiRateLimit row is auto-created from these."""
        return {"nps": ServiceDefaults(display_name="National Park Service API", calls_per_minute=10, calls_per_day=500, usa_only=True)}

    def get_panel_sources(self):
        """Pin-detail panels (PanelSource subclasses)."""
        return [NpsPanelSource()]
```

### Contribution points

| Method | Contributes |
| --- | --- |
| `get_service_defaults()` | Default `ApiRateLimit` config per service key (rate limits, USA-only flag, notes) |
| `get_panel_sources()` | `PanelSource` panels on the pin detail page (Wikipedia-style sections and Media-gallery providers) |
| `get_satellite_providers()` | `SatelliteViewProvider` gateways for the satellite carousel |
| `get_street_view_providers()` | `StreetViewProvider` gateways for the street-view carousel |
| `get_name_providers()` | `NameProvider` sources of place-name candidates for a location (see below) |
| `get_enrichment_sources()` | `EnrichmentSource` kinds of data the hourly background-enrichment task backfills for every pinned/wiki'd location (see below) |
| `get_photo_keyword_providers()` | `PhotoKeywordProvider` strategies that keyword uploaded photos in the background, making them text-searchable (see below) |
| `register(hooks)` | Arbitrary action/filter callbacks on the shared hook bus |

Contributions across plugins are ordered by `(plugin.order, plugin.name)` — the imagery
carousels use this for slide ordering (Google Maps is 10, Esri 20, ...).

New extension points should prefer a dedicated `get_*` method on `UrbanLensPlugin` when
the contribution is a typed object the core aggregates; use the hook bus
(`urbanlens.dashboard.plugins.hooks`) for lifecycle notifications and lightweight
value-transforming filters.

### Panel sources

`get_panel_sources()` is the most-used contribution point, and the class it returns is the
part you actually write. Pick the shape by what the panel *renders*, not by where its data
comes from:

| Base class | Use when | Must declare |
| --- | --- | --- |
| `InfoPanelSource` | The panel is its own section on the pin detail page (the common case) | `key`, `cache_source`, `section_id`, `icon`, `title`, `fetch`, `render_context` |
| `CoordinateGatedInfoPanelSource` | Same, but meaningless without coordinates | the above; optionally `geo_boundary` to restrict it to a region |
| `GalleryMediaSource` | The panel is a source tab inside the combined Media gallery | `key`, `cache_source`, `fetch`, `media_items` |
| `SlidesPanelSource` | The panel feeds the satellite/street-view carousels | `key`, `section_id`, `icon`, `title`, `collect(lat, lng)`; readiness is a cache flag, not a `LocationCache` row |
| `PanelSource` | Nothing is rendered - the fetch backfills data other surfaces read (e.g. `BoundaryPanelSource`, whose boundaries the map and the external API consume) | `key`, `is_ready`, `fetch` |

Only the two *section*-rendering shapes need `section_id` and `title`: gallery providers sit
inside markup the gallery controller supplies, and a data-only source renders nothing at all.

A complete `InfoPanelSource`, modelled on the shipped Photon panel:

```python
class TidesPanelSource(CoordinateGatedInfoPanelSource):
    """Tide predictions for a coastal pin."""

    key = "tides"                       # URL segment, Celery argument, cache key
    cache_source = "tides"              # LocationCache.source this panel reads and writes
    section_id = "tides-section"        # DOM id HTMX swaps against
    icon = "waves"                      # Material Symbols name
    title = "Tides"                     # panel heading, and the pending placeholder's

    def gate(self, pin):
        """Optional. Return False to skip fetching entirely - the panel 204s quietly."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin):
        """Runs in Celery. Must write LocationCache under `self.cache_source`."""
        data = TidesGateway().for_point(pin.effective_latitude, pin.effective_longitude)
        LocationCache.set(pin.location, self.cache_source, data, query_key="")

    def render_context(self, pin, data):
        """Build `_simple_info_panel.html`'s context, or None to render nothing (204).

        `section_id`/`icon`/`title` are filled in from the class attributes above -
        don't repeat them here. Useful keys: `heading_name`, `chips`, `meta`,
        `header_link`, `footer_link`.
        """
        if not data.get("next_high"):
            return None
        return {"heading_name": data["next_high"], "chips": [], "meta": []}
```

`is_ready` and the scheduling, single-flight and failure-suppression machinery come from
`LocationCachePanelSource` - readiness is "a fresh `LocationCache` row exists for
`cache_source`", so a panel whose `fetch` writes a *different* source string will poll
forever and never render.

#### Mistakes that fail quietly

Three of those attributes have permissive defaults, so getting them wrong raises nothing:

- a wrong or missing **`cache_source`** - the fetch writes one key, the read looks for
  another, and the panel sits in its pending state indefinitely;
- a missing **`section_id`** - the section renders with an empty DOM id, so HTMX has
  nothing to swap;
- a missing **`title`** - the panel and its loading placeholder render with no heading.

`services.pins.external_data.panel_source_problems(source)` returns these as readable
strings, `panel_sources()` logs them once per key at ERROR, and a test asserts every panel
in this repo is well-formed. Run it after adding a panel.

#### The REData gate

Most shipped panels are REData-backed and end their `gate()` with `redata_configured()`,
which is False unless `UL_REDATA_API_URL` and `UL_REDATA_API_KEY` are both set. A gated
panel returns 204 rather than rendering, which is correct for an install without REData but
looks exactly like a broken panel while you are developing one - and caught this repo's own
tests out. If a new panel never appears, check the gate before anything else.

### Name providers

A `NameProvider` (`urbanlens.dashboard.services.locations.name_resolution`) yields raw
place-name candidates for a `Location`. Providers must not make network calls — they
read data the plugin's panels already cached. The common case is one or more top-level
keys of the plugin's `LocationCache` payload, which `LocationCacheNameProvider` handles
declaratively:

```python
def get_name_providers(self):
    return [LocationCacheNameProvider(source="nps", cache_source="nps", keys=("fullName", "name"), verbose_name="National Park Service")]
```

Candidates from all plugins are cleaned, quality-gated (meaningless names and
address-derived fragments like street or city names are rejected), and persisted as
official aliases attributed to the provider's `source` slug. Sources listed in
`naming._FALLBACK_ONLY_SOURCES` (currently just Google Places, whose results are often
generic/noisy) are dropped outright whenever any other source has a surviving
candidate, and considered only when nothing else does. A `NameResolver` then picks the
official name from what's left: a name that two or more sources agree on wins;
otherwise the site-admin's source priority order (Site Admin → *Name source priority*)
decides, with unlisted sources falling back to plugin order. This ordering is an
admin-only decision - individual users cannot override it.

### Enrichment sources

An `EnrichmentSource` (`urbanlens.dashboard.services.locations.enrichment`) is one kind of
proactively backfillable data. The hourly `run_scheduled_enrichment` task computes how
much of each declared `service_keys` rate limit is safely spendable (keeping the
admin-configured buffer in reserve and pacing multi-day limits evenly), picks the
highest-impact locations still missing the data, and calls `enrich()` for each with a
stagger pause between items. Completion is tracked per source — usually via the
existence of the source's `LocationCache` row, which `LocationCacheEnrichmentSource`
handles declaratively (subclasses implement only `fetch(location)`). Sources whose
`refreshes_names` is True get official names/aliases re-resolved after each cycle.
An "attempted but found nothing" result must still persist a marker, so hopeless
locations are never retried every cycle.

### Photo keyword providers

`get_photo_keyword_providers()` returns `PhotoKeywordProvider` instances
(`services.photos.photo_keywords`). Each runs in the background after a photo upload and stores
its own `ImageKeyword` rows attributed to its `slug`, which is what makes uploaded photos
text-searchable in global search.

```python
class MyKeywordProvider(PhotoKeywordProvider):
    slug = "my_plugin_keywords"      # stored on ImageKeyword.source
    label = "My keywords"            # shown in logs and admin surfaces

    def is_available_for(self, image: Image) -> bool:
        # The pipeline already checks the uploader's `generate_photo_keywords`
        # setting; override only for provider-specific gates - a subscription
        # feature, configured credentials, a per-profile AI toggle.
        return True
```

Several providers run simultaneously and independently - one failing or being unavailable does
not suppress the others, and each owns its own rows, so re-running one never disturbs another's
keywords. Built-in examples span the range: `photo_keywords_metadata` reads keywords already
embedded in the file, `photo_keywords_classifier` and `photo_keywords_ai_vision` derive them from
image content, and the Ollama plugin contributes its own vision provider - so a plugin can add
keywording without being an AI plugin at all.

### Rules

- Plugin classes are instantiated during `AppConfig.ready()`. **Imports and `__init__`
  must never touch the database or network.** Real work belongs in the contribution
  objects, which run at request/Celery time.
- API client code stays a `Gateway` subclass (`dashboard/services/apis/...`) with a
  `service_key`, so rate limiting, call logging, and the admin enable/disable toggle
  keep working unchanged. The plugin is the *manifest* that wires the gateway into the
  app.
- A failure importing, instantiating, or calling any one plugin is logged and isolated —
  it never breaks startup or a request.

## How plugins are discovered

Discovery runs once at startup, from three sources:

1. **Bundled** — every module in `urbanlens/dashboard/plugins/builtin/`. Drop a module
   there and it is picked up automatically; modules are scanned for `UrbanLensPlugin`
   subclasses defined in them.
2. **Settings** — dotted module paths in the `UL_PLUGIN_MODULES` env setting
   (comma-separated), for site-local plugins that aren't packaged.
3. **Entry points** — pip-installed packages exposing the `urbanlens.plugins` entry-point
   group, for distributable plugins:

   ```toml
   [project.entry-points."urbanlens.plugins"]
   my_plugin = "my_package.urbanlens_plugin"   # module, plugin class, or instance
   ```

## Enabling and disabling

- **Install level**: list plugin names in the `UL_DISABLED_PLUGINS` env setting
  (comma-separated) and restart. A disabled plugin stays visible in the admin inventory
  but contributes nothing.
- **Runtime service level** (no restart): the site-admin **API Limits** page toggles
  `ApiRateLimit.enabled` per service key, which blocks the actual HTTP calls.
- The site-admin **Plugins** page (`/site-admin/plugins/`) lists every discovered plugin,
  its source, its contributions, and the enabled state of its services.

## Hooks and filters

`urbanlens.dashboard.plugins.hooks.hooks` is a WordPress-style bus:

```python
from urbanlens.dashboard.plugins.hooks import hooks

hooks.add_filter("some_value", lambda value: value + 1, priority=10)
value = hooks.apply_filters("some_value", 0)      # -> 1

hooks.add_action("plugins_loaded", lambda registry: ...)
```

Callbacks run in ascending priority (registration order breaks ties); a callback that
raises is logged and skipped. The framework currently fires one action,
`plugins_loaded` (after discovery, with the registry as argument).
