# D21 — The Channels layer gets its own small Dragonfly, because the shared store refuses writes once full and a refused `group_send` is a live message nobody receives

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D21` · `status: accepted` · `updated: 2026-09-24`

Extends D16 (`designs/dragonfly-rabbitmq-pgvector-stack-adoption.md`), whose closing paragraph left
this open: after the broker moved to RabbitMQ and proxied bytes moved to `tile-cache`, sessions and
the Channels layer still shared the `dragonfly` instance with the Django cache. Verified as finding
G4-21 in N29.

## The problem

`dragonfly` runs without `--cache_mode` and with `--maxmemory=1gb` (`docker-compose.yml`, the
`dragonfly` service), deliberately: D16 finding 1 is that `cache_mode` evicts silently, and evicting
a Channels key breaks a live socket. The consequence is that a full store refuses every write,
including `channels_redis`'s. `services/core/channel_broadcast.py` treats a failed enqueue as
acceptable because the row is already durable, but the live half of chat, safety check-in chat,
notifications and game sessions stops until the cache drains. What fills the cache (sessions,
throttles, locks, map documents, saved-filter and Immich marker lists) grows with users. What the
Channels layer holds does not: `capacity` (1500) and `expiry` (60s) in `settings/base.py` bound it.

## Chosen

A third Dragonfly service, `channel-layer`: `--maxmemory=256mb` (Dragonfly's floor per thread),
`--proactor_threads=1`, no `cache_mode`, `mem_limit` 384m, `app_network` only.
`settings/base.py` reads `UL_CHANNEL_LAYER_URL` into `CHANNEL_LAYER_URL`, falling back to
`DRAGONFLY_URL` the way `PROXIED_BYTES_URL` does. A deployment that has not provisioned the
instance keeps working and simply is not isolated. Only `x-app-env` names it: `celery-worker` runs
`broadcast_channel_group_message(s)` and `app-ws` consumes; the sandbox and AI tiers enqueue that
task rather than sending themselves (`tasks.py`, `broadcast_channel_group_message`).

## Rejected

- **Another logical database on the same instance** (`redis://…/1`). Dragonfly's `maxmemory` covers
  every database on the instance, so a full cache would still refuse channel writes.
- **`cache_mode` on the shared instance.** This is D16's own rejection: it evicts sessions and
  Channels keys silently.
- **A RabbitMQ channel layer** (`channels_rabbitmq`). RabbitMQ is already deployed as the broker,
  but this would be a second backend with different group semantics, and `channels_redis_dragonfly_patch.py`
  and the WebSocket tests are written against `channels_redis`. It is worth revisiting if RabbitMQ
  becomes the one queueing system. It was not measured.

## Not done

The k3s deployments (`/projects/UrbanLens/infrastructure/platform/`) run one Dragonfly
(`--maxmemory=512mb --proactor_threads=1`) and set no `UL_CHANNEL_LAYER_URL`, so they use the
fallback. The infrastructure repo was not edited. The instance's steady-state memory under real
socket load was not measured; 256mb is a floor chosen from Dragonfly's startup check, not a
measurement.
