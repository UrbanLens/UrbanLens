# Prompts Backlog Audit — 2026-07-14

Audit of `docs/prompts.txt` (the historical log of prompts submitted to AI coding
assistants) against the current state of the codebase, to confirm which requested
features/bugfixes actually landed. Checked via codebase inspection only (no git
history). Roughly 150 discrete asks were checked across 9 feature domains (pin
details, media section, trips, direct messages, search/lists, settings/admin,
boundary/wiki architecture + security, media uploads/misc bugs, safety checkins).

The overwhelming majority of items were confirmed implemented and matching spec.
This file records what wasn't.

## Real gaps (functionality genuinely missing or broken)

### Media uploads (biggest cluster of misses)
- ~~Import-pins flow has no photo/video upload integration~~ — **correction**:
  this was a false negative. `dashboard/pages/location/import/csv.html` already
  has a full drag-and-drop implementation that uploads photos/videos through the
  existing `memories.photos.upload` endpoint independently of the location-file
  parse/preview/confirm flow, exactly as originally requested (no new backend
  upload code). I initially suspected `PinController.import_form` wasn't passing
  `can_upload_videos`/`can_use_ai_features` into the template context and "fixed"
  it - turned out both are already supplied globally by the `add_feature_access`
  context processor, so that change was a no-op and was reverted. Added
  `test_import_form_media_gate.py` as a regression guard for that
  context-processor wiring since none existed before.
- ~~Video/document upload is essentially a stub~~ — **fixed 2026-07-14**: added
  `SiteFeature.DOCUMENT_UPLOADS`; `Image.media_type`/`ocr_text` fields;
  `services/videos.py` (ffprobe metadata + ffmpeg downscale) and
  `services/documents.py` (LibreOffice-to-PDF + pypdf/OCR text extraction),
  both new dependencies (`pypdf`, `pytesseract`, `pdf2image`) and new Dockerfile
  system packages (`ffmpeg`, `poppler-utils`, `tesseract-ocr`,
  `libreoffice-writer/-calc`); `process_image_upload` now dispatches by media
  type but shares the same tail (location resolution, `VisitSuggestion`
  creation, quota accounting) across all three types - no duplicated logic;
  backend+frontend max-file-size setting (`max_upload_file_size_mb`) distinct
  from storage quota; admin settings for video downscale policy
  (enabled/max-height/subscriber-exemption) and a user-level override, mirroring
  the existing photo-downscale settings; `ocr_text` wired into site search.
  **Caveat**: the actual ffmpeg/LibreOffice/tesseract conversion and OCR
  behavior could not be exercised end-to-end in this environment (no Docker
  access here) - all service-level tests mock the subprocess/library calls, so
  they validate the branching/decision logic, not real media processing. This
  needs a real run in Docker (with the new Dockerfile packages built in) to
  fully confirm.
- ~~The "media badge" type doesn't exist~~ — **fixed 2026-07-14**: added
  `KIND_MEDIA` label kind, signup seeding, a full Organize-page CRUD tab, a
  per-photo label picker, and site-search integration (`labels__name`).

### Confirmed live bug — child pin coordinates
- ~~`Pin.effective_latitude/longitude` always reads from the parent
  `Location`... child pins sharing a Location render stacked on one point~~ —
  **correction**: also a false negative. `detail_pins.py`'s
  `_location_for_coords()` already gives every detail pin its own Location row
  (`threshold_meters=0`, exact-match-only reuse), and `test_child_pins.py`'s
  `DetailPinCoordinateDedupTests` already covers exactly this scenario
  (its docstring quotes the same "stacked on one point" report). No code
  change needed; verified 2026-07-14.

### Security/cleanup
- ~~Name-field sanitization... not implemented anywhere~~ — **fixed
  2026-07-14**: added `naming.sanitize_name()` (allowlisted charset, NFKC
  normalization, curly-quote/dash folding), invoked from `Pin`/`Wiki`/
  `Location`/alias `save()` plus the one bulk-`.update()` bypass in
  `tasks.py`. Also fixed a related, previously-undiscovered bug this work's
  own tests surfaced: `LabelEditView` only validated the *target* kind of a
  kind-conversion POST, not the source, so a crafted request could silently
  convert a People or Media label into a Tag/Category/Status.
- ~~Several dead, unrouted REST ViewSets still exist~~ — **fixed 2026-07-14**:
  deleted `WikiViewSet`, `LocationViewSet`, `CategoryViewSet`, `TripViewSet`,
  `ImageViewSet`, `FriendshipViewSet`, `CommentViewSet` and their `__init__.py`
  references. (Their serializer.py files were left in place - not explicitly
  authorized for deletion.)

### Navigation/UI gaps
- No **"Lists" link in the top nav** — Lists is only reachable as an Organize
  sub-tab; the Lists page also inherits Organize's hero instead of its own
  banner.
- Smart Lists: the boundary-drawing map has no "enable"/add button on the list
  page — you can only clear an existing smart boundary, not create a new one
  from there.
- Trip Overview's "New Trip" button still navigates to the trip list page rather
  than opening the dialog in place.
- Edit-Activity dialog on trips was never updated to match the Add-Activity
  redesign — still has the old proposed/confirmed toggle, "(optional)" text, and
  an always-visible child-trip box.
- The Filters-tab include/exclude pickers use a bespoke parallel "chip-like"
  checkbox style instead of the site's real shared chip component.
- Likely cause of the `htmx:syntax:error` on page load: the global search
  input's `hx-trigger="input[target.value.length >= 2...]"` bracket-filter
  expression is loaded on every page via the shared header, not just the filters
  dialog.

## Minor/cosmetic gaps (low priority)
- Cover-photo scroll arrows aren't hidden when there's no other photo to flip
  to.
- Alias "nickname-only" control is an icon toggle, not a visible checkbox
  positioned below the input.
- `@map` isn't wired into DM's inline-mention-link system (other `@` mentions
  work).
- DM push notifications for shared maps are text-only, no preview thumbnail.
- Onboarding uses three separate privacy toggles instead of one combined
  selector (functionally equivalent).
- Name-source-priority has only a site-wide default, no per-user override.
- Calendar-import checkbox fix kept the native checkbox visible and hid the
  custom one (inverse of the literal ask, but achieves the same visual result).

## Worth knowing, not action items
- The safety-checkin AI note claiming "route markup can't be drawn until after
  checkin creation" appears **stale** — later work added lazy draft-map
  creation, so that limitation seems already resolved.
- `Review` model and the `Location` REST viewset were correctly *kept*/
  *neutralized* rather than deleted — Review is still genuinely used for star
  ratings, and `LocationViewSet` returns `.none()` and isn't registered, so
  there's no actual leak.
