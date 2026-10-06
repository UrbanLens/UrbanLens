# REData's Google Open Buildings source reads a level-4 shard whole, and the median one is 138 MiB

- **Status: SENT 2026-10-05.** Found while fixing UrbanLens's P319. Read from REData `origin/main` and
  `origin/release/0.3.2` on 2026-10-05; nothing here was run against a REData deployment.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.
- `id: N43` · `status: current`

## What REData does

`parcels/services/building_footprints/gateways.py::GoogleOpenBuildingsGateway` downloads
`open-buildings-data/v3/polygons_s2_level_4_gzip/{token}_buildings.csv.gz` with `response.content`: the whole shard,
with no size cap, then keeps up to four in `_shard_byte_cache` in memory. It runs only when a caller names
`?source=google_open_buildings`, since it is outside the default source set, and UrbanLens never names it. So this is
latent, not something UrbanLens is hitting.

## What the shards weigh

Listed from `storage.googleapis.com/storage/v1/b/open-buildings-data/o?prefix=v3/` on 2026-10-05:

| Prefix | Shards | Median | Largest | At most 64 MiB |
|---|---|---|---|---|
| `polygons_s2_level_4_gzip/` | 333 | 138 MiB | 8.4 GB | 133 |
| `polygons_s2_level_6_gzip_no_header/` | 3,330 | 8.8 MiB | 1.7 GB | 2,704 |

Lagos's level-4 shard is 1.9 GB and Luang Prabang's 3.7 GB. One non-US parcel query there would hold that in a worker,
and four cached shards could hold several gigabytes. A 404 (every US cell) returns None, which
`_cached_shard_bytes` does not keep, so each query in the US asks Google again.

## What UrbanLens did (P319, P301)

- Reads the level-6 shards. They carry no header row; their columns are the level-4 header's
  (`latitude,longitude,area_in_meters,confidence,geometry,full_plus_code`).
- Streams each shard and skips one past 64 MiB from its `Content-Length`, or by reading one byte past the cap.
- Asks only for cells under the 333 level-4 cells v3 covers (UrbanLens's
  `boundaries/data/open_buildings_v3_level_4_cells.txt`), so a US lookup makes no request.
- Remembers a missing or oversized shard for 30 days.

## The ask

Bound the download as above, or drop the source if nothing is expected to name it. Either is REData's call.
