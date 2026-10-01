# N15 — `celery-metrics` has been crash-looping on staging since it was deployed, because nothing gates it on the flag it requires

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: N15` · `status: current` · `updated: 2026-09-10`

**Stopped 2026-09-10 ~14:46Z at 4,410 restarts**, by hand on damballa. The gate
below is on `release/v_0_8_0`; staging's checkout is 52 commits behind it, so the
permanent fix is a scheduled deploy rather than a container action. See
[`../handoffs/infrastructure-metrics-exporter-loop-closed.md`](../handoffs/infrastructure-metrics-exporter-loop-closed.md).

Observed 2026-09-10 on damballa:

```bash
ssh damballa 'docker ps --format "{{.Names}}\t{{.Status}}" | grep celery_metrics'
# urbanlens_staging_celery_metrics   Up Less than a second
ssh damballa 'docker inspect urbanlens_staging_celery_metrics --format "{{.RestartCount}}"'
# 2578
```

The container's own log ends each cycle with `CommandError: UL_METRICS_ENABLED is off; this
exporter has nothing to serve`, and its environment has `UL_METRICS_ENABLED=false`.

The command is right to refuse. What is missing is the gate: `docker-compose.yml`'s comment on the
service already says "this service simply restarts in a loop if metrics are off - hence deploying it
is conditional on the same flag", but nothing in the file makes that conditional - the service
carries `restart: unless-stopped` and no `profiles:` key, so `docker compose up` starts it
unconditionally and Docker restarts it forever. The stated condition lives only in the comment.

Two consequences worth naming separately:

1. **Staging has no Celery task metrics at all** - the exporter is the only thing that publishes
   them, so every dashboard or alert built on task events has been reading nothing. A metrics gap
   that presents as a running container is worse than one that presents as a missing service.
2. Each restart pays a full `django.setup()` (~178 MiB resident before it exits), several times a
   minute, forever.

Fix is one line - `profiles: ["metrics"]` on the service, with `COMPOSE_PROFILES=metrics` documented
next to `UL_METRICS_ENABLED` in `.env-sample`. Keep the `CommandError`: it is correct behaviour for
a service that should not have been started, and it is what made this diagnosable at a glance.

The same shape is worth checking for anywhere else a service's precondition is expressed as a
comment rather than as compose syntax. Not surveyed this session.

Production's `celery_metrics` container was **not** verified - the read-only inspect returned
nothing for it, and production is not a scratch environment. Check before assuming it shares the
fault.
