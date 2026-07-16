# UrbanLens Plugin System

External integrations — third-party APIs and the features built on them — are packaged as
**plugins** so each UrbanLens install can add, remove, and disable them independently.
The framework lives in `urbanlens.dashboard.plugins`.

## What a plugin is

A plugin is a single class subclassing `UrbanLensPlugin` that bundles everything one
integration needs:

```python
from urbanlens.dashboard.plugins import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


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
| `register(hooks)` | Arbitrary action/filter callbacks on the shared hook bus |

Contributions across plugins are ordered by `(plugin.order, plugin.name)` — the imagery
carousels use this for slide ordering (Google Maps is 10, Esri 20, ...).

New extension points should prefer a dedicated `get_*` method on `UrbanLensPlugin` when
the contribution is a typed object the core aggregates; use the hook bus
(`urbanlens.dashboard.plugins.hooks`) for lifecycle notifications and lightweight
value-transforming filters.

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
official aliases attributed to the provider's `source` slug. A `NameResolver` then picks
the official name: a name that two or more sources agree on wins; otherwise the
site-admin's source priority order (Settings → *Name source priority*) decides, with
unlisted sources falling back to plugin order.

### Enrichment sources

An `EnrichmentSource` (`urbanlens.dashboard.services.enrichment`) is one kind of
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
