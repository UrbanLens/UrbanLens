# Codebase Audit

Full-codebase sweep for bugs, inefficiencies, and maintainability/extensibility improvements,
requested 2026-07-25. Organized as full-stack "feature units" (models + controllers + services +
templates + frontend for a given feature) rather than by file type, so cross-layer issues (signal
misuse, N+1s introduced at the view layer, template/JS drift) surface naturally. Each unit is
audited by a dedicated research pass that reads every file in scope (not just greps) and reports
findings with file:line references, cross-checked against the conventions in the root `CLAUDE.md`
(docstrings, signal/save rules, migration rules, N+1 prevention, HTMX/OOB rules, etc.).

**Status legend**: `[ ]` not started · `[~]` in progress · `[x]` done (findings recorded below)

Real bugs/gaps that can't be fixed within this audit's scope get filed to `docs/PROBLEMS.md` per
the project's existing convention; this document tracks audit *progress* and holds the detailed
per-unit findings (including minor/stylistic ones not worth a PROBLEMS.md entry).

---

## Progress checklist

### Foundation
- [x] 00 - Core architecture: `models/abstract/`, `core/tests/`, `core/controllers/`, `UrbanLens/settings/`

### Mapping & Pins
- [x] 01 - Pin & Location core (`models/pin`, `models/location`, `controllers/pin.py`, `pin_edit.py`, `pin_bulk.py`, `pin_restructure.py`, `detail_pins.py`, `services/pins/pin_creation.py`, `pin_sync.py`, `pin_restructure.py`, `services/map_pins/`)
- [x] 02 - Pin sharing, aliases, links, tombstones (`models/pin_share`, `aliases`, `links`, `link_extraction`, `pin_tombstone`, `controllers/pin_sharing.py`, `aliases.py`, `links.py`, `services/sharing/pin_sharing.py`, `share_provenance.py`)
- [x] 03 - Wiki & community wiki (`models/wiki`, `wiki_edit`, `wiki_stat_vote`, `controllers/location_wiki.py`, `wiki_create.py`, `wiki_media.py`, `pin_wiki_sync.py`, `services/wiki/wiki_seed.py`, `wiki_merge.py`, `wiki_access.py`, `community_counts.py`)
- [x] 04 - Boundary & markup maps (`models/boundary`, `boundary_vote`, `markup`, `controllers/boundary.py`, `markup.py`, `services/geo/boundary_voting.py`, `geo_boundary.py`, `services/locations/boundaries*`)
- [x] 05 - Lists & saved filters (`models/pin_list`, `saved_filter`, `search_history`, `controllers/pin_lists.py`, `saved_filters.py`, `services/pins/pin_list_membership.py`, `pin_list_markup.py`, `pin_list_trip.py`, `saved_filter_cache.py`, `filter_criteria.py`, `geo_filter.py`)
- [x] 06 - Search: global + region (`services/global_search/`, `services/search/search.py`, `controllers/search.py`, `region_search.py`, `forms/search.py`)

### External data
- [x] 07 - External data enrichment / plugin system / API gateways (`dashboard/plugins/`, `services/apis/`, `models/cache`, `services/locations/enrichment.py`, `external_data.py`, `news.py`, `debug_overlay.py`, `rate_limiter.py`)

### Photos & Memories
- [x] 08 - Photos, images, memories (`models/images`, `controllers/photos.py`, `image_gallery.py`, `media.py`, `media_proxy.py`, `memories.py`, `services/media/images.py`, `media_materialize.py`, `media_relevance.py`, `photo_import.py`, `photo_coordinates.py`, `photo_keywords.py`, `videos.py`, `services/memories/`)
- [x] 09 - Pin suggestions & visits (`models/pin_suggestions`, `visit_suggestions`, `visits`, `controllers/pin_suggestions.py`, `visit_suggestions.py`, `visits.py`, `services/pins/pin_suggestions.py`, `visits.py`, `visit_invites.py`)

### Trips & Safety
- [x] 10 - Trips + calendar sync (`models/trips`, `calendar_sync`, `controllers/trip.py`, `calendar_sync.py`, `services/trips/trip_ai_suggestions.py`, `trip_legs.py`, `trip_names.py`, `trip_share_tracking.py`, `trip_visibility.py`)
- [x] 11 - Safety check-ins (`models/safety`, `controllers/safety.py`, `services/visits/safety.py`)

### Social
- [x] 12 - Social layer: friendship, profile, reviews, comments, reactions (`models/friendship`, `profile`, `social_link`, `reviews`, `comments`, `reactions`, `controllers/friendship.py`, `userprofile.py`, `comments.py`, `services/profile/identity_visibility.py`, `connections.py`, `profile_preview.py`, `profile_photos.py`, `avatar.py`, `avatar_colors.py`, `mentions.py`)
- [x] 13 - Labels: tags/categories/statuses/people (`models/labels`, `tags`, `categories`, `controllers/labels.py`, `services/labels/`)
- [x] 14 - Notifications (`models/notifications`, `push_device`, `controllers/notifications.py`, `services/notifications/notifications.py`, `notification_delivery.py`, `notification_text_alerts.py`, `push.py`)

### Power-user features
- [x] 15 - Custom fields (`models/custom_fields`, `controllers/custom_fields.py`, `services/custom_fields/custom_field_references.py`)
- [x] 16 - External photo integrations: Immich/Google Photos/Flickr (`models/immich`, `google_photos`, `flickr`, `controllers/immich.py`, `google_photos.py`, `flickr.py`, `services/auth/google_oauth.py`)
- [x] 17 - Account & Auth incl. webauthn/2FA/onboarding/API keys (`models/account`, `controllers/account.py`, `account_deletion.py`, `webauthn.py`, `two_factor.py`, `onboarding.py`, `setup.py`, `api_keys.py`, `services/auth/auth_backend.py`, `webauthn.py`, `two_factor.py`, `api_keys.py`, `account_deletion.py`, `username.py`, `passphrases.py`, `email_normalization.py`, `email_safety.py`)
- [x] 18 - Undo framework (`models/undo`, `controllers/undo.py`, `services/undo/`)
- [x] 19 - Site admin & settings (`models/site_settings`, `subscriptions`, `api_call_log`, `api_rate_limit`, `controllers/site_admin.py`, `settings.py`, `costs.py`, `services/admin/site_admin.py`, `infrastructure_stats.py`, `backups.py`)
- [x] 20 - AI integration (`services/ai/`, `controllers/ai_extraction.py`, `assistant.py`, `auto_tag.py`)
- [x] 21 - REST API surfaces, internal + external (`dashboard/external_api/`, spot-check `models/*/viewset.py`, `urls.py`)

### Messaging & real-time
- [x] 22 - Direct messaging & E2EE (`models/direct_messages`, `group_chats`, `e2ee`, `controllers/direct_messages.py`, `direct_message_shares.py`, `group_chats.py`, `e2ee.py`, `services/messaging/direct_messages.py`, `direct_message_shares.py`, `group_chats.py`, `e2ee.py`, `dm_location_detection.py`, `map_pin_share_detection.py`, `map_sharing.py`, `map_snapshot.py`)
- [x] 23 - Real-time / WebSockets (`dashboard/consumers.py`, ASGI routing, channel layer config)

### Games
- [x] 24 - SpotGuessr (`models/spotguessr`, `controllers/spotguessr.py`, `services/spotguessr/`)
- [x] 25 - Trivia (`models/trivia`, `controllers/trivia.py`, `games.py`, `services/trivia/`)

### Misc & cross-cutting
- [x] 26 - Misc data models: epa_facility, google_place, property_owner, routes, public_pins, email_log, auto_removals + their plugins/services
- [x] 27 - Import/Export & data safety (`services/import_export/import_data.py`, `export.py`, `export_formats.py`, `archive_extractor.py`, `malware_scan.py`, `content_sniffing.py`, `documents.py`)
- [x] 28 - Core infra services (`services/core/gateway.py`, `geo.py`, `loc.py`, `json_safety.py`, `pagination.py`, `text_limits.py`, `timeout_utils.py`, `units.py`, `url_safety.py`, `redact.py`, `storage.py`, `vestigial_assets.py`, `home_widgets.py`, `digitalcommonwealth.py`)
- [x] 29 - Forms, templatetags, validators, management commands
- [x] 30 - Frontend TypeScript (`frontend/ts/entries`, `entries-classic`, `shared`, `tools`, `types`)
- [x] 31 - Frontend SCSS (`frontend/sass/`)
- [x] 32 - Template hygiene cross-cutting pass (base templates, includes/macros, OOB-swap patterns, pagination partial usage) - beyond what's caught per-feature above
- [x] 33 - Migrations audit (dependency-graph integrity, index-ordering rule, duplicate-index rule, squash artifacts)
- [x] 34 - Test suite quality review (`dashboard/tests/`, hypothesis usage, mocking of external services, TestCase patterns)

---

## Executive summary

**All 35 units complete (2026-07-25).** This was a full-codebase pass — every model/controller/
service directory, all ~50 TS files (34 read in full), all 63 SCSS partials (31 read in full plus
full-tree greps), all 383 templates (per-feature spot checks + one dedicated cross-cutting pass),
all 31 migrations, and a survey of all 385 test files. Overall the codebase is in noticeably better
shape than its "beta, expect bugs" framing suggests — docstring discipline, signal/`dispatch_uid`
hygiene, and N+1 avoidance are consistently good, and several subsystems (undo framework, E2EE,
plugin/gateway architecture, REST API surfaces, migrations, template hygiene) came back essentially
clean. The real problems cluster in a few recurring shapes rather than being uniformly spread:

**Recurring bug shapes** (same mistake made independently in multiple features — worth fixing as a
pattern, not just per-instance):
- **TOCTOU races on check-then-act rate/quota logic**: external API rate limiter (07), outbound
  email caps (26), storage quota (08), AI daily-extraction/trip limits (20), notification
  text-alert debounce (14), safety-checkin escalation re-notify (11), game answer-submission
  status check (24, 25).
- **Missing `"updated"` in partial `update_fields` saves**, silently breaking delta-sync/mtime
  semantics: pin edit/bulk controllers (01), onboarding (17).
- **Orphaned serializers/filtersets/viewsets** left behind after a DRF viewset was deleted, several
  of which would crash immediately if ever wired up: wiki (03), images (08), notifications (14),
  reviews/profile/comments/friendship/social_link (12), trips (10), categories (13), locations (01).
- **Dead/duplicate service files** from earlier refactors that risk a future edit landing in the
  unused copy: `services/news.py`, `loc.py`, `digitalcommonwealth.py`, `routexl.py` (07, 26, 28).
- **SpotGuessr/Trivia near-duplicate multiplayer architecture** (consumers, realtime, chat,
  session lifecycle) copy-pasted rather than sharing a base — a fix in one needs manual porting to
  the other, and the identical unauthorized-answer-submission bug already proves this (24, 25).
- **Undefined CSS custom-property fallbacks** (`var(--text, …)`, `var(--ul-accent, …)`, etc.) used
  as if they were live design tokens across ~10 SCSS files — always renders the fallback, can never
  theme-switch (31).

**Highest-severity individual findings** (see each unit's section above for full detail and
file:line references): the SSRF gap in Immich's user-supplied server URL (07, 16); the
CGNAT-range gap in `url_safety.py`'s SSRF blocklist affecting three other subsystems (28); the
decompression-bomb protection in the full-archive importer that checks only forgeable declared
sizes (27); the `receive()` signature bug that crashes every WebSocket consumer on a binary frame
(23); AI token/cost double-counting for two of three providers (20); the friend-request-visibility
and common-pin-count privacy bypasses (12); the login-lockout bypass via email-normalization
variants (17); the `Label` kind-conversion bug that permanently orphans a converted category (13);
and the safety check-in escalation path that can re-notify every emergency contact on a partial
failure (11).

Full per-unit findings (bugs, inefficiencies, and improvements, ranked by severity within each
unit) are below. A curated subset of the highest-impact items has been filed to `docs/PROBLEMS.md`
per the project's convention; everything else — including all the "improvement" and
maintainability-grade findings — lives only here.

## Findings log

(Findings are appended here per unit as each completes, most-recent last within each unit.)

### 05 - Lists & saved filters

**Health**: Well-documented (thorough docstrings), solid slug/race-condition handling in the
abstract base. Main weakness is the smart-list sync path: re-evaluates every smart list from
scratch on every pin/label edit with per-list DB round trips, and bypasses the pin-count cap the
manual-add path enforces. Real N+1 in `PinList.pin_count` on every list-index render.

1. **[bug]** `services/pins/pin_list_membership.py:55-93` (`resync_smart_list`) never checks
   `SiteSettings.get_current().max_pins_per_list` — `PinListAddPinsView.post`
   (`controllers/pin_lists.py:433-441`) caps manual bulk-adds at this limit, but smart-list
   filter/boundary resync can `bulk_create` unboundedly past the site's configured per-list cap.
2. **[bug]** `services/pins/pin_list_membership.py:43-49` (`sync_pin_against_smart_lists`) uses a
   check-then-`create()` pattern against a table with `UniqueConstraint(fields=["pin_list",
   "pin"])` (`models/pin_list/model.py:131`) — overlapping `Pin.save()` transactions can race this
   and raise an unhandled `IntegrityError` in the `transaction.on_commit` callback instead of using
   `get_or_create`/catching the violation.
3. **[inefficiency]** `models/pin_list/model.py:78-81` (`PinList.pin_count`) always issues a fresh
   `self.items.count()` query even when `PinListsIndexView.get` already prefetched
   `items__pin` — N+1 on every list-index render (`_organize_lists_panel.html:46`), and the
   prefetch it paid for is unused for count purposes.
4. **[inefficiency]** `models/pin_list/signals.py:13-42` + `pin_list_membership.py:31-52` — fires
   on every Pin post_save/labels-m2m-change, doing up to N separate `deserialize_criteria()` +
   `.exists()` + membership queries per smart list with no batching/shared computation.
5. **[inefficiency]** `controllers/saved_filters.py:255-304` (`SavedFilterMatchCountsView.get`) —
   O(F²) query construction across `saved_filters × active_filters` for F saved filters, on every
   toolbar refresh.
6. **[inefficiency]** `controllers/saved_filters.py:212-215` (`SavedFilterEditView.post`) — loops
   over `derived_pin_lists.all()` calling `resync_smart_list` serially, each re-resolving the same
   `Label`/`CustomField` objects for identical new criteria instead of computing the matching set once.
7. **[improvement]** `controllers/pin_lists.py:25` — `serialize_form_criteria` imported but never
   used (dead import).
8. **[improvement]** `services/pins/pin_list_membership.py:79-93` — `to_add` is a Python `set`;
   `bulk_create` order assignment iterates it non-deterministically, so smart-added items land in
   arbitrary order on each resync.
9. **[improvement]** `services/pins/pin_list_membership.py:96-99` — `PinListItem.added_via` isn't
   refreshed when an item keeps matching but via a different reason (smart_filter vs
   smart_boundary) — stale provenance icon/tooltip, cosmetic only.
10. **[improvement]** `controllers/pin_lists.py:391-406` (`PinListAddPinsView.post`) — non-numeric
    `pin_ids` POST values raise an uncaught `ValueError` (500) instead of a clean 400.
11. **[improvement]** `services/geo/geo_filter.py` is misleadingly named — it's a USA-bbox gate for
    external API gateways, not saved-filter geo include/exclude (that's in `filter_criteria.py` +
    `models/pin/queryset.py:390-393`). Worth a rename or cross-reference docstring.
12. **[improvement]** Three near-duplicate saved-filter delete-confirmation markup blocks across
    `_saved_filters_section.html`, `_filters_tab_grid.html`, `_saved_filters_toolbar.html` —
    candidate for a shared include.

Clean: `SearchHistory`/`SearchHistoryQuerySet`/`SearchHistoryManager` (frecency dedup, race-safe
`F()` increment, bounded pruning), slug-generation in `abstract.model.PublicDashboardModel`, and
the `filter_criteria.py` serialize/deserialize round-trip all check out with no issues.

### 03 - Wiki & community wiki

**Health**: Generally well-engineered. Discovery gate (`resolve_visible_wiki`/`location_visible_to`)
is consistently applied and privacy-tested; auto-seeding never overwrites; auto-nesting merge logic
is careful about cycles. Two real problems: a revert-history gap that can silently clobber other
users' edits, and a privacy-fuzzing gap on community stat votes that mirrors an already-fixed pin-count issue.

1. **[bug]** `controllers/location_wiki.py:402-444` (`_revert_edit_fields`), used by revert (466-485)
   and expunge (503-519) — blindly restores the edit's stored "from" value with no check that the
   field still holds the edit's "to" value. Editor A reverting their own edit can silently overwrite
   editor C's later change with no conflict warning. No test covers this scenario.
2. **[bug]** `models/wiki_stat_vote/queryset.py:45-60` (`composite()`) +
   `_wiki_stat_rating_item.html:18,24` — exposes exact vote count/average with no fuzzing, unlike
   `services/wiki/community_counts.py::approximate_pin_count` which exists specifically to prevent
   watching a count tick 1→2 to learn when a second person votes. Same attack applies here, unaddressed.
3. **[inefficiency]** `controllers/location_wiki.py:79-96` (`_wiki_stat_context`) — 8 separate DB
   round-trips per wiki page load (2 queries × 4 stat fields) that could collapse to 2 grouped queries.
4. **[improvement]** `models/wiki/serializer.py` + `filterset.py` — dead code, no consumers anywhere
   (a `viewset.py` was deleted at some point, leaving orphaned serializer/filterset + stale .pyc).
   `WikiSerializer.create()` also has a redundant no-op `wiki.save()`.
5. **[improvement]** `models/wiki/model.py:228-259` (`would_create_cycle`) is a near-verbatim
   duplicate of `models/pin/model.py:340-370` — candidate for a shared abstract mixin parameterized
   on the parent-FK name.
6. **[improvement]** `models/wiki/model.py:219-221` — alias-sync in `save()` swallows `DatabaseError`
   at `logger.debug` level, silently violating the "alias list is the full set of names ever used"
   invariant with no operational visibility.
7. **[bug, minor]** `models/wiki_stat_vote/queryset.py:60` — `round(avg)` uses banker's rounding
   (2.5→2, 4.5→4) instead of the `ROUND_HALF_UP` pattern used elsewhere for display rounding
   (`services/apis/locations/google/place_info.py:38`).

Clean: `wiki_access.py` discovery gate, `wiki_seed.py` auto-seed, `wiki_merge.py` auto-nesting,
`pin_wiki_sync.py` manual sync, and template comment hygiene in the wiki templates.

### 08 - Photos, images, memories

**Health**: Well-documented; heavy lifting (EXIF/GPS extraction, downscaling, video re-encoding,
keyword generation) correctly isolated in Celery tasks with graceful per-field failure handling.
Memories aggregator/journal modules are clean. Two serious issues: a queryset that scales with
total site uploaders rather than the gallery being viewed, and a real storage-quota race under
concurrent multi-file uploads the UI explicitly supports.

1. **[bug]** `models/images/queryset.py:60-83` (`_allowed_uploader_ids`) queries
   `Profile.objects.filter(uploaded_images__isnull=False)` **site-wide**, unscoped to the queryset
   it's called on — every gallery render (even 2 photos) walks every uploader in the entire system,
   with up to 3 more queries per uploader for relationship-based visibility. Gets slower as the site
   grows, independent of the actual gallery size.
2. **[bug]** `services/media/storage.py:103-122` (`quota_error_for_upload`) has no locking/transaction —
   check-then-create at 5+ call sites (`controllers/photos.py:259-269`, `image_gallery.py:107-123`,
   `:291-306`, plus article/DM/visits/safety/tools uploads) lets N concurrent uploads from one user
   each pass the check before any row commits, blowing past quota by up to N files. The gallery
   upload UI fires one fetch per file in parallel with zero server-side guard against this.
3. **[inefficiency]** `controllers/pin.py:659-686` (`media_send_to_wiki`) synchronously downloads
   up to 20 media items in the request handler (15s timeout each, up to 20MB), violating the
   "non-instant operations must use Celery + progress indicator" rule — worst case blocks for
   minutes risking a request timeout/502.
4. **[improvement]** `models/images/serializer.py` + `filterset.py` — dead code, zero consumers
   (same pattern as unit 03's orphaned wiki viewset — a deleted `viewset.py` left dependents behind).
5. **[inefficiency]** `controllers/image_gallery.py:171-177` — bulk delete loops per-row
   (file delete + DB delete) instead of collecting paths and using `images.delete()` for one bulk DB delete.
6. **[improvement]** Upload-handling sequence (checksum → dedupe → quota → create → enqueue) is
   duplicated nearly verbatim across 8 call sites — candidate for a shared
   `create_and_process_image_upload(...)` helper.
7. **[improvement, minor]** Gallery thumbnails serve the full downscaled file (up to 3840px per
   user preference) with no separate thumbnail derivative — a 12-24 item grid can load dozens of MB.

Clean: `services/media/images.py`, `videos.py`, `photo_keywords.py`, `photo_coordinates.py`,
`media_relevance.py`, `services/memories/` package, `controllers/media.py`'s authenticated gate
(matches documented residual-risk families exactly), and `media_proxy.py`'s signing (no regression
of the already-fixed percent-encoding bug).

### 06 - Global & region search

**Health**: Well-architected (clean provider/ABC pattern, shared filter helpers, consistent access
scoping — no DM/private-pin leakage found, no SQL injection). Weak spot is the NL parser: several
heuristics silently misinterpret realistic multi-clause queries, and the plain-text fallback
doesn't actually rescue the most common failure case.

1. **[bug]** `services/global_search/parser.py:398` (`_extract_person`) — greedy end-anchored
   "from X" regex swallows trailing clauses: `"pins from Alice in Cincinnati"` → person becomes
   `"alice in cincinnati"` instead of splitting person/place. Untested.
2. **[bug]** `parser.py:341` — bare 4-digit 2000s-range token treated as an implicit year filter
   even with zero contextual signal (preposition optional) — `"Building 2024"` gets silently
   rewritten into a Jan-Dec date range.
3. **[bug]** `engine.py:93-101` — plain-text fallback rebuilds terms from the raw query without
   stripping type keywords/stopwords, so for the exact "helpful NL" queries the docstring
   advertises (which contain a type word), the fallback requires that keyword to literally appear
   in the target text — effectively a no-op for the common failure mode, while still paying for a
   full second fan-out across ~10 providers.
4. **[bug]** `parser.py:23-58` (`TYPE_KEYWORDS`) — includes ordinary words like "pin", "map",
   "trip", "visit", "image", "comment" as whole-token type restricters with no escape hatch —
   `"please visit my page"` gets misrouted to visits-only, hiding all other matches.
5. **[inefficiency]** `controllers/search.py:48-76` (`_verified_hints`) — up to 9 sequential full
   `GlobalSearchEngine().search()` calls (each fanning to ~10 providers) on cold dialog open —
   worst case ~90 synchronous queries in the request path.
6. **[improvement]** `_dialog.html:30-153` — ~120-line inline `<script>` block for the search
   dialog bypasses the TS build/lint/type-check pipeline entirely, unlike every other JS-heavy
   interaction in the codebase.
7. **[improvement]** `services/search/search.py:100,122` — `except (ImportError, DatabaseError,
   Exception):` — the specific exceptions are redundant subclasses of `Exception` already listed; misleading.
8. **[improvement]** `providers.py:663-666` (`MarkupMapSearchProvider`) — `seen_map_ids` dedup set
   is seeded with a key format that can never match the second loop's dedup key — dead pre-seed.
9. **[improvement]** `controllers/region_search.py:24` — only search-slice view missing method-level docstring/type hints.
10. **[improvement]** `services/apis/locations/nominatim.py` — no per-call usage tracking, inconsistent with project-wide cost/usage tracking goal.

Clean: permission scoping (pins/photos/DMs/safety/trips/comments all correctly scoped, backed by
tests), no SQL injection risk, `forms/search.py` GeoJSON/label-group parsing is defensive.

### 07 - External data enrichment / plugin system / API gateways

**Health**: Unusually well-engineered for beta stage — plugin/hook/registry core is clean and
defensive, `Gateway`/rate-limiter pattern gives every integration rate limiting + call logging +
sane timeouts nearly for free. Real issues: an SSRF gap in Immich, a rate-limiter race condition,
and several dead/duplicated gateway files from an earlier layout.

1. **[bug/security]** `services/apis/immich/gateway.py` + `models/immich/model.py:83` +
   `controllers/immich.py:108-110` — SSRF via user-controlled Immich `server_url` (plain
   `URLField`, no loopback/private/link-local restriction); `ImmichSettingsView.post` pings it
   server-side before saving, later flows proxy responses back through the app — a semi-blind
   SSRF oracle against internal infra for any logged-in user. (Also independently found by unit 16 below.)
2. **[bug]** `services/core/rate_limiter.py:279-491` — `check_rate_limit` (COUNT) and `log_api_call`
   (INSERT) are non-atomic with no locking; concurrent requests can all pass the check before any
   logs, causing real bursts above configured limits — notably breaches Nominatim's 1 req/sec ToS
   limit or EPA ECHO's 5/min under a handful of concurrent pin-detail loads.
3. **[bug/dead-code]** `services/news.py` + `services/apis/search/news.py` — two unreferenced
   near-duplicate `NewsGateway` classes, both hardcoding a nonexistent Google News API domain (would fail DNS even if wired up).
4. **[improvement/dead-code]** `services/digitalcommonwealth.py`, `loc.py`, `routexl.py` — stale
   pre-refactor duplicates of the `services/apis/` versions actually in use; risk of a future edit landing in the wrong copy.
5. **[improvement]** `services/core/gateway.py:65` `Service.paid_service` flag is declared on ~30
   subclasses but never read anywhere — scaffolded-but-never-wired.
6. **[improvement]** Several explicitly paid services (google_places, mapbox, bing_maps,
   apple_maps, azure_maps, google_earth) have no `cost_per_call` — the "cost tracking on every
   external call" roadmap item is satisfied for only ~1 of 15+ paid services.
7. **[inefficiency]** `models/cache/signals.py:35-59` — Wikipedia-cache-write signal reseeds
   articles/links for every pin at a location on *every* cache write, not just the first, inside a
   `transaction.on_commit` callback — unbounded per-pin loop for popular locations.

Clean: `plugins/base.py`/`hooks.py`/`registry.py`, `Gateway`/rate-limiter timeout handling across
~100+ call sites, `external_data.py`/`enrichment.py` design, ~12 sampled plugins, `models/cache/`.

### 16 - External photo integrations: Immich/Google Photos/Flickr

**Health**: Well-built — correct `EncryptedTextField` credential storage, self-healing managers
recover from stale encryption keys, every import path is Celery-backed with progress/toasts,
Flickr album import correctly bounds to 100 photos via the API (no SSRF there). One clear
regression with an already-fixed sibling sitting right next to it in the same codebase.

1. **[bug]** `controllers/google_photos.py:144-153` — `GooglePhotosCallbackView.get()`
   unconditionally overwrites the stored refresh token with `tokens.get("refresh_token") or ""` on
   every reconnect; if Google's response omits a refresh token (common on reconnect without fresh
   consent), the previously-valid token is wiped, permanently breaking auto-refresh. The sibling
   `controllers/calendar_sync.py:167-171` already has the correct fix (only overwrite if present) — a one-line port.
2. **[bug/security]** `models/immich/model.py:83` + `forms/immich_form.py` — same Immich SSRF gap
   independently found in unit 07 above (no private-IP/scheme guard on `server_url`, server-side
   ping + proxying). Low urgency (self-hosted, single-tenant) but worth documenting/guarding.
3. **[improvement]** `_immich_account.html:58-92` — 30-line `<style>` block embedded in an HTMX
   partial (not in SCSS source) that gets re-injected as a duplicate `<style>` tag on every
   connect/disconnect toggle (`hx-swap="outerHTML"`).
4. **[improvement]** `tasks.py` (import_immich_photos, import_flickr_photos, import_google_photos,
   import_flickr_album_photos) — each issues one dedupe `.exists()` query per photo inside the
   download loop instead of a batched pre-check.
5. **[improvement]** `_request_profile`/`_with_toast`/radius-choice constants/progress-view bodies
   duplicated near-identically across `controllers/immich.py`, `google_photos.py`, `flickr.py` —
   ~60 lines of copy-paste that already let the refresh-token bug drift between copies.
6. **[improvement]** `controllers/flickr.py:406,461` — Flickr error partials reuse the
   `immich-picker-error` CSS class name, misleading for feature-scoped grepping.

Clean: model layer encryption/self-healing managers, Flickr OAuth1 flow, `services/auth/google_oauth.py`, plugin registration files.

### 02 - Pin sharing, aliases, links, tombstones

**Health**: Unusually well-maintained — every `PinShare` creation path correctly calls
`resolve_origin_share`/`record_share_exposure`, and alias/link deletion-permanence is correctly
threaded through AI-extraction and external-name-sync paths. One genuine, well-evidenced
deletion-permanence violation found outside this slice's own models.

1. **[bug]** `services/pins/pin_suggestions.py:622-641` (`_apply_suggested_enrichment`) creates
   `PinAlias`/`PinLink` rows with no `PinAutoRemoval.objects.was_removed(...)` check, unlike every
   sibling auto-creation path (`services/ai/link_extraction.py:281`,
   `services/locations/naming.py:609,662`) — a user who deletes a suggestion-added alias/link can
   have it silently recreated the next time a `PinSuggestion` for the same pin is accepted. Untested.
2. **[improvement]** `controllers/links.py:116-134` — adding/removing a wiki link never writes a
   `WikiEdit` history row, unlike alias add/remove/use and every other wiki mutation — edit history
   is silently incomplete for link changes.
3. **[improvement]** `models/pin_tombstone/model.py:16-19` — docstring says pruning is "for a
   future scheduled task" but it's already wired into Celery beat daily — stale comment.
4. **[inefficiency]** `controllers/memories.py:790-802` — `PinShare.chain_share_count` (BFS,
   one query per chain-depth level) invoked once per shared-pin group in a Python loop instead of batched.
5. **[inefficiency]** `controllers/pin_sharing.py:128-145` (`_create_pin_from_share`) — photos
   copied via per-image `Image.objects.create()` in a loop instead of `bulk_create`.
6. **[inefficiency]** `services/messaging/group_chats.py:618-628` (`share_pin_in_group_message`) — loops
   `create_pin_share` per group member with no batching; scales linearly with group size.

Clean: `pin_share`/`share_provenance.py` (all call sites verified), case-insensitive alias/link
uniqueness, `pin_tombstone` idempotent record/pruning semantics.

### 04 - Boundary & markup maps

**Health**: Well-engineered — provider chain, boundary voting, and geometry-fixing helpers show
careful invalid-geometry handling and fallback ordering, backed by strong test coverage. Weaknesses
are on the consumption side: an N+1 geometry walk on markup-triggered pin-share-detection, and a
latitude-dependent circle-distortion bug duplicated across three hand-rolled implementations.

1. **[inefficiency]** `services/sharing/map_pin_share_detection.py:308-316` — `effective_polygon_for_pin`
   called once per candidate pin in a Python loop inside `detect_shared_pins`; each call is itself
   multiple unprefetched queries — N+1 scaling with sender's pin count in the viewport.
2. **[inefficiency]** `models/markup/signals.py:36-39` — schedules a full pin-inference resync
   (triggering the N+1 above) on *every* `MarkupMap` post_save, including trivial title renames or
   autosaved pan/zoom view-state changes, with no changed-fields check.
3. **[improvement]** `controllers/boundary.py:193-231` (`list_boundaries`, url name `boundary.list`)
   — fully dead code (no template/TS/test references it); if ever wired up as-is, its `defaults`
   branch does an unbounded full-table scan with no bounding-box filter.
4. **[bug]** `models/boundary/queryset.py:26-41` (`circle_for_coordinates`) — converts radius to
   degrees via a flat `radius_meters / 111_000` applied uniformly to both axes, producing an
   ellipse (not a circle) that shrinks east-west by `cos(latitude)` — e.g. ~half-width at 60°N. The
   same unprojected-degree approximation is independently re-implemented with different constants
   in `services/sharing/map_pin_share_detection.py:153-191` and `models/markup/model.py:41-64` — three
   hand-rolled conversions instead of one shared latitude-correct helper.
5. **[improvement]** `models/boundary/model.py:99` — `Boundary.default_radius_meters` is a real
   field but no controller ever sets it to anything but the class default — unfinished feature or
   dead customization surface.
6. **[improvement]** `services/apis/locations/boundaries.py:127-150` — multi-kind vs single-kind
   provider distinction relies on a fragile method-identity check (`type(provider).get_typed_boundaries
   is BoundaryProvider.get_typed_boundaries`) rather than an explicit flag — a future provider
   trivially overriding the method would silently lose an optimization with no test to catch it.
7. **[improvement]** `models/markup/__init__.py:2-6` — managers/querysets aren't exported in
   `__all__`, unlike the parallel `boundary/__init__.py` — inconsistent public surface.

Clean: `services/geo/boundary_voting.py`, Boundary/BoundaryVote models, all 5 provider implementations,
`services/apis/locations/base.py` geometry-fixing helpers, markup signal transaction-commit
deferral, `map_sharing.py`, `map_snapshot.py` input sanitization, boundary/markup-editor TypeScript.

### 00 - Core architecture

**Health**: Solid overall — slug-generation retry logic, network-guard test infra, and the
pydantic `AppSettings` layer are thoughtfully engineered. Found a directory-creation bug that
silently prevents several app dirs from ever being created, a logging-handler-restoration bug in
the test runner, and a chain of small issues in the backup controller. Also a fair amount of
dead/orphaned code.

1. **[bug]** `settings/app.py:317-322` (`AppSettings.ensure_paths()`) — directory-vs-file branch is
   inverted from its own comment: when a path has *no* period (the actual directory case —
   `backups_dir`, `downloads_dir`, `exports_dir`, `static_root`, `media_root`), it calls
   `value.parent.mkdir(...)`, creating only the *parent*, never the directory itself. Only
   `log_root` works, because it bypasses this method with its own explicit `mkdir` — evidence this
   was noticed and patched around rather than fixed. `backups_dir` is never actually created by app startup.
2. **[bug]** `core/tests/runner.py:52-64` (`QuietTestRunner.run()`) — `default_handlers =
   logging.root.handlers` is a reference not a copy; removing handlers from it also empties the
   "saved" list, so the later restoration loop is a no-op. After the test suite finishes, the root
   logger permanently has zero handlers for the rest of the process.
3. **[bug]** `core/controllers/backups/db.py:31-42,116-117` — `create_backup_dir()` is defined but
   never called anywhere, so (combined with #1) the backup directory may not exist when `run()`
   invokes `pg_dump -f <dir>/...`.
4. **[bug]** `core/controllers/backups/db.py:29,116-117` — `request_finished.connect(self.trigger_backup)`
   has no `dispatch_uid`, contradicting the repo's own documented signal-connection gotcha; if
   `AppConfig.ready()` ever runs twice, duplicate connections could double-fire backup checks.
5. **[bug]** `core/controllers/backups/db.py:22` — `self.lock = Lock()` created but never acquired
   anywhere — dead synchronization primitive suggesting an anticipated but unguarded race
   (concurrent requests each enqueuing duplicate backup tasks).
6. **[bug]** `settings/app.py:188-193` — `AppSettings.database_*` fields are entirely dead
   configuration; `settings/base.py:141-154` builds `DATABASES` directly from `UL_DB_*` env vars,
   never reading these pydantic fields, whose env-var names (`UL_DATABASE_*`) don't even match
   what's actually consumed — an operator setting `UL_DATABASE_HOST` gets silently ignored.
7. **[improvement]** `models/abstract/model_queue.py:12-13` — unused dead `Queue` class.
8. **[improvement]** `core/tests/testcase.py:15-82` — `TestCases`/`TestCasesTemplate`/`TestEntry`
   (~70 lines of parametrized test-data templating) never imported/used by any actual test file.
9. **[improvement]** `core/tests/testcase.py:84-168` — `_MessagePrefixMixin` is effectively inert;
   no test sets `self.target`/`self.method_name`, so `get_message_prefix()` always returns `""`.
10. **[improvement]** No abstract ViewSet base class exists in `models/abstract/` despite
    CLAUDE.md describing the pattern as "base Model/QuerySet/Manager/ViewSet/Serializer" — each
    entity's viewset subclasses DRF directly, duplicating cross-cutting concerns per entity.
11. **[improvement]** `models/notifications/serializer.py:8-13` — subclasses plain `Serializer`
    (not `ModelSerializer`) but sets `Meta.model` — dead/misleading, DRF never reads `model` there.
12. **[improvement]** `models/abstract/addressable.py:31-67` — several proxy properties
    (`latitude`, `longitude`, `state`, `county`, `city`, `country`) have no docstring while sibling
    properties in the same class do.
13. **[improvement]** `models/abstract/model.py:36-47` — `DashboardModel.Meta` docstring documents
    `unique_together`/`indexes` as if set on this class; the actual body only defines `abstract`/`app_label`.
14. **[improvement]** `UrbanLens/environments/` + `settings/app.py:357-381` — `AppSettings.select_environment()`/
    `refresh_django()` is never called anywhere in the runtime app; the module-level function it
    wraps is called directly elsewhere instead, leaving this whole parallel path dead in practice.
15. **[inefficiency]** `core/controllers/backups/db.py:29,107-114` — `trigger_backup` runs on
    `request_finished` for *every* HTTP request app-wide, doing a DB lookup + filesystem stat pass
    just to check an hourly-granularity condition, duplicating the existing Celery Beat schedule
    entry that already polls this.
16. **[improvement]** `core/controllers/backups/db.py:44-59` (`purge_old_backups()`) — treats every
    entry in the backup dir as a backup file for retention/deletion, with no filename validation.

Clean: `DashboardQuerySet`/`Manager` hierarchy, `slug_or_uuid()`, slug-generation retry/collision
logic, `security.py`/`choices.py`, `SignalSafeBaker` test fixture, `urls.py`/`wsgi.py`/`asgi.py`.

### 01 - Pin & Location core

**Health**: Pin/Location separation is largely respected (Pin's address fields proxy entirely to
`self.location` via `AddressableModel`). Signals, the Redis map-pin cache, and `pin_sync.py` are
well-designed. Systemic bug: several controller write-paths silently break the delta-sync contract
by omitting `updated` from `update_fields`; the shared pin-creation path also bypasses its own
fuzzy-coordinate Location dedup.

1. **[bug]** `services/pins/pin_creation.py:156` (`create_pin_for_profile`, the single documented path
   behind every pin creation) resolves Location via exact-coordinate `get_or_create` instead of the
   fuzzy 50m-radius `get_nearby_or_create()` used everywhere else — GPS jitter creates duplicate
   Location rows instead of consolidating, undermining Location's dedup design.
2. **[bug]** Missing `"updated"` in `update_fields` breaks delta-sync at multiple controller call
   sites: `controllers/pin_edit.py:496,618,653`, `controllers/pin_bulk.py:144,158,192,231` — external
   sync clients silently miss these edits (correct pattern used elsewhere: `models/pin/model.py:424,471,473`).
3. **[bug]** Race condition in coordinate-based dedup — `models/location/queryset.py:133-167` and
   `models/pin/queryset.py:510-583` (`get_nearby_or_create`) both do unlocked check-then-create;
   `PinManager`'s version has no `try/except IntegrityError` against the per-profile unique
   constraint, so a genuine race surfaces as a 500.
4. **[bug]** `models/pin/queryset.py:205-217` (`nearby_pins`) and `models/location/queryset.py:44-59`
   (`nearby_locations`) call Python math functions directly on Django `F()` expressions and filter
   against a never-annotated `distance` — would raise `TypeError` on first use. Dead, unused, broken.
5. **[bug]** Inconsistent label tie-break: `models/pin/model.py:502` (`icon_source_label`) has no
   secondary sort key while `services/map_pins/payload.py:100-101` breaks ties by name — same-order
   labels can pick a different "winning" icon between pin detail and map marker.
6. **[bug]** `controllers/pin_edit.py:401` — `pin.labels.filter(kind="category")  # prime M2M cache`
   builds a lazy QuerySet never iterated; comment describes behavior the line doesn't produce.
7. **[inefficiency]** `controllers/pin_bulk.py`'s bulk edit (170-234) and bulk merge (122-164)
   mutate pins one at a time with individual `.save()` calls instead of `bulk_update`/`update()`,
   each re-firing the full post_save signal chain (Redis cache refresh, wiki-stat sync) per pin.
8. **[inefficiency]** `models/pin/signals.py:107-120` — editing a shared Label re-fetches every
   Pin+Profile carrying that label individually inside `transaction.on_commit` — O(N) synchronous
   work per label edit for popular tags/statuses.
9. **[inefficiency]** `models/pin/queryset.py:453-486` (`overlapping`) materializes every pin's
   boundary polygon in Python and runs O(n²) pairwise `.intersects()` instead of using PostGIS spatial predicates/index.
10. **[improvement]** `models/pin/model.py:520-543` (`effective_address_basic`/`city`/`state`/etc.)
    are documented as "pin's own X, or the location's" fallbacks, but Pin has zero independent
    storage for any of these — the fallback is a no-op and the comments suggest an override
    capability Pin doesn't have (the anti-pattern the architecture forbids).
11. **[improvement]** `models/pin/model.py:703-713` (`effective_latitude`/`longitude`, marked `#
    TODO: Delete this`) duplicate inherited `AddressableModel.latitude`/`longitude`.
12. **[improvement]** `models/pin/model.py:755-778` (`change_category`, `add_category`) call a
    redundant full `self.save()` after an M2M `.add()`/`.remove()`, needlessly re-firing every
    post_save signal.
13. **[improvement]** `models/location/serializer.py:14-17` — redundant double-save
    (`create()` then immediate `.save()` again).
14. **[improvement/dead-code]** `LocationSerializer`/`LocationFilter` unused, not wired into any
    router/viewset.

Clean: `services/map_pins/cache.py` (Redis map cache with locking/pipelines/TTL), `pin_sync.py`
cursor/watermark design, `pin_restructure.py` (correctly includes `updated`), pin/location
templates (no comment/stringformat issues).

### 09 - Pin suggestions & visits

**Health**: Well documented and mostly well tested. Models/controllers are clean with correct
ownership/ordering guards and no misused signals. Main weaknesses are performance patterns in the
matching/clustering path that don't reuse a spatial-prefilter fix already applied to a sibling
function elsewhere in the same file.

1. **[inefficiency]** `services/pins/pin_suggestions.py:264-289` (`_match_hits_to_pins`) loads every
   root pin and resolves its boundary polygon unconditionally, testing every hit against every
   polygon in a nested Python loop (O(hits × pins)) — `services/visits/visits.py:605-616` already hit and
   fixed this exact class of bug (a documented production 60s-timeout incident) with a PostGIS
   `near_point` prefilter; the batch-ingest path (full Immich sweeps) never got the same fix.
2. **[inefficiency]** `services/pins/pin_suggestions.py:319-326` — queries *all* of a profile's pending
   pin-less suggestions fresh on every new cluster inside the ingest loop instead of fetching once.
3. **[bug]** `services/pins/pin_suggestions.py:293-316` (`_cluster_hits`) — the incremental running
   centroid used to decide cluster merges ignores `hit.weight`, while the final stored centroid is
   weight-aware — can misclassify hits near the radius boundary of a heavy local-scan cluster.
4. **[bug]** `models/pin_suggestions/model.py:17` + `services/pins/pin_suggestions.py:153-155`
   (`_merge_dates`) — keeps the **earliest** 30 distinct visit dates and silently drops later ones,
   when recent visits are the more useful ones to keep.
5. **[improvement]** `templates/pages/memories/locations.html:75-86` +
   `controllers/pin_suggestions.py:236-256` — bulk accept/reject correctly returns
   `processed`/`requested` counts and logs exceptions server-side, but the frontend only reads
   `processed` for its toast, silently dropping per-item failures with no error surfaced.
6. **[improvement]** `tests/hypothesis/test_pin_suggestions.py` has zero `@given` tests despite
   covering exactly the pure-function logic (clustering, centroid, date-merge/cap) ideal for
   property-based testing — a direct gap against CLAUDE.md's own mandate.
7. **[improvement]** `frontend/ts/shared/photo-location-cluster.ts:15` (100m) vs
   `services/pins/pin_suggestions.py:56` (~50m) — client "already have a pin" grouping uses a looser
   radius than the server's actual match boundary, misleading the user about what will happen (not
   functionally broken — server re-evaluates on upload).
8. **[improvement]** `frontend/static/js/pin-select-map.js` — hand-written plain JS with no TS
   source/type-checking, inconsistent with the TS-first frontend architecture.
9. **[minor]** `controllers/visits.py:207-225` (`_parse_visited_at`) always attaches UTC tzinfo
   regardless of the user's actual timezone — a late-evening visit in a non-UTC timezone could
   store as the adjacent calendar day.

Clean: model `Meta` (indexes/constraints/choices, including a nice defensive `CheckConstraint` on
`VisitSuggestion`), no signal usage in this slice, ownership/authorization checks, pagination
template guards.

### 10 - Trips + calendar sync

**Health**: Well-engineered — calendar-sync carefully guards against import/export loops, AI
suggestions have careful multi-layer privacy gating, and the identity-masking gap the audit was
primed to look for is already fixed at every single-trip render site checked. Real issues found are
narrower but include one genuine event-duplication bug in calendar auto-sync.

1. **[bug]** `trip_members_panel.html:7-12,122` + `controllers/trip.py:436-448` — the trip settings
   panel's "Allow members to add people" (None/Organizers/Everyone) is fully honored server-side,
   but the "Add Member" button/dialog is hard-gated to the creator only and `_addable_friends()`
   unconditionally returns `[]` for non-creators — setting this permission to "Organizers" or
   "Everyone" has zero visible effect.
2. **[bug]** `models/trips/serializer.py:6-15` — `TripSerializer.Meta.fields` lists `"status"` and
   `"tags"`, neither of which exists on `Trip` or its base classes — would raise on instantiation.
   Dead code today (no viewset references it) but exported via `__all__`.
3. **[bug]** `services/trips/calendar_sync.py:590-635` + `:687-713` — importing a *timed* Google Calendar
   event with sync enabled creates a `TripActivity`, which later gets exported as a brand-new
   separate timed event, while the trip-level auto-sync push PATCHes the *original* linked event to
   all-day — enabling sync on import of a timed event converts it to all-day on the real calendar
   AND creates a duplicate timed event, the opposite of the module's documented dedup guarantee.
4. **[inefficiency]** `models/trips/signals.py:46-67` + `services/trips/calendar_sync.py:783-810` — every
   Trip/TripActivity save enqueues a full `push_trip_to_calendar` task that re-upserts *every*
   scheduled activity's event, not just the changed one — no coalescing/debounce; scales as
   activities × saves × synced profiles.
5. **[improvement]** `controllers/trip.py:636-664` (`TripOverviewView.get`) never applies
   `_apply_trip_list_identity_masking` to its trip lists — currently harmless (mini-row doesn't
   render identity) but a future template tweak could silently reopen the masking gap with no guard.
6. **[inefficiency]** `controllers/trip.py:765-774` + `services/trips/calendar_sync.py:405-454`
   (`_invite_participants`) — both invite paths loop per-invitee with individual
   `get_or_create`/`create` calls instead of a bulk operation.
7. **[improvement]** `models/trips/model.py:201` + `controllers/trip.py:967` — `TripActivity.order`
   has no uniqueness constraint and is set via read-then-write with no locking — concurrent adds
   can produce ambiguous sort ties.
8. **[improvement]** `templates/pages/trips/detail.html` — ~1850-line hand-rolled-JS template;
   some interactivity (tab-switching, dialog field population) looks pushable to HTMX per the
   project's stated philosophy, beyond what genuinely needs JS (map, drag).

Clean: `trip_visibility.py`/`trip_legs.py`/`trip_ai_suggestions.py` privacy gating, calendar-sync's
loop-prevention (`event_originated_from_urbanlens`), `models/trips/queryset.py:60-64`'s documented
fix for a real Django join-reuse pitfall, docstrings/type hints throughout (bar the dead serializer).

### 14 - Notifications

**Health**: Generally well-engineered — live-push signals, native-push device revocation, and
email-safety rate limiting are carefully documented with races explicitly considered. But the
"11×4 matrix" is not actually table-driven: only WhatsApp/SMS got centralized; in-app/email are
still hand-rolled at 10+ call sites, several of which never check the user's preference at all.

1. **[bug]** `controllers/pin_sharing.py:273` + `services/sharing/pin_sharing.py:76` — `PIN_SHARED`
   notifications created unconditionally with no read of `recipient.notification_preferences.pin_shared`
   anywhere — the preference has zero effect on this event type, duplicated in two places.
2. **[bug]** `controllers/comments.py:592-617` (`_notify_reply`, `_notify_reaction`) —
   `COMMENT_REPLY`/`COMMENT_LIKED` created with no `DeliveryPreference` check at all, unlike
   `friend_request`/`added_to_trip`/`visit_suggested` which do check — settings-page controls are silent no-ops.
3. **[bug]** `services/notifications/notification_text_alerts.py:76-86,139-149` — debounce check (`cache.get`)
   and marker set (`cache.set`) are non-atomic — two Celery workers can both pass the debounce
   check before either sets the marker, sending two billed texts instead of one. Same TOCTOU
   pattern repeated in `services/messaging/direct_messages.py:388-535` — a repo-wide gap, not unique here.
   `cache.add()` would close it.
4. **[improvement]** `controllers/notifications.py:23-35` (`_PREF_FIELDS`) is missing
   `safety_checkin_partner_invite`, the 12th preference group on the model — users can never
   change it from its default via Settings UI even though `services/visits/safety.py:460-494` fully
   respects it if changed programmatically.
5. **[inefficiency]** `controllers/notifications.py:60,110` — dropdown/mark-all-read only
   `select_related("source_profile")`, but the template accesses `pin_share`/`visit_suggestion`
   relations and triggers a per-row `Friendship.objects.between()` query for friend-request rows —
   up to ~20 extra N+1 queries per dropdown open.
6. **[improvement]** `NotificationType` defines 28 values but `NotificationPreference` only has
   fields for 12 — 5 ordinary user-facing types (`FRIEND_SUGGESTION`, `SPOTGUESSR_INVITE`,
   `TRIVIA_INVITE`, `AI_EXTRACTION`, `PIN_IMPORT_COMPLETE`) bypass the preference system entirely
   with no model field.
7. **[improvement]** `models/notifications/serializer.py` — dead code, zero consumers (same
   deleted-viewset pattern seen in units 03/08).
8. **[improvement]** `models/notifications/meta/status.py:30` — `Status.DISMISSED` defined but
   never set anywhere in application code; no dismiss affordance exists.
9. **[improvement]** Duplicated `_send_email` helpers across `services/notifications/notifications.py`,
   `safety.py`, `account_deletion.py`; combined with findings #1/#2, preference-branch logic is
   scattered across ~10 files rather than centralized — only the WhatsApp/SMS leg is actually table-driven.

Clean: signal receivers (all use `dispatch_uid`, never call `.save()`, defer via
`transaction.on_commit`), `services/notifications/push.py` (race-free `F()` counting, SSRF-hardened endpoint
validation), WhatsApp/SMS no-op behavior, the 60s polling fallback (lightweight unread-count only).

### 11 - Safety check-ins

**Health**: Well-documented and carefully reasoned; the privacy boundary between
contacts/partners/live-location is genuinely well tested. But the newest work (live location
sharing) has a real functional gap, and the escalation path — the single most safety-critical code
path in the feature — has a duplicate-notification bug easy to trigger under any partial failure.

1. **[bug]** `services/visits/safety.py:940-981` (`escalate_checkin`) iterates all contacts unconditionally
   (no `notified_at__isnull=True` filter) and only saves `status`/`escalated_at` *after* the whole
   loop completes — if anything raises mid-loop, the next 5-minute beat tick re-matches the checkin
   and **re-emails every contact already notified**, including real emergency contacts. Contrast
   `_resolve_as_found_safe` (1038-1044), which flips status *before* its loop for exactly this reason.
2. **[bug]** `tasks.py:1629-1671` + `settings/base.py:266-277` — the three checkin beat tasks
   (reminders, final warnings, escalation) run every 5 minutes with no locking, unlike the
   `RUN_LOCK_CACHE_KEY` pattern already established elsewhere in the same file for this exact problem.
3. **[bug]** `templates/pages/safety/detail.html:142-160` + `consumers.py:363-394` — live location
   has no initial-render hydration; the detail page never reads `live_latitude`/`live_longitude`
   server-side and the chat consumer sends no initial state on join. If the owner's phone dies
   mid-trip (the exact scenario this feature exists for), a partner opening the check-in later sees
   "waiting for an update" forever despite the last position being durably stored.
4. **[bug]** `controllers/safety.py:1528-1555` (`SafetyCheckinMessageView.post`) — the no-JS/socket-down
   HTTP chat fallback saves the message but never calls `_broadcast_chat_message` (unlike every
   other message-creation path) — invisible in real time to other participants with an open socket.
5. **[inefficiency]** `services/visits/safety.py:953-976,642-669,1064-1090` — contact loops lack
   `select_related("contact_profile__user")` and `is_contact_opted_out` issues one query per
   contact instead of batching — O(3n) avoidable queries per escalation.
6. **[inefficiency]** `tasks.py:1636,1651,1666` — due/final-warning/overdue querysets aren't
   `select_related("profile__user")` despite every downstream call accessing `checkin.profile.user.email`.
7. **[bug, minor/edge]** `controllers/safety.py:182-204` (`_get_checkin_as_partner`) — `slug` is
   only unique per-owner, so a partner accepted on two owners' checkins with the same slug text
   raises an uncaught `MultipleObjectsReturned` (500) instead of a graceful fallback.
8. **[improvement]** `services/visits/safety.py:98-115` (`_send_email`) — `render_to_string` runs outside
   the `try`, so a template-context bug raises uncaught instead of being logged like surrounding SMTP failures.
9. **[improvement]** `models/safety/__init__.py:1-22` — `SafetyCheckinPartner`, `SafetyContactOptOut`
   etc. missing from the package's public `__all__` despite being core actively-used types.
10. **[improvement]** `templates/pages/safety/detail.html:477-478` — live-location "last updated"
    uses `toLocaleTimeString()` only (no date) — ambiguous staleness display in a safety-critical UI.
11. **[improvement]** `controllers/safety.py:1048-1100` — no server-side rate limiting on the
    location-update endpoint; only a client-side 30s throttle.

Clean: consumer token/session authorization split (well tested, including the tricky "same profile
is both contact and partner" case), opt-out scoping, the "exactly one of profile/email" CheckConstraint.

### 12 - Social layer: friendship, profile, reviews, comments, reactions

**Health**: Unusually well-documented; identity-masking/visibility infrastructure
(`identity_visibility.py`, `Profile.visibility_permits`) is genuinely well-designed and mostly
correctly routed through. Found one confirmed privacy bypass, one confirmed information leak, and
several pieces of dead/broken code that would crash the moment anything tried to use them.

1. **[bug]** `controllers/friendship.py:666` — `invite_by_email`'s visibility check for a matched
   existing account only tests `!= VisibilityChoice.NO_ONE`, unlike `request_friend` (line 317)
   which runs the full `visibility_permits` evaluator — a profile restricted to FRIENDS/COMMON_PIN/
   COMMON_FRIEND/COMMON_TRIP/ANYTHING_IN_COMMON can still be friend-requested by any stranger who
   knows their email. Only the NO_ONE case is tested.
2. **[bug]** `controllers/userprofile.py:157-158` + `templates/pages/profile/index.html:66-77` —
   `common_pin_count` is shown unconditionally; only the *link* to detail is gated by
   `can_view_common_pins_with` — any two strangers see "3 Places in Common" even when the setting
   says only friends should know the overlap exists at all.
3. **[bug]** `models/friendship/model.py:21` — `permissions` field is required but no production
   code path ever sets it (`request()`/`accept()`/`decline()`/etc. all omit it) — every real row
   persists `permissions=""`, an invalid choice, making `queryset.py:105-109`'s `has_permission()` dead/non-functional.
4. **[bug]** `models/profile/filterset.py:8-11` — `ProfileFilter.Meta.fields` includes
   `icon`/`categories`/`priority`/`last_visited`, which are `Pin` fields, not `Profile` fields
   (copy-paste) — raises `TypeError` at class-definition time if ever imported/used. Currently unreferenced.
5. **[bug]** `models/reviews/viewset.py:25-46` — `ReviewViewSet.create()`'s `else` branch does a
   second `serializer.save()` for the same (profile, pin) after `get_or_create` already succeeded,
   violating the unique_together constraint, plus assigns a raw int to `data["pin"]` incorrectly.
   Dead code (only `create_or_update` is registered) but would crash immediately if exposed.
6. **[inefficiency]** `controllers/comments.py:186-191` vs `:211,224` — `can_view_comments_from` is
   called once per comment/reply instead of cached per author, redundantly re-running up to 3 extra
   query pairs for the same commenter across a thread.
7. **[improvement]** `models/comments/serializer.py`, `profile/serializer.py`, `social_link/serializer.py`,
   `friendship/serializer.py` — all dead (unregistered), and unlike `ReviewSerializer` leave FK
   fields fully writable — an instant impersonation/reassignment bug if ever wired up without guards.
8. **[improvement]** `models/friendship/queryset.py`/`model.py` docstrings are terse one-liners
   missing Args/Returns, unlike sibling modules.
9. **[improvement]** `models/profile/model.py:807-851` — `can_view_common_pins_with` extends the
   "pending request counts as friend" courtesy that `can_view_contact_info` explicitly opts out of
   for being "more sensitive" — common-pins data arguably deserves the same opt-out (product call, not a guaranteed bug).
10. **[improvement]** `templates/partials/profile/friends_page_content.html` — `is_own_profile`
    branch is always true given the controller's own gating — dead conditional.

Clean: `identity_visibility.py`, `avatar_colors.py`, `connections.py`, `profile_preview.py`,
`mentions.py`, `profile_photos.py` — no leak patterns found; Reaction's polymorphic unique
constraints and Comment's parent-deletion signal are correctly implemented.

### 15 - Custom fields

**Health**: Well-architected — typed value columns, thorough docstrings, careful CASCADE-based
referential integrity, disciplined access-scoping. Main weaknesses: an edit-time validation gap
letting a field definition drift out of sync with stored values, and reference-field choice-building
that re-executes an unbounded query on nearly every map/search request.

1. **[bug]** `controllers/custom_fields.py:290-317` — guards against changing `field_type`/`ref_type`
   while values exist, but applies no such guard when editing a SELECT field's `options` — removing/
   renaming an option leaves existing stored values orphaned (renders blank, then silently deleted
   on the next unrelated save). Untested against this scenario.
2. **[inefficiency]** `services/custom_fields/custom_field_references.py:69,129,186` — pin reference choices have
   no `select_related("location")` unlike wiki's equivalent, so `reference_label()` issues one extra
   query per unnamed pin candidate (up to 501).
3. **[inefficiency]** `forms/search.py:139-140` — `reference_choices()` (full query + Python sort of
   up to 500 rows) runs for every REFERENCE-type custom field on every `SearchForm` construction,
   including `pin_list_panel` which fires on nearly every map pan/zoom/filter change.
4. **[improvement]** `controllers/custom_fields.py:157-213` (`_parse_definition`) — `display` isn't
   validated against `entity_type`, allowing meaningless persisted state (e.g. pin-only display
   modes on a PHOTO field).
5. **[inefficiency]** `services/import_export/import_data.py:1084-1130` — reimplements `CustomFieldValue.set_value()`'s
   type coercion instead of reusing it, and is looser (CHECKBOX accepts any truthy JSON; SELECT
   re-import never validates against `select_choices`).
6. **[improvement]** `models/custom_fields/model.py:601-608` — unchecked HTML checkboxes always go
   through the "delete row" branch, never `set_value`'s `value_boolean=False` path — consistent with
   query semantics but reads like an oversight without a comment explaining it's deliberate.
7. **[improvement]** `models/custom_fields/model.py:334-342` — slider bounds stored as JSON floats
   render as `"100.0"` instead of `"100"` in min/max inputs — cosmetic.

Clean: cross-entity leakage guards (every endpoint filters by explicit `entity_type=`), CASCADE-based
reference-target deletion (avoids signal/save-in-handler pitfalls), `rows_for_target`'s two-query batching.

### 17 - Account & Auth incl. webauthn/2FA/onboarding/API keys

**Health**: Generally well-engineered — hashing used consistently for backup codes/API keys,
WebAuthn replay/sign-count handling correct, TOTP step-replay protection correct, encrypted-field
failures handled thoughtfully (self-healing, not crash-looping). Most significant issue: login
lockout keys on the raw unnormalized username field even though login accepts username/primary/
secondary email with Gmail normalization — brute-force lockout is bypassable.

1. **[bug]** `controllers/account.py:51-58,623-634,657-692` — login lockout/failed-attempt tracking
   is keyed by the raw submitted "username" string, but `EmailOrUsernameModelBackend` resolves that
   same field against primary email, verified secondary email, and Gmail dot/plus-normalized
   variants before authenticating — an attacker can brute-force one account indefinitely by rotating
   through equivalent-but-textually-distinct login strings, each getting its own untripped counter. Untested.
2. **[improvement, security]** `controllers/webauthn.py:75-83` (`PasskeyDeleteView`) and
   `controllers/two_factor.py:124-134` (`TOTPDisableView`) — removing a passkey or disabling TOTP
   requires no password re-entry, unlike account deletion — a meaningful defense-in-depth gap on
   the surface where step-up auth matters most (session compromise/stored-XSS-driven forced POST).
3. **[bug]** `controllers/account.py:379` — `ResendVerificationView.post()` looks up the pending
   account via a raw `email__iexact` filter, bypassing `find_user_by_email`/`normalize_email` used
   everywhere else — a user who resends with a dot/plus-variant of their signup email silently gets
   no match (still shows the generic "check your email" page, so no enumeration issue, but the user
   never receives anything).
4. **[inefficiency]** `services/auth/username.py:52-70` (`username_is_taken()`) pulls every username in
   the `User` table into Python and compares in a loop, on every signup attempt and up to 20x per
   random-username generation call — O(total users), no caching/indexed lookup.
5. **[improvement]** `controllers/onboarding.py:41-42` — `profile.save(update_fields=[...])` omits
   `"updated"`, so this save silently doesn't bump the modification timestamp — same class of bug
   found independently in unit 01 (pin/location).

Clean: `models/account/` (backup codes, API keys, WebAuthn/TOTP scoping), `auth_backend.py`,
`webauthn.py`/`two_factor.py` (correct replay protection), `api_keys.py` (correct active+hash-compare
scoping), `HaveIBeenPwnedValidator`'s documented fail-open, no N+1s found (API-key usage log correctly prefetched).

### 19 - Site admin & settings

**Health**: Generally solid — permission gating consistently uses `dashboard.view_site_admin`,
settings mutation is defensively clamped/validated, subscription/feature model is clean. Two real
defects: pending-subscription-grant redemption silently drops grants for expired invites, and the
backup writer has no atomic-write protection against partial dumps.

1. **[bug]** `controllers/account.py:928-931` + `models/friendship/invitation/model.py:52` —
   `_collect_pending_invitations` only returns invitations with `expires_at__gt=now()` (14-day
   default); a `PendingSubscriptionGrant` attached to an invite is only redeemed for invitations
   returned by that query — if the invited user signs up after 14 days, the grant is orphaned forever.
2. **[bug]** `core/controllers/backups/db.py:79-105` — `pg_dump` writes directly to the final
   filename with no temp-file-then-rename step; a mid-dump process death leaves a truncated `.sql`
   file that `backup_files()`/`purge_old_backups()` treat as a normal completed backup (no size/validity check).
3. **[bug]** `controllers/account.py:956-957` — `_apply_pending_invitation` returns immediately for
   the self-invite edge case before the grant-redemption loop runs — any attached subscription grant
   is silently dropped rather than applied or logged.
4. **[bug]** `controllers/site_admin.py:1461-1484` (`CeleryTaskStatusView`) only requires
   `LoginRequiredMixin` with no ownership check on `task_id`, unlike `ExportStatusView`/`ImportStatusView`
   in `controllers/tools.py` which explicitly verify ownership — any authenticated user who obtains a
   task_id can poll its progress/result cross-account.
5. **[inefficiency]** `controllers/site_admin.py:1045-1078` + `services/media/storage.py:62-100` —
   `SiteAdminUsersView.get()` calls per-user quota/storage-used helpers in a loop (~3 extra queries
   × 25 users per page = ~75 queries) instead of batched grouped queries.
6. **[improvement]** `controllers/site_admin.py:568,595,612,618` — subscription grants/revoke are
   scoped to `granted_by=request.user` — on a multi-admin site, one admin can't see/revoke another
   admin's grants.
7. **[improvement]** `controllers/site_admin.py:47-71` (`_monthly_series`) approximates months as
   flat 30-day blocks rather than real calendar-month subtraction — can be a month off depending on
   which months fall in range.
8. **[improvement]** `services/admin/backups.py:23-27` + `core/controllers/backups/db.py:44-59` — backup
   file counting/purging has no filename validation; a stray non-backup file inflates stats and can
   be deleted/counted toward retention.
9. **[improvement]** No restore tooling exists anywhere in the codebase — only `pg_dump`-based backup, no `pg_restore`/documented recovery path.
10. **[improvement]** `controllers/site_admin.py:1-4` — file's own header TODO about redundant
    `handle_no_permission` overrides is still unresolved, duplicated across 7 view classes.
11. **[improvement]** `models/api_rate_limit/queryset.py:16-21` — manually overrides `get_queryset()`
    instead of the `Manager.from_queryset(...)` pattern used elsewhere in this slice — stylistic inconsistency.

Clean: `models/site_settings/` (thorough validators/CheckConstraints), `models/api_call_log/`
(efficient single-query `summary_by_service()`), `infrastructure_stats.py`'s 30s-TTL cache around
the expensive Celery RPC collection.

### 18 - Undo framework

**Health**: Well above codebase average — the design explicitly fixes a real "cache eviction
silently loses an undo entry" race by moving the restore payload onto the durable `UndoAction` row
itself. Decorator-based handler registry, no signal/dispatch_uid concerns (deletes are stashed
explicitly, not via post_delete), hourly pruning task. Gaps are in per-controller call sites, not the core framework.

1. **[bug]** `controllers/pin_bulk.py:81-83`, `detail_pins.py:216-218,421-424`,
   `location_wiki.py:302-304` — stash-then-loop-delete a subtree with no `transaction.atomic()`
   wrapper (none of these files even import it), unlike `models/pin/viewset.py:127-137` which
   correctly wraps stash+delete atomically. A mid-loop delete failure leaves an `UndoAction` row
   claiming the whole subtree was deleted when only some rows actually were — restoring later duplicates survivors.
2. **[bug]** `services/undo/handlers/safety_checkin.py:99-103`, `handlers/wiki.py:95,111`,
   `handlers/pin.py:111` — `restore()` blindly recreates rows from stashed FKs with no existence
   check; if a referenced profile/label/wiki-creator is independently deleted during the 7-day
   retention window, `restore()` raises an uncaught `IntegrityError` (surfaces as an unhandled 500)
   instead of the graceful `UndoExpiredError` path — neither restore view catches anything but that one exception.
3. **[inefficiency]** Same four call sites as #1 — `Pin.parent_pin`/`Wiki.parent_wiki` are already
   `on_delete=CASCADE`, so deleting the subtree root already cascades everything; the per-descendant
   delete loop is redundant work a single `filter(pk__in=[...]).delete()` would replace.
4. **[improvement]** `services/undo/service.py:31` call sites — `model_label` is a bare hand-typed
   string duplicated at ~8 call sites with no shared constant tying it to each handler's
   `model_label` — a typo only fails at runtime via `get_handler`'s `ValueError`.
5. **[improvement]** `tasks.py:1691-1693` (`prune_expired_undo_actions`) docstring is stale —
   describes a cache-TTL relationship the framework deliberately moved away from.

Clean: handlers' documented scope limits (cascade children intentionally not restored, clearly
surfaced in toast copy) are a deliberate, well-communicated trade-off, not a gap. Listing view has
no N+1 (covered by the `profile+created` composite index).

### 13 - Labels: tags/categories/statuses/people/media

**Health**: The unified `Label` model design is coherent and core primitives are well-tested with
hypothesis coverage. But `controllers/labels.py` (1300+ lines) shows real strain from carrying five
kinds through one god-controller — found a genuine data-corruption bug in kind-conversion, a real
N+1, and a hierarchy-total display bug.

1. **[bug]** `controllers/labels.py:467-478` (`_apply_kind_conversion`) has no branch for
   `new_kind == KIND_CATEGORY` — converting a global Tag to a Category via the standard edit form
   leaves `label.profile=None`, but Category lookups use exact-match `.for_profile()` with no global
   fallback, so the label vanishes from every listing and becomes permanently un-editable/undeletable
   through the UI (`_can_modify_label` returns `False` for non-tag labels with `profile=None`) — a stuck orphaned row.
2. **[inefficiency]** `controllers/labels.py:285-288` (`_queryset_for_kind`) — KIND_USER/KIND_MEDIA
   branches never call `.with_pin_counts()` unlike TAG/CATEGORY/STATUS, but the shared row template
   unconditionally evaluates `children.all()`/`parents.all()` per row — every row on People/Media
   Organize pages fires 2 extra unprefetched queries.
3. **[bug]** `templatetags/dashboard_tags.py:159-172` (`tag_total_pins`) only sums direct pin count
   plus **direct** children's counts (one level), while actual map/pin filtering
   (`get_label_and_descendants`) walks the full multi-level subtree via BFS — the "total pins
   including sub-labels" badge undercounts for 3+ level hierarchies vs. what clicking through actually returns.
4. **[bug]** `controllers/labels.py:926-934,978,995-998` (bulk edit/convert) resolve
   `add_parent_ids`/`add_child_ids` via a raw unscoped query, bypassing `_parent_candidates()`'s
   KIND_USER/KIND_MEDIA isolation enforced everywhere else — not reachable through the shipped UI
   today, but the server trusts client-supplied kind-scoping rather than enforcing it.
5. **[bug]** No cycle prevention on any parent/child write path (`LabelEditView.post`,
   `LabelCreateView.post`, bulk paths) — `get_label_and_descendants` is explicitly BFS-with-`visited`
   because cycles are anticipated as reachable, but nothing at write time rejects an `A→B→A` selection.
6. **[improvement/dead-code]** `models/categories/serializer.py:10-40` — `CategorySerializer`
   declares a `location_count` `SerializerMethodField` but never defines `get_location_count` — would
   raise `AttributeError` if ever serialized. Entire `models/categories/` package is an unused
   backward-compat shim nothing else imports.
7. **[improvement/dead-code]** `models/tags/` — only a stale `.pyc` remains, no source file, nothing imports it.
8. **[improvement]** `models/labels/__init__.py:1` — re-exports kind constants but omits
   `KIND_MEDIA`; every caller works around it by importing from `.model` directly.
9. **[improvement, maintainability]** `controllers/labels.py` — the `ai_kind_enabled`/
   `keyword_kind_enabled` dict-lookup block is duplicated verbatim between `LabelEditView.get`
   (598-609) and `.post` (670-679); the five-way kind if/elif ladder repeats across
   `LabelMultiMergeView` and `_queryset_for_kind`/`_parent_candidates` — the "one model, `kind`
   field, many concepts" design is visibly reaching its complexity ceiling.

Clean: `models/labels/model.py`/`queryset.py`/`signals.py`/`customization/`/`profile_assignment/`
(hypothesis + example test coverage), `services/labels/statuses.py`/`style_suggestions.py`,
LabelCustomization-on-merge (non-issue in practice — customizations only exist for global labels,
merge/convert always restricts sources to profile-owned).

### 21 - REST API surfaces, internal + external

**Health**: Noticeably better shape than average — the external API is deliberately
over-engineered for a third-party-facing surface: key hashing via `make_password`/`check_password`
(timing-safe, indexed-prefix lookup), scope enforcement fails closed, throttling correctly keyed
per-credential, SSRF guarded on the push-device endpoint. One real structural gap: the "deliberately
minimal" internal-REST claim is enforced by how a viewset happens to be wired into urls.py, not by
the viewset class itself.

1. **[improvement]** `models/reviews/viewset.py:16-46` — `ReviewViewSet` subclasses full
   `ModelViewSet` and defines an unrouted `create()` method — inert today (only `create_or_update`
   is bound in urls.py) but a loaded gun: registering it on a router by analogy with `PinViewSet`
   would silently activate list/retrieve/update/destroy.
2. **[improvement]** `services/pins/pin_creation.py:5` — module docstring references a nonexistent
   `PinCreateView` (actual class is `PinsView`) — stale from a prior refactor.
3. **[improvement]** `models/account/model.py:175-183` + `controllers/api_keys.py:39-52` — every
   API key is created with all four scopes and there's no scope-picker UI, so `HasApiKeyScope`
   enforcement is currently unused defense-in-depth rather than a real restriction (documented as
   an intentional stepping-stone, not a bug, but worth prioritizing per the roadmap).
4. **[improvement]** `models/pin/viewset.py:38,109` — `partial_update`/`destroy` re-check
   `instance.profile.user != request.user` after `get_object()`, but `get_queryset()` already
   filters to the requesting user's pins — the check is unreachable dead code that could mask a
   future queryset regression.
5. **[improvement]** `external_api/serializers.py:139-142,148-151` — `limit` fields declare
   `min_value=1` but no `max_value` (the real 1000 cap is enforced downstream, not in the schema) —
   the published OpenAPI contract undersells the actual limit to third-party integrators.
6. **[improvement]** `external_api/permissions.py:47` — correctly fails closed for an
   unrecognized credential type but the docstring doesn't explain this, risking a future refactor
   reading it as a bug.

Clean: authentication precedence/revocation checks, key storage/verification, throttling scoping
(no double-throttling), schema preprocessing hooks, internal router's genuinely minimal PATCH/DELETE-only
`PinViewSet`, push-device SSRF guard, pin-sync N+1-free querysets (reuses `MapPinPayloadService.prepare_queryset`).

### 22 - Direct messaging & E2EE

**Health**: Unusually mature — E2EE key management (opportunistic per-pair/per-group encryption,
canonical-ordered conversation keys, opaque rotation tokens, atomic reset-with-rewrap) is carefully
reasoned and cross-referenced with docs/e2ee.md; most tricky concerns (masking on live WS payloads,
existence-oracle prevention, ReDoS-safe regexes, disappearing-message hard-delete) already have
documented fixes from a prior audit pass. Remaining gaps are N+1s concentrated in the group-chat
notification/broadcast path — ironic since a neighboring function in the same file explicitly
engineers around that exact anti-pattern.

1. **[inefficiency]** `services/messaging/group_chats.py:420-450` (`_notify_group_message`) — loops every
   non-sender membership running a separate unread-check query plus an unprefetched
   notification-preferences access — ~100 extra queries for a full 50-member group on one message
   send, contradicting the explicit anti-N+1 discipline documented one function away in the same file.
2. **[inefficiency]** `services/messaging/group_chats.py:537` (`broadcast_group_message`) — `serialize_group_message`
   re-runs the same `message.shares.exists()` once per group member instead of computing it once.
3. **[bug]** `services/messaging/direct_messages.py:310` (`_notify_recipient`) — the "already has an unread
   message, don't notify again" check doesn't exclude self-deleted-but-still-unread rows; a
   recipient who "Remove for me"s an unread message keeps in-app notifications silently suppressed
   from that sender until the thread is next opened.
4. **[inefficiency]** `services/messaging/direct_messages.py:901-942` + `templatetags/dashboard_tags.py:88-116` —
   `conversations_for`'s bulk last-message fetch has no `prefetch_related("images")`; image/map-only
   last messages trigger an extra `images.exists()` query per sidebar row.
5. **[improvement]** `controllers/group_chats.py` — `membership_for`/`is_manager` are re-queried
   redundantly across context builders that could reuse the already-resolved membership from `_get_group`.
6. **[improvement]** `models/direct_messages/model.py:120-142` — `is_expired_for_recipient` and
   `DirectMessageQuerySet.due_for_hard_delete` duplicate the same retention-threshold logic in two
   places with no shared helper (both already commented as "mirrors," so the risk is flagged but not eliminated).
7. **[improvement]** `services/messaging/direct_messages.py:993-1038` (`thread_page`) — reply-quote WS payload
   calls `quoted.images.exists()` per reply broadcast instead of prefetching — same shape as #4, smaller blast radius.
8. **[improvement, minor]** `models/direct_messages/share.py:90-101` — `_friend_request_exists`
   queries `Friendship` with no caching on a relatively hot per-render path.
9. **[improvement]** `services/messaging/group_chats.py:299-334` (`serialize_group_message`) — has no
   images/markup_map/location_mentions/reply_to fields at all (consistent with documented group-chat
   scope gap) — flagged as the one place that would silently keep dropping that data if group parity is ever extended.

Clean: `services/security/e2ee.py`, `controllers/e2ee.py`, `dm_location_detection.py`,
`map_pin_share_detection.py`, `map_sharing.py`, `DirectMessageConsumer`, `direct_message_shares` —
transactions correctly wrap multi-model writes, permission checks precede state changes,
existence-oracle leaks explicitly guarded, E2EE reset/rewrap atomicity matches its documentation exactly.

### 26 - Misc data models: epa_facility, google_place, property_owner, routes, public_pins, email_log, auto_removals

**Health**: Better shape than a typical "misc" grab-bag — `EpaFacility`, `GooglePlace`,
`PublicPinCandidate`/`Vote`, and `PinAutoRemoval`/`WikiAutoRemoval` are carefully designed with
genuine attention to concurrent-write races and anti-ballot-stuffing. Two real problems: a TOCTOU
race in the outbound-email rate limiter, and an inconsistent dedup path for owner creation.

1. **[bug]** `services/security/email_safety.py:89-111`, consumed at `controllers/friendship.py:649-720` and
   `services/visits/visit_invites.py:82-109` — `email_rate_limit_error()` is a non-atomic check-then-act
   read with the matching write happening much later after an SMTP send; concurrent requests can
   all pass the check before any logs, letting per-hour/day/month invite-email caps be exceeded arbitrarily.
2. **[bug]** `controllers/property_owner.py:331-338` (`WikiOwnershipPanelView.post`) — adds an owner
   unconditionally with no dedup, while the Sale-tab path just below (`:411-416`) does
   `filter(name__iexact=name).first()` first — two code paths for the same shared model disagree on
   dedup, producing duplicate `WikiOwner` rows. Identical inconsistency for `PinOwnershipPanelView.post`
   (line 198) vs. `PinPropertySaleTabView.post`'s `get_or_create` (273-275).
3. **[inefficiency/dead-code]** `services/routexl.py` + `services/apis/routing/routexl.py` — two
   nearly-identical `RouteXLGateway` classes, neither ever imported/instantiated anywhere despite
   `rate_limiter.py`/`site_admin.py` referencing `"routexl"` as if it's a live service — an
   unwired/incomplete route-optimization integration.
4. **[improvement]** `models/property_owner/model.py` — no DB-level uniqueness backs the dedup
   intent above (`PinOwner` has no `(pin, name)` constraint) — even after fixing the controller
   inconsistency, nothing prevents duplicate owner rows under concurrent writes.
5. **[improvement]** `models/google_place/queryset.py:11-17` — `by_coordinates()`/`by_cid()` are
   dead code; all real lookups bypass them and query the manager directly.
6. **[improvement]** `models/routes/` — no dedicated test file for the `Route` model/queryset
   itself (only GPX-simplification helper tests).

Clean: `EpaFacility` (IntegrityError-hardened concurrent-fetch race handling, correct merge
semantics), `PublicPinCandidate`/`Vote` (unique per-profile vote constraint, careful
annotation-fan-out handling), `PinAutoRemoval`/`WikiAutoRemoval` (consistent normalization, wired
into every auto-creation path), `GooglePlaceService`, `EmailSendLog` model itself (the bug is in
the service layer around it, not the model).

### 29 - Forms, templatetags, validators, management commands

**Health**: Generally clean and well-documented — consistent form patterns, careful XSS/N+1
avoidance in templatetags. Two real gaps: an unfixed production bug documented by a leftover debug
script, and a completely unvalidated file-upload path feeding an archive-extraction pipeline.

1. **[bug]** `services/apis/locations/google/geocoding.py:354` (`get_coordinates_by_cid`) — builds
   Places Details requests with a param shape (`params={"cid": str(cid), ...}`) that the diagnostic
   command `management/commands/test_places_api.py:141-166` explicitly demonstrates is broken
   (labeled "[broken format]"), showing the working alternative (`place_id=f"cid:{cid}"`) right next
   to it — the diagnostic confirmed the fix but it was never applied; CID-based coordinate lookups
   (Google Maps URL imports) are likely still silently failing in production.
2. **[bug]** `forms/upload_datafile.py:24-40` — `_MultipleFileField.clean()` validates only
   non-emptiness; no file-size limit, extension allow-list, or MIME check anywhere in the form, and
   the consuming view (`controllers/pin.py:1055-1088`) unconditionally feeds the bytes into
   `is_archive`/`extract_archive` (zip/tgz decompression) with no size ceiling — an attacker can
   upload an arbitrarily large file or a decompression bomb with no gate at the form layer.
3. **[improvement]** `management/commands/test_places_api.py` — a committed one-off debug script
   (hardcodes a CID/URL from a past incident), uses raw `print()` instead of `self.stdout.write`
   unlike its sibling `test_search_api.py`, bypassing Django's output-stream redirection.
4. **[improvement]** `test_places_api.py`/`test_search_api.py` — both named `test_*.py` under
   `management/commands/`, which pytest's default collection glob scans on every full-suite run
   (harmless today, but a naming collision waiting to cause confusion).
5. **[improvement]** `templatetags/dashboard_tags.py:238` (`filter_criteria_summary`) — danger-range
   check uses truthiness while structurally identical priority/vulnerability checks correctly use
   `is not None` — a `max_danger=0` filter silently omits "danger range" from the summary card.
6. **[improvement]** `forms/settings_form.py:279-284` vs `forms/profile_form.py:76-102` — private
   Discord-username field has zero format validation while the public Discord social-link field
   enforces a regex — different fields/purposes so not a bug, but worth a shared validator.
7. **[improvement]** `templatetags/dashboard_tags.py:322-329` (`is_material_icon`) — regex
   `^[a-z_]+$` rejects legitimate Material Symbols icon names containing digits (e.g. `filter_1`,
   `3d_rotation`) — latent since no currently-registered icon has a digit, but a landmine for the next one added.
8. **[improvement]** `forms/search.py:219-241` (`parse_label_groups`) — silently drops any group
   with a negative-number id (`str(i).isdigit()` is False for "-5") with no error surfaced —
   consistent with its documented "never raises" contract, but an unlogged edge case.
9. **[improvement]** `management/commands/backfill_location_country.py` — otherwise solid
   (idempotent, `--dry-run`, `.iterator()`, per-row `.update()`) but no visible cost/usage tracking
   at the command level for its geocoding gateway calls.

Clean: `templatetags/map_components.py`, `memories_components.py`, `validators/password.py`
(correct HIBP fail-open behavior), `provision_mobile_oauth_client.py` (idempotent), `immich_form.py`/`onboarding_form.py`/`profile_form.py`.

### 23 - Real-time / WebSockets

**Health**: Well-documented and structurally sound — origin validation + `AuthMiddlewareStack`
wired correctly, every group name keyed off a server-derived numeric PK (no injection risk),
safety check-in authorization (session-auth owners/partners + token-auth contacts) checked
correctly and consistently with HTTP fallback paths. Main issues: a universal crash bug from an
incomplete `receive()` signature, a narrow pre-accept group-membership leak, and heavy duplication across consumers.

1. **[bug]** `consumers.py:54,133,409,599,733` — every consumer's `receive()` is declared
   `async def receive(self, text_data):` with no `bytes_data` param; Channels' base class calls
   `receive(bytes_data=...)` for any binary WS frame, raising an uncaught `TypeError` that
   propagates out of the ASGI coroutine — `disconnect()`/`group_discard()` never runs, leaking a
   dead channel registration. Affects all five consumers identically; one binary frame from any client kills the connection.
2. **[bug]** `consumers.py:379-393` (`SafetyCheckinChatConsumer.connect`) — `group_add`s happen
   before `self.accept()`; if a later step throws, `close(4500)` before accept never triggers a
   `disconnect` event per ASGI semantics, leaking channel-layer group membership. Same pre-accept-add pattern in every consumer, narrower elsewhere.
3. **[improvement/fragility]** `consumers.py:379,384` vs `services/visits/safety.py:606,1208,1246` —
   safety check-in group names are hardcoded independently in three places instead of behind a
   shared helper, unlike every other real-time surface (`notification_group_name()`,
   `direct_message_group_name()`, `session_group_name()`) — a future rename silently breaks
   delivery with no exception raised.
4. **[improvement]** `consumers.py:535-676` vs `679-810` — `GameSessionConsumer` and
   `TriviaSessionConsumer` are near line-for-line duplicates (connect/disconnect/receive/relay
   handlers) — a shared generic base would cut ~140 lines and keep them in sync.
5. **[improvement]** `consumers.py:626-645,760-779` — seven relay handlers per game/trivia
   consumer are undocumented one-line pass-throughs, violating the docstring-on-every-method rule.
6. **[improvement]** Whole file — no type hints on any of ~30 consumer methods, systemic across the file.
7. **[improvement]** `consumers.py:28-53,99-132,363-407,563-597,697-731` — connect/disconnect
   boilerplate (auth check, close-code convention, group add/discard) hand-copied across all five
   consumers instead of a shared base class.
8. **[inefficiency/hardening gap]** No per-connection rate limiting in `receive()` for any chat/DM
   consumer — a client can spam message-creation in a tight loop with no throttle.
9. **[minor]** `consumers.py:74,287` — `UserNotificationConsumer`/`DirectMessageConsumer` are two
   separate per-profile sockets per tab, each independently resolving the profile on connect —
   architecture observation, not a defect.

Clean: no cross-tenant auth bypass found anywhere — origin validation, PK-keyed groups, safety
session/contact-token verification, GameSession/TriviaSession participant verification, and the
consistent 404-not-403 existence-leak-avoidance convention all check out.

### 30 - Frontend TypeScript

**Health** (34 of ~50 files read in full, plus a full grep sweep): Noticeably more disciplined than
a typical legacy corner — consistent `escHtml()` before any `innerHTML` write of user/server data,
careful event-listener/object-URL cleanup, well-reasoned E2EE crypto/client trust boundaries.
Main weaknesses are architectural: real duplication between trivia.ts/spotguessr.ts, a whole dead
class duplicating a newer one, and one API call path bypassing the project's rate-limiter layer.

1. **[improvement]** `shared/label-bulk-manager.ts:124-625` — the entire 625-line
   `BulkEntityManager` class is dead code (its only callers, `entries/categories.ts`/`tags.ts`, no
   longer exist); `shared/organize-tab-manager.ts`'s `OrgTabManager` re-implements ~70% of the same
   logic under different config field names — ~500 duplicated lines to delete/unify.
2. **[improvement]** `entries/trivia.ts:113-345` vs `entries/spotguessr.ts:200-1257` — lobby/chat/
   WebSocket plumbing (`urlFor`, `postForm`, `connectSessionSocket`, `handleSocketMessage`,
   `appendChatMessage`, etc.) is near byte-identical between the two game entries (trivia.ts's own
   comment admits "mirrors spotguessr.ts's shape") — belongs in a shared module.
3. **[bug]** `entries/trivia.ts:196` + `entries/spotguessr.ts:545` (`handleInviteMore`) — invites a
   friend via a native `window.prompt()` requiring an exact typed username; any typo/case mismatch
   silently no-ops with zero error toast, on a page that otherwise uses a proper checkbox picker and toasts everywhere else.
4. **[bug/inefficiency]** `entries/trivia.ts:269-285` + `entries/spotguessr.ts:1181-1197`
   (`connectSessionSocket`) — no `error` listener and no reconnect logic; any unexpected close
   silently stops live round sync/chat for the rest of the session with no user-facing notice.
5. **[improvement]** `shared/location-search-engine.ts:120-145,891-913` — OSM Nominatim is called
   directly from the browser on every debounced keystroke, while Google Places goes through a
   server-side proxy — bypasses the rate-limiter/cost-tracking layer for one of two geocoding
   providers and pushes every user's browser at Nominatim's free service against its own usage policy.
6. **[improvement]** `shared/e2ee-client.ts:257-265` — failed derived-mode login uses legacy
   `document.write()` to swap in an error page, inconsistent with the other three form-wiring flows
   in the same file which all just call `form.submit()`.
7. **[improvement]** `shared/e2ee-client.ts:532,1052` — redundant `(window as {toastr?})` narrowing
   casts even though `window.toastr` is already declared non-optional in `types/globals.d.ts`.

Clean: XSS hygiene (every dynamic `innerHTML` write checked runs through `escHtml()` first, the one
exception is a safe `data:` URI), `e2ee-crypto.ts`/`e2ee-store.ts` (no raw secrets ever leave the
browser/get logged, fails closed), `photo-location-scan.ts`/`article-wysiwyg.ts` cleanup, `tools/generate-e2ee-fixture.ts` self-verification.

### 25 - Trivia

**Health**: Well-documented, deliberately-mirrored port of SpotGuessr's multiplayer architecture
with good docstring discipline and solid hypothesis/example test coverage for pure-logic pieces.
Core solo-play and moderation-classifier paths are correct and fail closed. Two real problems: an
authorization gap letting an invited-but-not-joined participant actually play, and a documented
spec feature scaffolded in the schema but never implemented.

1. **[bug]** `services/trivia/session.py:279-349` + `controllers/trivia.py:43-52`
   (`submit_answer`) never verifies `TriviaSessionParticipant.status == JOINED` — an
   INVITED-but-never-joined participant can POST a real answer, both scoring points for them and
   inflating the answer count used to decide round completion, potentially flipping a round to
   `revealed` before all actually-joined players answer. Untested (the existing test builds the
   scenario but never has the never-joined participant call `submit_answer`). **Identical gap
   exists in `services/spotguessr/session.py:submit_guess`** — a shared architectural hole, not Trivia-specific.
2. **[bug/gap]** `models/trivia/model.py:105-107` (`wiki_incorporated_at`) — docs/prompts/todo.md
   specifies AI should incorporate upvoted trivia into wiki articles; the field exists in the
   schema for this purpose but is never read/set anywhere outside `model.py` and its migration —
   entirely unimplemented, just looks done because the column exists.
3. **[gap]** No moderation backstop anywhere — `TriviaQuestion` moderation is 100% automated by the
   classifier with no admin registration, no site-admin review UI for PENDING_REVIEW/REJECTED/
   heavily-reported questions — staff can't correct a bad AI verdict short of a raw DB edit, despite
   the classifier's own docstring calling this "the highest-harm piece of the whole feature."
4. **[inefficiency]** `services/trivia/eligibility.py:58-68` (`eligible_questions()`) does two
   per-row Python loops with per-candidate queries on every call, and `get_or_create_round` calls it
   on *every* round transition (session start, every completing answer, every idle poll) — O(locations
   + questions) extra round-trips per round, unlike SpotGuessr's equivalent which stays a single lazy queryset.
5. **[improvement]** `consumers.TriviaSessionConsumer` is a near line-for-line copy of
   `GameSessionConsumer`; `services/trivia/realtime.py`/`chat.py`/session lifecycle are structurally
   identical to their SpotGuessr counterparts — any lobby/chat/consumer fix (e.g. finding #1) has to
   be applied twice, contrary to the project's inheritance-first convention.
6. **[improvement]** `frontend/ts/entries/trivia.ts:186-207` (`handleInviteMore`) — same
   `window.prompt()`-with-exact-match pattern flagged in unit 30 for spotguessr.ts, here too instead
   of reusing the checkbox picker already built for the initial start flow in the same file.
7. **[improvement]** `trivia.ts` action handlers (`startGame`/`joinLobby`/`beginGame`/`submitAnswer`)
   — none disable their button or show a loading state while the fetch is in flight.
8. **[improvement]** `TriviaSessionStatus.ABANDONED` — defined but never assigned anywhere; a
   never-begun or abandoned-mid-game lobby stays LOBBY/ACTIVE forever with no cleanup sweep (same
   gap pre-exists on SpotGuessr's equivalent status).
9. **[improvement]** `services/trivia/deterministic.py:57,76` — dedupe key is keyed only on
   building `name`; two distinct buildings sharing an identical/truncated name would collide and
   silently keep only the first building's question.
10. **[improvement]** No leave/cancel/kick path for a lobby once invited.

Clean: no Django signal usage in Trivia at all; AI-gated paths (classifier, answer-check,
generation) all fail closed correctly with prompt-injection wrapping and cost logging.

### 27 - Import/Export & data safety

**Health**: Unusually well-documented and security-conscious in intent — path-traversal,
content-sniffing, malware-scanning, and malformed-input handling are all present and mostly
correct, heavy work properly dispatched to Celery. But the full-archive importer re-implements zip
handling from scratch instead of reusing `archive_extractor.py`'s safer primitives, and drops the
one protection that matters most for a decompression bomb.

1. **[bug/security]** `services/import_export/import_data.py:275-289` — decompression-bomb protection checks only
   the ZIP's *declared* (attacker-controlled) `file_size` metadata against a ceiling, then calls
   `zf.extractall()` unbounded — Python's zipfile only detects a declared-vs-actual mismatch via
   CRC32 *after* a member is fully decompressed and written to disk. A crafted ZIP well within the
   500MB upload cap with forged small `file_size` fields but highly compressible payloads can
   decompress to hundreds of GB before the mismatch is caught. `archive_extractor.py:_extract_zip`
   already correctly guards this exact scenario (caps each member read, rejects on overrun) but
   `import_data.py` doesn't reuse it — the team's own test asserts this exact guarantee that the
   code doesn't actually deliver.
2. **[bug]** `services/import_formats/gpx.py:41` + `gpx_tracks.py:140` — `gpxpy.parse(text)`
   internally builds an `lxml`/ElementTree XML tree with no `defusedxml` wrapper, inconsistent with
   `osm_xml.py:21` which explicitly uses `defusedxml` for the same class of untrusted uploaded XML —
   a latent XXE surface since lxml is installed and gpxpy will prefer it.
3. **[inefficiency]** `services/import_export/import_data.py:1338` → `services/media/storage.py:99` — `_import_photos`
   calls `quota_error_for_upload` once per photo row, each recomputing a fresh full-table
   `Sum("file_size")` aggregate from scratch — restoring an archive with thousands of photos issues
   thousands of increasingly-expensive aggregate queries instead of tracking usage incrementally in memory.
4. **[bug/improvement]** `services/import_export/import_data.py:275-289` — unlike `archive_extractor.py` which
   explicitly skips symlink ZIP entries, the full-archive importer's extraction has no symlink
   check — likely low exploitability but an unexplained inconsistency between two archive-handling paths.
5. **[improvement]** `services/security/malware_scan.py:71` used from `import_data.py:340` —
   `_bounded_copy`'s corrupt-stream detection silently no-ops for plain filesystem handles (no
   `.size` attribute), disabling a safety net added in response to a production ClamAV incident.
6. **[improvement]** Two independent, divergent size-ceiling constant sets exist for the same class
   of guard (`import_data.py`'s vs. `archive_extractor.py`'s) — invites silent divergence when one is tuned and not the other.
7. **[improvement]** `templates/pages/tools/index.html:458-1305` — ~650 lines of inline
   `<style>`/`<script>` instead of the SCSS/TS pipeline used elsewhere.

Clean: `content_sniffing.py`/`documents.py` (fail-safe OCR degradation), `export.py`/`export_formats.py`
(deliberate redaction logic, no PII leakage, `bulk_create` used appropriately), path-traversal/zip-slip
guards (including sibling-directory-prefix variant), trust-boundary handling for
connections/trips/DMs/comments importers (matches documented "requests not facts" design).

### 20 - AI integration

**Health**: Unusually mature — the tool-calling allowlist in `assistant.py` is genuinely enforced
server-side (dict dispatch, not model trust), prompt-injection scanning is applied consistently in
most places, and the link-extraction field registry is a well-designed parse/apply allowlist. Real
problems: a token/cost-accounting bug inflating cost estimates for 2 of 3 wired providers, and two
synchronous LLM calls violating the project's non-instant-operation rule.

1. **[bug]** `services/ai/gateway.py:385-391` + `anthropic.py:117` + `openai.py:121` — received-token
   counting is double-applied for Anthropic and OpenAI (each provider's `_parse_response` calls
   `receive_tokens` itself, then the base `send_prompt`/`send_prompt_list` calls it again on the
   same string) — Cloudflare's parser correctly doesn't double-call, proving this is a regression.
   Roughly doubles every cost/token estimate for Anthropic and OpenAI calls.
2. **[bug]** `services/ai/openai.py:100-102` — `_get_response` only catches `openai.BadRequestError`;
   `RateLimitError`/`AuthenticationError`/`InternalServerError`/`APIConnectionError` are siblings
   under `APIError`, not subclasses of `BadRequestError`, so none are caught — a routine OpenAI rate
   limit surfaces as an unhandled 500 instead of degrading gracefully like the Anthropic path does.
3. **[inefficiency]** `models/pin/serializer.py:76-86` — `PinSerializer.create()` calls
   `AutoTagService().suggest_for_pin(pin, apply=True)` synchronously in the DRF request cycle,
   potentially blocking pin creation on live LLM latency with no Celery offload/progress indicator.
4. **[inefficiency]** `controllers/pin.py:1189-1200` + `services/ai/document_import.py:125-210`
   (`parse_for_preview`) — loops every uploaded document doing a blocking AI call + geocoding
   inline in one HTTP request, unlike link_extraction's equivalent flow which is properly queued via `safely_enqueue_task`.
5. **[improvement]** `services/ai/link_extraction.py:613-633` — fetched page text is interpolated
   directly into the prompt without `scanner.wrap_user_data`, unlike `document_import.py`/`auto_tag.py`
   which wrap untrusted content per the scanner's own documented contract.
6. **[bug]** `services/ai/huggingface.py:23-36` — `setup()` raises `NotImplementedError`
   unconditionally with genuine setup code sitting dead below the raise; not wired into
   `factory.py`'s dispatch or `AiProviderChoice` at all — pure dead code suggesting a 4th provider
   that doesn't actually work.
7. **[bug]** `services/ai/assistant.py:283` (docstring at line 40) — loop runs
   `range(MAX_TOOL_CALLS + 1)`, an off-by-one letting 7 tool executions occur when 6 is documented
   as the budget — including side-effecting `create_trip`/`add_trip_activity`.
8. **[bug]** `services/ai/link_extraction.py:390-403,504-519` + `assistant.py:165-178`
   (`_tool_create_trip`) — both the daily-extraction-limit check and max-upcoming-trips check are
   TOCTOU races with no transaction/select-for-update between check and create.
9. **[inefficiency]** `services/ai/assistant.py:150-162` (`_tool_list_trips`) — `trip.activities.count()`
   runs once per trip in a loop (N+1) instead of an annotated `Count("activities")`.
10. **[improvement]** `services/ai/assistant.py` loop — the accumulating prompt is rescanned in full
    by `scan()` every iteration, re-scanning already-vetted tool-result JSON, with a pathological
    case where a user's own data matching an injection pattern gets silently redacted inside a tool
    result the model needs verbatim.

Clean: tool allowlist enforcement (real server-side dispatch), link-extraction field registry
(strong pattern, held up as a model for future AI-writes-to-data features), SSRF handling in
link_extraction (re-validated per redirect hop), no signal usage in this slice, docstring/type-hint coverage complete throughout.

### 24 - SpotGuessr

**Health**: Unusually well-documented and internally consistent — eligibility rule, point-vs-boundary
scoring, and Glicko-2 pairing all implemented exactly as the design doc describes; the multiplayer
round-completion race is correctly closed with `select_for_update()`. The most serious problem is an
infrastructure mismatch, not a game-logic bug: the country/state/city bonus feature is wired to an
external service whose rate limit makes it non-functional under real multiplayer load.

1. **[bug]** `services/spotguessr/geo_bonus.py:93` + `plugins/builtin/nominatim.py:138-143` —
   `bonus_points_for_guess()` calls Nominatim reverse-geocoding synchronously on every scored guess,
   but that service is globally rate-limited to 1 call/minute **shared across the entire app**; in
   any multiplayer round with more than one guess per minute (the normal case), all but the first
   call raises `RateLimitExceededError`, silently swallowed into `BonusResult(total=0)` — the
   country/state/city bonus is effectively non-functional for anyone but the first guesser per
   minute site-wide, with no coordinate-based caching to reduce call volume.
2. **[inefficiency]** `services/spotguessr/street_view.py:36` + `session.py:284-318` +
   `serializers.py:66` — Street View round generation calls the billed Google Maps Street View API
   once per candidate location in a retry loop (up to 25 attempts) AND again on every reload/reconnect,
   against a service capped at only 200 calls/day app-wide (shared with pin-detail carousels) — a
   single unlucky round plus a couple of reloads can consume a meaningful fraction of the site's daily budget.
3. **[inefficiency]** `services/spotguessr/session.py:284-318` (`get_or_create_round`) — the retry
   loop re-runs the full multi-join `eligible_locations()` query from scratch on every attempt
   (up to 25x per round) instead of computing the eligible id set once and filtering in Python.
4. **[improvement]** `frontend/ts/entries/spotguessr.ts:535-557` (`handleInviteMore`) — same
   `window.prompt()`-with-exact-match pattern flagged for trivia.ts, inconsistent with the proper
   checkbox picker used for the initial invite flow a few functions away.
5. **[improvement]** `templates/pages/spotguessr/index.html:6-10` — Leaflet CSS is version-pinned
   with an SRI hash, but the Leaflet/leaflet-draw JS loaded from the same CDN has neither a version
   pin nor an SRI hash — no supply-chain integrity check on the JS.
6. **[improvement]** `frontend/ts/entries/spotguessr.ts:659-698` (`renderRound()`) — doesn't reset
   the pin-search input between rounds; a previous round's picked label remains visible until overtyped.
7. **[improvement]** `tests/hypothesis/test_spotguessr_photos.py` — zero `@given` tests despite the
   directory name; the purest-math modules in this slice (`glicko2.py`, `scoring.py`, `geo_bonus.py`)
   have no tests at all despite being ideal Hypothesis candidates.
8. **[improvement]** `services/photos/photo_coordinates.py:58-61` (`recompute_estimated_coordinates()`) —
   re-reads the entire correct-guess history for a photo on every new correct guess with no
   windowing/cap, run synchronously inline with the guess request (acknowledged as a deliberate,
   currently-cheap trade-off, but unbounded over the game's lifetime).

Clean: eligibility engine, multiplayer round-completion race guard (`select_for_update` +
unique-constraint catch), Glicko-2 math (faithful to Glickman's paper), `PhotoCoordinateGuess`
privacy invariant, `GameSessionConsumer` participant-check/close-code conventions.

### 31 - Frontend SCSS

**Health** (31 of 63 files read in full/substantial part, plus full grep sweeps across all 63): the
token architecture itself (`_tokens.scss`/`_surfaces.scss`/`_mixins.scss`) is genuinely well
designed — capped elevation-ladder custom properties that invert cleanly under dark mode. Problems
concentrate in feature files that bypass the token system with raw hex literals, patched
selector-by-selector in a parallel 1101-line `_dark.scss` — a pattern that reliably produces missed
patches. Most important finding is systemic: a family of `var(--fake-token, #hex)` references to
custom properties that are **never defined anywhere**.

1. **[bug]** widespread across ~10 files — `var(--text, …)`, `var(--text-muted, …)`,
   `var(--color-muted, …)`, `var(--surface-2, …)`, `var(--ul-surface-alt, …)`, `var(--ul-danger, …)`,
   `var(--ul-accent, …)` are used as if live design tokens, but none of these custom properties is
   ever defined anywhere in the codebase — every one permanently renders its literal fallback and
   can never respond to `[data-theme="dark"]`, despite looking token-driven. Worst in `_e2ee.scss`
   (13 occurrences), `_setup.scss` (12), plus `_markup.scss`, `_webauthn.scss`, `_messages.scss`,
   `_gallery.scss`, `_games.scss`, `_trivia.scss`, `_assistant.scss`, `_pin-detail.scss`,
   `_profile.scss`, `_wiki.scss` — a broader, previously-uncaught instance of the color-token class
   already "resolved" for _explainer/_map/_e2ee (that review didn't catch that the tokens themselves don't exist).
2. **[bug]** `_messages.scss:2088,2157,2199` — group-chat avatar/member badges use the undefined
   `--ul-surface-alt` fallback (`#eef2ff`), rendering as a pale lavender box against dark surfaces —
   a visible theme break in the actively-developed group-chat feature.
3. **[bug]** `_pin_lists.scss:801-808` — Saved Filters region include/exclude buttons hardcode raw
   green/red with no dark-mode counterpart, unlike every other colored control on the same panel.
4. **[bug]** `_nav.scss:824-825` — Settings "preferences saved" confirmation banner has no
   dark-mode override, renders as a light-green patch on a dark page.
5. **[bug]** `_admin.scss:37-38` — subscription-admin role-pill gradient has no dark-mode override.
6. **[bug]** `_trips.scss:2041-2050,2108` — `.trip-complete-btn`/`.cal-cell--drag-over` hardcode
   light colors with `!important` and no dark patch, inconsistent with neighboring elements in the
   same file that are correctly patched.
7. **[inefficiency]** `_dark.scss` (1101 lines) re-declares ~150+ individual selectors owned by
   other partials (several needing `!important` to win) instead of relying on auto-inverting custom
   properties — doubles the cost of every themed component and is the direct root cause of the
   missed-patch bugs above.
8. **[inefficiency]** Inconsistent responsive breakpoints — `_mixins.scss` defines canonical
   `up()`/`down()` mixins, but many feature files hardcode unrelated ad-hoc breakpoints (720px/900px/
   600px/etc. across gallery, messages, trips, admin, homepage, search), plus `_reset.scss` still
   carries stale Materialize breakpoints matching nothing in the current scale.
9. **[improvement]** 190+ `!important` occurrences app-wide, concentrated in `_map.scss` (~50),
   `_tags.scss`, `_trips.scss`, `_pin-detail.scss`, `_icon-picker.scss`/`_dev_toolbar.scss` (repeat
   `font-size: N !important` half a dozen times each — a case for a shared icon-size mixin); most
   carry no comment explaining the specificity conflict they're working around.
10. **[improvement]** Several raw hex literals duplicate an existing `$color-*` token
    (`_pin_lists.scss:140`, `_admin.scss:37`, `_homepage.scss:361,436`, `_map.scss:79,97`) —
    defeats the point of having named tokens.

Clean: `_mixins.scss`, `_surfaces.scss`, `_buttons.scss`, `_toastr.scss`, `_property_owner.scss`,
`_ai_extractions.scss`, `_pin_suggestions.scss`, `_collapsible.scss`, `_undo_history.scss`,
`_account_deletion.scss`, `_env_indicator.scss` — all consistently token-driven with no dark-mode gaps.

### 28 - Core infra services

**Health**: Generally well-designed — small, single-purpose, well-documented, genuinely reused
files. Two real defects: a verified SSRF gap in the IP-blocklist, and a severe measured performance
bug where every redaction call costs ~217ms of CPU from password-grade PBKDF2 hashing used for
simple log fingerprinting.

1. **[bug]** `url_safety.py:20-22` (`is_blocked_address`) misses RFC 6598 Carrier-Grade-NAT/Shared-Address-Space
   (`100.64.0.0/10`) — verified empirically that this range fails every check the function performs.
   Many cloud providers route internal-only infra through this range, so a URL resolving there
   sails through `ensure_public_http_url` unblocked — a real SSRF gap in link extraction, pin-suggestion
   photo download, and media materialization, all of which rely on this function as their only IP-range guard.
2. **[bug/inefficiency]** `redact.py:25-28` (`_fingerprint`) uses PBKDF2-HMAC-SHA256 with 310,000
   iterations (OWASP password-hashing-grade) purely to produce a log-safe fingerprint — benchmarked
   at ~217ms per call. Called from 37 files as ordinary positional arguments to `logger.debug(...)`,
   so Python evaluates the fingerprint unconditionally **before the logger checks its level** —
   production deployments at INFO/WARNING still pay 200ms+ per suppressed debug call, on the request
   thread, for every location/search/weather/asset gateway call. A fast keyed HMAC would give the
   same guarantee at a fraction of the cost.
3. **[improvement]** `services/loc.py` + `services/digitalcommonwealth.py` — complete unreferenced
   dead-code duplicates of the live `services/apis/assets/` versions (echoes the same finding from unit 07).
4. **[improvement]** The "manually follow redirects + re-validate each hop" DNS-rebind-closing
   pattern is copy-pasted verbatim in 3 places (`media_materialize.py`, `pin_suggestions.py`,
   `ai/link_extraction.py`) instead of one shared helper in `url_safety.py` — any future caller who
   forgets the manual-redirect dance reopens the DNS-rebind gap.
5. **[improvement]** `timeout_utils.py:69-85` (`call_with_deadline`) always returns `default` and
   never raises, making caller-side exception handling around it dead code (`controllers/pin.py:825-844`'s
   `except` clauses can never fire).
6. **[improvement]** `json_safety.py:24-34` (`safe_json_for_script`) has no handling for
   `Decimal`/`datetime`/other non-JSON-native types — would raise uncaught `TypeError` if ever
   passed one (hasn't bitten yet, current call sites only pass primitives).
7. **[improvement, minor]** `redact.py:19` — `_COORDINATE_PARAM_NAMES` allowlist misses several
   gateways' differently-named coordinate params (`apple_maps.py`'s `"loc"`, azure's `"coordinates"`)
   — not currently a live leak, but would silently pass raw coordinates through if reused against those gateways.
8. **[improvement, minor]** `pagination.py:40-44` docstring inaccuracy (claims "nearest valid page"
   clamping; Django actually always resolves out-of-range to the last page).
9. **[improvement, minor]** `geo.py:66-77` (`dissolve_polygons`) — merge loop restarts the full O(n²)
   scan after every pairwise merge, worst-case closer to O(n³); not an active problem at current polygon counts.
10. **[improvement, minor]** `home_widgets.py:118-157` — ~14 independent queries to build one
    homepage context; none are N+1s but could combine via aggregate/annotate if latency becomes a concern.

Clean: `text_limits.py`, `units.py`, `storage.py` (aside from the already-flagged race condition),
`vestigial_assets.py`.

### 32 - Template hygiene cross-cutting pass

**Health**: Ran repo-wide greps for all 6 documented CLAUDE.md template gotchas across the entire
templates/ tree (not a sample) and manually traced every hit. **Zero confirmed violations of any of
the 6 gotchas** — a genuinely strong negative result. Clear evidence of deliberate, disciplined
avoidance: `|add:` targets are pre-stringified almost everywhere, the one genuinely dual-rendered
partial explicitly bypasses `_pagination_controls.html` with a comment explaining why, and every
dialog-with-Leaflet-map case correctly sequences `showModal()` before map init (several with an
explicit comment citing the exact CLAUDE.md failure mode).

1. **[improvement]** `themes/auth_base.html` vs `themes/base.html` — a fully separate root template
   (not `{% extends %}`-based) re-declares favicon/viewport/CSS-bundle links independently; nothing
   enforces the two staying in lockstep if the compiled bundle path or favicon changes.
2. **[improvement]** `themes/base.html:16` — loads a pre-release `jquery-4.0.0-beta.min.js` from CDN
   on every authenticated page, unlike the pinned/stable versions used for toastr in the same file.
3. **[improvement]** `themes/base.html:27` — the htmx CDN script tag has no `integrity`/`crossorigin`
   attributes while jQuery/toastr tags on the same page do.

All 6 gotchas individually confirmed absent: `htmx:afterSwap`/`showModal()` ordering (correct in
every Leaflet-in-dialog case checked), `_page_hero.html` div-wrapping (0 of 58 occurrences wrap it),
`_pagination_controls.html` against unstable `request.path` (all 7 use sites confirmed
per-instance-stable; the one dual-rendered partial explicitly avoids the shared component),
`next_page_number`/`previous_page_number` without a guard (all 4 use sites correctly guarded),
`|add:` on unstringified ids (all 36 occurrences across 25 files correctly pre-stringified or are
legitimate numeric/string-only concatenation), multi-line `{# #}` comments (every hit opens/closes
on the same line).

### 34 - Test suite quality review

**Health**: Strong overall — all 385 test files live under `tests/hypothesis/` (no separate
example-based tree despite the directory name). Mocking discipline around external services is
consistently good, no skip/xfail markers exist anywhere in the suite, and the documented
`@given`+`self.client` anti-pattern occurs exactly once. Main opportunities are coverage gaps in
several large, logic-heavy files with zero hypothesis tests, and underuse of the shared strategies module.

1. **[bug]** `tests/hypothesis/test_map_controller.py:199-209` — the one real instance of the
   documented `@given`+`self.client` anti-pattern in the whole suite; the author added a per-example
   re-login workaround but per CLAUDE.md this combination is documented as unreliable regardless.
   Contrast with `test_safety_partners.py:101-107`, which correctly splits a `@given` property test
   into its own class specifically to avoid this — that pattern should be applied here instead.
2. **[gap]** `tests/hypothesis/strategies.py` is a well-built shared strategy module (safe-ASCII
   text avoiding Postgres ILIKE case-folding divergence, Pin-matching coordinate strategies) but only
   8 of 97 `@given`-using test files import from it — the remaining ~89 redeclare ad hoc strategies
   inline, several not reusing the documented-safe alphabet, a latent source of the exact spurious-failure class the module exists to prevent.
3. **[gap]** `test_export_import_completeness.py` (672 lines) has zero `@given` tests despite
   testing exactly what property-based round-trip testing is built for (export → import → assert equivalence).
4. **[gap]** `test_model_querysets.py` (736 lines, ~40 methods) has zero `@given` tests; several
   methods are the canonical low/high-pk order-independence pattern checked against only one hand-picked pair.
5. **[gap]** `test_child_pins.py` (686 lines: merge/swap/coordinate-dedup logic) has zero `@given`
   tests despite being pure-enough, non-trivial, natural-fit logic (same shape of gap already
   flagged for `test_pin_suggestions.py` by a sibling audit).
6. **[gap]** `test_share_provenance.py` (510 lines) has zero `@given` tests, despite guarding
   exactly the "every share path must call resolve_origin_share + record_share_exposure" invariant
   CLAUDE.md calls out — property-based coverage over arbitrary chain length/branching would
   strengthen this privacy-adjacent invariant more than fixed 2-3-hop examples can.

Confirmed negative results (no issues): no skip/xfail anywhere in 385 files; no unmocked real
network calls (all 10 files importing requests/httpx directly mock appropriately); no exact-log-string
assertions (the handful of `assertLogs` uses check level/category or a substring tied to real
behavior); no other `@given`+`self.client` co-occurrences found among the 25 files where both
patterns appear (all correctly separated into different classes).
