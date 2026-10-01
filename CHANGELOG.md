# Changelog

## [0.8.0](https://github.com/UrbanLens/UrbanLens/compare/v0.7.0b0...v0.8.0) (2026-10-01)

About 1,600 commits since v0.7.0: 120 features, 680 fixes, 115 performance changes. The full account is in [#188](https://github.com/UrbanLens/UrbanLens/pull/188). The main areas:

- **Media pipeline.** Upload parsing runs in a sandboxed worker, media is served from its own origin behind the media gate, third-party images are shown from this site's own copy, and a failed upload is offered back to its owner.
- **Maps.** MapLibre runs alongside Leaflet, this deployment's own PMTiles and vector basemaps are drawn, imported tile overlays are rebuilt onto this site's sheets, and the map fetches its pins once instead of twenty times.
- **Places and buildings.** REData place panels and CRIS inventory, a campus's buildings as child pins and wikis, and one list of a property's buildings and child pins (P172).
- **Social and safety.** A block hides the pair from each other in group chats and trips (I7), trips can be invited to by email without revealing whether an account exists, and self-destructing messages expire.
- **Vault.** Photos, Documents and profile-level albums.
- **AI assistant.** Native tool calling on ai-worker, behind a Django-free inference service.
- **Operations.** `/metrics`, a task outbox, per-queue time limits, an inbound rate limit, and nightly pruning.

Deploying needs a `db` rebuild before migrating, and two management commands afterwards; see #188's "Deploying" section.

### Bug Fixes

* quote nginx static-asset regex to stop it crashing on startup ([#140](https://github.com/UrbanLens/UrbanLens/issues/140)) ([fc696ac](https://github.com/UrbanLens/UrbanLens/commit/fc696acd02d99a27dcd08b55b3c70372db1a8d77))
