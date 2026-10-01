# Closing #6: the loop is stopped, and the permanent fix is a deploy rather than a fix

- **Status: SENT, 2026-09-10.** Records what was done to the restart-looping
  exporter on damballa, and what is deliberately left undone. Closes the
  operational half of your plan item #6; the remaining half is a scheduled
  staging deploy, which is Jess's and is not urgent.
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N19` · `status: current`

## What was done

Jess ran, on damballa, 2026-09-10 ~14:46Z:

```bash
docker stop urbanlens_staging_celery_metrics
```

Verified after:

```
status=exited  restarts=4410  policy=unless-stopped  cpu=0.00%
```

It will not come back on its own. `unless-stopped` restarts a container unless
it was *explicitly* stopped, and an explicit stop survives a daemon restart — so
this holds until someone runs `docker compose up` in the staging project, which
is the deploy described below.

Nothing was lost by stopping it. The exporter had never served anything: it
exits at startup with `CommandError: UL_METRICS_ENABLED is off; this exporter has
nothing to serve`, which is why it was looping in the first place.

## What it cost while it ran

| | |
|---|---|
| Restart count at stop | **4,410** |
| Looping since | 2026-09-09 08:17Z — about **30 hours** |
| Rate | ~3 restarts/minute (2,578 → 4,400 over ten hours, measured twice) |
| CPU while looping | **51.4% of a core**, continuously, on the host that also runs production |
| Production affected | **No** — `urbanlens_production_celery_metrics` does not exist |

Production is unaffected for a structural reason rather than by luck: production
runs a compose file with 12 services and `celery-metrics` is not one of them. It
is one of seven services that exist only on `release/v_0_8_0`
(`ai-inference`, `ai-worker`, `egress-proxy`, `media-nginx`, `media-worker`,
`media-worker-batch` are the others). Only staging tracks a branch that has it.

## What is deliberately left undone

The root cause is not code. **Staging's compose checkout is 52 commits behind
the branch it tracks:**

```
/projects/environments/staging/UrbanLens/UrbanLens
  branch: release/v_0_8_0   at cb6a18f72 (2026-09-09)
  compose: defines celery-metrics, carries no profiles: gate
  working tree: clean
  behind origin/release/v_0_8_0 by 52 commits
```

The `profiles: ["metrics"]` gate is already on that branch (`7f88bf428`). So the
permanent fix is:

```bash
git -C /projects/environments/staging/UrbanLens/UrbanLens pull --ff-only
docker compose up -d --remove-orphans     # in that project
```

**This is a staging deployment, not a container fix, and should be scheduled as
one.** Those 52 commits include changes to the process model and the database
connection budget that want their own verification, not a drive-by `up`:
`--worker-connections 20 --backlog 256` on gunicorn, `cpu_shares` weights across
16 services, `mem_reservation` floors on `app` and `db`, a new request-telemetry
middleware, and `/health/ready` gaining `connections` and `degraded` fields (the
last of which changes the readiness body's key set, which your monitoring may
assert on).

Until that deploy happens the container stays stopped and costs nothing, so
there is no time pressure.

## For your plan item #6

The operational half is closed. If it is easier to track the remainder as a
staging-deploy item rather than as a metrics-exporter item, that is probably the
truer shape of it — nothing about the exporter is now broken; staging is simply
old.

One thing worth keeping from this even after the deploy: the container had been
crash-looping for 30 hours and read as *present* in `docker ps`. It took reading
`RestartCount` to see it. Whatever eventually alerts on this host, a container
restarting three times a minute should reach it — `docker inspect --format
'{{.RestartCount}}'` is the whole check, and the failure mode it catches is one
that looks healthy from every angle that only asks "is it running".
