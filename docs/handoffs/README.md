# Handoffs

Correspondence with another repository or another team. Each file records what
was asked, what was measured, and what was decided on one particular day, and
is **frozen at its send date**: when its status changes the header line changes
and the body does not. A record edited to agree with the present is a second
copy of the present with a misleading date on it.

That makes this directory the one place under `docs/` exempt from the
present-tense rule in [`../README.md`](../README.md). Everywhere else a stale
sentence is a bug and gets rewritten; here it is the content.

`grep -n 'Status:' docs/handoffs/*.md` is the whole picture.

| Note | Direction | Status |
|---|---|---|
| [`infrastructure-media-and-static.md`](infrastructure-media-and-static.md) — reply on `/static/`, object-storage media, and the 100 MB upload cap | outbound, `UrbanLens/infrastructure` | SENT 2026-09-06 |
| [`infrastructure-0054-friendship-merge.md`](infrastructure-0054-friendship-merge.md) — reply confirming 0054's `IntegrityError` on a reciprocal pair, and what the suggested fix would have cost | outbound, `UrbanLens/infrastructure` | SENT 2026-09-08 |
| [`infrastructure-availability-drills-and-gunicorn-dev-envs.md`](infrastructure-availability-drills-and-gunicorn-dev-envs.md) — asks for a dev environment that runs gunicorn, and where availability chaos scenarios should live given drills are explicitly not a test suite | outbound, `UrbanLens/infrastructure` | ANSWERED 2026-09-10 |
| [`infrastructure-availability-drills-reply.md`](infrastructure-availability-drills-reply.md) — accepts both their corrections, takes their broker finding into D11 and P105, and returns two of our own | outbound, `UrbanLens/infrastructure` | SENT 2026-09-10 |
| [`infrastructure-availability-drills-followup.md`](infrastructure-availability-drills-followup.md) — the metrics override guards a branch that does not exist, and their Docker-access item is already satisfied | outbound, `UrbanLens/infrastructure` | SENT 2026-09-10 |
| [`infrastructure-metrics-exporter-loop-closed.md`](infrastructure-metrics-exporter-loop-closed.md) — the 30-hour restart loop is stopped at 4,410; what remains is a scheduled staging deploy | outbound, `UrbanLens/infrastructure` | SENT 2026-09-10 |
| [`infrastructure-neighbour-test-results.md`](infrastructure-neighbour-test-results.md) — the neighbour test runs; the 19x headline was a dev-server artifact, the connection cap works, and a Valkey outage is worse than predicted | outbound, `UrbanLens/infrastructure` | SENT 2026-09-10, corrected same day |
| [`infrastructure-per-tier-database-roles.md`](infrastructure-per-tier-database-roles.md) — staging and production need `UL_DB_APP_PASS` before their next deploy; k8s needs the per-tier roles | outbound, `UrbanLens/infrastructure` | SENT 2026-09-15 |
| [`redata-maplibre-catalogue-wiring.md`](redata-maplibre-catalogue-wiring.md) — reply to REData's T8: the dormant tile catalogue is wired and called, the map count is corrected, and the MapLibre migration itself is deferred to PL8 | inbound, `../REData` | PARTIALLY ANSWERED 2026-09-19 |
| [`redata-cris-attachment-500-and-places-429.md`](redata-cris-attachment-500-and-places-429.md) — some CRIS attachment downloads 500 with an HTML page, and nearby Places answers 503 on a Google 429 | outbound, `../REData` | ANSWERED 2026-10-01 by REData T10 (`docs/urbanlens-2026-10-01-replies.md`): fixed on REData `main`, deploy held until v0.8.0 ships; closes after a location run against deployed REData |
| [`redata-survey-roster-marks-other-surveys-on-property.md`](redata-survey-roster-marks-other-surveys-on-property.md) — every building on any survey naming one HRSH building comes back on the property; about 200 of HRSH's 300 | outbound, `../REData` | ANSWERED 2026-10-01 by REData T10 (`docs/urbanlens-2026-10-01-replies.md`): fixed on REData `main`, deploy held until v0.8.0 ships; closes after a location run against deployed REData |
| [`redata-osm-relation-building-returned-as-point.md`](redata-osm-relation-building-returned-as-point.md) — Kirkbride, an OSM multipolygon relation, comes back as its centre point, so its building place has no outline (P182) | outbound, `../REData` | ANSWERED 2026-10-01 by REData T10 (`docs/urbanlens-2026-10-01-replies.md`): fixed on REData `main`, deploy held until v0.8.0 ships; closes after a location run against deployed REData |
| [`infrastructure-app-0.8.0-deploy-findings-reply.md`](infrastructure-app-0.8.0-deploy-findings-reply.md) — reply to their 0.8.0 deploy findings: `:main` waits for CI, P181's step is migration 0034, releases publish again | outbound, `UrbanLens/infrastructure` | SENT 2026-10-02 |
| [`infrastructure-jess-decisions-2026-10-02.md`](infrastructure-jess-decisions-2026-10-02.md) — Jess wants `localize_article_images` and `sweep_unnamed_pin_images --delete` run now on staging and production; Postgres restore tooling is theirs | outbound, `UrbanLens/infrastructure` | OPEN 2026-10-02, for Jess to pass on |
| [`redata-chronicling-america-description-dropped.md`](redata-chronicling-america-description-dropped.md) — Chronicling America records reach UrbanLens with no description, because a list is stripped to ""; the Historic Newspapers tab is empty (P216) | outbound, `../REData` | OPEN 2026-10-03 |
| [`redata-imagery-composed-past-native-resolution.md`](redata-imagery-composed-past-native-resolution.md) — Sentinel-2 cloudless is composed at z18 from 10 m data, and the first render is kept forever; clamp by `resolution_meters` and re-render (P232) | outbound, `../REData` | OPEN 2026-10-03 |
| [`redata-overture-near-point-lookups.md`](redata-overture-near-point-lookups.md) — `/buildings/` and the `overture` points-of-interest provider time out on the deployed REData, which UrbanLens now depends on inside the US (P110, P240); plus unsynced roof fields and `operating_status`, and applicability wider than the synced shards | outbound, `../REData` | OPEN 2026-10-03 |
| [`redata-gdelt-months-cap.md`](redata-gdelt-months-cap.md) — `/search/news/` accepts `months` up to 120, but GDELT refuses a window reaching before 2017, so anything over 117 fails today | outbound, `../REData` | OPEN 2026-10-03 |

## The convention

Every file opens with its `# Title`, then two bullets and nothing else before
the body:

```markdown
- **Status: OPEN as of YYYY-MM-DD.** / **Status: SENT, YYYY-MM-DD.** ...
- **Direction: inbound / outbound.** Who sent it to whom.
```

The shape is the `infrastructure` repo's, deliberately — these are two halves
of one thread, and a reader following it should not have to learn two filing
systems. A reply is named after the ask it answers, so `ls` shows the thread.

Decisions that came out of a handoff live under [`../designs/`](../designs/)
with a `D#` id, not here: this directory is what was said, and a decision has to
stay editable when it is superseded.
