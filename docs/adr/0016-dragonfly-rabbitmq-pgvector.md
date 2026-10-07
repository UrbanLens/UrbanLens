---
status: accepted
date: 2026-09-16
---

# Adopt Dragonfly, RabbitMQ and pgvector together

Formerly `D16`. Detail: [`docs/designs/dragonfly-rabbitmq-pgvector-stack-adoption.md`](../designs/dragonfly-rabbitmq-pgvector-stack-adoption.md).

All three were on Jess's list of likely stack additions, to be adopted early rather than after a throwaway fix, and she asked for them directly. Dragonfly replaces Valkey as cache, sessions store and Channels layer. RabbitMQ takes the Celery broker, so the broker's unbounded keys no longer share a keyspace with sessions and cache (closing H54). pgvector is enabled on the Postgres image but not yet used.

## Consequences

- Dragonfly runs without `--cache_mode` on purpose: with it, a full store evicts silently; without it, a write that does not fit raises, which the cache backend already handles.
- RabbitMQ is pinned below 4.x: 4.x refuses kombu's transient non-exclusive pidbox queue and every Celery worker crash-loops on connect.
- The result backend stays on Dragonfly. Sessions and Channels still shared the cache instance until ADR-0022.
