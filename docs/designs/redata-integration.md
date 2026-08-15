# REData integration - coverage map and designed follow-ups

*2026-08-15. Status of UrbanLens's consumption of REData's API surface after the
four-round integration push (commits `e3272d5e` … this one). REData's own docs:
`../REData/docs/api-reference.md` and `../REData/CHANGELOG.md`.*

## What UrbanLens consumes today

Every REData endpoint family is consumed except the four listed under
"Designed follow-ups" below. The newest additions:

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
| `/media/lookup/?is_aerial=true` | Aerial & Drone Media-gallery tab |
| `/reference-documents/search/` `chronicling_america` | Historic Newspapers gallery provider |
| `/parcels/{uuid}/assessments/` | Assessment history on the Property Records card |
| `/capabilities/` | REData-capabilities card on `/site-admin/api-limits/` |
| `/parcels/{uuid}/sale-records/` | Supplementary sales merged into `sales_history` → `OFFICIAL` `WikiPropertySale` rows. Rows are near-parcel with no parcel link, so attribution is by normalized situs-address (or raw Cook County PIN) match - unmatched rows are dropped rather than misattributed - and explicitly non-arms-length rows (bundle sales, nominal transfers) are excluded because the pipeline can't carry the price caveat |

## Designed follow-ups (deliberately deferred, not overlooked)

1. **`/weather/history/`** (ERA5 daily weather back to 1940). The natural
   surface is the visit record: "what was the weather when I was there" on the
   shared visit dialog / Memories, and retro-filling a trip activity's
   conditions. Needs a small gateway method plus a place in the visit dialog's
   template - the endpoint is cheap (cached per day, only gaps fetched).

2. **`/imagery/timeline/` + `POST /imagery/capture/`** (which dates exist at a
   point; materialize one). The right UX is a time slider on the satellite
   carousel - `POST /imagery/timeline/` when the slider opens (it queues
   permanent archiving), `providers_timeline.time_series` for the continuous
   sources (pick a date inside an interval, then `POST /imagery/capture/` with
   that layer's `time_series_asset_uuid`; `continuous: false` means a date may
   still 404 - not an error). This is a real frontend feature, not a panel;
   scope it as its own piece of work.

3. **`/tiles/` basemap catalogue**. REData publishes a basemap-layer catalogue
   (`/tiles/sources/`) that could extend the map's base-layer set beyond the
   built-in street/dark/topo/satellite. Needs the same key-hiding tile-proxy
   treatment as historical maps (`historical_map_tiles.py` is the template to
   copy).

Also not consumed, deliberately: per-user OAuth media (Flickr/Google
Photos/Immich stay direct - they read a *user's* library, not location data),
and the IIIF endpoints (REData's own viewers' concern; UrbanLens links out).
