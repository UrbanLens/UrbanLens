# PROBLEMS

Open problems and tasks are GitHub issues: `gh issue list --state open`, or
<https://github.com/UrbanLens/UrbanLens/issues>. File a new one there, with the
labels in [`agents/triage-labels.md`](agents/triage-labels.md).

This file maps the ids they had before they moved (2026-10-07), so a `P#` or
`T#` cited from code or another document still resolves. Resolved problems
were never migrated; they keep their `P#` in
[`archive/PROBLEMS-ARCHIVE.md`](archive/PROBLEMS-ARCHIVE.md). An id in
neither place was never allocated.

**Citing an issue from code:** the number *and* enough words to survive a
retitle - `see UrbanLens#286 ("articles name provider images")`.

| former id | issue | title at migration |
|---|---|---|
| P1 | [#264](https://github.com/UrbanLens/UrbanLens/issues/264) | VirusTotal scanning is hash-lookup-only, so a file VirusTotal has never seen falls back to ClamAV forever |
| P5 | [#265](https://github.com/UrbanLens/UrbanLens/issues/265) | Dialog forms still post every field; edit handlers write only the columns that changed, but submits are not dirty-only |
| P7 | [#266](https://github.com/UrbanLens/UrbanLens/issues/266) | REData's reconciled building `ref` has no stability guarantee, and UrbanLens persists it as permanent identity |
| P9 | [#267](https://github.com/UrbanLens/UrbanLens/issues/267) | Land-use-area boundary geometry is not drawn, pending a map-overlay decision |
| P11 | [#268](https://github.com/UrbanLens/UrbanLens/issues/268) | Frontend TS audit: its correctness bullets are fixed, the structural debt it found is not |
| P13 | [#269](https://github.com/UrbanLens/UrbanLens/issues/269) | Pin-detail external-data freshness is one site-wide `external_data_cache_days` knob, not per-source |
| P14 | [#270](https://github.com/UrbanLens/UrbanLens/issues/270) | Historical `pin_images/` files whose Image row is gone: `sweep_unnamed_pin_images` exists, not yet run on any environment |
| P19 | [#271](https://github.com/UrbanLens/UrbanLens/issues/271) | Audit residue: group chats lack direct messages' features, and the hypothesis strategies are barely shared |
| P24 | [#272](https://github.com/UrbanLens/UrbanLens/issues/272) | A campus pin's CRIS detail fetches stop at a per-pass cap, and a child the site's roster misses fetches its own |
| P36 | [#273](https://github.com/UrbanLens/UrbanLens/issues/273) | 43 BEM modifiers are applied in templates with no CSS rule, so intended visual states never render |
| P50 | [#274](https://github.com/UrbanLens/UrbanLens/issues/274) | `test_safety_chat` and `test_migration_0039_reverse` fail only under a randomized suite order |
| P56 | [#275](https://github.com/UrbanLens/UrbanLens/issues/275) | `Cross-Origin-Embedder-Policy` is report-only pending one measurement; `require-corp` is ruled out |
| P85 | [#276](https://github.com/UrbanLens/UrbanLens/issues/276) | Managers are typed, but `misc` stays off: it reports 478 lookup and plugin findings, and annotations do not survive a model-bound queryset's rows |
| P111 | [#277](https://github.com/UrbanLens/UrbanLens/issues/277) | A gunicorn worker's memory is set by peak concurrent response size, and it never gives it back |
| P113 | [#278](https://github.com/UrbanLens/UrbanLens/issues/278) | 54 verified places where one account's ordinary use can degrade the site for everyone else, all fixed except 4 parked by decision |
| P125 | [#279](https://github.com/UrbanLens/UrbanLens/issues/279) | This deployment's ceiling is between 500 and 1,000 concurrent users, and every wall it has hit so far was a container CPU limit |
| P131 | [#280](https://github.com/UrbanLens/UrbanLens/issues/280) | REData's ~1.45s PBKDF2 key-check is fixed upstream (confirmed 2026-09-21); basemap tiles now pay 0.43–0.98s for cold-tile rendering instead, and the concurrency bound's own trigger condition is met for auth but not for that |
| P132 | [#281](https://github.com/UrbanLens/UrbanLens/issues/281) | A global search read the whole site's rows to answer one viewer's question; semi-join probes cut its SQL 60%, and the ceiling moved off the database |
| P134 | [#282](https://github.com/UrbanLens/UrbanLens/issues/282) | The app tier, not the database, is what runs out; caching the navbar's access question moved 500 concurrent users from six budget breaches to none, confirmed by a second ladder on the released tree |
| P144 | [#283](https://github.com/UrbanLens/UrbanLens/issues/283) | Every UrbanLens environment shares one REData key and its 1,000/hour lookup budget, and REData has no way to exempt production |
| P145 | [#284](https://github.com/UrbanLens/UrbanLens/issues/284) | The HRSH courtyard pin on staging got a circle, a service road for a title, a building's name as an alias, no Wikipedia article and one building in its CRIS card |
| P148 | [#285](https://github.com/UrbanLens/UrbanLens/issues/285) | A county-sized "parcel" put strangers across the Capital District into one wiki and pin-in-common domain |
| P165 | [#286](https://github.com/UrbanLens/UrbanLens/issues/286) | Articles saved before 2026-09-30 name provider images in their source until `manage.py localize_article_images` runs on each deployment |
| P167 | [#287](https://github.com/UrbanLens/UrbanLens/issues/287) | Upstream-bound tasks with four-minute limits share the interactive worker's four slots with safety alerts and signup mail |
| P170 | [#288](https://github.com/UrbanLens/UrbanLens/issues/288) | Nothing deletes article revisions, and each one is a full copy of the article |
| P182 | [#289](https://github.com/UrbanLens/UrbanLens/issues/289) | A building place from an OSM relation has no outline, because REData sends the relation's centre point; containment can never reach it |
| P206 | [#290](https://github.com/UrbanLens/UrbanLens/issues/290) | `dashboard_location_cache` is 81% of production's database |
| P210 | [#291](https://github.com/UrbanLens/UrbanLens/issues/291) | Pin-share notifications stored before 2026-10-02 still name the sender's own pin; fixed on `release/v_0_9_0` by migration 0075, live until 0.9.0 deploys |
| P240 | [#292](https://github.com/UrbanLens/UrbanLens/issues/292) | Inside the US, Overture data needs REData's index-backed lookups, which production now runs (v0.3.4); only the buildings route has been seen answering |
| P242 | [#293](https://github.com/UrbanLens/UrbanLens/issues/293) | Migration 0033's operator command can't run on the schema it is meant for, since 0040 added a Location column |
| P277 | [#294](https://github.com/UrbanLens/UrbanLens/issues/294) | A first visit to a place still waits on every panel whose answer is not stored, so its tail is unchanged |
| P279 | [#295](https://github.com/UrbanLens/UrbanLens/issues/295) | Legacy `BLOCKED` friendship rows may still record the wrong blocker |
| P286 | [#296](https://github.com/UrbanLens/UrbanLens/issues/296) | A campus pin could lose its own National Register listing, because REData answered a point with the rows last found from it; fixed in REData v0.3.4, not yet seen on production |
| P304 | [#297](https://github.com/UrbanLens/UrbanLens/issues/297) | A Location History file over about 180 MB, or a GPX file over about 60 MB, fails its preview on the time limit |
| P316 | [#298](https://github.com/UrbanLens/UrbanLens/issues/298) | Nine tests fail under `bin/host_pytest.sh` on `release/v_0_9_0`, from three causes |
| P318 | [#299](https://github.com/UrbanLens/UrbanLens/issues/299) | The import wizard sent each Google Maps CID through the browser as a JSON number, zeroing its low digits; fixed, but stored rounded CIDs in import failures need `fix_float_rounded_cids` |
| P321 | [#300](https://github.com/UrbanLens/UrbanLens/issues/300) | A data export that includes photos or image overlays fails outright on object storage, which production 0.8.0 uses; fixed on `release/v_0_9_0`, live until 0.9.0 deploys |
| P336 | [#301](https://github.com/UrbanLens/UrbanLens/issues/301) | A location hidden after export stays on a calendar no push reaches: exports without auto-sync, visibility changes with no trip edit, and a deleted activity's orphaned event |
| P337 | [#302](https://github.com/UrbanLens/UrbanLens/issues/302) | A failed Google token refresh, or a 403 about the site rather than the user, still drops the user's calendar connection |
| P338 | [#303](https://github.com/UrbanLens/UrbanLens/issues/303) | A hidden stop added from a place search still shows the place's name to members who may not see it, because the name is stored as its title |
| T1 | none (done) | Became note `N45` in [`INDEX.md`](INDEX.md) |
| T2 | [#304](https://github.com/UrbanLens/UrbanLens/issues/304) | HIGH 0-ref findings are all triaged; the MEDIUM tier and Jess's caching requests are still open |
| T3 | [#305](https://github.com/UrbanLens/UrbanLens/issues/305) | Nothing in the integration suite opens the comment-map composer; only a browser harness with a stubbed server does |
