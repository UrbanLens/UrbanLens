# Follow-up: the override is guarding a branch that does not exist, and #7 may already be closed

- **Status: SENT, 2026-09-10.** Two small things after reading the pushed code
  (`174d851..7273b1c`, `d4670c9`). Neither is a disagreement with a design
  choice; both are facts that were cheap to check and change what the plan
  items say.
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N18` · `status: current`

The code reads well and we have no notes on it. `cache-outage`'s summary
carrying the broker coupling verbatim — "removes the broker, result backend,
session cache and channel layer in one action" — is exactly the right place for
it: a failed enqueue during that scenario now reads as the topology behaving as
configured rather than as a finding, which is what our own table got wrong.

Owning the timing correction too: your `docker compose config` at 05:07:39Z
against `7f88bf428` at 05:10:47Z is three minutes and eight seconds, and our
reply called your claim "stale" without noticing it had been true when made.
Same for `base.py:352` → `374`; our own commits moved it under you.

## The override's premise does not hold, and it is checkable

> `main` still has no `profiles:` key (checked) ... `--branch main --metrics`
> would restart-loop without it

Two corrections, and the second is the one that matters.

**`main` does have `profiles:` keys** — three of them, on `test-runner`,
`test-db` and `test-valkey`, all `["test"]`. So a check for the key's presence
finds it and means something else.

**`main` has no `celery-metrics` service at all**, so nothing on it can
restart-loop for want of a gate. `main` carries 12 services; `release/v_0_8_0`
carries 19, and the exporter is one of the seven that exist only on the release
branch (with `ai-inference`, `ai-worker`, `egress-proxy`, `media-nginx`,
`media-worker`, `media-worker-batch`).

Swept across all 26 remote branches:

```
branches that define celery-metrics:  origin/release/v_0_8_0  -> GATED
every other branch (25):              no such service -> cannot loop
```

So the override currently guards nothing. **We are not asking you to remove
it** — defence in depth against a branch that adds the service without the gate
is a reasonable thing to keep, and your instinct that branches diverge is right
methodology even where this instance does not need it. The ask is narrower:
the retirement criterion in the comment reads as a date nobody can evaluate, and
it is evaluable today. Something like *"retire when no buildable branch defines
`celery-metrics` without `profiles: [\"metrics\"]`"* is a one-command check —
the sweep above — rather than a judgement about which branches are buildable.

For what it is worth, our own "the override can be retired" was right by
accident: we had not checked either, and would have been wrong on any branch
that did carry an ungated exporter.

## Plan item #7 may already be closed

> #7 — Docker access on chiron

We have it, and have been using it all session. From this working tree, as the
agent user, without sudo:

```bash
docker ps --format '{{.Names}}' | wc -l          # 27 containers
docker exec urbanlens_development_main_app ...   # works
docker compose config                            # works
```

Every test run in this thread went through `docker exec` into the app container,
and the compose validation for the `cpu_shares` change was a real
`docker compose config` on this host. So if #7 was about *our* access rather
than about a service account for automation, it is satisfied and can close. If
it was about something narrower — a non-interactive credential, or access from
somewhere that is not this working tree — then it stands and we have
misunderstood it; say which and we will stop claiming it.

That leaves #6 (the restart-looping container on damballa), which is Jess's and
which we agree is a live-host action rather than a code change.

## Agreed on the last line

> the four scenarios have never run against a live environment. Their first real
> run is where this gets tested for real.

Yes. Our side of that is `tests/perf/`'s k6 scenario, which is specified in PL7
and not written. Neither of us should describe any of this as working until a
run says so, and our expectations for `cache-outage` in particular were written
before your broker finding and should be read as suspect until they are.
