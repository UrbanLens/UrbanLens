# X29 — What the loading background costs, measured

> **Written by a Claude agent. Not authoritative.** Measured on
> `development_main` on 2026-09-23, against commit `6db33613f`. Re-run before
> relying on any figure here.

A gap in the tile grid used to show the map container's own flat colour, which is
whatever CSS picked rather than anything about the place being drawn. A fast zoom
opens gaps faster than tiles can fill them, and in dark mode a white flash is the
brightest thing on the screen.

## What it is now

`frontend/ts/shared/map-layers.ts` draws a blurred picture of the world behind
everything, in a pane at `z-index: 180` (Leaflet's own `tilePane` is 200).

The picture is **sixteen tiles at `z=2`, fetched from the active base's own
endpoint**, drawn once into a 1024×1024 canvas. An `L.GridLayer` subclass cuts
every underlay tile out of that canvas, so Leaflet owns the positioning, the pan
and the zoom animation while `createTile` never touches the network.

Cutting from a canvas rather than asking for a coarser tile level is the whole
point: the list of sixteen URLs is fixed forever, so a zoom or a pan is free. An
earlier attempt drew a `TileLayer` pinned to a coarse zoom, and that asked the
network for something on every gesture — measured 2 new tiles zooming 13→17 and
2 more panning SF→NYC.

Per base rather than from one generic world image, because a gap has to hold the
colours that will replace it. A different vendor's world is a different
cartographer's palette showing through every gap.

## Measured

Six base/theme combinations, eight gestures each (four zooms 13→18, four pans
across SF, NYC, Tokyo, Sydney and Iceland), fresh browser context each time:

| | world tiles at rest | after eight gestures | metered API |
|---|---|---|---|
| every combination | 16 per base visited | **0** | **0** |

On a browser that has been to the site before, all 16 come back from its own
cache: `underlay 16/16 cached` on visits 2 and 3, against a same-origin hashed
static asset control at 18/18.

How well the fill matches what replaces it, as the mean RGB of the map area with
every tile-bearing pane hidden but the underlay's, against the same area loaded
(0 is identical, 441 is black-to-white):

| base / theme | loaded map | gap fill | apart | from white |
|---|---|---|---|---|
| satellite / light | (105, 121, 111) | (64, 82, 68) | 71.1 | 318.4 |
| satellite / dark | (105, 121, 111) | (64, 82, 68) | 71.1 | 318.4 |
| street / light | (183, 187, 188) | (197, 198, 198) | 20.4 | 99.3 |
| street / dark | (36, 36, 36) | (26, 26, 26) | 17.3 | 396.6 |
| terrain / light | (175, 195, 202) | (175, 194, 200) | 2.2 | 114.7 |
| terrain / dark | (16, 32, 39) | (15, 32, 38) | 1.4 | 393.0 |

Satellite is the loosest at 71 because one pixel of a `z=2` world covers ~39 km,
so a coastal view averages in a lot of ocean. Same hue, and the fine detail is
neither available at this depth nor wanted.

## Two things the measurement found

**Terrain in dark mode was 278 apart, not 1.4.** `topographic` is a light map that
a CSS filter inverts; an underlay cut from the uninverted tiles put a bright
background behind a dark map — the flash it exists to prevent, arriving from the
other direction. The underlay pane now takes the same filter through a
`--ul-underlay-tone` custom property.

**A year is the right life for the bytes and the wrong one for an absence.**
`controllers/basemap_tiles.py` keeps `z <= 2` for 365 days, because those sixteen
URLs per layer are the background of every map on the site forever. A definitive
404 is a claim about the vendor on one day, and at this depth a wrong one is a
quadrant of every underlay missing until it expires — so `_ttl_for_absence` caps
it at the ordinary week.

## A detector that read every cache hit as a miss

CDP's `Network.responseReceived` carries `response.fromDiskCache`, and on its own
it reported **0 of 16** tiles cached on a repeat visit — which looked like the
underlay defeating the browser cache. It was not: a memory-cache hit arrives as a
separate `Network.requestServedFromCache` event and never sets that flag. Counting
both gives 16/16.

What caught it was a control, not suspicion: a hashed, `immutable` static asset in
the same run read 0/18 too, and that cannot be true. Any cache-hit detector here
needs one — the same shape of error as `response.serverAddr()`, which reads
identically for a known-cacheable asset and a metered one.
