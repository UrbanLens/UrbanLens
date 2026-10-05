# REData composes Sentinel-2 cloudless at a street zoom, and keeps the blur forever

- **Status: ANSWERED 2026-10-05: fixed on REData `release/0.3.0` (`95753ecb`) and deployed to its staging. Production REData is still 5aabe887.** Found through UrbanLens's P232 (Jess, on production v0.8.0: the Sentinel-2
  slides are blurry). The REData code cited is `main` as of 2026-10-03.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

## What happens

`render._default_zoom` (`src/redata/parcels/services/imagery/render.py`) derives a zoom from
`imagery_render_span_meters` (300 m) over the requested width, then clamps it by `max_zoom` and Esri's
`max_map_level`. It never looks at the asset's own `resolution_meters`. `Sentinel2CloudlessGateway.annual_mosaics`
publishes `resolution_meters=10.0` and `max_zoom=18`, so a 1024-pixel composite at 41.7° N renders at z18:
0.45 m a pixel from 10 m data, each source pixel enlarged about 22 times.

`ImageryAssetDownloadView` stores that first render and ignores `zoom`/`width`/`height` on every later request,
so the blur is permanent per asset.

## What UrbanLens has changed

From the next UrbanLens release, the carousel passes `zoom` on the download when the row's `resolution_meters`
can't fill REData's default span. That zoom is the deepest one whose metres a pixel is no finer than the source's,
z13 for Sentinel-2 there, clamped by the row's `min_zoom`/`max_zoom`/`max_map_level`. This only helps assets
REData hasn't rendered yet. Every s2cloudless year at a point UrbanLens has already shown stays blurry.

## The ask

1. Clamp `_default_zoom` by `resolution_meters` as well, using the same rule, so every client gets an image its
   source supports without asking.
2. Re-render the archived copies made past their source's resolution. Each one records `render_resolution_meters`
   in `attributes`, so they are the rows where that is well below `resolution_meters` (s2cloudless and any
   time-series capture such as `nasa_gibs`). Either drop their stored file so the next download re-renders, or
   re-render them in place.

UrbanLens-side follow-up: P232's archive entry in `docs/archive/PROBLEMS-ARCHIVE.md`.
