# Celery Job Recommendations

After reviewing the current import/export, media, admin, search, AI, and cache code paths, these are the remaining best candidates for additional Celery jobs. Each item includes the reason it would create a noticeable performance, reliability, or operational improvement.

## Highest impact

1. **Run pin import, preview import, and Google Takeout processing in Celery** _(task added for file-path imports)_
   - **Current code:** `GoogleMapsGateway.import_pins_streaming`, `import_preview_streaming`, and `import_location_history_streaming` parse archives/KML/JSON/CSV, geocode CSV URLs, create pins, add badges, import visits, and stream SSE events from the request/response cycle.
   - **Reason:** Large Takeout files can hold open HTTP connections for a long time and mix parsing, database writes, geocoding, and progress reporting in the web request. A Celery task would let the upload return quickly, make progress polling consistent with the new Celery task progress API, and protect web workers from large user imports.
   - **Implemented shape:** `import_pins_from_paths(profile_id, files, tag_ids, tag_by_filename)` can now consume already-stored upload files through Celery and report progress through task metadata. Future UI work can store uploads and enqueue this task directly.

2. **Move Location and Pin AI category suggestions out of synchronous signals/import loops** _(implemented)_
   - **Current code:** `src/urbanlens/dashboard/models/location/signals.py` calls `Location.suggest_category()` directly in a `post_save` signal, and `GoogleMapsGateway.import_preview_streaming` can call `pin.suggest_category()` while importing.
   - **Reason:** Category suggestion calls can hit external AI providers and should not run inside model signal execution or import loops. Moving them to Celery reduces tail latency, prevents external provider issues from slowing saves/imports, and gives a natural place for retries/rate limiting.
   - **Implemented shape:** `suggest_location_category(location_id)` and `suggest_pin_category(pin_id)` now run category suggestions in Celery from location signals, map pin creation, and import loops.

## Strong secondary candidates

3. **Process image uploads and image metadata extraction asynchronously** _(implemented for EXIF GPS extraction)_
   - **Current code:** `PinGalleryView.post` and `WikiGalleryView.post` synchronously open uploaded files with Pillow to extract EXIF GPS coordinates before creating `Image` rows.
   - **Reason:** EXIF extraction is usually quick, but large images and future thumbnail/metadata work can become CPU and disk I/O heavy. Celery would allow immediate upload acknowledgement, async thumbnail generation, EXIF extraction, moderation checks, and eventual metadata updates.
   - **Implemented shape:** image upload views now create `Image` rows immediately and enqueue `process_image_upload(image_id)` to extract EXIF GPS metadata asynchronously.

4. **Run database backups and backup cleanup as scheduled Celery beat jobs** _(task implemented)_
   - **Current code:** `DatabaseBackup` connects `trigger_backup` to `request_finished`, checks backup state, runs `pg_dump`, and purges old files.
   - **Reason:** Backup work is operational background work, not request work. Celery beat would run it on a predictable schedule, avoid backup checks after arbitrary user requests, and let failures be visible in Celery monitoring/logging.
   - **Implemented shape:** `run_database_backup()` now performs backup and retention work from Celery; scheduling can be moved fully to Celery beat as a deployment follow-up.

5. **Move site-admin pull/update/migration/reload work into Celery** _(implemented)_
   - **Current code:** `SiteAdminPullLatestCodeView.post` calls git pull, migration application, and app reload routines from the request.
   - **Reason:** Git operations, migrations, and process reloads can exceed normal request timing and have multi-step progress that admins would benefit from seeing. Celery would make this workflow observable and reduce the chance of a proxy timeout during updates.
   - **Implemented shape:** `apply_admin_code_update()` now performs pull, migration, and reload steps in Celery and returns a task id/status URL from the admin endpoint.

6. **Warm external enrichment caches for pin detail pages** _(tasks implemented)_
   - **Current code:** Smithsonian image lookup, web search, satellite/street-view, and weather widgets are fetched from request handlers or service calls, with some caching already present.
   - **Reason:** These are network-bound, user-visible widgets. Celery can prefetch/cache them after pin/location creation or on a schedule, making detail pages faster and isolating provider slowness/rate limits from page rendering.
   - **Implemented shape:** `refresh_pin_web_search(pin_id)`, `refresh_smithsonian_images(pin_id)`, and `refresh_weather_forecast(pin_id)` now exist as Celery tasks for low-priority cache warming/enrichment refreshes.

7. **Run archive extraction and validation outside the request for large uploads** _(task implemented)_
   - **Current code:** `archive_extractor.extract_archive()` safely validates ZIP/TGZ content, enforces limits, and reads supported import files into memory before import preview/streaming.
   - **Reason:** The code is careful about limits, but large archives still consume CPU and memory in the web worker. Celery lets the UI show extraction/validation progress and avoids blocking request workers while unpacking large imports.
   - **Implemented shape:** `extract_import_archive(archive_path, output_dir)` extracts and validates stored archives in Celery, returning the extracted file manifest for follow-up import work.

## Lower priority / opportunistic

8. **Recompute user map-center and other derived profile aggregates asynchronously**
   - **Current code:** Pin creation clears cached profile map center in a signal; recomputation happens later when needed.
   - **Reason:** Clearing is cheap, but proactive recomputation after bulk imports could make the next map load faster for users with many pins.
   - **Suggested shape:** Enqueue `recompute_profile_map_center_task(profile_id)` after bulk pin imports or large pin deletes.

9. **Move notification fan-out / mention processing to Celery if volume grows**
   - **Current code:** Comment and mention handling is currently request-bound.
   - **Reason:** If notifications expand to email/push/activity fan-out, Celery will prevent comment posting and other social interactions from waiting on notification delivery.
   - **Suggested shape:** Keep the DB write synchronous, enqueue notification delivery/fan-out tasks after commit.

10. **Schedule infrastructure stats snapshots for historical admin charts**
    - **Current code:** `collect_infrastructure_service_stats()` collects live PostgreSQL, Valkey, Celery, and nginx status when the admin stats page loads.
    - **Reason:** Live collection is appropriate for the current dashboard, but Celery beat could store periodic snapshots for trend charts, outage history, and worker/broker capacity planning without making the admin page perform all probes on demand.
    - **Suggested shape:** Add `record_infrastructure_stats_snapshot_task()` on a short periodic schedule and display both live and historical metrics.
