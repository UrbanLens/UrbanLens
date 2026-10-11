---
status: accepted
date: 2026-09-24
---

# The Channels layer gets its own small Dragonfly

Formerly `D22`.

The shared Dragonfly runs without `cache_mode` (ADR-0016), so once full it refuses every write, including a Channels `group_send`, and the live half of chat, check-ins, notifications and games stops. What fills the cache grows with users; what the Channels layer holds is bounded. So the Channels layer gets its own Dragonfly (`channel-layer`, 256 MB), named by `UL_CHANNEL_LAYER_URL`, which falls back to the shared store when unset.

## Considered options

- A second logical database on the shared instance: Dragonfly's `maxmemory` covers every database, so a full cache would still refuse channel writes.
- `cache_mode` on the shared instance: evicts sessions and Channels keys silently.
- A RabbitMQ channel layer: different group semantics and untested here; worth revisiting if RabbitMQ becomes the only queueing system.

## Consequences

- The k3s deployments do not set `UL_CHANNEL_LAYER_URL`, so they use the fallback and are not isolated.
