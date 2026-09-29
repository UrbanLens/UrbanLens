# N30 — Where every verified N29 finding stands

`id: N30` · `status: current` · `updated: 2026-09-29`

N29's 131 claims were re-verified into 103 real or partial findings (G1–G6; those verification reports lived in a
session scratchpad and are not in the repo). On 2026-09-29 two independent passes checked every finding against
`release/v_0_8_0`, reading code rather than commit messages: six read-only agents at `467da1871`, then five more at
`08a39e4ea`. The first pass concluded that every finding not listed below was fixed. The second found that false for
the rows marked *second pass*, which were fixed or filed the same day.

A third pass on 2026-09-29 found that claim false again: the findings under *Unhandled* below had nothing fixing,
filing or dispositioning them. The G1–G6 lists aren't in the repo, so some ids were rebuilt from commit messages
and may be approximate. Everything under *Kept* is an agent's call made without Jess's input, waiting for her review.

## Fixed

| Finding | What changed | Tests |
| --- | --- | --- |
| G4-20 notification email on the request path | `send_notification_email` queues `send_notification_email_task` on commit. The eleven copied preference blocks are now `deliver_notification`. A mute silences the email as well as the row, and comment notifications name their commenter so they can be muted. Safety partner invites, check-in updates, found-safe and deletion notices go through `queue_email`; beat-driven safety alerts still send directly, which is off the request path | `test_notification_email_off_request_path.py`, `test_notification_delivery_service.py`, `test_request_path_emails_queued.py` |
| G1-10 SpotGuessr state bonus, "NY" vs "New York" | `canonical_state` / `canonical_country`, Nominatim asked for English, blanks never match | `test_spotguessr_geo_bonus.py` |
| G1-17 split probing grows with the domain | At most `MAX_SUBDIVISION_PROBES` (20) neighbours, nearest first | `test_subdivision_probe_cost.py` |
| G5-16 / G6-25 push dispatch serial in one task | 40 devices per task, 8 concurrent, the rest handed to `dispatch_push_to_devices` | `test_push_dispatch_bounded.py` |
| Batch 1: an invitee who never joined drives the round (*second pass*) | N29 named SpotGuessr; Trivia and Consensus had the same shape. The first pass counted it fixed because the LEFT half was. All three round views now refuse a non-joined participant through `refuse_unless_joined` | `test_game_round_invitee.py` |
| Batch 15: social-link probe follows redirects anywhere (*second pass*) | `SocialLinkVerifyView` goes through `open_public_url` | `test_fixed_host_egress.py::SocialLinkProbeTests` |
| Batch 26: map centre recomputed on the request after seven days (*second pass*) | A page that finds an unfinished claim re-queues it after an hour and serves the cached centre | `test_map_center_signal.py::AStaleCentreIsServedWhileItRecomputesTests` |
| Batch 12: encrypted connections deleted on `InvalidToken` (*second pass*), P169 | Reads keep an undecryptable row and report it absent; disconnect and reconnect remove it. The four managers are one `ProfileConnectionManager` | `test_undecryptable_connections.py` |
| Batch 1 (third pass): the getaddrinfo pin isn't re-installed after a later monkey-patch | Reproduced: a resolver patched in after import rebound the validated host to loopback and the request was delivered before the peer check refused it. An IDN host was unpinned even without a patch, since urllib3 resolves the punycode spelling. The pin now lives in urllib3's `create_connection` hook, which dials the validated IP literal, keys pins by the host as urllib3 spells it, and is re-installed before every hop. The `socket.getaddrinfo` patch is gone | `test_ssrf_dns_rebind.py::PinSurvivesALaterResolverPatchTests`, `::PinnedConnectionTests` |

## Reverted by Jess

| Finding | Ruling |
| --- | --- |
| P152, found while fixing G3-31: Google links re-keyed to `sub` | Reverted; links stay keyed by address, and only an unverified address is refused (D24) |
| A P11 bullet: browser-direct Nominatim and as-you-type autocomplete, proxied and made cache-only | Reverted; browser-direct with live autocomplete (D25) |

## Unhandled (third pass, 2026-09-29)

| Finding | Where |
| --- | --- |
| Batch 1: smart-list resync runs inline on the request below its ceiling | `models/pin_list/signals.py` |
| Batch 1: the Wikipedia-cache first-title hook seeds one article per pin | `models/cache/signals.py` |
| Batch 1: `resolve_deferred_pin_locations` has `max_retries=None` (bounded in practice by a 2-day deadline) | `tasks.py` |
| Batch 28: `pg_dump` gets the password through `PGPASSWORD` in its environment | `core/controllers/backups/db.py` |
| Batch 29: the map payload counts every matching pin before capping | `services/map_pins/filter_results.py` |
| Batch 30: `ReputationEvent` and `WikiEdit` rows are never deleted | no prune task; retention is Jess's call |
| Batch 31: trivia questions are never deleted | no prune task; retention is Jess's call |
| Batch 32: photo-map sampling loads up to 50,000 coordinates | `services/geo/sampling.py` |
| Batch 26: calendar export makes one Google request per activity (the lost-enqueue half is fixed) | `services/trips/` calendar export |

## Open, filed

| Finding | Where |
| --- | --- |
| G4-1 / G4-2 blocks and shared spaces | Needs a product decision; options in I7 |
| P167 slow tasks on the interactive worker | Needs an enrichment worker across compose, k3s and production |
| Batch 22: device-scan type trust and marker visibility (*second pass*; the first pass missed it) | P168 |
| Batch 33: article revisions never deleted (*second pass*) | P170 |
| Batch 33: album grid and membership unbounded (*second pass*; P166 had listed it as left open) | P171 |
| Batch 35: organize screen materialises every label | P66, already open |

## Kept, with the reason

| Finding | Why it stays |
| --- | --- |
| G3-10 floorplan editor embeds every label | Bounded: location labels only, and `LABELS` caps them (default 2,000) |
| G5-32 map page loads every label, list, custom field | Bounded by the per-user caps (defaults 2,000 / 500 / 100); the 100,000 figures are admin ceilings. Global labels are exempt from the label cap. Their bound is the `edit_global_label` permission, not a count |
| G2-14 trip forecast fetched on the request | `WeatherForecastUpstream` holds the request for at most its 8 s deadline, the fetch carries on to fill the cache, and the panel polls while pending |
| G2-12 profile preview fabricates rows per request | Real rows in a rolled-back transaction are what make the preview run the real authorization code. Every side effect of those rows is `on_commit` and dies with the rollback |
| G2-1 residual: login-params endpoint unthrottled | Unknown identifiers already get decoy answers, so a throttle would not close an oracle, and a per-address cap would bite shared NATs |
| G1-31 settings geocode runs inline | One explicit click, login-required and rate-limited (P150) |
| G1-30 file sizes | Observation, not a defect. Still true: `tasks.py` has grown to about 4,260 non-blank lines |
| Batch 1: a single pin save refits its parent's boundary in the signal | P161 batches every bulk path to one refit per parent; one save is one hull over one parent's direct children |
| Batch 1: deleting a parent pin or wiki cascades | Deliberate, and paired with a whole-subtree undo (`bulk_delete_pins`, `delete_pin`) |
| Batch 7: achievement icons open to any signed-in account | A secret achievement's icon is shown on its earners' profiles, so anyone who can see one needs it, and the stored name is not guessable |
| Batch 10: `advance_pwyw_usage_ledgers` is one task | It pages by primary key in 500-row chunks, so memory is bounded; only the run time grows |
| Batch 15: Gotify POST not through `open_public_url` | Only an admin sets the URL, and a Gotify server is usually on a private network, which the public-URL guard would refuse |
| Batch 29: channel-layer buffer unchanged | `send_group_message` documents live delivery as a bonus on a row already saved; whether every client catches up on a dropped frame was not checked |
| Batches 29–30: `get_or_create` on a one-to-one without a retry | Django's `get_or_create` catches the `IntegrityError` of a raced insert and re-reads, when the lookup matches the unique constraint, as in both cases here |
| Batch 30: external API page offsets | An out-of-range page costs one `COUNT`, as every page does; a deep valid offset is bounded by the caller's own rows. Capping depth would refuse real clients with large libraries |
| Batch 31: trivia generation sequential | A background task under its own lock, not a request |
| Batch 34: safety overview unpaginated | Left by P69 deliberately, with the row cost pinned by `test_safety_home_render_scaling.py`. A null auto-delete window is the user's own choice of "never" |
