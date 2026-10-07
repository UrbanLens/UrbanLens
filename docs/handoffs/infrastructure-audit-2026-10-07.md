# What the infrastructure repo's 2026-10-07 audit recorded about this repo's side

- **Status: RECEIVED 2026-10-07.** The infrastructure repo's cross-repo status audit (its #8, merged as `524ceaf`) brought its own handoff records up to date and named the places this repo's records disagreed. Everything below is read from its `origin/main` at that commit. Nothing in it needs an answer from the infrastructure repo.
- **Direction: inbound.** From `UrbanLens/infrastructure` (private, so cited by path and commit, not linked) to this repo.
- `id: N49` · `status: current`

This file records what the infrastructure repo now says about threads that touch UrbanLens. Each item lists
where this repo's own record changed, so the two sides read alike.

## Handoffs of theirs that this repo had no record of

- **`docs/handoffs/urbanlens-app-local-only-media-subtrees.md`: ANSWERED 2026-10-05.** The W002 check named three of
  the five `MEDIA_ROOT` subtrees the app writes with `os.path`. [`70f96f8cb`](https://github.com/UrbanLens/UrbanLens/commit/70f96f8cb2f7143107ae5012271fb7d0d80b95a9)
  makes it name all five. It is on `release/v_0_9_0` and not on `main`, so production's 0.8.0 still warns about three until
  0.9.0 deploys. Nothing there waits on this repo.
- **`docs/handoffs/urbanlens-app-staging-deploy-findings.md`: ANSWERED 2026-10-05, with item 1 obsolete.**
  - Item 1 (a stale production-hosts denylist) was fixed by [`b08996aca`](https://github.com/UrbanLens/UrbanLens/commit/b08996aca77e9653be28722db00456ce3e945d5e),
    which refused `beta.urbanlens.org` as a production host. [`2419fb84c`](https://github.com/UrbanLens/UrbanLens/commit/2419fb84c914a13bab8e3141a13cc8919c620e3f)
    removed the entry again when beta was retired, so the entry the finding asked for no longer exists. The list in
    `tests/integration/lib/production-guard.ts` on `release/v_0_9_0` holds `urbanlens.org`, `www.urbanlens.org` and
    `app.urbanlens.org`.
  - Item 2 (the login race) is in 0.8.0 ([`d1fb1bf`](https://github.com/UrbanLens/UrbanLens/commit/d1fb1bf5d5d4bffb026b27d5183f94340ee4d317)).
  - Item 3 (the schema-guessing warning) is [`dcd3a43a9`](https://github.com/UrbanLens/UrbanLens/commit/dcd3a43a92ad6905bc4e4c0e5456b4809acb923d), on `release/v_0_9_0` and not on `main`.

## Their 0.8.0 findings: the 24 retries of one parcel

Their `redata-errors-seen-from-urbanlens-production.md` told REData, after production's first day on 0.8.0, that "the 24
retries of one parcel are UrbanLens's to explain". They are explained, and fixed on `release/v_0_9_0`, by
[#365](https://github.com/UrbanLens/UrbanLens/pull/365): `_fetch_payload` asked REData for a parcel's demographics once per
location whose property-records row was missing, and nothing shared that answer per parcel, so 24 locations on one parcel made
24 calls in a cycle. #365 asks once per parcel (a complete answer shares for an hour, a partial one for five minutes, and a
failure is remembered per parcel). It is open and not yet deployed, so production's 0.8.0 still makes them.

## Where their records and this repo's differ, and which side owns the rest

- **N8** (media and static): they answered and built it; this repo's row said SENT.
- **N20** (neighbour test): they record all four questions as open and theirs. This repo's row said two.
- **N23** (per-tier roles): the Pooler question is answered, and their superuser hook is "Not built here". It is tracked by
  [#360](https://github.com/UrbanLens/UrbanLens/issues/360).
- **N33** (the two commands): they schedule both with the 0.9.0 deploy, in their `plans/phase-9-cutover.md` #12, not now.
- **N15 and N19** (the metrics exporter): compose staging is stopped (their `4f20bd7`), so the staging deploy to carry the
  gate is moot; 0.8.0 (`d1fb1bf`) gates the exporter.
- **A 180 MB Location History upload** (#297) cannot work: their `UL_MAX_REQUEST_BODY_MB=100` is on web and websocket, and
  no chunked upload exists until [#359](https://github.com/UrbanLens/UrbanLens/issues/359).
