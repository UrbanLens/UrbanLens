# D13 — Celery queue classes: one account's big job must not delay everyone's small ones

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D13` · `status: accepted` · `updated: 2026-09-11`

> **Not built, and not yet reviewed.** `accepted` is the only non-superseded
> value `bin/check_docs_index.py` allows for a `D` record; it does not mean Jess
> has signed off on the table below. The classification is a proposal, and the
> placements most worth a second opinion are called out under *Safety overrides
> the beat rule*.

## The problem, measured

**87 of 96 `@shared_task` definitions declare no queue**, so they all land on `celery` and are drained
by one `celery-worker` at `--concurrency=4`. An Immich library sweep can hold one of those four slots
for the length of the task's hard limit. Four presses used to mean four sweeps and zero remaining
slots — P113's in-flight guard now stops the four, but one is still enough to take a quarter of the
pool for the better part of an hour, and a safety check-in escalation queues behind it.

That is the availability requirement failing in the background tier rather than the request tier.

## This is not a new idea here

`services/sandbox/queues.py` already splits `SANDBOX` from `SANDBOX_BATCH`, and its docstring gives
the reason in one line: *"its own worker, so an hour-long import never queues in front of a photo
upload."* The principle is settled and already deployed for media. It was simply never applied to
the default queue, which is where everything else lives.

So this extends an existing pattern rather than introducing one, and reuses the existing shape:
a `Queue` member, a `queue=` on the decorator, and a worker service with `-Q`.

## The classes

- **INTERACTIVE** — a person is waiting for the result, or a deadline is safety-critical.
  Short `soft_time_limit`. Never shares a worker with anything that can run for minutes.
- **BULK** — one account's large job. Long limits, low CPU weight, its own worker.
- **MAINTENANCE** — beat-driven and site-wide. Lowest weight; may wait.

**Safety overrides the beat rule.** `escalate_overdue_checkins`, `send_due_checkin_reminders` and
`send_final_checkin_warnings` are beat-driven, which by the rule above would make them maintenance.
They are INTERACTIVE, because the thing they do is tell somebody that a person is overdue. A
classification that put them behind a photo import would be the worst possible instance of the bug
this design exists to fix.

## Topology

One new worker, not two. `celery-worker` keeps `-Q interactive` and the current concurrency; a new
`celery-worker-bulk` drains `-Q bulk,maintenance,celery` at low concurrency and a low `cpu_shares`.
Maintenance does not need isolating from bulk — those tasks are rare, and a third container costs
connections and memory for no invariant.

It drains `celery` as well, deliberately: a task whose `queue=` is forgotten still runs, on the slow
pool, rather than vanishing into a queue nothing consumes. The startup check below is what stops
that becoming the normal case.

Staging runs the same topology at lower limits, per the standing rule that staging emulates
production without competing with it.

## Enforcement

A Django startup check that fails when a task declares no queue. Added **after** all 96 are
classified, so it ships with zero violations and no exemption list — a check that reports 87
problems on its first run is a check somebody switches off.

## The classification

`unchanged` means the task already declares a queue and this design does not move it.

| task | class | why |
|---|---|---|
| `run_assistant_turn_task` | AI | unchanged |
| `auto_nest_building_pins` | BULK | walks the account's pins |
| `cleanup_export_artifacts_task` | BULK | per-job file cleanup |
| `cleanup_import_artifacts_task` | BULK | per-job file cleanup |
| `evaluate_achievements_for_profile` | BULK | whole-profile evaluation |
| `import_flickr_album_photos` | BULK | one user's import |
| `import_flickr_photos` | BULK | one user's import |
| `import_google_photos` | BULK | one user's import |
| `import_immich_photos` | BULK | one user's import |
| `mirror_buildings_to_wiki` | BULK | walks a location's buildings |
| `process_device_scan_upload` | BULK | up to 100,000 rows |
| `recompute_fact_confidence` | BULK | recompute over a fact set |
| `recompute_reputation_total` | BULK | whole-profile recompute |
| `resolve_deferred_pin_locations` | BULK | one user's whole pin payload |
| `run_user_data_export` | BULK | copies every photo the account owns, twice |
| `submit_redata_photo_vote` | BULK | batched with its sibling |
| `submit_redata_photos` | BULK | one user's batch |
| `sweep_immich_library_locations` | BULK | up to 500k assets, one PostGIS query per photo |
| `sync_redata_pin_assignment` | BULK | per-pin external sync |
| `upgrade_placeholder_pin_names` | BULK | walks the account's pins |
| `archive_safety_checkin` | INTERACTIVE | safety path, kept with its siblings |
| `broadcast_channel_group_message` | INTERACTIVE | live websocket fan-out |
| `build_map_document` | INTERACTIVE | a user is waiting on the map |
| `cache_media_item_into_album` | INTERACTIVE | the tile is on screen |
| `cache_media_item_into_wiki` | INTERACTIVE | the tile is on screen |
| `classify_detail_marker` | INTERACTIVE | feeds a suggestion the user is looking at |
| `classify_trivia_submission` | INTERACTIVE | a player is waiting on the verdict |
| `detect_dm_address_mentions` | INTERACTIVE | message is being read now |
| `dispatch_native_push` | INTERACTIVE | a person is waiting for the notification |
| `enrich_wiki_location` | INTERACTIVE | on-demand enrichment |
| `ensure_wiki_for_location` | INTERACTIVE | on-demand |
| `escalate_overdue_checkins` | INTERACTIVE | safety: someone is overdue and help is being notified |
| `fetch_panel_source` | INTERACTIVE | feeds a progress UI; PANEL_FETCH already exists for this and is not used |
| `fetch_recorded_weather` | INTERACTIVE | panel content |
| `generate_boundaries_for_location` | INTERACTIVE | on-demand |
| `generate_image_keywords` | INTERACTIVE | follows an upload the user is watching |
| `prefetch_location_external_data` | INTERACTIVE | panel content |
| `prewarm_spotguessr_round` | INTERACTIVE | game start |
| `prewarm_spotguessr_solo_start` | INTERACTIVE | game start |
| `push_trip_to_calendar` | INTERACTIVE | user pressed a button |
| `refresh_pin_web_search` | INTERACTIVE | panel content |
| `resolve_location_place_name` | INTERACTIVE | on-demand resolution |
| `run_link_extraction` | INTERACTIVE | user just added the link |
| `score_reputation_event` | INTERACTIVE | immediate feedback on an action |
| `send_direct_message_email_if_unread` | INTERACTIVE | notification delivery |
| `send_direct_message_text_alerts_if_unread` | INTERACTIVE | notification delivery |
| `send_due_checkin_reminders` | INTERACTIVE | safety: the reminder is the whole mechanism |
| `send_final_checkin_warnings` | INTERACTIVE | safety: last warning before escalation |
| `send_notification_text_alerts_if_unread` | INTERACTIVE | notification delivery |
| `suggest_pin_category` | INTERACTIVE | feeds a suggestion the user is looking at |
| `suggest_wiki_category` | INTERACTIVE | feeds a suggestion the user is looking at |
| `warm_saved_filter_cache` | INTERACTIVE | user-facing latency |
| `advance_pwyw_usage_ledgers` | MAINTENANCE | beat-driven and site-wide |
| `archive_link_to_wayback` | MAINTENANCE | beat-driven and site-wide |
| `backfill_achievement` | MAINTENANCE | beat-driven and site-wide |
| `backfill_image_analysis_thumbnails` | MAINTENANCE | beat-driven and site-wide |
| `backfill_image_marker_thumbnails` | MAINTENANCE | beat-driven and site-wide |
| `backfill_image_thumbnails` | MAINTENANCE | beat-driven and site-wide |
| `backfill_location_address` | MAINTENANCE | beat-driven and site-wide |
| `cleanup_vestigial_assets_task` | MAINTENANCE | beat-driven and site-wide |
| `delete_expired_safety_checkins` | MAINTENANCE | beat-driven and site-wide |
| `discard_unretried_failed_uploads` | MAINTENANCE | beat-driven and site-wide |
| `evaluate_public_pin_candidates` | MAINTENANCE | beat-driven and site-wide |
| `hard_delete_expired_accounts` | MAINTENANCE | beat-driven and site-wide |
| `hard_delete_expired_direct_messages` | MAINTENANCE | beat-driven and site-wide |
| `prune_api_call_logs` | MAINTENANCE | beat-driven and site-wide |
| `prune_expired_undo_actions` | MAINTENANCE | beat-driven and site-wide |
| `prune_pin_tombstones` | MAINTENANCE | beat-driven and site-wide |
| `requeue_stalled_pending_uploads` | MAINTENANCE | beat-driven and site-wide |
| `run_database_backup` | MAINTENANCE | beat-driven and site-wide |
| `run_scheduled_database_backup` | MAINTENANCE | beat-driven and site-wide |
| `run_scheduled_demo_account_purge` | MAINTENANCE | beat-driven and site-wide |
| `run_scheduled_enrichment` | MAINTENANCE | beat-driven and site-wide |
| `run_scheduled_redata_public_locations_sync` | MAINTENANCE | beat-driven and site-wide |
| `run_scheduled_trivia_generation` | MAINTENANCE | beat-driven and site-wide |
| `run_scheduled_trivia_wiki_incorporation` | MAINTENANCE | beat-driven and site-wide |
| `send_account_deletion_reminders` | MAINTENANCE | beat-driven and site-wide |
| `sweep_achievements` | MAINTENANCE | beat-driven and site-wide |
| `sweep_achievements_range` | MAINTENANCE | beat-driven and site-wide |
| `sweep_due_safety_checkin_archival` | MAINTENANCE | beat-driven and site-wide |
| `sweep_reputation` | MAINTENANCE | beat-driven and site-wide |
| `sweep_reputation_range` | MAINTENANCE | beat-driven and site-wide |
| `sweep_stale_preview_sources` | MAINTENANCE | beat-driven and site-wide |
| `sweep_stalled_consensus_sessions` | MAINTENANCE | beat-driven and site-wide |
| `sweep_stalled_spotguessr_sessions` | MAINTENANCE | beat-driven and site-wide |
| `sweep_stalled_trivia_sessions` | MAINTENANCE | beat-driven and site-wide |
| `sync_redata_label_definitions` | MAINTENANCE | beat-driven and site-wide |
| `sync_stripe_subscriptions` | MAINTENANCE | beat-driven and site-wide |
| `generate_image_analysis_thumbnails` | SANDBOX | unchanged |
| `generate_image_marker_thumbnails` | SANDBOX | unchanged |
| `generate_image_thumbnails` | SANDBOX | unchanged |
| `process_image_upload` | SANDBOX | unchanged |
| `render_media_preview` | SANDBOX | unchanged |
| `scan_comment_image` | SANDBOX | unchanged |
| `scan_trip_comment_image` | SANDBOX | unchanged |
| `run_user_data_import` | SANDBOX_BATCH | unchanged |
## What this does not do

It does not make any individual task cheaper. `sweep_immich_library_locations` still issues one
PostGIS query per photo; it simply stops doing so in front of a notification. The per-task costs are
P113's other families and are separate work.

It also does not bound how many bulk jobs one account may queue over time — only how many run at
once, which the in-flight guards already do per job type. A user who exports, then imports, then
sweeps still occupies the bulk pool for as long as those take. That is the intended shape: the bulk
pool exists to be occupied.
