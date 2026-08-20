# REData integration - coverage map and designed follow-ups

*2026-08-15. Status of UrbanLens's consumption of REData's API surface after the
four-round integration push (commits `e3272d5e` … this one). REData's own docs:
`../REData/docs/api-reference.md` and `../REData/CHANGELOG.md`.*

## What UrbanLens consumes today

**This section was materially wrong until 2026-08-19** and is worth reading with that in mind: it
claimed every endpoint family was consumed bar four, while a route-by-route diff found 45 of
REData's 106 routes with no UrbanLens caller at all. Two of the three "designed follow-ups" it
listed had in fact shipped, and the third had not been started. A summary claim like "everything
except N" ages badly against a service that adds endpoints weekly; the durable list is the diff, and
it now lives in `docs/PROBLEMS.md` under *"OPEN 2026-08-19: REData consumption gaps left after this
session's sweep"*, which names the 15 unconsumed routes judged worth wiring up.

The newest additions:

| REData surface | UrbanLens consumer |
| --- | --- |
| `/walkability/`, `/land-cover/`, `/soil/` | Site Conditions panel (`plugins.builtin.redata_site_conditions`) |
| `/air-quality/` | Air Quality panel |
| `/underground/` | Underground Structures panel |
| `/incidents/` | Reported Incidents panel |
| `/permits/` | Permits & Violations panel |
| `/hydrology/` | Water & Hydrology panel |
| `/hazards/` (`nifc_wildfires`, `fema_disasters`) | Fire & Disaster History panel (earthquakes stay on the seismic panel) |
| `/street-view/timeline/` | Street-view carousel (one dated slide per capture date) |
| `/maps/` + warped tiles | Historical-map tile overlays (`MapImageOverlay.tile_url_template`, tile proxy in `controllers/historical_map_tiles.py`) |
| `/media/lookup/` + an `is_aerial` filter applied client-side | Aerial & Drone Media-gallery tab. `?is_aerial=` is a `filterset_fields` entry on REData's `/media/` *viewset*, not a parameter `/media/lookup/` reads - passing it did nothing, so the tab showed every nearby media item as drone footage until 2026-08-19 |
| `/reference-documents/search/` `chronicling_america` | Historic Newspapers gallery provider |
| `/parcels/{uuid}/assessments/` | Assessment history on the Property Records card |
| `/capabilities/` | REData-capabilities card on `/site-admin/api-limits/` |
| `/parcels/{uuid}/sale-records/` | Supplementary sales merged into `sales_history` → `OFFICIAL` `WikiPropertySale` rows. Rows are near-parcel with no parcel link, so attribution is by normalized situs-address (or raw Cook County PIN) match - unmatched rows are dropped rather than misattributed - and explicitly non-arms-length rows (bundle sales, nominal transfers) are excluded because the pipeline can't carry the price caveat |

## Designed follow-ups

1. ~~**`/weather/history/`**~~ (ERA5 daily weather back to 1940) - **done for trips.**
   `services/apis/locations/redata_weather_gateway.RedataWeatherHistoryGateway`
   fetches a date range; `services/locations/visit_weather.py` answers per
   (location, day), cache-first, and refuses to ask for a day ERA5 cannot yet
   have (before 1940, or inside its ~6-day publication lag) so those never
   become cached blanks. A recorded day never changes, so that cache
   deliberately ignores `LocationCache.is_stale`.
   The surface is the trip weather panel: a past activity shows what the weather
   actually was on its day (`controllers.trip._build_activity_history`), fetched
   as one range per location rather than one call per day. The *visit* surfaces
   the original design named - the shared visit dialog and Memories - are still
   open, and `recorded_weather(location, day)` is the single-day entry point they
   would use.

2. ~~**`/imagery/timeline/` + `POST /imagery/capture/`**~~ - **done.** See
   `services/locations/imagery_timeline.py` and
   `plugins/builtin/satellite_imagery.py`. Note that `POST /imagery/capture/`
   itself is still not called: `_historical_slides` correctly *skips*
   continuous `time_series` rows rather than materialising a date from them, and
   as of 2026-08-19 `_slide_from_result` skips them on the `/imagery/` path too
   (they were previously rendered straight into an `<img src>` with a literal
   `{time}` in the URL - a guaranteed broken slide on every pin, for every
   registered NASA GIBS layer).

3. ~~**`/tiles/` basemap catalogue**~~ - **done.** See
   `controllers/basemap_tiles.py` and
   `services/apis/locations/redata_basemap_tiles_gateway.py`, which took the
   key-hiding tile-proxy treatment from `historical_map_tiles.py` as intended.

Also not consumed, deliberately: per-user OAuth media (Flickr/Google
Photos/Immich stay direct - they read a *user's* library, not location data),
and the IIIF endpoints (REData's own viewers' concern; UrbanLens links out).
