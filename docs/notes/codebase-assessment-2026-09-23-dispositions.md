# N30 — Where every verified N29 finding stands

`id: N30` · `status: current` · `updated: 2026-09-29`

N29's 131 claims were re-verified into 103 real or partial findings (G1–G6; the verification reports lived in a
session scratchpad and are not in the repo). On 2026-09-29, six read-only agents checked each one against
`release/v_0_8_0` at `467da1871`, reading code rather than commit messages. The follow-ups below landed after that
check. Every finding not listed here is fixed and has a regression test named in its P-record (P149–P166).

| Finding | Disposition | Where |
| --- | --- | --- |
| G4-20 notification email on the request path | Fixed 2026-09-29: `send_notification_email` queues `send_notification_email_task` on commit | `test_notification_email_off_request_path.py` |
| G1-10 SpotGuessr state bonus, "NY" vs "New York" | Fixed: `canonical_state` / `canonical_country`, Nominatim asked for English, blanks never match | `test_spotguessr_geo_bonus.py` |
| G1-17 split probing grows with the domain | Fixed: at most `MAX_SUBDIVISION_PROBES` (20) neighbours, nearest first | `test_subdivision_probe_cost.py` |
| G5-16 / G6-25 push dispatch serial in one task | Fixed: 40 devices per task, 8 concurrent, the rest handed to `dispatch_push_to_devices` | `test_push_dispatch_bounded.py` |
| G3-10 floorplan editor embeds every label | Bounded: location labels only, and `LABELS` caps them (default 2,000) | `test_floorplans.py`, `test_capacity_limits.py` |
| G5-32 map page loads every label, list, custom field | Bounded by the per-user caps (defaults 2,000 / 500 / 100). The 100,000 figures are the admin ceilings, not the defaults. Loading chips lazily would be a UI change, not a fix | `services/core/capacity.py` |
| G2-14 trip forecast fetched on the request | By design: `WeatherForecastUpstream` holds the request for at most its 8 s deadline, the fetch carries on to fill the cache, and the panel polls while pending. This is the one request-path policy T3 applied everywhere | `test_trip_weather_off_the_request.py` |
| G2-12 profile preview fabricates rows per request | Kept. Real rows in a rolled-back transaction are what make the preview run the real authorization code; a simulated relationship could disagree with what others see. Every side effect of the rows it creates is `on_commit` and dies with the rollback (checked: pin, trip, reputation, achievement, label signals) | `middleware.py:_respond_as_ghost` |
| G2-1 residual: login-params endpoint unthrottled | Kept. Unknown identifiers already get decoy answers, so throttling would not close an oracle. A per-address cap would limit *successful* sign-ins, which the login view does not, and would bite shared NATs and the e2e suite | `controllers/e2ee.py:E2EELoginParamsView` |
| G1-31 settings geocode runs inline | Deferred by P150: one explicit click, now login-required and rate-limited (G1-8) | `controllers/settings.py:geocode_address` |
| G1-30 file sizes | Observation, not a defect | — |
| G4-1 / G4-2 blocks and shared spaces | Open, needs a product decision; options in I7 | I7 |
| P167 slow tasks on the interactive worker | Open; needs an enrichment worker across compose, k3s and production | P167 |
