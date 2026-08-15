# Codebase audit — 2026-08-11

A broad sweep for bugs, performance problems, and gaps against the project's own documented
standards. This file records **what was fixed**, **what was checked and found clean** (so it
isn't re-derived), and **which audit methods produce misleading results on this codebase**.

Open items found during the sweep were filed in `docs/PROBLEMS.md` and are only cross-referenced
here.

**Verification (current as of 2026-08-14).** The full Python suite runs in a single pass -
**10,765 passed, 0 failed, 1:11:17**, the run that validated the nine prefetch/N+1 fixes together. That run was left strictly alone for its duration; two
earlier full runs had source copied into the container mid-flight and are recorded in section 4d
as inconsistent snapshots rather than reported as results. Alongside it: **mypy clean across 784
source files**, `ruff check src/urbanlens` clean, `bun run typecheck` clean, `bun test`
**394 pass / 0 fail**, and `bun run build` (the real bundler, not just typecheck) green.

Coverage over the view layer was measured separately and is the basis for a large part of the later
work: **80% of 22,081 statements**, with **208 of 1,795 callables never executed** - see
`docs/reports/2026-08-14-view-coverage.md`.

An earlier version of this paragraph reported 10,406 passing over batch-partitioned runs. It was
accurate when written and quietly wrong for some time afterwards, which is the same failure this
report documents elsewhere: a number that was true once and never re-checked.

---

## 0. The findings that matter most

Full detail below; this is what a reader short on time should look at first.

**Fixed, and user-affecting:**

1. **A scripted SVG could be stored as a photo and served from this origin** — stored XSS. The
   upload gauntlet fails open for formats with no magic-byte signature, and SVG has none.
2. **The "don't keep my location" setting was honoured in the database but not in the file** for
   TIFF, AVIF and *every* video. HEIC has the same gap and is filed, not fixed.
3. **A removed emergency contact kept receiving a safety check-in's chat** — the token route had no
   revocation path at all, while the HTTP endpoint for the same data refused them correctly.
4. **Backup codes were consumable twice**, and a single undecryptable Gotify token 500'd the whole
   site.
5. **One malformed KML aborted an entire import**, losing every other file in the same upload.
6. **Downscaling a PNG rotated it ninety degrees**, permanently.

**Filed, not fixed — each needs a decision that is not mine to make:**

- Refunds and chargebacks never reverse pay-what-you-want access.
- A password reset evicts every session but no API key, and minting a key needs no password.
- Login lockout is identifier-only, so knowing a username is enough to lock someone out.
- No Content-Security-Policy anywhere.
- HEIC uploads cannot have their GPS stripped at all.

**Worth reading even if nothing else:** section 4, on the audit methods that produce false
findings against this codebase — nine catalogued there, with more recorded inline in section 3
(a fetch/catch ratio, a listener add/remove ratio, a docs-coverage scan matching filenames rather
than prose, an admin-permission scan that could not see a local mixin, a `get_or_create` scan blind
to expression-based unique constraints). The pattern is consistent enough to state plainly: on this
codebase a first-cut mechanical scan is a hypothesis generator, never a finding.

## 1. Fixed

### Privacy
- **A scripted SVG could be stored as a photo, and served from this app's origin**
  (`services/security/content_sniffing.py`, `services/media/images.py`). The upload gauntlet is
  size → magic-byte sniff → antivirus, and the sniff deliberately fails *open* for formats
  `filetype` cannot fingerprint. That is right for documents, but **SVG has no magic-byte
  signature at all**, so a `.svg` containing `<script>` passed sniffing, passed ClamAV (script in
  markup matches no virus signature), and was stored.

  Verified end to end before fixing: `image_upload_error` returned `None`, the file was written to
  `avatars/avatar.svg`, and `<script>` survived in the stored bytes. The stored *extension* then
  decides the served Content-Type — nginx's mime.types maps `.svg` to `image/svg+xml` — and this
  app sets **no Content-Security-Policy**, so navigating to that URL executes the script with the
  app's own origin. `X-Content-Type-Options: nosniff` does not help, because nothing is being
  sniffed: the file genuinely is an SVG. Avatars are the worst case, since `MediaGateView` serves
  `avatars/` to any signed-in user by design.

  Photos are now allowlisted by extension, checked *before* the sniff. Scoped to
  `MediaKind.PHOTO` only: documents legitimately arrive as `.docx` and friends and are converted
  after upload, so an unconditional allowlist would have broken them — a test pins that asymmetry.
  Note what the fix deliberately does *not* do: SVG bytes uploaded as `photo.jpg` are still
  accepted, and are harmless, because they are served as `image/jpeg` with `nosniff`. The
  constraint that matters is the stored extension, not the bytes.

  Worth noting the near-miss in the other direction: the two server-generated SVGs in this codebase
  (`AvatarService.generate_emoji_svg`) are **not** vulnerable — the emoji comes from a fixed dict
  and the colour is checked against `MaterialColor.values` with a constant fallback, so the
  template can only ever receive allowlisted values.
- **Self-review of the video fix found two further defects in it** (`services/media/videos.py`).
  Re-reading my own chunk-55 change surfaced that `needs_strip` keyed off
  `metadata["latitude"]` — i.e. off the coordinates having *parsed* — which is the same mistake
  shape as the TIFF bug it was written to fix (gating on `img.info["exif"]` rather than the actual
  EXIF). A location tag in a notation `_parse_iso6709` doesn't handle discloses the location just
  as well and would have been left in place. It now keys off the tag's presence.

  Writing the test for that exposed a second, *pre-existing* defect: `extract_video_metadata` read
  ffprobe's format tags with lowercase keys, but ffprobe reports Matroska tags **uppercase** — and
  `mkv`/`webm` are both accepted uploads. So no `.mkv`/`.webm` ever had its location detected at
  all, for extraction or stripping. Tag lookup is now case-folded. Two false starts on the test
  itself, both caught by its fixture-validity assertion rather than passing vacuously: the MP4
  muxer silently refuses to write a non-ISO 6709 `location` tag, so the fixture had to move to
  Matroska, which stores it verbatim.

- **Video uploads ignored the location opt-out entirely — the flag guaranteed the opposite**
  (`services/media/videos.py`, `tasks.py`). `_process_video_upload` expressed "strip the location"
  by calling `process_uploaded_video(image, None if strip_location else max_height)`. But
  `max_height=None` means *skip processing entirely*, so asking to scrub a video reliably produced
  the untouched original — location tag and all — as the stored, served file. Only the derived
  `Image.latitude`/`longitude` and `taken_at` were dropped. Videos plainly carry coordinates here:
  the same module parses `location` / `com.apple.quicktime.location.ISO6709` out of them.

  A second, independent defect sat behind it: `_reencode` passed no metadata flags, and ffmpeg
  copies container metadata across a transcode — verified directly, the tags survive the old flag
  set — so even a video that *was* downscaled kept its coordinates.

  Both fixed. `strip_location` is now a real keyword argument independent of `max_height`: a video
  needing no downscale is scrubbed by a lossless stream copy (a rewrite, not a transcode — no
  quality cost), and `_reencode` clears the tags when downscaling. Only the location tags are
  cleared, never the whole metadata block, mirroring the photo path's "drop the GPS IFD, keep the
  rest of the EXIF". The size-regression guard gained the same exemption the photo path has, so a
  scrub whose output is fractionally larger isn't discarded in favour of the still-tagged original.

- **GPS coordinates survived the "don't keep my location" setting, for TIFF and AVIF photos**
  (`services/media/images.py`). Turning off `track_pin_visits` sets `strip_location`, which makes
  `_process_photo_upload` skip `Image.latitude`/`longitude`, pop `GPSInfo` from the `exif_data`
  snapshot, and ask `downscale_stored_image` to scrub the **file** — the copy actually served.
  Two accepted formats ignored that request, each for a different reason:

  - **TIFF**: the strip was gated on `img.info["exif"]`, but TIFF carries EXIF in its own native
    IFD and leaves that key unset, so a GPS-tagged TIFF was never even examined. Now driven off
    `img.getexif()`.
  - **AVIF**: returned early as "not a processable format" *before* reaching the strip. The
    downscaler's format set and the set of formats a GPS strip can be honoured for are now
    separate (`_EXIF_REWRITABLE_FORMATS`), so "we would never resize an AVIF" can't silently mean
    "we never scrub an AVIF's coordinates either".

  Demonstrated end-to-end before and after, per format, by authoring files that genuinely carry a
  GPS IFD and reading the coordinates back off the bytes on disk. Two false starts on the way —
  the first fixture set encoded no GPS at all (caught by an explicit validity check rather than
  being reported as "no leak"), and a `tag_v2` deletion written against a wrong theory of the TIFF
  failure was removed once measurement showed it changed nothing.

  HEIC/HEIF has the same gap and is *not* fixed: Pillow cannot open it without `pillow-heif`, so
  the file is stored untouched. Both remedies are product decisions rather than bug fixes, so it
  is written up in `PROBLEMS.md` with the trade-offs rather than decided here.

**Pin cover photo leaked other users' hidden photos.** `PinCoverPhotoView` resolved the candidate
image with a bare `get_object_or_404(Image, pk=...)` and accepted it when
`image.location_id == pin.location_id`. Every pin upload stamps `Image.location`, so any two users
who pin the same place share a location id — user A could mount user B's
`photo_upload_visibility=NO_ONE` upload as their own hero banner, and the response returned the
file URL. The wiki twin already filtered through `visible_to`; the pin side never had.
Regression test: `tests/hypothesis/test_pin_cover_photo_privacy.py`.

- **Per-viewer media responses carried no cache directives at all**
  (`controllers/media_auth.py` + six byte-serving views). Every media endpoint —
  the authenticated `/media/` gate, the Google Maps photo proxy, media previews, and the
  Immich / Google Photos / pin-suggestion thumbnails — authorizes its bytes *per viewer*: the
  same URL legitimately returns an image for one profile and a 404 for another. None of them set
  `Cache-Control`. Verified by probing the live view in both serving modes: the only cache-relevant
  header emitted is `Vary: Cookie`.

  `Vary: Cookie` is the right signal but not a sufficient one — shared caches commonly honour only
  `Vary: Accept-Encoding` and otherwise key on the URL alone. These URLs end in real image
  extensions, which is precisely what extension-based CDN cache rules match, and with no
  `Cache-Control` such a cache falls back to its own default TTL on one user's private photo. The
  deployments described in `CLAUDE.local.md` sit behind exactly that kind of CDN.

  Added `media_auth.mark_private_media()` — one helper next to the existing shared media-auth rule,
  rather than six copies of a header string — emitting `private, max-age=300`: `private` forbids
  shared storage, the `max-age` keeps the requester's own (per-user, therefore safe) browser cache
  so gallery scrolling doesn't refetch every thumbnail. Applied to all ten byte-serving returns,
  including the X-Accel branch, which is the one that matters in production since nginx copies
  upstream headers onto the file it streams.

  Note this is defence-in-depth, not a demonstrated leak: the authorization logic itself is sound
  (auth before path resolution, traversal-checked, uniform 404 with no existence oracle), and
  whether any given edge actually caches these responses depends on CDN configuration this repo
  does not contain. The directive is correct regardless of what any particular cache does.

- **E2EE enrolment accepted arbitrarily weak Argon2 parameters** (`controllers/e2ee.py`). The
  endpoint read `kdf_opslimit`/`kdf_memlimit` from the request and checked only that they were
  positive, then stored them on the bundle. Those two numbers are the entire cost of
  brute-forcing `password_wrapped_secret` — the user's private key wrapped under their password —
  and the design's stated claim (in `e2ee-crypto.ts`'s own docstring) is that the server must not
  be able to compute that wrapping key. `opslimit=1, memlimit=1` was accepted. Now floored at the
  pinned defaults `(2, 64 MiB)`, still accepting stronger values so a future client can raise them
  without a server change. Zero compatibility risk: the server default has never been anything
  else (single migration) and the real client sends exactly those constants.

  Verified with teeth: reverting the floor to the old `> 0` check fails exactly the two
  weak-parameter tests. The first version of that test was itself wrong — it guessed a URL
  namespace and a payload shape rather than reusing the existing E2EE suite's helpers, and failed
  with `NoReverseMatch`. Rewritten against those helpers, plus a case I had originally omitted:
  that the floor still *accepts* what the real client sends, which is the obvious way a change like
  this breaks production.

  The matching client-side half — the re-wrap path deriving its wrapping key from *server-reported*
  parameters — is **not** fixed. It needs `/rewrap` to accept and update the parameters so the
  client can insist on its own, and it is a write path where a mistake permanently locks a user out
  of their key. Filed in `PROBLEMS.md` with the reason clamping the unwrap paths would be the wrong
  fix.

- **A divergent copy of the SSRF guard let CGNAT addresses through**
  (`services/notifications/push.py`). A UnifiedPush endpoint is a user-supplied URL the server
  later POSTs notification payloads to, so whatever a user can register becomes a server-side
  request primitive. Registration *was* validated — scheme, no embedded credentials, DNS
  resolution, and a private/loopback/link-local/reserved/multicast check — but that check was an
  inline **copy** of the one in `services.security.url_safety`, and it had drifted: it lacked the
  RFC 6598 CGNAT range (`100.64.0.0/10`). The shared helper blocks that range explicitly, with a
  comment recording that Python's `ipaddress` does not classify it as private and that cloud
  providers route internal-only infrastructure through it — i.e. the shared copy was fixed for this
  exact gap and the duplicate never was. Now calls `is_blocked_address`, so the two cannot diverge
  again. `push.py`'s own extra check (rejecting `user:pass@` credentials) was kept — it is stronger
  than the shared helper there.

  Checked for other copies at the same time: this was the only one. Every other user-directed fetch
  (`media_preview`, `map_overlays`, `media_materialize`, `link_extraction`, the Immich form,
  `pin_suggestions`) already routes through `ensure_public_http_url`.

### Safety-critical

**A resolved check-in could be dragged back into escalation.** The beat sweeps read their rows up
front, then spend real time per row (one rendered email per contact). `send_checkin_reminder`
ended with an unconditional `status = AWAITING_CHECKIN` write — and
`SafetyCheckin.objects.overdue()` selects on exactly that status. An owner who checked in while
the sweep was mid-send was pushed back to `AWAITING_CHECKIN`, so the next tick called **every
emergency contact** for someone already safe (and, with `notify_community_wiki`, posted publicly).
`escalate_checkin` had the same unconditional `OVERDUE` write and posted to the wiki before any
status check.

Both now guard on the stored row (`_is_resolved_in_db`) and finish with conditional UPDATEs;
`escalate_checkin` re-checks per contact so a late resolution spares the ones not yet reached.
`_resolve_as_found_safe` and `archive_checkin` were already correct — these two transitions had
simply been missed. Tests: `tests/hypothesis/test_safety_resolution_races.py`.

**Backup codes could be consumed twice.** `verify_and_consume_backup_code` selected unused codes,
matched in Python, then wrote `used_at` unconditionally — so two racing submissions of one
intercepted code both succeeded. `verify_totp_code`, 50 lines later in the same file, already
claims its step with a conditional UPDATE and its comment names the exact threat ("a phishing
proxy replaying it against a parallel session"). Fixed to the same compare-and-set idiom.
Test: `tests/hypothesis/test_backup_code_single_use.py` (simulates the interleaving
deterministically rather than with threads).

**An undecryptable Gotify token took the whole site down, including its error page.**
`EncryptedTextField` splits its fields in two: credentials fail loud "because their callers
already catch `InvalidToken` and drop the row so the user simply reconnects", content sets
`fail_soft=True`. `SiteSettings.notify_gotify_token` was in neither camp — shaped like a
credential, but with **no caller anywhere** that catches `InvalidToken`, and `SiteSettings` is a
singleton three context processors load on *every* render including for anonymous visitors. So a
key change without `rotate_field_encryption` (precisely what that command exists to prevent) made
every page raise — **and the styled 500 page too**, since it runs the same context processors.

Verified by writing undecryptable ciphertext straight into the column: direct read raised, both
probed pages raised, and `handler500` failed to render. Fixed with `fail_soft=True`
(+ migration `0040`): the token is unusable either way, so Gotify pushes stop regardless — the
only question is whether the site stays up while an admin re-enters it, and the read is still
logged loudly with the field name and the setting to check. Every other credential field was
checked and *does* have an `InvalidToken`-catching caller (`two_factor`, `flickr`,
`google_photos`, `immich`, `calendar_sync`). Test:
`tests/hypothesis/test_site_settings_encrypted_degradation.py`.

- **A removed emergency contact kept receiving a safety check-in's chat**
  (`consumers.py`, `services/visits/safety.py`). `SafetyCheckinChatConsumer` resolves authority
  once, at `connect()`. For partners that has always been paired with a revocation path — an
  immediate `partner_access_revoked` broadcast plus periodic re-validation as a backstop. The
  token-authenticated contact route had **neither**, on the stated reasoning that a magic-link
  token "is either valid or it isn't", and `partner_access_revoked` explicitly returns early for
  contact connections.

  That reasoning is wrong: a contact token is revoked by *deleting the row*, and
  `set_checkin_contacts` deletes every contact missing from a resubmitted list — i.e. removing
  someone from a check-in's contact list is an ordinary, user-facing edit. Reproduced before
  fixing: with the contact's portal open, the owner clears the contact list, and the contact's
  socket still receives the next chat message. Meanwhile the HTTP fallback serving the same data
  refuses them correctly, because it re-resolves the token per request — so the two transports
  disagreed about who may read a safety check-in's chat.

  One characteristic of the fix, found by re-reading it later and worth stating: like the seven
  existing `send_group_message` calls in that module, the revocation broadcast fires *inside* the
  enclosing `transaction.atomic()` rather than from `transaction.on_commit`. If that transaction
  rolled back, a contact who still exists would be disconnected once. It is self-healing — their
  token is still valid, so a reconnect succeeds and the periodic re-check added alongside confirms
  them — and diverging from the module's convention for one of eight call sites would be worse than
  the nuance. Noted rather than changed; making all eight commit-safe is a coherent follow-up.

  Fixed by mirroring the partner mechanism exactly: `_broadcast_contact_access_revoked` fired for
  each removed row, a `contact_access_revoked` handler that closes only the matching connection,
  and the periodic re-check extended to the contact route (where "still authorized" means the row
  still exists on this check-in). The comments asserting the old reasoning were corrected rather
  than left to mislead the next reader.

- **Imported track dwells were recorded as live-device geolocation visits**
  (`services/import_formats/gpx_tracks.py`). `detect_dwells_and_create_visits` stamped its
  `PinVisit` rows `source=GEOLOCATION`, which the enum documents as "added when the user's device
  provided a geolocation" and which `record_geolocation_visits` writes under the `track_geolocation`
  setting. These rows come from a track file the user *uploaded*, reached through route import and
  gated by `track_routes`. So the same `VisitSource` had two producers under two different
  settings, and the one whose name matched the recorded source had no say over it. They now use
  `HISTORY` ("Imported") — the enum's documented value for exactly this, and what the sibling
  Google Takeout importer already writes. Fixes the UI label too: these showed as "Geolocation"
  for visits derived from a file.

- **"Friend request accepted" notifications named no actor on one of three paths**
  (`services/social/friendship.py`). `accept_friend_request` created the `FRIEND_ACCEPTED`
  `NotificationLog` without `source_profile`, while the other two paths set it. The external API's
  `NotificationSerializer` exposes that field, so a mobile client had no profile to link back to -
  even though the message text and the url on the *same row* both named that profile, which makes
  the omission a contradiction rather than merely a gap. Carried over from an older extraction that
  was deliberately behaviour-preserving, and filed as open since 2026-07-26 pending a test across
  all three paths; both now done.

- **A settable WhatsApp/SMS toggle that could never fire** (`services/notifications/`). Two
  defects stacked. `_enabled_channels` derived the preference column from the notification's *type
  value*, which agrees with the column stem for 31 of 32 types — but
  `SAFETY_CHECKIN_PARTNER_INVITE` has the value `safety_ci_partner_invite` against columns named
  `safety_checkin_partner_invite*`, so `getattr`'s `False` default reported it as a deliberate
  opt-out. And `TEXT_ALERTABLE_TYPES` omitted the type entirely, so that lookup was never even
  reached. Either alone made the toggle a lie; together they made it silent.

  Open since 2026-07-26 on the assumption that the fix required "a rename plus a migration". It
  didn't: every other consumer reads these preferences by the enum *member name*, so the lookup now
  does too — measured as 12 types resolvable by value versus 13 by member name, exactly one
  difference. The set's own docstring defines membership as "types with a toggle pair" (MESSAGE
  excepted), and 13 stems have a pair against 11 listed, so the omission was an oversight rather
  than a delivery decision. The stem/value mismatch itself is untouched, so external API field
  names are unchanged.

- **One malformed KML aborted the entire import** (`services/apis/locations/google/maps.py`). The
  bulk importer has a per-file handler precisely so a bad file is logged and skipped while the rest
  of the upload proceeds. Its tuple listed `XMLParseError` — which is
  `defusedxml.ElementTree.ParseError` — and **neither** error a malformed KML actually raises is
  that type: fastkml's `KMLParseError` descends from `FastKMLError`, and lxml's `XMLSyntaxError`
  from `SyntaxError`. Both escaped the KML parser's own handler *and* the caller's, so a file with
  unparseable coordinates or a truncated tag took down the whole import stream — losing every
  other file in the same upload, not just the broken one. Both failure modes were reproduced from
  ordinary bad input before fixing.

  The two handlers needed the same list and had already drifted, so the fix is a single named
  `IMPORT_PARSE_ERRORS` constant used by both rather than a second inline tuple. The tests import
  *that constant* rather than restating it — the first version restated the list and so passed
  even with the production fix reverted, which is the failure mode that makes a regression test
  worthless.

- **The same bomb gap on a request path** (`controllers/labels.py`). Sweeping every Pillow
  `open()` call site for the hierarchy gap above found three more. Two mattered:
  `_resize_custom_icon`, whose docstring promises to return the original "if already small enough
  **or unreadable**" and catches `(OSError, ValueError)` to deliver that — but `Image.open()`
  raises the bomb error on the *header*, before any decode, so an oversized custom label icon
  500'd the request instead of falling back; and the keyword-downscale helper, where it aborted
  keywording rather than skipping one image. Both now catch it.

  The third site — the metadata keyword plugin — was deliberately **left alone**: its runner
  already wraps `provider.generate()` in `except Exception` per provider, so a bomb there is
  already contained and a second handler would be redundant. Checked before patching rather than
  after.

- **A decompression bomb crashed the photo-processing task** (`tasks.py`). Pillow refuses to
  decode above `MAX_IMAGE_PIXELS` (89 MP), which is what prevents the memory exhaustion — but it
  signals that with `DecompressionBombError`, which inherits straight from `Exception`, **not**
  from `OSError` the way the rest of Pillow's failures do (`UnidentifiedImageError` does). The
  downscale was wrapped in `except (OSError, ValueError)`, so a bomb escaped and failed the whole
  Celery task: the upload stayed stored but entirely unprocessed — no checksum, no EXIF, no
  downscale — and it surfaced as an unhandled task exception rather than the logged warning every
  other unprocessable image gets. Now caught alongside the others. Not a vulnerability (Pillow's
  ceiling already stops the DoS), a failure-mode fix.

- **Downscaling a PNG rotated it ninety degrees** (`services/media/images.py`). Nothing in this
  pipeline calls `ImageOps.exif_transpose`, which is fine *provided* the EXIF `Orientation` tag
  survives the re-encode — browsers rotate from it. The save path passed `exif` through for
  JPEG/WEBP/TIFF/AVIF but **not PNG**, so a PNG carrying an orientation kept its unrotated pixels
  and lost the tag explaining them. Every later view of that photo renders it wrong, permanently,
  with nothing logged. PNG is now in the set; Pillow writes an eXIf chunk and the tag round-trips.

  Measured per format rather than assumed, which mattered: TIFF *also* comes back without the tag,
  but its pixels arrive pre-rotated (a 2400x1200 source returns 400x800), so it is already correct
  and a test demanding the tag would have failed on a good result. The tests therefore assert
  "displays upright — tag kept **or** pixels rotated" rather than one mechanism. They also cover
  the GPS-strip path, since that rebuilds the EXIF block from `getexif()` and is a second place
  unrelated tags could be dropped.

- **The wiki article conflict check was a TOCTOU** (`services/wiki/articles.py`). Community
  articles already have optimistic concurrency — the editor posts the `base_revision_id` it started
  from, a mismatch raises `ArticleConflictError`, and the client gets a 409 that keeps the user's
  text. But `save_article_checked` read the latest revision id and then wrote, with no lock and no
  transaction around the pair. Two editors who both loaded revision R both read `latest_id == R`,
  both passed, and both appended — so one editor's save silently stopped being the current article,
  which is the exact outcome the check exists to prevent, and they were told it succeeded.

  Nothing else caught it: `ArticleRevision` has no revision number and no unique constraint, and
  "latest" is just `-created`. The read-check-write now runs in a transaction with the article row
  locked. A *first* save needs no lock — `Article.pin`/`.wiki` are `OneToOneField`, so the database
  settles that race — and the fix is scoped accordingly. Severity is bounded by history being
  append-only: the losing editor's text survives as a revision, so this was a silent supersede
  rather than data loss.

### Correctness

**API cost reporting excluded every AI service.** Two compounding bugs. `services/ai/vision.py`
computed a per-call cost from real token usage, logged it to the *application* log, and never
passed it to `log_api_call` — so `ApiCallLog.cost_estimate` was NULL for the priciest call type in
the app. And `api_spend_summary_30d` decided what to include by asking whether a service had a
flat `ServiceDefaults.cost_per_call`: **of 46 registered services exactly one does**
(`google_geocoding`). Every AI service prices per call and declares no flat rate, so all of them
were counted "unpriced" and their spend was dropped from the totals shown on the site-admin cost
page *and the public running-costs page*. Now keyed on whether a service recorded any cost.
Tests: `test_vision_cost_logging.py`, `test_api_spend_summary.py`.

**A dead route returned 500 for everyone.** `/map/pin/<slug>/google/` was wired to
`PinController.as_view({"get": "get_google_images"})`; that method does not exist, and DRF resolves
handler names at request time, so it raised `AttributeError` for any caller. Nothing reversed the
URL name and the live Google Images feature goes through the plugin system. Removed.

**Customized labels disagreed between map and sidebar.** The map payload prefetches label
customizations; the pin-list sidebar didn't. `Label._get_customization()` reads a prefetch
attribute rather than querying, so a missing prefetch silently reads as "no customization" — the
same pin showed the user's own label on the map and the global one in the list. The sidebar
template now reads `effective_name`/`effective_color`, so the customization covers the label's
name and colour, not only its icon.

**Fact confidence could crash on a weight tie.** `services/facts/confidence.py::_cluster_categorical`
builds `(weight, value)` tuples and sorts them with a bare `reverse=True`. Tuple ordering falls
through to the second element on a tie, so two equally-weighted clusters were ranked by comparing
their **values** — unintended, and a `TypeError` when the values aren't mutually comparable.

Reachable, not theoretical: `FactEvidence` stores `data_type` per row precisely so "old rows stay
interpretable" after a fact key's registered type changes (the model says so), meaning one fact can
hold a text row and a bool row simultaneously; and every `value_*` column is nullable. Either
combination raises inside `recompute_fact_confidence` — a Celery task, so the fact just silently
stops being recomputed. Reproduced both variants (`None` vs `str`, `str` vs `bool`), then fixed by
sorting on the weight alone. Test: `tests/hypothesis/test_fact_confidence_tie_break.py`.

- **Nearest-building ranking compared degrees, not ground distance**
  (`plugins/builtin/redata_building_attributes.py`). `_nearest_building` ranked a parcel's
  buildings with `hypot()` over raw lat/lng deltas. A degree of longitude is only `cos(latitude)`
  as long as a degree of latitude, so east-west separation was over-weighted and the ranking
  inverted for any two buildings whose true distances differ by less than that factor — 1.36x at
  this app's latitudes. Concretely, at Albany a building 25 m north beat one 20 m east. The
  function's whole purpose is the multi-building parcel (it is what makes a detail pin resolve to
  *its own* building), and the winner's name is contributed as a `NameProvider` candidate that
  `default_name_resolver` gives **outright priority** when naming that pin's location — so a
  wrong pick silently mislabels the user's location, not just one panel. Now ranks via
  `site_scope.meters_between`, the shared equirectangular helper `pin_wiki_sync` already uses for
  the same parcel-scale comparison. The existing tests missed it because their "far" building is a
  third of a degree away, where no correction changes the answer; the new case is two buildings at
  comparable distance on different bearings.

- **Auto-discovered links could permanently break a pin's enrichment**
  (`services/locations/external_links.py`). `add_pin_link`/`add_wiki_link` used
  `get_or_create` on `(pin, url)` / `(wiki, url)`, which carry no unique constraint, and they run
  from a `LocationCache` post-save signal — fired by panel fetches, which have their own Celery
  queue at concurrency 20. Two panels surfacing the same URL for one pin both miss and both
  insert; from then on `get_or_create` raises `MultipleObjectsReturned` on *every* later call for
  that URL, inside a signal handler, on a task queue. Now check-then-create, so a duplicate stays a
  harmless extra row rather than a permanent exception. The race itself needs a unique constraint,
  which is entangled with the user-facing "add a link" flow and is filed in `PROBLEMS.md` with the
  four other unconstrained `get_or_create` lookups.

- **The nightly achievement sweep starved a fixed set of users, silently**
  (`services/achievements/evaluate.py`). Measured earlier in this audit at ~30 queries per profile
  with the whole sweep as one task under a hard 3600s limit, so past a certain user count each run
  is killed part-way. `Profile.objects.iterator()` has a stable order, so it truncated at the
  *same* place every night — the same tail of users permanently stopped earning the awards that
  only this safety net catches, and nothing reported it because the task simply died.

  The two fixes originally filed (batch the metrics; split the task) both need a maintainer's call,
  so neither was attempted. A third, smaller change removes the part that harms users: the sweep
  now checkpoints to the cache every 500 profiles, resumes from the cursor, and resets it on
  reaching the end — so whatever a resumed run skips is covered by the next one and no profile is
  starved indefinitely. A resumed run logs a warning, which is what makes the truncation visible.
  This does **not** make the sweep cheaper, and `PROBLEMS.md` still carries the batching fix as the
  real answer.

- **A forecast slot with a UTC offset could 500 the trip page** (`controllers/trip.py`).
  `ForecastSlot.date` is parsed with `datetime.fromisoformat`, which passes an offset through
  unchanged, and the three weather providers do not agree on a format — REData's is whatever its
  API emits. Subtracting an aware slot from the naive target raises
  `TypeError: can't subtract offset-naive and offset-aware datetimes`. Both sides are now forced
  naive before comparing.

  Scoped deliberately: this fixes the **crash**, not the timezone bug filed alongside it. Slots are
  still compared in whatever wall clock each provider used, and on the Open-Meteo path — the
  unconditional fallback, requested with `timezone=auto` — that is local time for the pin against a
  UTC target. Re-checked while here whether a complete fix was available without a product call: it
  is not, since the app has no timezone-resolution library and no per-location timezone field. The
  tests therefore assert no-crash and pointedly do **not** assert the right slot is picked on that
  path, because it isn't.

- **Device-scan ingestion resolved expected markers one query per device**
  (`services/device_scan/ingestion.py`). The upload endpoint accepts up to 200 devices and persists
  them **synchronously** inside the request, so a per-device `WikiDeviceMarker` lookup was up to 200
  extra round-trips on top of the per-device writes. Measured before changing anything: ~5 queries
  per device, linear; batching the marker resolution took a 30-device upload from 123 to 94
  queries. Now one `uuid__in` query and a dict lookup.

  Worth noting what the surrounding code already gets right, since that shaped how far to go:
  readings are `bulk_create`d per device, both list sizes are bounded (200 devices x 500 readings),
  coordinates are range-validated in the serializer, MACs are normalised through a dedicated
  validator, and the heavy classification work is deferred to Celery via `transaction.on_commit`.
  What remains is ~4 queries per device from the `get_or_create` per MAC and the per-device entry
  insert — reducible only by restructuring the write, which is a bigger change than this warranted.

### Performance

Measured with a query-count probe at two data sizes; all now flat.

| endpoint | before | after | cause |
|---|---|---|---|
| `map.pins.list` | 29 → 53 | flat 23 | `Pin.icon_source_label()` used `labels.exclude(...)`, which builds a fresh queryset and **ignores `prefetch_related`**; plus missing `location__wiki` for `Location.display_name` |
| `GET /photos/` (API) | 19 → 67 | flat 8 | per-photo `VisitSuggestion.exists()` in `classify_photo`; `Profile.username` → `auth_user`; `pin_name` → `Location.display_name` → wiki |
| `trips.overview` | 33 → 87 | flat 16 | four call sites hand-built querysets and missed the `_eff_start`/`_eff_end` annotations `for_list_page` already had; plus identity-masking a list that never reaches the template |
| `trips.calendar` | 14 → 38 | flat 8 | same missing annotations |

Extracted `TripQuerySet.with_effective_dates()` so the annotation has one definition instead of a
pattern each caller must remember. Also reordered three `Image.objects.visible_to(...)` calls to
narrow *first* — that method is eager, and one of them was on the path that serves every media
file. Regression test: `tests/hypothesis/test_query_scaling.py` (asserts the *slope*, not an
exact count).

### Build / tooling

**`bun` was pinned as a dependency, so every script ran on a two-year-old Bun.** `package.json`
declared `"bun": "^1.0.15"` under **dependencies**, so `bun install` put a project-local **1.1.6**
in `node_modules/.bin` — and `bun run` prepends that directory to `PATH`. Every `bun run <script>`
therefore executed on 1.1.6 rather than the 1.3.14 the developer *and the Docker image* actually
have. `bun` is never imported as a module; the dependency did nothing but shadow the real runtime.

This was the single cause behind **two** separately-filed bugs, both of which had been diagnosed
wrongly (once by this audit):

- `bun run build` failing with "Formats besides 'esm' are not implemented" — 1.3.14 implements
  `--format iife` fine; only 1.1.6 rejects it.
- `bun run test:ts` failing 1–2 tests inside happy-dom's event dispatch — reproduces on 1.1.6,
  not on 1.3.14.

Fixed with `bun remove bun`: **`bun run test:ts` goes to 383 pass / 0 fail (three consecutive
runs)** and `bun run build` succeeds using Bun's own IIFE output. An earlier workaround in this
same audit (hand-wrapping the classic bundles because `iife` "wasn't implemented") was reverted as
unnecessary — verified afterwards that all four bundles build, `node --check` parses each as a
classic script, and every `window.*` global survives.

Worth internalising: `bun run <script>` and the same command typed at a shell can execute
*different binaries*. Ten runs were needed to establish that cleanly — 5/5 failing via `bun run`,
0/5 via the identical command direct.

**`--reuse-db` silently poisons the test database.** The `urbanlens-mobile` OAuth `Application` is
created by a data migration. Django only guarantees migration data for `TestCase`; a
`TransactionTestCase` truncates every table and this suite has ~31 of them. So the first run
including one destroys the row and, with `--reuse-db`, it never returns — every later run fails
with `Application.DoesNotExist`, which reads like a product bug. Added
`core/tests/oauth.py::first_party_application()` (get_or_create) and routed the six affected test
modules through it: on an already-poisoned database that took the same selection from
**98 failed / 3 passed → 1 failed / 189 passed**. See `docs/PROBLEMS.md` for the residual
(`test_oauth_client_provisioning` legitimately asserts the migration's own output and still needs
a fresh DB).

---

## 2. New permanent guards

Added because each covers a failure mode that per-case tests structurally cannot: the case nobody
wrote a test for.

- **`test_cross_user_route_access.py`** — walks every owner-scoped route (167 of them) and asserts
  a logged-in stranger gets nothing, for GET, for POST/DELETE, for nested child ids smuggled
  through the caller's own parent, and for anonymous visitors. Also catches routes wired to a
  handler that doesn't exist, which is how the dead `google_images` route was found.
- **`test_query_scaling.py`** — four endpoints rendered at two data sizes; fails if query count
  grows with row count.
- **`test_undo_round_trip.py`** — serialize → delete → restore for every registered undo handler,
  plus an assertion that **every registered handler has coverage here**, so a ninth handler fails
  until someone adds it.
- **`test_share_provenance_conformance.py`** — AST check that any module creating a `PinShare`
  also calls `record_share_exposure`, enforcing a rule `CLAUDE.md` states but nothing checked.
- **`test_map_pin_payload_contract.py`** — pins the map pin payload's key set to
  `PIN_CACHE_VERSION`. The existing `pin-cache.contract.test.ts` guards that the TS reader and the
  template's inline writer agree on a version; it cannot catch a *shape* change that leaves both
  at the same number, which is exactly the case `CLAUDE.md` warns goes "silently stale". Adding a
  payload field now fails with instructions to bump the version so cached clients refetch.
- **`test_site_settings_encrypted_degradation.py`** — an undecryptable `SiteSettings` field must
  degrade, not 500 every page (see the Gotify fix above).
- **`test_fact_confidence_tie_break.py`** — pins the tie-break bug above, then *generalises* it.
  `CLAUDE.md` asks for hypothesis property tests wherever possible, and the module already had
  them for `_decay`; but `_cluster_categorical` was only ever exercised with a **single
  `"a"`-valued row**, which is exactly why a tie-break over mixed types went unnoticed. Four
  properties now hold for any evidence list — never raises whatever the value types (weights drawn
  from a coarse set so ties are common), cluster weights sum to the reported total, clusters are
  ordered heaviest-first, and clustering neither invents nor drops evidence. Confirmed the
  properties catch the original defect by reverting the one-line fix in the container and watching
  both `TypeError`s reappear.
- **`test_video_location_strip.py`** — runs real ffmpeg end to end: authors a video carrying
  location tags, processes it, and reads the tags back with ffprobe. Covers the case the old code
  could never reach (small video, no downscale warranted) and the one it silently failed
  (downscaled but metadata copied), plus the negative case. Verified both fixes are independently
  load-bearing: disabling the strip fails two tests, removing only `_reencode`'s tag clearing
  fails one. Skips cleanly when ffmpeg isn't on PATH.
- **`test_gps_strip_by_format.py`** — runs the real `downscale_stored_image` over every format
  the pipeline can rewrite and reads the GPS back off the stored bytes. The fixture-validity test
  carries as much weight as the strip test: an authoring mistake that produced GPS-less fixtures
  would make every assertion pass while testing nothing, which is precisely what happened during
  the investigation. Verified by reverting both arms of the fix and watching the format list come
  back populated.
- **`test_api_call_log_retention.py`** — `ApiCallLog` has two readers with very different
  horizons: `check_rate_limit` counts 30 days for `calls_per_30_days`, and `monthly_cost_series`
  reconstructs a rolling 12-calendar-month spend chart from the same rows. Pruning is set by the
  longer one (400 days) and `prune_api_call_logs`' docstring already warns that the model helper's
  90-day default would "silently zero out three-quarters of that chart". This turns that warning
  into a failing test. Both failure modes are silent and opposite — the limiter would under-count
  and let a service exceed its ceiling, the chart would just render zeros — and neither raises.
  The bound is *derived*, not hardcoded: an initial crude `13 x 31` estimate falsely flagged a
  correct configuration, so it now computes the true worst-case calendar reach (365 days, leaving
  35 days of margin) and separately asserts that both readers' windows haven't moved, so a change
  to either lands here.
- **`test_beat_lock_intervals.py`** — the beat tasks guard themselves with
  `cache.add(key, timeout=TTL)` so two runs can't overlap, and the TTLs are hand-tuned to sit
  "just under" the beat interval (the comments in `tasks.py` say so explicitly). But the TTLs are
  integers in `tasks.py` and the intervals are integers in `settings/base.py`, with nothing tying
  them together — the same cross-file constant coupling that produced three findings earlier in
  this audit. The quiet direction is the dangerous one: drop the safety check-in interval to four
  minutes and the 270s lock silently *skips every other tick*, halving the rate at which overdue
  check-ins escalate, with no error and no log. Four tests: the interval invariant, that named
  entries still exist, an AST completeness arm so an eleventh lock-guarded beat task can't be
  silently uncovered, and a floor so the scan can't pass by matching nothing. Verified by
  shortening one interval below its lock (fails) and dropping one task from the map (fails).
- **`test_safety_contact_revocation.py`** — mirrors the partner revocation tests one-for-one,
  including their two delivery concerns: the broadcast is *enqueued* rather than performed (so it
  needs `broadcasts_delivered_inline`), and it is best-effort (so a separate test deletes the row
  with no broadcast at all and asserts the periodic backstop still closes the socket). A fourth
  test asserts the backstop does *not* evict a contact who is still on the list. Verified both
  mechanisms are independently load-bearing by disabling each in the container: neutering the
  handler fails 2 tests, restricting the backstop to the session route fails the third.
- **`test_private_media_cache_directives.py`** — the per-view assertions are the easy half; the
  one that earns its keep is the AST scan asserting that *every* byte-serving return in the six
  media controllers routes through `mark_private_media`. A per-view test only covers views someone
  remembered to write a test for, and the failure mode is silent — the response is correct in every
  visible way except the missing header. Verified it has teeth by unwrapping one `HttpResponse`
  return and one `FileResponse` return in the container and watching each get named with its line
  number, plus a floor assertion so the scan can't pass by matching nothing.
- **`e2ee-interop-fixture.test.ts`** — `docs/e2ee-interop-fixture.json` is generated from the web
  client's own crypto so a native client (the Flutter app's `E2eeService`) can replay each step
  and match byte-for-byte. Nothing in this repo read it back, so the coupling ran one way: change
  `KDF_OPSLIMIT`/`KDF_MEMLIMIT` — a plausible hardening change — and the committed contract
  silently stops describing what the web client does, while the native side keeps passing against
  a stale fixture and diverges from the server it must interoperate with. Verified the fixture is
  currently *valid* (both KDF derivations reproduce byte-for-byte), then added the guard; validated
  it by bumping `KDF_OPSLIMIT` and confirming the failure. The coupled party here is outside this
  repo, which is exactly why it needed a test rather than a comment.
- **`test_panel_flight_ttl_invariant.py`** — `FLIGHT_TTL_SECONDS` (150) must stay above
  `fetch_panel_source`'s hard `time_limit` (130). A hard-killed task never reaches the `finally`
  that releases its single-flight marker, so the TTL is the only thing that frees the panel. The
  two numbers are literals in different modules, each documented by a comment pointing at the
  other, with nothing checking they still agree — raise the task limit past the TTL and the marker
  expires mid-run, so every polling page enqueues a duplicate fetch against a slow provider. Fails
  silently as extra API spend, never as an error.

### Undo restore fell back to `undo:write` alone for three of eight undo types (fixed)

`external_api/views_undo.py` keeps two hand-maintained maps from undo `model_label` to the domain
scope that label requires. Both listed **five** labels; the `services.undo.handlers` registry has
**eight**. Missing: `pin_list`, `label`, `markup_map`.

The two directions fail differently, which is why this survived:

```python
domain_scope = _DOMAIN_WRITE_SCOPES_BY_MODEL_LABEL.get(entry.model_label)
required = {ApiKeyScope.UNDO_WRITE, domain_scope} if domain_scope else {ApiKeyScope.UNDO_WRITE}
```

On the **read** path an unmapped label is simply omitted from the response — invisible, harmless,
and it produces no symptom anyone would report. On the **restore** path `.get()` returns `None` and
the `if domain_scope else` branch collapses the requirement to `{UNDO_WRITE}` on its own. A
credential holding only `undo:write` could restore a deleted pin list, label, or markup map without
ever holding `lists:write` or `labels:write` — flatly contradicting the view's own docstring
("Requires `undo:write` **and** the entry's own domain write scope — restoring a delete needs the
same authority the delete itself needed").

Both maps now name all eight. `pin_list` → `LISTS_*` and `label` → `LABELS_*` are exact. There is
no markup-specific scope in `ApiKeyScope` (grepped `MARKUP`, `MAP_`, `_MAPS` — nothing), so
`markup_map` → `PINS_*`: a markup map is an annotation layer on a pin's own map, and requiring
pin-write authority is both defensible and strictly stronger than requiring nothing.

Guarded by `tests/hypothesis/test_undo_scope_coverage.py`, which asserts against the handler
registry by introspection rather than a hand-written label list — a ninth undo handler cannot
repeat this. Teeth-checked by deleting the `pin_list` write entry (2 checks fail, restored 5 pass).

This is the third instance of the same class (chunk 143's journal sources, `views_search.py`'s
`SEARCH_SECTION_SCOPES`, this): a paired registry where one side is generated by the code and the
other is typed by hand. The general lesson is that `.get()` returning `None` into a conditional is
the fail-open shape; `filter_sources_by_grants`'s "iterate the mapping" shape is the fail-closed
one, and it is the reason the other two consumers were safe by construction.

### Paginated endpoints could repeat and drop rows on a tied ordering (fixed)

`PaginatedListMixin.paginated_response` stated its own precondition — "Must have a
deterministic ordering, or pages will overlap and drop rows" — and then did nothing to
establish it. Thirty call sites hand it a queryset, each responsible for satisfying that on its
own.

Page-number pagination is `LIMIT`/`OFFSET`. Postgres may return rows with equal sort keys in any
order and is not obliged to pick the same order for page 2's query as for page 1's, so a row can
appear on both pages while another appears on neither. Nothing errors — the caller just never
sees that row. Most orderings here end in a timestamp and are fine; several end in a plain
`CharField` (`WikiOwner.name`, `PinAlias.name`, `Album.name`), which ties readily.

Rather than auditing thirty sites and trusting the thirty-first, the tie-break is appended inside
`paginated_response` via a new `stable_ordering()`. It deliberately leaves four cases alone,
because "always append pk" would be a worse bug than the one being fixed:

- **lists** — already materialised in a fixed order;
- **`distinct()` queries** — `SELECT DISTINCT` requires every `ORDER BY` term in the select list,
  so appending `pk` to a `.values()` projection is a database error, not a fix;
- **aggregate queries** — Django folds `ORDER BY` into `GROUP BY`, so a `pk` term would split
  every group into one row per row and change the numbers the endpoint reports;
- **orderings already ending in the primary key**, which are already deterministic.

Teeth-checked, and this one matters: with `stable_ordering` neutered,
`test_paging_through_sees_every_row_exactly_once` genuinely fails against Postgres over 23 rows
sharing one name. The overlap is real behaviour, not a theoretical property of the SQL standard.

**A wrong turn worth recording.** The four `property_owner` models each declare their own
`class Meta:` without inheriting the abstract base's, and the base is where `ordering` lives — the
classic Django trap. I was confident this silently dropped the ordering, which would have made
`WikiOwner`/`WikiPropertySale` completely unordered and turned a tie-break into a much larger
finding. Checking `_meta.ordering` at runtime disproved it: Django's `ModelBase` explicitly copies
`ordering` (and `get_latest_by`) from an abstract parent's Meta when the child's Meta omits them.
All four inherit correctly, and `for_location`'s docstring claim of "newest first (model default
ordering)" is accurate. The runtime check took one command; the reasoning that replaced it was
wrong.

Broader context, not acted on: **161 of the dashboard's models have an ordering with no unique
final key** (or none at all). That is only a bug where the rows are paginated or sliced, which is
why the fix went at the pagination boundary rather than into 161 `Meta` classes.

### The pin cache's field-set contract is now guarded (improvement)

`pin-cache.ts` reads a localStorage blob whose only writer is `pages/map/index.html`'s inline
script. An earlier audit added `pin-cache.contract.test.ts` to pin the *version* and *key*, after
the reader sat on v6 while the writer moved to v8 — every read returned `[]` and the features
built on it went quiet without erroring.

The version is not the whole contract. The writer runs each pin through `_slimPin`, which keeps
only the fields named in a `_CACHE_FIELDS` Set, so that Set decides what the reader can see.
Nothing checked it against what the reader consumes (`uuid`, `name`, `latitude`, `longitude`,
`icon`, `address`, `tags`). Dropping or renaming one fails exactly like the version drift did:
`readCachedPinsForSearch` skips every pin missing `name`/`latitude`/`longitude` and returns `[]`,
so instant search suggestions silently stop; `readCachedPinLocations` returning `[]` makes the
Tools-page folder scanner stop filtering locations the user already has pins for, producing
duplicate suggestions. Neither throws.

The contract test now parses `_CACHE_FIELDS` out of the template and asserts all seven are
present. Teeth-checked by deleting `'address'` from the Set. No live bug — all seven are cached
today; this closes the recurrence path for a failure this exact file already had once.

### Self-review of this changeset: two unintended edits (one fixed, one flagged)

The least-audited code in the repository is this session's own 215-file working tree, so it got the
same treatment as everything else.

**Fixed — 458 lines of line-ending noise.** `docs/notes/mobile_app_requirements.md` (341/341 lines
changed) and `docs/designs/rejected-and-deferred/split-architecture.md` (117/117) looked like whole
-file rewrites. The actual edits were 4 and 6 lines respectively — documentation cross-reference
repairs from section 4c. Rewriting those files through Python silently normalised CRLF to LF across
every line, burying ten real changes in 458 and flipping both files against the 63 tracked files
that still use CRLF. CRLF restored byte-for-byte; the content edits verified intact and the diffs
are now 4 and 6 lines. Worth noting as a method problem, not just a one-off: any whole-file
`write_text()` on a CRLF file does this, and the diff is the only place it shows up.

**Flagged, not changed — `uv.lock` lost six packages.** `django-extensions`, `esprima`, `patsy`,
`python-decouple`, `scipy`, and `statsmodels` were pruned when `uv sync` ran early in the session.
This is legitimate — none of the six is declared in `pyproject.toml` and none is imported anywhere
under `src/` or `bin/` — but it is an unrelated dependency-metadata change riding along in a
behavioural changeset. Whether it belongs in the same commit is the owner's call, which is why it
was left alone rather than reverted or quietly kept.

### Self-review, part two: a bug I introduced this session (fixed)

Continuing the self-review into the code added this session rather than only its diff hygiene.
`stable_ordering` (added in chunk 146) was the obvious first target: newest, and it now sits in the
path of all 30 paginated endpoints.

**`stable_ordering` raised `TypeError` on a sliced queryset.** Django refuses to reorder a query
once a slice has been taken, so `paginated_response(qs[:100], ...)` would have become a 500 on an
endpoint that previously worked. No current call site passes a slice - checked all 30 - so this was
latent rather than live, but the helper is now a shared boundary and the next caller to slice would
have found it in production. Combined querysets (`union`/`intersection`/`difference`) are skipped
for the same reason: their `ORDER BY` may only name columns in the combined select list, and "pk is
usually there" is not worth a 500. Both guarded, both teeth-checked.

The general point: the fix in chunk 146 removed a class of bug at a shared boundary, which is the
right shape - but moving logic to a boundary means every caller's edge cases now arrive there, and
the four exclusions I wrote initially covered the cases I had thought of rather than the cases
Django actually rejects. Enumerating what the API refuses (slices, combinators) found what
reasoning about what "should" be safe did not.

### `normalize_longitude` folded one meridian two different ways (fixed)

Same self-review pass, `services/geo/longitude.py`. The function special-cased the literal `180.0`
to preserve which side of the line a bound sat on - deliberate, and documented. But 180 and 540 are
the same meridian, and 540 fell through to the general fold and came back as `-180`. So the
documented guarantee ("a bound *on* the line keeps its side") held only for the spelling the author
happened to test.

Now decided on the sign of the input instead: any positive input landing on the antimeridian
returns `+180`, any negative one `-180`, however many times the client wrapped it. Idempotence and
range are pinned as properties, and the equivalence is pinned directly. Teeth-checked by restoring
the old special-case.

Realistically hard to trigger - Leaflet emits 181, not 541 - so this is a latent trap in a shared
primitive rather than a live bug. It is recorded because the entire reason that module exists is to
be the one place antimeridian reasoning is correct, and a primitive that contradicts its own
docstring is exactly what callers will trust and not re-check.

Also checked, no change made: `split_at_antimeridian(None)` raises `AttributeError`, and
`_pin_in_boundary` does not guard a null `smart_boundary` the way `_boundary_matching_ids` does.
Its only production caller short-circuits on `pin_list.smart_boundary` first, so the path is
unreachable; the asymmetry is a readability wart, not a defect, and churning a private helper to
even it out would be noise.

### Self-review, part three: two more defects in this session's own code (fixed)

**`_deferred_deadline_passed` died on a naive timestamp.** The two-day CID retry window parses
`started_at` with `datetime.fromisoformat` and guards the parse with `except ValueError`. A naive
timestamp parses fine and then raises `TypeError` on the subtraction against an aware
`timezone.now()` - a different exception, so the guard misses it and the Celery task dies instead of
retiring the batch. The only producer stamps `timezone.now().isoformat()` (aware, `USE_TZ=True`), so
no live path reaches it; a replayed or hand-enqueued message does.

Fixed by making a naive stamp aware rather than by widening the `except`. Swallowing it would
return "not expired", and a batch that can never expire is precisely what the deadline was added to
prevent - the failure would have been silent and permanent instead of loud and immediate. This is
the same shape as the `stable_ordering` slice bug in chunk 149: a guard covering the failure mode
the author pictured, next to an adjacent one it does not catch.

**`release_lock` cried overrun when nothing was wrong.** Releasing a key that had simply expired
with no new holder - or releasing the same token twice - logged
`Sweep lock %s outlived its TTL; leaving the current holder's lock alone`, naming a "current holder"
that does not exist. That warning is a genuine operational signal (two runs overlapped, work outran
its TTL) and it is only usable while the benign case does not also raise it. Now split: absent key
logs at debug, a *different* holder still warns.

Verified by direct probing rather than by reading: mutual exclusion holds, a wrong token cannot
steal the lock, `beat_lock` releases on an exception in the body, and a double release is a no-op.
Both fixes teeth-checked (the naive-timestamp guard unambiguously: 2 tests fail without it, pass
with it).

Not changed: `test_sweep_lock_release.py` uses `assertRaises`, which ruff's `PT027` flags. `tests/`
is excluded from the prescribed `ruff check src/urbanlens` run (which passes clean), the directory
carries 647 pre-existing findings when explicitly named, and 79 test files use `assertRaises`
against 37 using `pytest.raises`. Converting one file would be churn against the dominant
convention, not a fix.

### Location guessing silently lost confidence at high latitudes (fixed)

`import_failure_guess.py` compared candidate positions against two bounds expressed in **degrees**:
`_AGREEMENT_DEGREES = 0.15` (how close a decoded S2 cell must sit to a geocoded match to count as
corroboration) and `_MAX_HINT_DEGREES = 0.5` (how far a candidate may sit from a caller's area
hint). Latitude was compared with `abs()`, longitude with the antimeridian-safe `longitude_delta` -
both correct as *degree* arithmetic, and both wrong as *distance*.

A degree of longitude shrinks with latitude. Measured:

| latitude | 0.15 deg N-S | 0.15 deg E-W |
|---|---|---|
| 0 (equator) | 16.7 km | 16.7 km |
| 42 (Massachusetts) | 16.7 km | 12.4 km |
| 60 (Anchorage, Oslo) | 16.7 km | 8.3 km |
| 70 (Tromso) | 16.7 km | 5.7 km |

So the corroboration box tightened east-west the further north the pin was - to half the documented
16 km at 60 deg, a third at 70. A northern pin whose cell and geocoded match agreed to within 10 km
failed to corroborate purely because of latitude, dropping the suggestion from `address+area`
(confidence 0.95) to `address` (0.8), or from `name+area` (0.85) to a name match capped at 0.75.
The docstring's "~16km" was true only at the equator.

That directly undercuts the point of the feature. The request was to *combine* signals - S2 decoding
plus an OSM lookup - so that two independent agreements raise confidence; a latitude-dependent
failure to corroborate removes the combination exactly where it was asked for.

Both bounds now use `haversine_km` from `services/geo/distance` - the shared primitive consolidated
earlier in this audit - as true distances (16 km and 55 km), isotropic everywhere. Teeth-checked:
with the degree box restored, latitudes 60 and 70 lose corroboration while 0 and 42 still pass,
which is precisely the predicted signature. A companion test asserts a genuinely distant cell
(~167 km) still fails to corroborate, so the latitude test cannot pass by everything agreeing.

mypy clean at 783 files after the change.

### The guess endpoint logged an ERROR traceback per rate-limited card (fixed)

`guess_for_failure` wrapped both Nominatim lookups in `except Exception: logger.exception(...)`.
That catches `RateLimitExceededError` along with everything else - and Nominatim's usage policy caps
this project at `calls_per_minute=1` (declared in its plugin), while the import-failure queue fetches
a guess per card via `hx-trigger="revealed once"`.

So the common path was the error path. Scrolling a queue of several hundred failures - the exact
scenario the feature exists for, since an import that leaves 600 failures is what prompted it -
would emit an ERROR-level traceback per refused card, burying any genuine geocoder failure among
hundreds of expected ones. Third instance of this class in three chunks, after the lock overrun
warning and the naive-timestamp guard: an expected condition raising the signal reserved for
unexpected ones.

`RateLimitExceededError` is now caught separately and logged at debug, matching the convention
already used in `cid_resolution.py` and `external_data.py`. Behaviour is unchanged - both paths
already fell back to the S2 area guess, so a rate-limited card still shows "roughly here" when the
cell decodes.

**Correction (chunk 153).** The handler added here was unreachable. `NominatimGateway.search`
caught the rate limit itself and returned `[]`, so the exception never reached
`guess_for_failure`. The test passed only because it mocks `search` directly, which bypasses the
gateway's own catch - a green test for a path production cannot take. The real log spam was inside
the gateway, and is fixed there in chunk 153; this handler is live now that `search` propagates.

Teeth-checked, on the second attempt. The first tried to delete the handlers with a regex, which
broke the file and produced *collection errors* rather than test failures - a passing-looking red
that proves nothing about the test. Redone as a one-word mutation (`logger.debug` to
`logger.warning`), which fails exactly the intended assertion and leaves the file parsing.

Also verified on this endpoint, no change needed: `PinImportFailureGuessView` scopes its lookup with
`get_object_or_404(PinImportFailure, pk=..., profile=profile)` under `LoginRequiredMixin`, so one
user cannot request guesses for another's import failures.

Band covering chunks 149-151: 759 passed.

### Nominatim flattened "we did not ask" into "there is nothing" (fixed)

Sweeping the class behind three of the previous findings - an expected condition raising the signal
reserved for unexpected ones - across the whole codebase rather than just this session's code.

109 broad `except` + `logger.exception` blocks exist; most are legitimate catch-alls (channel-layer
cleanup, plugin isolation). The ones that matter are those wrapping a call through
`_RateLimitedSession`, which `Gateway` installs as `self.session` on every subclass declaring a
service key, and which raises `RateLimitExceededError`. Two real instances:

**`NominatimGateway.search`/`lookup`** caught the rate limit alongside genuine transport failures,
logged a traceback, and returned `[]`. Two consequences. The logging one: Nominatim is capped at one
call a minute app-wide, so every refused call produced an ERROR-level traceback. The worse one: a
caller could not tell "Nominatim knows of no such place" from "we never asked" - the exact
distinction `reverse_geocode_admin` on the same class documents itself as *deliberately* preserving,
because `geo_bonus` gives the two cases very different cache TTLs. Two methods on one gateway took
opposite positions on the same question. `search`/`lookup` now propagate the rate limit and still
flatten genuine failures to `[]`.

**`geo_bonus._reverse_geocode_admin_cached`** wrapped the (correctly propagating)
`reverse_geocode_admin` in `except Exception: logger.exception(...)`. The file's own comments name
the rate limit as expected - "any multiplayer round with more than one guess/minute would have every
guess but the first silently lose its bonus to a `RateLimitExceededError`" - and then logged a
traceback for exactly that. Split: rate limit at debug, genuine failure still `logger.exception`.
Both cache with the short error TTL, so behaviour is unchanged.

**This also invalidated a fix from the previous chunk** - see the correction above. Worth stating
plainly: the chunk-152 test passed, its teeth-check failed correctly when mutated, and the code was
still dead, because the mock replaced the very layer that made it unreachable. A test that patches
the collaborator it is reasoning about proves the handler runs *when reached*, never that it can be.
The replacement patches the session instead, so the exception travels the real path, and it fails
when `search` is reverted to swallowing.

Two near-misses on method, both from truncating a search: `grep "raise RateLimitExceededError"`
returned nothing because the raise site assigns `to_raise = RateLimitExceededError(service)` first -
briefly suggesting the exception was never raised at all. And the first grep for `_RateLimitedSession`
users searched only `services/apis/`, missing `services/core/gateway.py`, where it is actually wired.
Both were caught by widening rather than concluding from absence.

### A rate-limited imagery panel was remembered as empty for 12 hours (fixed)

Following the previous chunk's class one layer further out. The question that found this: does any
caller *cache* a flattened-empty result, so that a transient refusal becomes durable wrong data?

`SlidesPanelSource.fetch` warmed every imagery provider and then set a "ready" marker for
`SLIDES_READY_TTL_SECONDS` - twelve hours - unconditionally. Two things combined to make that wrong:

1. The collectors (`collect_satellite_slides`, `collect_street_view_slides`) caught
   `RequestCancelledError`, the *base class* of `RateLimitExceededError`, logged it at debug, and
   appended **no** `ProviderFetchResult` at all. A rate-limited provider therefore registered as
   neither a success nor a failure.
2. `fetch` called `self.collect(lat, lng)` and discarded the return value entirely - including the
   `ok` flag that exists precisely to report per-provider outcomes.

So a provider refused by its own rate limiter left the panel marked warm and empty for twelve
hours, indistinguishable to every reader from "this location genuinely has no imagery". The panel
task's own handler already distinguishes `RateLimitExceededError` correctly (`external_data.py:1274`)
- it just never saw one, because the collector below had swallowed it.

Now: a rate-limited provider is recorded with `ok=False`, and `fetch` stamps the full window only
when every provider actually answered, falling back to `FAILURE_SKIP_TTL_SECONDS` (5 minutes, the
cadence already used for panel failures) otherwise. A genuinely empty result still gets the full
twelve hours, so locations without imagery are not re-queried every few minutes.

`ServiceDisabledError` deliberately keeps the long window. An admin turning a provider off is a
stable state, not a transient one, and re-warming every five minutes because of it would be worse
than the bug. This mirrors `geo_bonus`, which gives a real "nothing found" 30 days and a failed
lookup 60 seconds for the same reason.

Teeth-checked: reverting `fetch` to the unconditional marker fails exactly the three tests that
assert the short window, and none of the others.

Method note: the first version of these tests asserted on `cache.ttl(key)`, which does not exist on
`LocMemCache` - the test backend - so all five TTL assertions errored rather than failed. Rewritten
to observe the `cache.set` call's timeout argument, which is what actually changed, and which works
on any backend.

### The test suite had no cache isolation between tests (fixed)

The panel band run at the end of chunk 154 came back with two failures in
`test_pin_detail_ext_panel_204.py`. They pass alone. First job was establishing whether the change
just made had caused them: reverting only chunk 154's `fetch` edit and re-running the band left both
still failing, so no - pre-existing, and order-dependent.

The cause is general, not local to those tests. Django rolls the **database** back between tests and
does not roll the **cache** back, and rollback *reuses primary keys*. The panel system keys its
readiness marker on a Location pk (`ulfetch:ready:<source>:loc<id>`, via `PanelSource.scope`), so:

1. some earlier test warms `...:loc7`;
2. its transaction rolls back, freeing pk 7;
3. `test_pending_placeholder_carries_the_marker` creates "a freshly-created pin", whose Location is
   handed pk 7 again;
4. its "cold cache" assumption is false, it takes the ready branch instead of the pending branch,
   and the test fails - pointing at whichever test ran first rather than at itself.

Neither base class cleared the cache. Both `TestCase` and `SimpleTestCase` now mix in
`_CacheIsolationMixin`, which clears it in `setUp`. The band went from 452 passed + 2 failed to 454
passed.

This is worth more than the two tests it fixes. Every test that writes to the cache was leaking into
every test after it, and the resulting failures name the wrong culprit - which is exactly the kind of
flake that gets re-run until it passes rather than diagnosed. Full-suite run started to confirm
nothing depended on the leak.

Also found, not yet acted on: `GeoBoundary._geometry()` sets `_loaded = True` even when the loader
raised, so one transient failure makes that boundary report "unavailable" for the object's whole
lifetime - and its own docstring notes these are typically held as `ClassVar`s, i.e. for the process
lifetime. `contains()` then returns False ("gate closed") permanently. Same class as the panel and
Nominatim findings; deferred to the next chunk rather than bundled into a test-infrastructure change.

### One transient failure disabled a geo-gated plugin for the worker's lifetime (fixed)

`GeoBoundary` resolves its geometry lazily and memoizes the result - correct, and the reason a
boundary is safe to build at plugin-import time. But `_geometry()` set `_loaded = True` in the
`except` arm too, so a loader that *raised* was memoized exactly like one that *answered*.

Those are not the same event. A loader returning `None` has answered: this boundary does not
resolve, remember that. A loader raising has not answered at all, and will likely succeed on the
next attempt - the state-boundary loader fetches from TIGERweb through a rate-limited gateway, so a
single rate limit or network blip is enough.

The consequence follows from where boundaries live. The class docstring notes they are typically
held as plugin `ClassVar`s, so the instance lives as long as the process. `contains()` returns False
when there is no geometry ("gate closed"), and `PanelSource.applies_to` gates on exactly that
(`external_data.py:526`). So one transient TIGERweb error at first use silently disabled that
plugin's panel for every location, in that worker, until restart - days for a Celery process, with
no error after the first and nothing to indicate the gate was closed for the wrong reason.

Failures are now honoured for `_FAILED_LOAD_RETRY_SECONDS` (300s, the failure cadence panel fetches
already use) and then retried; successes and genuine `None` answers memoize permanently as before.
Retrying on every call was rejected as the fix: it would re-hit the provider on each `contains()`
and log per call, trading a silent permanent failure for a loud continuous one.

The log level also dropped from `logger.exception` to `logger.warning` with `exc_info=True`. The
traceback is still there; the severity now matches an event that self-heals in five minutes.

Teeth-checked: restoring `_loaded = True` in the except arm fails exactly the two tests asserting
recovery, and none of the sixteen others. mypy clean at 783 files.

### Verification pass over this report's own claims

The report is what another agent reads instead of redoing the work, so its checkable claims were
re-run rather than trusted. `date.today()` (9 sites), the identifier-only login lockout, and "no
Content-Security-Policy anywhere" all still hold exactly as filed; the undo-CASCADE entry in
`PROBLEMS.md` already reflects the later photo-relink fix. Counts: 8 undo handlers, 50 `@receiver`s,
30 `paginated_response` callers, 43 plugins - all still correct. `ExternalApiView` subclasses have
grown 191 to 197 with this session's work; the figure is updated above and the "zero gaps"
conclusion was independently re-checked in the undo-scope work.

The one thing worth recording is a method trap this nearly walked into. A naive AST count of
`ExternalApiView` subclasses returns **114**, against the report's 191 - which reads as a badly
stale claim. It is not: 114 counts only classes naming `ExternalApiView` as a *direct* base, while
the original resolved the hierarchy transitively. Any "the docs are wrong" finding derived from a
recount is only as good as the recount matching the original's method, and a direct-vs-transitive
mismatch produces a confident, wrong correction.

Also closed out here: the last shape of the "transient condition recorded as a settled answer"
class - in-process memoization that caches a failure. One instance, `core/version._git_fetch`
(`@lru_cache(maxsize=1)`), and it is deliberate and documented: caching the failure stops repeated
admin page loads re-running `git fetch` against unreachable remotes. Its cost is a stale flag on an
admin page, not a closed functional gate, which is exactly the distinction that made `GeoBoundary`
worth fixing and this one worth leaving. Five real instances fixed across chunks 150-156; one
correctly left alone.

### Organize bulk actions silently did nothing for categories (fixed)

Self-review of the Display Order work (feature request 3), in the frontend rather than Python.

The Organize page speaks two vocabularies for the same three things. `Label.kind` - what `data-kind`
carries in rendered markup - is `"tag" | "category" | "status"`. `OrgNamespace`, which every
per-namespace registry on `window` is keyed by, abbreviates the middle one to `"cat"`.

`organize-priority.ts` looked registries up straight from `data-kind`:

```js
const opener = kind ? window._orgBulkEditByIds[kind] : undefined;
```

Two of three values coincide, so tags and statuses worked and **categories alone** fell through to
"Bulk edit is not available for this type." The bulk-edit lookup is pre-existing; the merge and
delete lookups added for this feature faithfully reproduced it, so all three bulk actions were dead
for categories on that tab.

What made it survive review is the shape of the failure. A bug affecting the middle of three sibling
cases, surfacing as a polite toast rather than an error, reads as a backend permissions problem -
not as a string mismatch two files away. And it is invisible to typechecking: both sides are
`string`-keyed records, so `Record<string, ...>` accepts either vocabulary happily.

Fixed with an exported `ORG_NS_BY_LABEL_KIND` living beside the `OrgNamespace` type it translates
into, used by all three lookups. The regression test pins the translation, pins that *every* kind
the priority list can render has one (derived from `KIND_* = "..."` in `models/labels/meta.py`
rather than a hand-written list, so a fourth kind cannot be added without noticing), and
structurally guards against a future edit going back to a raw `data-kind` lookup - which nothing
else in the suite would catch.

Teeth-checked by reverting one lookup. Frontend suite 390 passed, `tsc --noEmit` clean.

Chosen deliberately for this chunk: the Python full-suite run validating the cache-isolation change
was still going, and TypeScript is disjoint from it, so this could not muddy which change a failure
belonged to.

### End-to-end verification of the Display Order fix, and a sweep for its class

The previous chunk fixed the `Label.kind` -> `OrgNamespace` mismatch. A translation is only useful
if something is registered under the translated key, so the chain was walked rather than assumed:

- `_priority_list.html` renders `data-kind="{{ item.kind }}"`, i.e. `tag`/`category`/`status`
  (`_NON_PRIORITY_KINDS` excludes only `user` and `media`, so all three reach that list);
- `ORG_NS_BY_LABEL_KIND` maps `category` to `cat`;
- `entries/organize.ts` constructs one `OrgTabManager` per kind, each **gated on its `*-rows`
  element existing** - which was the thing worth checking, because a lazily-loaded tab would leave
  the registry empty and the fix inert;
- `pages/organize/index.html` server-side `{% include %}`s all three label panels in one render, so
  all three register at init regardless of which tab is showing. Only the `lists`/`filters` tabs are
  HTMX-lazy, and neither is a label kind.

So the fix holds end to end. Also verified: the bulk bar's six element ids match between template
and TypeScript; templates call only the `window._orgBulk.*` dispatchers, never the per-kind
registries, so there is no second lookup site bypassing the translation.

Swept for other instances of the class. Two heuristics:

- **TS literal unions whose members do not all exist in Python.** Two hits, both false positives:
  `BrowserPermissionState`'s `"unsupported"` and `PanelName`'s `"game"` are client-side concepts
  that mirror nothing server-side.
- **A `dataset.*` value used directly as an object key** - the exact shape of the bug. The only
  remaining hit is `map-layers.ts`, which normalises and falls back to a default
  (`TILE_DEFS[kind] || TILE_DEFS[normalizeBase(kind)] || TILE_DEFS.street`), so an unknown value
  degrades instead of vanishing.

The class is contained to the one site now fixed.

### End-to-end verification of all three feature requests

Chunk 158 found that one requested feature was wired to a registry key nothing had registered under,
so the other two got the same treatment: not "do the tests pass" but "can a user reach this".

**Location guessing for unresolvable import failures** - reachable. Route
`memories.locations.import_failures.guess` exists, and `_pin_import_failure_card.html:37` requests it
per card with `hx-trigger="revealed once"`, which is the lazy per-card fetch the feature was designed
around. Total references: 3 (route, test, template).

**The two-day CID retry window** - verified by simulating the schedule rather than by re-deriving the
arithmetic. `_DEFERRED_LOOKUP_DEADLINE` is exactly 2 days; attempts run 120s, 120s, 120s, then 300s,
600s, 1800s, 1h, 2h, 4h, then 6h steady; the batch retires at attempt 16 after 49.9h. That is "give
up only after two days, backing off considerably after the first few, rescheduled infrequently".

**Display Order delete / bulk delete / merge** - fixed in chunk 158, verified in 159.

**A false finding I published mid-chunk, and the cause.** Between those two checks I stated, with the
word "confirmed", that the guess route was referenced only by tests and the feature was unreachable
from the UI. That was wrong, and I had started looking for the template to edit. The cause was
`grep ... | head -10`: the matches are returned in directory-traversal order, controllers and tests
and models come before templates, and the one line that disproved the claim was on the far side of
the cut. Re-running with no `head` returns three references, one of them the template.

This is the same truncation failure recorded twice already in this audit (chunks 105 and 142), and
the discipline stated there - never let a conclusion rest on the *absence* of matches from a
truncated search; count first, or list in full - is evidently not something to state once. The
specific trap is that `head` is harmless when confirming something exists and actively misleading
when concluding something does not, and the two look identical while typing them.

### `main` is missing a migration for a model change that is already committed

Re-examining the migration-dependency risk flagged earlier in this audit turned up something
stronger than the original concern.

Two untracked migrations sit in `dashboard/migrations/`: `0040_gotify_token_fail_soft` and
`0041_pin_import_failure_maps_url`, and 0041 depends on 0040. The original worry was the documented
`NodeNotFoundError` trap - a new migration silently depending on someone *else's* uncommitted file.
That is not what this is. Both were generated during this audit, and the actual state is worse in one
respect and simpler in another.

`0040` reflects `fail_soft=True` on `SiteSettings.notify_gotify_token`. That model change **is
already committed** - `git show HEAD:...site_settings/model.py` contains it, and the file is
unmodified in the working tree. Only its migration was never committed. Verified with Django rather
than by reading:

- with 0040 and 0041 on disk, `makemigrations --check --dry-run` reports **"No changes detected"**;
- with both moved aside, Django wants to generate a migration for *two* things: `maps_url` on
  `pinimportfailure` (this audit's, model change still uncommitted) **and** `notify_gotify_token` on
  `sitesettings` (already at HEAD).

So anyone on `main` today has a dirty `makemigrations --check` and a migration graph that does not
describe their models. The practical consequences are mild - `fail_soft` is a Python-level flag on
`EncryptedTextField`, so the `AlterField` carries no meaningful schema change - but the drift is
real, and it means **0040 is not optional**: it is a missing migration, not a speculative one.

Both files must be committed, together, and 0041 after 0040. Committing 0041 alone would give every
other checkout the `NodeNotFoundError` the original note warned about.

### 58 indexes duplicate ones Django already creates (filed, not fixed)

Auditing the migration chain against the two ordering rules `CLAUDE.md` states, then following the
one that led somewhere real.

**Rule "index creation goes dead last" - no action.** Six migrations technically violate it, all of
them large squashed ones where Django's own squash interleaves `CreateModel`/`AddField`/`AddIndex`.
They are already applied and cannot be rewritten, and the rule is guidance for authoring a chain,
not a property to retrofit onto history.

**Rule "a nullable+unique field must carry `unique=True` in the `AddField`" - one match, benign.**
`0010_v0_6_0` adds `notificationlog.uuid` as `UUIDField(null=True)` and later alters it to
`unique=True`, which is the documented anti-pattern's exact shape. Checked against the real schema
rather than assumed: `dashboard_notifications` carries exactly **one** index on `uuid`. The
duplicate-index consequence needs the `AddField` to create an index of its own, and a plain nullable
`UUIDField` creates none. Shape without consequence - worth recording so nobody "fixes" it.

**What the schema check did find: 58 exact duplicate indexes.** Every `ForeignKey` defaults to
`db_index=True`, so Django creates a single-column btree automatically; 25 model files additionally
declare an `idxdb_*` index on the same single column. Verified by comparing `pg_index.indkey` column
lists, excluding partial and unique indexes and `varchar_pattern_ops` variants - the `_like` indexes
are for `LIKE` prefix matching and are *not* redundant with a plain btree, which a naive
column-list comparison flags wrongly. Two definitions, one pair, byte-identical apart from the name.

Filed in `PROBLEMS.md` with the full list rather than fixed. It is 25 model files plus a migration
dropping 58 indexes, and this audit's tree already carries 219 changed files - a schema migration
that size buried in it makes the whole changeset harder to review and riskier to land, and index
drops against production are worth the owner timing. Each drop is individually safe and trivially
reversible: the identical twin remains, so no query plan can regress.

The reusable query is in the filing; it distinguishes exact duplicates from the ~20 additional
composite-prefix cases (an index on `(a)` beside one on `(a, b)`), which are a genuine trade-off
rather than free waste and were deliberately left out of the count.

### Query budgets for the main pages, measured rather than reasoned about

Earlier N+1 work in this audit was reactive - a page looked slow, so it got read. This measured the
four highest-traffic pages directly, at two dataset sizes, because an N+1 is invisible on a
one-object fixture: it issues one extra query, the test passes, and the same code issues four
hundred for a real account.

Measured 2026-08-13, at 4 pins and again at 24 (each pin with its own Location and two labels):

| page | 4 pins | 24 pins | delta |
|---|---|---|---|
| `map.view` | 17 | 17 | +0 |
| `organize.index` | 28 | 28 | +0 |
| `profile.view` | 15 | 15 | +0 |
| `pin.details` | 26 | 24 | -2 |

All four are flat and modest. `organize.index` is the notable one: it was **244** queries earlier in
this audit, is 28 now, and is lower than the 38 recorded after that fix - the label prefetch work in
the `LabelledModel` chunk improved it further. `pin.details` dropping by two on the larger dataset is
branch-dependent, not a scaling effect.

Kept as `test_page_query_budgets.py`, asserting the property that matters (the count does not grow
with the dataset) plus a loose absolute ceiling at roughly 1.7x measured, so a real regression trips
it while an added `select_related` does not.

**The first teeth-check failed to bite, and that is the useful part.** Reintroducing the exact
prefetch-defeating `labels.filter(kind=...)` N+1 that this audit fixed in `LabelledModel` left all
five tests passing - so the guard does not cover that accessor, because none of these four pages
renders pin labels through it. Rather than report a guard that works in general, the check was
repeated against something these pages do use: disabling `with_hierarchy`'s prefetch takes
`organize.index` from 28 flat to **238 -> 278**, and the test fails with
"238 queries at 4 pins, 278 at 9 - scales with row count".

So the scope is exactly: prefetch and join regressions on the four measured pages. Stating that is
worth more than the test, because a query-budget test nobody has tried to break is indistinguishable
from one that cannot fail.

Two fixture traps worth recording for anyone extending this: `Location` is unique on
(latitude, longitude), so a second seeding pass must advance its coordinate grid rather than restart
it; and four of the five route names guessed from page names were wrong (`map.view`, `pin.details`,
`organize.index`, `profile.view`).

### The Organize page's deferred rows endpoint was O(labels x subtree) (fixed)

Chunk 165 measured the four main *pages* and found them flat. This measured what those pages defer
their expensive work to, which is where it had gone.

`organize.index` renders label cards immediately and lets each tab re-fetch its rows via
`hx-trigger="revealed"` - the controller's own comment says the counts are "the expensive part of
that page". Measured, with 143 labels in a two-level hierarchy:

| endpoint | ~23 labels | 143 labels | delta |
|---|---|---|---|
| `label.rows` (tags) | 33 | 113 | **+80** |
| `label.rows` (categories) | 66 | 146 | **+80** |

Exactly one query per added label. `map.pins` (5, flat), `memories.journal` (17, flat),
`memories.data` (6, flat) and `organize.priority.list` (9, flat) were all fine.

The cause is two compounding per-instance costs in `Label.total_pin_count`:
`get_label_and_descendants` is a BFS issuing **one query per node visited**, then a `Count` aggregate
follows, and the memo is per-instance - so every rendered card repeats the whole walk. A user with
200 labels paid 200+ queries every time they opened a tab.

Fixed with `Label.prime_total_pin_counts(labels)`, which loads the edge list once, traverses in
Python, and seeds each instance's existing memo, so the template filter reads it without touching the
database. `_rows_ctx` materialises its queryset before priming - priming a queryset that is
re-evaluated during rendering would seed memos on discarded instances and silently restore the old
behaviour, which is worth stating in the code because the failure would look like the fix simply not
working.

Result: **113 -> 14** queries for tags, **146 -> 13** for categories, and flat in both cases. Fixed
at three queries regardless of label count.

Correctness was the risk, not speed, so the tests assert the primed value equals the *unprimed* one
rather than a hand-written total - a hand-written number would just encode whatever the fast path
does. The awkward cases are covered deliberately: multi-level chains (the walk is not
direct-children-only), diamonds where a label is reachable two ways and must not be counted twice,
and cycles, which the original BFS tolerates and any replacement must too. Unprimed labels still
compute themselves, so single-label callers need no change. mypy clean at 783 files.

### A route-wide query-scaling sweep, and two versions of it that could not have worked

Chunk 166 found an N+1 by measuring one endpoint. This generalised that into a sweep over the whole
URL conf - and the interesting part is that the first two attempts reported "everything is flat"
while being structurally incapable of seeing the bug fixed one chunk earlier.

**Attempt 1** measured only routes reversible with **no arguments**: 134 routes, all flat. But
`label.rows` takes a label kind, so the one known N+1 in the codebase was outside the sample
entirely.

**Attempt 2** added single-argument routes by reading parameter names from each **leaf** URL pattern:
45 routes, all flat. `label.rows` is registered as `path("rows/", ...)` nested under a
`<str:label_kind>` include, so its leaf pattern declares no parameters - and most parameterised
routes in this project are nested that way. Reverting the chunk-166 fix and re-running still
reported all-flat, which is what exposed it.

**Attempt 3** accumulates parameters down the resolver tree. 51 single-argument routes become
visible, `label.rows` among them, and reverting the fix lights it up: `label.rows(tag)` 33 -> 113,
`label.rows(category)` 66 -> 146. With the fix in place, all 185 measured routes (134 no-arg + 51
single-arg) are flat.

Kept as `test_route_query_scaling.py`, asserting only that counts do not grow with the dataset -
never an absolute number, so a route that legitimately costs 40 queries is not this file's business
while one costing 40 then 400 is. It carries two guard tests of its own: that it still reaches 100+
routes, and that `label.rows` is still among them. The second exists precisely because attempts 1
and 2 would both have passed a "does it find anything" check written as "it ran without error".

Two implementation notes worth keeping: each request runs inside its own savepoint, because a route
raising a database error otherwise poisons the test transaction and takes every later route with it
- turning one broken route into a sweep-wide failure that names the wrong culprit. And the sweep
seeds nested labels (each label gets a parent), since a flat label set never exercises the
descendant walk that made `label.rows` expensive in the first place.

The general lesson, which now has three instances in this audit: a search or sweep that finds
nothing is a statement about the instrument until the instrument has been shown to find something.

### The external API's 39 list endpoints do not scale with data (verified, guarded)

The previous chunk's sweep silently excluded the entire external API: those routes need a bearer
key, so every one returned 401 and was dropped from the sample. This measured them with a key
carrying every scope.

Result: 66 reversible targets, 39 answering 200, **all flat** from 4 to 24 pins - including
`pins`, `photos`, `labels`, `trips`, `search`, `lists`, and the `memories.*`, `safety.*` and
`suggestions.*` families. The pagination change made earlier in this audit did not introduce
per-row work.

**Two instrument failures on the way, both of which produced a green.**

The first reported *zero targets*. `_walk` yielded each route's bare local name, but the external
API is namespaced - its routes reverse as `external_api:pins`, never `pins` - so nothing matched
and nothing was measured. A sweep reporting "0 scaling" while measuring 0 routes reads exactly like
success.

The second was the teeth-check. Removing `select_related("parent_pin")` from the pin sync page
changed nothing, which looked like the sweep failing to detect a real N+1. It was not: every seeded
pin has `parent_pin=None`, and dereferencing a null FK issues no query, so the fixture never
exercised the relation that was broken. **An inconclusive teeth-check is not a failed one**, and
reporting it as either would have been wrong. Re-run against `select_related("location",
"location__wiki")` - which every seeded pin does exercise - the sweep caught it immediately:
`external_api:pins` 11 -> 32 queries.

Both halves are now in `test_route_query_scaling.py`, with the namespace tracking and a guard test
asserting some `external_api:` route is still reachable, so the sweep cannot silently lose that half
again.

### The changeset was committed externally, and the commit cannot start

Following the migration omission found in the previous chunk, the obvious next question was whether
the commit had left anything *else* behind. It had, and worse.

`c3ae4911` left five non-test modules untracked while committing 19 files that import them:
`models/abstract/labelled.py` (1 importer), `services/core/locks.py` (3),
`services/geo/distance.py` (7), `services/geo/longitude.py` (6),
`services/pins/import_failure_guess.py` (2).

`models/abstract/labelled.py` is on Django's model-loading path: committed
`models/abstract/__init__.py:10` imports `LabelledModel` from it, and committed `Pin` inherits from
it. A fresh checkout therefore raises `ModuleNotFoundError` while importing models, so the web app,
every management command, both Celery workers and the entire test suite fail before doing anything.

It is invisible from any working copy that still has the files on disk - which is every machine this
audit has run on, and why the full suite currently running says nothing about it. The check that
found it was structural, not behavioural: compare what HEAD *imports* against what HEAD *contains*.

Filed as URGENT with the exact `git add`. Not staged here.

### The changeset was committed externally, without its migrations

`git status` showed 91 files where ~225 were expected. A commit `c3ae4911 audit` had appeared, made
outside this audit, carrying 139 of the changed files.

*Correction on timing, since the first write-up implied otherwise:* the commit was authored at
12:30:11, roughly 86 minutes before the full-suite run that this chunk started, not during it. The
command that began that run printed `working tree: 90 files` - the evidence was on screen and went
unread, because its output landed in a background-task file that was only skimmed for the task id.
The gap between "when it happened" and "when it was noticed" is entirely on the reading, and the
original phrasing ("mid-chunk, git status dropped") described the noticing while reading as though
it described the event.

It did not carry the two migrations, which are still untracked:
`0040_gotify_token_fail_soft` and `0041_pin_import_failure_maps_url`. It *did* carry the `maps_url`
field on `PinImportFailure`, so the committed model state and the committed migration graph disagree.
Confirmed by moving both files aside and running `makemigrations --check --dry-run`, which reports
`+ Add field maps_url to pinimportfailure` and `~ Alter field notify_gotify_token on sitesettings`.

A fresh checkout of that commit migrates to a schema without `maps_url` and then fails on every
query touching `PinImportFailure`. Existing developer databases already ran the untracked migrations,
so nothing breaks locally - which is precisely why this would surface first on a clone or a deploy.

Filed as URGENT in `PROBLEMS.md` with the exact `git add` (0040 first; 0041 depends on it). Not
staged here: this audit does not commit or stage unasked, and the commit was not its doing. This is
the risk flagged two chunks earlier, now realised - and it is worth noting the flag was about
*ordering* while the actual failure was *omission*.

### A pre-commit guard for the class that broke the commit

The startup failure found in the previous chunk had no guard, and could not have had one from
inside the app: importing Django to check would only prove the *working copy* is intact, which was
never in question. The property that actually matters is structural - does the committed tree import
anything the committed tree lacks?

`bin/check_imports_tracked.py` checks exactly that. It parses every tracked-or-staged Python file
under `src/`, collects each `urbanlens.*` import, and resolves it against the set of paths a fresh
checkout would contain: tracked files, plus staged additions, minus staged deletions - the last
because removing a module while leaving its importers behind fails identically. Wired into
`.pre-commit-config.yaml` as `imports-tracked`.

Teeth-checked in both directions on the live breakage: it exits 1 against the current repository
listing 28 unresolvable imports, exits 0 once the missing modules are staged, and exits 1 again when
they are unstaged.

**It immediately found a sixth missing module the manual pass had missed.** `core/tests/oauth.py` is
imported by six committed test files (`from urbanlens.core.tests.oauth import
first_party_application`) and is untracked. The manual pass filtered `/tests/` paths out, because it
was hunting modules that break *startup* - which this one does not. It does break collection of
those six test suites, so the repository's own tests would not run. That is a fair summary of why
the check is worth having as a hook rather than as a habit: the habit had a filter in it, and the
filter was reasonable.

### A third missing-file category, and narrowing the guard so it can block commits

Applying the same question to templates found a third kind of omission in `c3ae4911`: committed
`controllers/pin_import_failures.py` sets
`_GUESS_PARTIAL = "dashboard/partials/memories/_pin_import_failure_guess.html"`, and that template is
untracked. It fails one layer later than a missing import - the app starts, then
`PinImportFailureGuessView` raises `TemplateDoesNotExist` the first time a guess is produced, which
is a 500 on the feature the request asked for.

That brings the commit's missing set to **nine files**: six Python modules, two migrations, one
template.

`bin/check_imports_tracked.py` now checks templates as well as imports - and the interesting part is
what it took to make that safe. The obvious rule (every `.html` string literal must resolve) flagged
`Takeout/My Activity/Maps/MyActivity.html` in `test_archive_extractor.py`, which is a path *inside a
Google Takeout archive fixture*, not a template. This hook blocks commits, so a false positive is
worse than a missed one: it trains people to pass `--no-verify`, at which point the guard is
decorative.

Narrowed to literals that are actually used as templates - passed to `render`, `render_to_string`,
`get_template`, `select_template` or `TemplateResponse`, or assigned to `template_name` /
`*_TEMPLATE` / `*_PARTIAL`. Runtime-built names are deliberately left unresolved rather than guessed.
The fixture path is gone; the real one remains. Teeth-checked end to end: 29 findings against the
current repository, exit 0 with all nine staged, back to failing when unstaged.

### Label uniqueness: the constraint, and three things it broke

Implemented at the owner's request: `Label` unique on `(lower(name), profile, kind)`, case-insensitive,
with `nulls_distinct=False` so duplicate *global* labels are caught too. Migration 0042 merges
existing duplicates (personal labels shadowing a global one merge into the global, which survives;
then duplicates within one owner merge into the oldest), 0043 adds the constraint.

Adding a constraint to a live model is mostly an exercise in finding what quietly depended on its
absence. Three things did:

**1. The migration could not run at all.** Tested against a database seeded with real duplicates
rather than trusting the unit tests, and it failed:
`cannot CREATE INDEX "dashboard_labels" because it has pending trigger events`. Django runs one
migration in one transaction, and Postgres refuses to build an index in a transaction that has
already modified the table - the foreign keys here are `DEFERRABLE INITIALLY DEFERRED`, so the merge
leaves pending trigger events behind. Split into two migrations, which is two transactions. The unit
tests could not have caught this: they call the merge helper directly, never the migration.

**2. 47 tests were relying on the duplication.** A new `Profile` is seeded with ~46 default labels,
so fixtures doing `Label.objects.create(profile=p, name="Hospital", kind=KIND_CATEGORY)` were
silently creating a *second* "Hospital" - precisely the duplication the constraint exists to stop.
Added `core/tests/labels.ensure_label()` for fixtures that want "a label with this identity", and
gave fresh names to the tests that genuinely need a *new* label (signal assertions, hierarchy
property tests) - `ensure_label` returning a seeded row breaks those, because no create signal fires
and the seeded row already has parents.

**3. Import would fail on a re-cased label.** `_import_labels` deduplicates against existing rows
with `name=` exactly. Harmless before; now the lookup misses "Abandoned" while importing "abandoned",
falls through to `create`, and raises `IntegrityError` - aborting the **whole import**, not just that
row. A user re-importing their own export after changing a label's capitalisation would lose the
entire restore. All three lookups are `name__iexact` now.

**A fourth thing the constraint changed: `get_or_create` can no longer self-heal.** Django recovers
from a racing `IntegrityError` by retrying the `get` - which only works if the retry finds the row.
With a *case-insensitive* constraint and a *case-sensitive* lookup it cannot: `get_or_create(name=
"factory")` misses an existing "Factory", inserts, violates, retries the same exact-match get, misses
again, and re-raises. Every `Label.objects.get_or_create` site was therefore checked:

- `google/maps.py` and `tasks.py` already looked up with `name__iexact` and put the value in
  `defaults` - the correct shape, written before this constraint existed;
- `pin_edit.py` and `media_labels.py` pre-filter with `name__iexact` and only fall through to
  `get_or_create` when nothing matches - also correct;
- the five `signals.py` seeding calls use fixed names at profile creation, when no labels exist;
- `pin/model.py` and `wiki/model.py` used an exact lookup. Both are wrapped in
  `except DatabaseError`, so the violation was caught and logged rather than crashing - but the
  category was then silently *not attached to the pin*. Converted to the `name__iexact` + `defaults`
  shape the other four sites already use.

Two assumptions behind the merge were verified rather than trusted. `LabelCustomization` really is
unique on `(profile, label)` - so the merge's "move only where that profile has no row yet, delete
the rest" is required, not defensive; a plain repoint would violate it. And the edit view's kind
conversion is covered by the conflict check, because it passes the incoming `new_kind` rather than
the label's current one: a tag called "Bridge" converted to a category has to collide with an
existing category "Bridge", which is the least obvious of the collisions since the *name* never
changes.

Graceful handling was the explicit request, and covers every write path: HTML create and edit (400
with a message), external API create and patch (409), and undo-restore, which now refuses with
"A tag called X already exists" instead of raising `IntegrityError` from the database. The check is
deliberately wider than the constraint - it also refuses a personal label shadowing a global one,
which the database permits because `profile` differs.

**The merge, validated against every shape it can meet.** Unit tests call the merge helper directly,
which is not the same as running the migration on a database that already violates the constraint -
that distinction is what surfaced the pending-trigger failure above. So the whole chain was run
against seeded data covering each case:

| seeded | result |
|---|---|
| three personal duplicates, one differing only by case | 1 row, oldest survives, pin retained |
| a global and a personal label with the same name | 1 row, **the global survives** and inherits the pin |
| two global labels with the same name | 1 row |
| a pin carrying *both* duplicates | survivor carried once, no through-table collision |
| duplicates with different parents, a child, and a customization | survivor inherits both parents, the child reparents onto it, the customization moves |
| the duplicate is the survivor's own parent | 1 row, no self-parent edge created |

The last two are the ones a naive `UPDATE ... SET label_id` gets wrong: the through table is unique
on (pin, label), so repointing the second row collides, and reparenting without a `<> keep_id` guard
makes the survivor its own parent.

One detail worth keeping: an expression-based `UniqueConstraint` is implemented as a **unique index**,
not a table constraint, because Postgres cannot express `lower(name)` as one. `DROP CONSTRAINT` by
that name reports it does not exist while `\d` lists it plainly under Indexes - which cost some time
while writing a test that needed to drop it.

### Full-suite results across this audit

Five complete runs, each against the working tree at that point:

| run | result | wall |
|---|---|---|
| after the first ~100 chunks | 10,579 passed | 1:20 |
| after chunk 141's changes (209 files) | 10,619 passed | 1:25 |
| after the cache-isolation change (chunk 155) | 10,664 passed | 1:25 |
| after chunks 156-168 | 10,686 passed | 1:29 |
| after the Label uniqueness work | 10,702 passed | 1:30 |
| after the constraint follow-ups, pushed state | **10,708 passed** | 1:29 |

Zero failures in every run (six now). The count grows only because this audit adds tests; nothing was
disabled or skipped to get there.

Worth stating plainly, because it is the limit of what these prove: every run executes the
**working copy**. None of them could have caught the missing files in `c3ae4911` - the modules were
present on disk throughout - and none exercised migration `0042` against data that violates the
constraint it adds, which is why that migration's `pending trigger events` failure had to be found
by running it against a seeded database instead. A green suite is evidence about the code, not about
the commit or the deploy.

**2026-08-14 (chunks 206-218): 10,736 passed, 0 failed, 1:07:01.** Up from 10,708; the difference
is tests added by these chunks. Two caveats recorded rather than glossed: an earlier coverage run
was abandoned because files were `docker cp`'d into the container *while it ran*, making it an
inconsistent snapshot, and this run has a smaller version of the same flaw - chunk 218's files
landed in the container in its final minutes. Nothing failed and already-imported modules do not
pick up mid-run changes, but the last few minutes ran against mixed source, so chunk 218 is
covered by its own targeted run instead (824 passed across labels, markup, saved filters, custom
layers and detail pins). The lesson is the obvious one: do not sync source into a container that
is running a suite.

**2026-08-14 (chunks 225-238): 10,758 passed, 0 failed, 1:08:53.** Up from 10,736. This run was
left strictly alone - no source copied into the container while it ran - so unlike the previous
two it is a clean snapshot of a single commit state, which is what makes the number meaningful.

**2026-08-14 (all N+1 work): 10,765 passed, 0 failed, 1:11:17.** The nine prefetch fixes had each
been verified by a targeted selection but never together, and they touch shared code - `to_json`,
`rating`, `PinViewSet`'s queryset, two export querysets, consensus eligibility, and the
`message_preview` template tag. Performance changes are the least test-covered category in this
audit (a correctness fix ships with a regression test that fails if reverted; a prefetch change is
invisible to everything but a query count), so this run was the real check on them. Clean snapshot,
no source copied in mid-run.


### "Detach location" is a guaranteed 500 (found, filed, not patched)

Following the constraint-vs-check lens onto `Location`, which is `unique_together` on
`(latitude, longitude)` **globally** - two users pinning one physical place share a row by design.

The concurrency path is exemplary. `Location.objects.get_exact_or_create` wraps its insert in a
*nested* `transaction.atomic()` so a raced insert fails inside its own savepoint, catches
`IntegrityError`, re-queries, and returns the winner - with a comment explaining that a bare
`except` without the savepoint left the connection broken and "turned a survivable race into a 500
in exactly the case it was written to survive". `get_nearby_or_create` does the same and delegates
to it at threshold 0.

Two controllers bypass both and call `Location.objects.create` directly, and one of them is broken
outright. `pin_edit.py`'s detach branch builds the new Location from
`pin.effective_latitude/longitude`, which is `float(self.location.latitude)` - *the pin's current
location's own coordinates*. Creating a Location at a point a Location already occupies violates the
constraint every time. Reproduced rather than reasoned about: `IntegrityError: duplicate key value
violates unique constraint "dashboard_locations_latitude_longitude_fdb6594d_uniq"`. Both branches of
the detach path fail the same way, and the constraint dates from `0001_initial`, so this is not
recent breakage.

The reason it survived is worth as much as the bug: **the branch has no test**. `PinRelinkView`
serves both `pin.link.to` (relink, covered by two test files) and `pin.link` (detach). Every one of
the nine `pin.link` references in the test tree is actually `pin.link.delete`, an unrelated endpoint.
Nothing posts to the detach route, so a path that fails 100% of the time reads as covered because
its *sibling* is. Route-name prefixes make that easy to miss by eye - `pin.link` matches
`pin.link.to` and `pin.link.delete` in any naive grep, which is what makes "is this tested?" a
question worth answering with an exact match rather than a search.

Filed rather than fixed because the repair is a product decision: `Location` being globally unique on
coordinates makes "give this pin its own Location at the same point" inexpressible, and choosing
between nudging the coordinates, using the pin's marker fields instead, or refusing the operation
outright is not a call to make from inside an audit. The 500 is wrong under all three, which is the
part worth acting on.

### Two unguarded localStorage writes could freeze the Organize page (fixed)

Sweeping the frontend for storage and parse failures. `JSON.parse` is clean - 18 sites, all inside a
`try`/`catch` (the one apparent exception is a *comment* mentioning `JSON.parse`). Storage writes
were not: 16 sites, 14 guarded, 2 bare, both in `organize-header.ts`.

Neither is the last statement in its function, which is what makes them matter:

- `setSharedView` - a throw skips `syncViewButtons()`, every tab's `applyView()`, and
  `applyAllOrgFilters()`. Clicking grid/list would appear to do nothing.
- the tab-switch handler - a throw skips the URL update, `_initPrioritySortable()`, the
  `org:tab-changed` event, `_orgClearAllSelections()` and `syncOrgFilterBarVisibility()`. Switching
  to Display Order would land on a tab whose drag-and-drop never initialised, carrying stale
  selections.

`setItem` throws on an exhausted quota, and this application *deliberately fills localStorage* - the
map caches every pin under `ul_pins_v5_<profile>`, sized by the user's own pin count. So the failure
is reachable in normal use by exactly the users with the most data, not just in Safari private mode.

The codebase already treats this as best-effort everywhere else - `pages/map/index.html` wraps its
cache write with `catch (err) { /* pins will reload next visit */ }` and its `ul_pins_dirty` flag
with `catch (e) { /* poll covers it */ }`. These two sites simply did not follow the convention.
Both now do: the preference stops persisting, the UI still works. `tsc --noEmit` clean, 390 frontend
tests pass.

---

### Abandoned dumps accumulated forever in the backup directory (fixed)

`core/controllers/backups/db.py`, a module no earlier chunk had opened.

The retention side is sound, and deliberately so. `purge_old_backups()` ends with
`backup_files[self.backup_retention:]`, which would delete *every* backup if retention were `0` -
but it cannot be: `site_settings/model.py:654` carries
`CheckConstraint(condition=Q(backup_retention__gte=1))` and `site_admin.py:211` clamps the form
input with `max(1, int(...))`. Bounded at both the input and the database. The purge also only
considers `is_backup_filename` matches, so a stray file in the directory is never counted toward
retention nor deleted alongside real backups.

The gap was on the other side of that same predicate. `run()` writes the dump to `<name>.sql.tmp`
and `os.replace()`s it into place only on success, so a partial dump can never be mistaken for a
complete backup - the module's own comment names the case it is protecting against: "a mid-dump
process death (OOM kill, container restart)". A `CalledProcessError` cleans its temp file up. A
*killed process* by definition does not, which is the whole point of the design.

Nothing reaped those. `is_backup_filename` excludes `.tmp` - correctly, since a partial dump must
never count as a backup - and that same exclusion means retention never removed one either. Every
OOM-killed dump left a file the size of a full database dump on disk, permanently, in the directory
whose whole purpose is to stay bounded. On a host where dumps get OOM-killed at all, they get
OOM-killed repeatedly.

`purge_stale_temp_files()` now runs at the end of `purge_old_backups()` (which itself runs after
every successful dump, so the reaper needs no scheduler of its own). It only touches files matching
this class's own `.tmp` scheme, and only those older than `STALE_TEMP_AGE_SECONDS` (24h) - deleting
a live dump's temp file would corrupt the backup being written, so the age floor is the load-bearing
part, not the filename match. Five tests in
`dashboard/tests/hypothesis/test_backup_temp_purge.py`, including the two that matter: an in-flight
temp file survives, and an unrelated `.tmp` from something else is never touched.

**Filed, not fixed - there is no restore path.** The only `pg_restore` in the repository is in
`bin/clone_prod_to_staging.sh`, operating on a dump that script creates itself; nothing restores the
*scheduled* backups, and no document describes how. That matters more than it sounds, because these
are plain-SQL dumps (`pg_dump ... -f`, no `-Fc`, named `.sql`) - an operator who reaches for the
repository's only restore example will run `pg_restore` against a plain dump and get *"input file
appears to be a text format dump. Please use psql."* An untested restore procedure is the ordinary
way backups turn out not to work. Recorded in `docs/PROBLEMS.md`.

### A wedged dump was the one unbounded subprocess call (fixed)

Follow-on from the module above, checking whether the missing `timeout=` there was systemic. It
was not - and the surrounding Celery configuration turns out to be the most carefully-reasoned
part of the settings file.

Ten of the eleven `subprocess.run` calls outside tests already pass a `timeout` (`media/videos.py`,
`media/documents.py`, all seven in `core/version.py`). The lone exception was the backup dump. It
was also bounded, just not locally: `run()` has exactly one caller, `tasks.py:1984`, so
`CELERY_TASK_TIME_LIMIT` (3600s) does cap it - the "hangs a worker forever" reading is wrong.

What the task limit produces is worse than a bounded hang, though. Hitting it SIGKILLs the worker,
and `CELERY_TASK_ACKS_LATE` + `CELERY_TASK_REJECT_ON_WORKER_LOST` (both deliberately on) then
redeliver the message - so a dump that wedges on an unreachable DB host is retried into the same
wedge, each attempt leaving another abandoned `.tmp` of exactly the kind the fix above now has to
reap. `subprocess.run` now takes `BACKUP_TIMEOUT_SECONDS` (1800s, env-overridable), chosen below
`CELERY_TASK_SOFT_TIME_LIMIT` (2700s) so the local timeout always fires first; a test asserts that
ordering rather than trusting the two constants to stay in sync.

The handler had to widen with it. `subprocess.TimeoutExpired` is **not** a subclass of
`CalledProcessError` - verified, not assumed; they only share `SubprocessError` - so adding a
timeout without touching the `except` would have thrown straight out of the task and left the
partial dump behind, converting a clean `return False` into the exact leak just fixed.

Worth recording what was checked and found sound, since it is where a bug of this shape would
otherwise live: `visibility_timeout` is raised to 2h with a written rationale for keeping it above
`max(time_limit, longest countdown)`; the result backend has a 5s retry timeout so a dead broker
fails the request path fast instead of after a retry storm; `worker_prefetch_multiplier` is 1.
Nothing to fix there.

### Three copies of session chat, collapsed behind a generic (refactor)

Sweeping the WebSocket layer - `consumers.py`, `routing.py`, the ASGI stack - which no earlier
chunk had opened. **No bug found there**, and that is worth stating plainly, because it is the
most defensively-written module in the codebase. Recorded in full under "Checked and clean"; the
short version is that every consumer verifies participation before joining a group, checks API-key
scope *before* the participant lookup (so a refused connection cannot time-oracle a real session
against an invented one), unwinds partial group membership when `accept()` fails, withholds the
live-location group from token-route contacts, and re-validates credentials on a timer so a dropped
revocation broadcast cannot leave an authorised socket open forever.

What the sweep did surface is on the service side. SpotGuessr, Trivia and Consensus each carried
their own `chat.py` and `realtime.py`, and the two sets were near-identical - `realtime.py` differed
only in a group-name prefix string, and the docstrings said so outright: *"Mirrors
`services.spotguessr.realtime` exactly."* Three copies of `send_chat_message`, three of
`recent_messages`, and three separate `MAX_MESSAGE_LENGTH = 1000`.

That triplication is the reason this chunk found nothing to fix and still changed something. The
consumer layer had already been generalised into `_ParticipantSessionConsumer`; the service layer
never was, so anything that ought to apply to session chat as such - a rate limit, moderation, edit
or delete - has to be written three times and kept in sync by hand.

`services/core/session_chat.py` (`SessionChat[SessionT: Model, MessageT]`) and
`services/core/session_realtime.py` (`SessionBroadcaster`) now hold the logic once. Each game's
`chat`/`realtime` module is a binding over them, so all six exported names and every import path
are unchanged - verified by enumerating what the rest of the tree actually imports from those
modules, not by assuming. 216 lines of triplicated logic became 135 lines of binding over 163 lines
of shared implementation; the win is single-sourcing, not line count.

Two details worth recording because they are where this refactor could have gone quietly wrong:

- **`SessionChat` takes the game's `realtime` *module*, not its `SessionBroadcaster`.** Binding the
  broadcaster directly is the obvious design and it silently breaks the test suite: existing tests
  patch `services.<game>.realtime.broadcast`, and a call to `broadcaster.broadcast` bypasses a patch
  on the module attribute entirely. Passing the module keeps the lookup at call time, so the
  established patch idiom still intercepts. A `SessionRealtime` Protocol types it; mypy checks the
  module against the protocol without complaint. This is not a hypothetical that was reasoned
  around - a test run launched against the intermediate version (binding the broadcaster directly)
  came back with exactly one failure, `test_spotguessr_chat.py::test_broadcasts_the_message`, while
  the same file passes after the change. The prediction and the observation match.
- **`MAX_MESSAGE_LENGTH` now comes from `core/text_limits.py`**, which exists precisely to be the
  one place these numbers live and did not have this one. All three copies were already `1000` and
  matched their `CharField(max_length=1000)` - checked, not assumed, since a constant above the
  column width would fail the insert at the database rather than truncate.

**Filed, not fixed: no rate limit on any chat socket.** Nothing in `consumers.py` throttles inbound
frames - no size cap, no per-connection rate limit - and each accepted frame is one DB insert plus a
broadcast amplified to every group member. It requires an authenticated participant, so it is abuse
rather than an open vector, and picking a threshold is a product decision. The shared module above
is now the single place to add it.

### A bulk import that queried, and streamed, once per entry (fixed)

A performance sweep: an AST pass over every non-test, non-migration module for ORM calls inside
`for` loops - 121 sites. Most are fine (bulk-creation loops, keyset batching, loops over
single-digit candidate sets), and grepping would have drowned in them; the point of triaging by
amplification is that only one site multiplies by *user-supplied* input.

`services/apis/locations/google/location_history.py` imports a Google Takeout export, where tens of
thousands of `placeVisit` entries is ordinary. Per entry it ran a PostGIS nearest-neighbour query, a
duplicate-check query, a write, and an SSE frame. Three of those four are now unnecessary:

- **The spatial query is memoised on the exact coordinate pair.** A location history is mostly the
  same few everyday coordinates repeated - home, work, the same shop - and each repeat was paying
  for its own `distance_lte` + `ORDER BY Distance` query. Keyed on the exact pair the file carries,
  so it dedupes repeats without changing which pin any coordinate resolves to.
- **The duplicate check is one query instead of one per matched entry**, seeded into a set of
  `(pin_id, visited_at)`. The set is updated as rows are created, because the previous
  implementation got intra-file duplicate protection for free by re-querying each iteration - a
  test pins that down, since it is exactly what a naive prefetch would break.
- **Progress frames are throttled to one per whole percent.** A 50,000-entry export was pushing
  50,000 SSE frames at a bar that can render 100 states, making the progress stream its own
  bottleneck at both ends.

What was deliberately *not* changed is as important. `PinVisit.objects.create()` stays a per-row
call: `models/achievements/signals.py` subscribes to `PinVisit` under `TRIGGER_VISIT`, and
`bulk_create` does not fire `post_save`, so batching the writes would silently stop awarding
visit achievements during an import. The same reasoning rules out the obvious fix in
`models/labels/signals.py`, where ~46 seeding `get_or_create` calls per signup look like an
obvious `bulk_create` candidate until you notice `Label` has `post_save` receivers syncing a
taxonomy. Recorded here so the next person to spot either loop does not "fix" it.

The module had **no tests whatsoever** before this, which is why the change ships with nine
covering the behavioural contract (radius matching, cross-run and intra-file idempotency, the
visit-logging setting, `last_visited` tracking) rather than only the cost properties. One of them
had to be corrected mid-write: the importer does `from ...visits.visits import find_nearest_pin`
*inside* the function, so the name is rebound from the source module on every call and a patch
applied to `location_history` would never have intercepted. In this case it failed loudly
(`AttributeError`) rather than passing vacuously, but only because `mock.patch` checks the
attribute exists - the same mistake against a name that *does* exist on the importing module is
the silent version, and is worth watching for anywhere this codebase imports inside a function.

A second test needed correcting for the opposite reason - it was measuring nothing while passing
its own premise. Asserting the throttle with 28 entries is vacuous: `int(i/28*100)` advances on
every single entry, so below ~100 items one-frame-per-percent and one-frame-per-entry are the same
thing. It now uses 500 non-matching coordinates, which exercises the throttle (about 101 frames)
without paying for 500 writes.

### The malware scanner admitted unscanned files as clean (fixed)

`services/security/malware_scan.py` is written to fail *closed* - its docstring says a scanner it
cannot reach must be surfaced as a 503, "not silently admit the upload", and a connection error
duly raises `MalwareScanUnavailableError`. Two paths did the opposite.

**An `ERROR` status read as clean.** clamd does not raise when it fails to scan a particular
target; it reports that target as `("ERROR", reason)` through the ordinary return value - clamd's
own response regex is `(FOUND|OK|ERROR)` and its docstring gives `{filename: ('ERROR', 'reason')}`
as an example. The result was destructured as `(result or {}).get("stream", ("OK", None))` and only
`FOUND` rejected, so an `ERROR` fell past the check and returned `None`: upload admitted, never
scanned. The `("OK", None)` default did the same for any response missing the `stream` key.

**`ResponseError` escaped entirely.** The handler caught `(clamd.ConnectionError, OSError)`.
`ResponseError` is a *sibling* of `ConnectionError` under `ClamdError`, not a subclass - checked
against the installed package's MRO rather than assumed - so a garbled response from clamd
propagated as an unhandled exception and became a 500 instead of the 503 that path exists to
produce. Order matters in the fix: `BufferTooLongError` subclasses `ResponseError` and keeps its
own distinct user-facing message, so it has to stay handled first, and a test pins that so the
widened handler cannot swallow it later.

Anything that is neither `OK` nor `FOUND` now raises `MalwareScanUnavailableError`. All three call
sites (`tasks.py`, `services/media/images.py`, `services/import_export/import_data.py`) were
already wrapping the call in `except MalwareScanUnavailableError`, so the new cases land on
handling that exists - checked, since widening when a function raises is only safe if every caller
already copes.

### Sweeping for the shape the scanner bug had

The malware-scan finding was a *fail-open path inside a fail-closed design*, so the obvious next
move was to ask where else that shape occurs. An AST pass over every non-test module for
security-ish predicates (`can_`, `allow`, `verify`, `valid`, `is_`, `has_`, ...) that `return True`
from inside an `except` handler: three hits, in two places.

One is a false positive worth recording so the scan is not re-run and re-triaged: `tasks.py`'s
`process_device_scan_upload` matched because `can_` appears inside "s**can_**upload". Its `return
True` means "task handled" - it has already recorded `FAILED` status on the row - and is correct.

The other two are both in `rate_limiter.check_rate_limit`, each a bare `except Exception` carrying
the same `# TODO: Catch specific exceptions`. Narrowed to `DatabaseError`, which is what the TODO
asks and what separates the two cases that were being conflated: an infrastructure failure, where
allowing the call rather than breaking a feature is a defensible choice, and a *bug* - a broken
plugin rate-limit declaration, say - which was being converted into "allowed" and logged.

The reason that distinction matters more here than the usual style argument is what this limiter
guards. It is not an access control; it caps calls to **paid** third-party APIs, and the project
tracks a cost estimate per call. A bug that reads as "allowed" does not leak anything - it spends
money, quietly, for as long as it goes unnoticed.

Reading the caller changes the picture in a way worth writing down, because it cuts against the
scary interpretation: `record_api_call` calls `check_rate_limit` *inside* a `transaction.atomic()`
that has already executed `ApiRateLimit.objects.select_for_update().get(...)`. A genuine database
outage therefore raises at that earlier line and never reaches the handler at all - so those two
handlers were, in practice, catching bugs far more often than infrastructure failures. That is an
argument for narrowing them, and against treating the remaining fail-open as urgent.

The remaining question - whether a paid API call should proceed unmetered when the database is
down, or fail - is a policy decision rather than a defect, and is filed in `docs/PROBLEMS.md`
rather than decided here.

### A credential redactor that returned the credential (fixed)

Continuing the same hunt one step wider: exception handlers that swallow with no logging at all.
511 of them, which sounds alarming and mostly is not - the bulk are narrow domain exceptions used
as control flow (`TripError` 52, `ValueError` 45, `Pin.DoesNotExist` 23), where catching and
returning is the design. Filtering to handlers that catch `Exception` outright leaves three, and
that is the useful list.

**`services/admin/infrastructure_stats._redact_url`** existed to hide credentials in the service
URLs printed on the infrastructure admin page, and one of its inputs is the Celery broker URL,
which embeds a password. It wrapped `urlparse` in `except Exception: return url` - so the single
case it was written to handle, a URL it could not make sense of, returned the string *verbatim*,
password included. `urlparse` does raise: an unclosed IPv6 bracket in the netloc is a `ValueError`.
Narrowed to `ValueError`, and the failure path now returns a placeholder rather than the input,
because at that point nothing is known about the string's structure and guessing which span was
the credential is exactly the mistake being fixed. Three tests, one of which asserts `urlparse`
really does raise on the fixture - otherwise the regression test would pass while quietly
exercising the ordinary path.

**`models/abstract/model._slug_max_length`** - the base class every model inherits - swallowed
everything around `_meta.get_field("slug")`. Only `FieldDoesNotExist` should reach the fallback:
returning the *default* length for a model whose slug column is deliberately shorter produces an
over-long value that fails at the database instead, which is a worse and much less obvious
outcome than the error it was hiding.

The third, `boundaries/overpass._endpoint_is_down`, is correct as written - its docstring says
"Fails open on cache errors" and a cache miss legitimately means "not known to be down".

### Reporting progress could fail work that had already succeeded (fixed)

Celery retry safety. 55 of the 75 tasks carry `autoretry_for=(OSError,)`, and
`CELERY_TASK_ACKS_LATE` + `CELERY_TASK_REJECT_ON_WORKER_LOST` are on globally, so redelivery is
not limited to those 55 - every task has to tolerate being run twice.

The photo importers looked like the obvious risk: five retrying tasks that call `.create()` with
no `get_or_create` guard, each downloading over the network first. They turn out to be safe, and
the reason is worth recording so nobody "fixes" them: each loads
`existing_checksums = set(Image.objects.filter(...).values_list("checksum", flat=True))` at the
top of the task body, so a retry re-reads it and skips everything the previous attempt created.
Content-hash idempotency, not luck.

The real finding was underneath them. `services/core/celery.update_task_progress` - called from
nearly every task in `tasks.py` - wrapped nothing around `task.update_state`, which writes to the
result backend. So a backend hiccup propagated out of whichever task was reporting and failed work
that had *already completed*. Worse, with `acks_late` and `autoretry_for=(OSError,)`, that same
failure redelivers the task and re-runs its side effects - and not all of those are idempotent:
`sweep_immich_library_locations` creates an unguarded `NotificationLog` on the line immediately
before its final progress call, so the duplicate-notification window was real rather than
theoretical.

This is now best-effort, matching the contract `channel_broadcast.send_group_message` already
documents for the other side channel in this codebase ("Never raises - ... already durably saved,
live delivery is a bonus"). The handler is deliberately broad, which is the opposite of the
narrowing done elsewhere in this audit, and the distinction is the point: a security predicate
must not convert a bug into "allowed", whereas a progress reporter must never convert a cosmetic
failure into lost work. A test covers the non-`OSError` case specifically, since redis-py raises
its own `ConnectionError` which is not an `OSError` - narrowing here would leave the common case
uncaught.

### Stored XSS through a label colour (fixed)

A frontend injection sweep. The headline numbers look reassuring and are: 150 `innerHTML` writes,
**zero** using `${...}` interpolation; the 8 using `+` concatenation are all `.test.ts` fixtures;
197 uses of `escHtml`/`textContent`. The codebase escapes by habit.

The hole was in the one place the habit lapsed. `label-picker.ts`'s `chipHtml` carefully escapes
`id`, `icon` and `label` - and then interpolates `color` raw into
`style="background:${bg};border-color:${border}"`, which `insertAdjacentHTML` parses.
`formulaPillHtml` does the same and additionally puts the raw value in `color:${txtCol}`.

The delivery path is the interesting part, because at first glance Django should have prevented
it. The colour is rendered as `data-label-color="{{ label.color }}"`, which **is** auto-escaped -
but the JS reads it back through `btn.dataset.labelColor`, and `dataset` returns the *decoded*
value. Escaped into an attribute, decoded out of it, then re-injected into fresh markup: the
escaping never reaches the place that needed it.

And the stored value cannot be trusted, which is the other half. `Label.color` is
`CharField(max_length=50, choices=COLOR_CHOICES)`, but Django enforces `choices` only in
`full_clean()`, which `save()` does not call - and all eight label write paths assign it directly
from `request.POST.get("color")` / `data.get("color")`. A value like `x" onmouseover="alert(1)`
fits in 50 characters and stores cleanly.

Fixed with `shared/color-safety.safeColor`, applied at both `label-picker.ts` sites and at
`organize-tab-manager.ts`'s `miniCardHtml`, which reads the same dataset-decoded colour and
appends the same alpha suffixes. **Validated, not escaped**: escaping stops the attribute
breakout but still allows CSS injection (`url(...)`) inside a style value, and these colours are
only ever hex. `""` falls through to each caller's existing "no colour" branch, so nothing needed
new failure handling. 394 frontend tests pass, `tsc --noEmit` clean.

**Correction to what this section first claimed.** It deferred the three `markup-engine`/
`markup-toolbar` sites on the grounds that a hex-only validator might blank a legitimate
`rgba()`/`none` value. Looking properly, that reasoning was sound but the premise was half wrong.
`markup-engine.ts` was *already* safe - it defines `safeColor(v, fallback)` and
`safeOptionalColor` (which passes `"none"` through) and sanitises into a local one line above the
interpolation the grep flagged. `markup-toolbar.ts` genuinely was not, and is now fixed with the
same shared helper, with `"none"` handled explicitly. The deferral was the right instinct applied
to a misread: the codebase had already answered the question I was hesitating over.

That leaves markup *less* validated server-side than labels, which is the part still open:
`MarkupShape.color` is `CharField(max_length=20, default="#e53e3e")` and `border_color`
`CharField(max_length=20, blank=True)` - no `choices` at all, and `x" onmouseover="a` is 17
characters. Colours handed to Leaflet as *options* (`fillColor:`, `color:`) are left alone on
purpose: they are set as style properties rather than interpolated into markup, so an invalid
value is inert there. The server-side validation gap is filed in `docs/PROBLEMS.md`, and remains
where this should really be solved - once, on the way in.

### Closing the colour class on the server, where it belongs

Two chunks had now fixed the same bug in two different renderers, which is the signal that the
renderer was the wrong place to be fixing it. The root cause is one line of Django semantics:
`Label.color` declares `choices` and `MarkupShape.color`/`border_color` declare nothing at all,
and field `choices` are enforced only inside `full_clean()`, which `Model.save()` does not call.
Every write path assigned straight from request data, so the stored value was "up to N characters
of whatever was posted".

`services/core/colors.clean_color` now validates on the way in, and this is where the chunk earned
its keep: the survey I had done while fixing the renderers found **8** write paths, all in
`controllers/labels.py` and `external_api/views.py`. Re-grepping for the *shape* rather than for
the files I already suspected found **11 more**, in `controllers/markup.py`,
`controllers/detail_pins.py`, `controllers/maps.py`, `controllers/custom_layers.py` and
`controllers/saved_filters.py` - more than half the real surface, in files the earlier pass never
opened. All 19 now go through the validator, and the same grep comes back empty.

Design notes worth keeping:

- **Coerced to the caller's default, not rejected.** These values come from palette pickers, so a
  non-colour is a malformed request rather than a user mistake worth reporting, and every one of
  these endpoints already treated a *missing* colour that way. Each call site keeps the default it
  had, so behaviour is unchanged for every valid input.
- **`"none"` is opt-in per call site** (`allow_none_keyword`). Markup borders use it to mean "no
  border" and the map renderer checks for it by name, so it is a meaningful value there - and a
  bare CSS keyword nowhere else, which is why it is not simply allowed everywhere.
- The renderers keep their own validation. This is the first line, not a replacement: a renderer
  added next year should not have to rediscover the rule, and the two layers fail independently.

A hypothesis property test asserts the part that actually matters - for arbitrary text input, the
result is always a hex colour, the `"none"` sentinel, or the default. Never an arbitrary string.

### An escaper that is safe for text and unsafe for attributes (fixed)

Started as an accessibility sweep of all 418 templates, which found nothing - see the methods note
below, because the instrument was worse than the result. What it did surface, incidentally, was two
JS-generated `<img>` tags in `pages/memories/index.html`, and reading them turned up the same shape
as the label-colour bug one layer along.

`popupHtml`/the card builder escape `event.title` and `event.subtitle` with `escapeHtml(...)` and
then interpolate `event.thumbnail_url` into `src="..."`, `event.url` into `href="..."`, and
`event.type` into a `class="..."` completely raw.

The trap is what "fixing" it naively would have done. That file's `escapeHtml` is
`div.textContent = value; return div.innerHTML` - which escapes `&`, `<` and `>` but **leaves
quotes untouched**, because quotes are not special in a text node. It is correct everywhere it is
currently used, and would have been useless in exactly the places that needed it: a `"` ends the
attribute regardless. Wrapping the three sites in the escaper already sitting there would have
looked like a fix and changed nothing.

Added `escapeAttr` alongside it, with a comment stating which of the two belongs in which context,
and applied it to the three attribute interpolations. `event.icon` goes into element *content*, so
it takes `escapeHtml` - the distinction the new comment exists to keep straight. The two `<img>`
tags also gained the `alt=""` they were missing, which is the correct value for a decorative
thumbnail sitting next to its own title text.

Unlike the colour finding, this is defence in depth rather than a demonstrated hole: these values
come from a server JSON endpoint where `type` is an enum, `url` is built by `reverse()`, and
`thumbnail_url` is a media path whose filename Django has already sanitised. Nothing here is known
to be exploitable today. It is the inconsistency that is worth removing - three raw interpolations
sitting beside two escaped ones, in a file whose own escaper cannot protect them.

### A scheme guard that stopped the wrong attack (fixed)

Sweeping every template and TS file for the shape chunk 219 found: a value interpolated into an
attribute without escaping. 68 hits, and the triage matters more than the number - most are UUIDs,
integer ids and enum keys, where nothing user-controlled reaches the string. Two groups are real,
both in `pages/map/index.html`.

**`pin.icon` into `src="…"`, behind a guard that does not guard this.** The line is preceded by
`/^(https?:\/\/|\/)/.test(pin.icon)`, which reads like a safety check and is one - against
`javascript:`. It does nothing about the attribute itself: `https://x" onerror="alert(1)` passes
that regex, and then its quote ends the `src`. `Pin.icon` is `CharField(max_length=255)` with no
validator, assigned from request data like the colours were, and pins are shared - so this is the
same stored-value shape as the colour finding, in a field `clean_color` does not cover.

**Google Places / NPS / Wikipedia payloads straight into `innerHTML`.** `place.vicinity`,
`place.description`, `d.formatted_address`, `d.editorial_summary.overview` and `d.rating` were
interpolated as element content with no escaping, and `place.url`/`d.website` into `href` with no
scheme check. This is third-party data containing user-submitted business names and descriptions,
so it is not attacker-controlled in the direct sense - but "we trust Google's payload to be
HTML-safe" is not an assumption worth holding.

The fix needed three helpers, not one, and the reason is the chunk 219 lesson applied: the file
*did* already contain an `escapeHtml`/`escapeAttr` pair - defined at line 5681, **inside** a
function body after an early `return`, so invisible to every site above it, and partial anyway
(`escapeHtml` escapes only `&` and `<`; `escapeAttr` only `"`). `_ulEscText`, `_ulEscAttr` and
`_ulSafeUrl` now sit at the top of the script with a comment on which context takes which, and
`_ulSafeUrl` exists because escaping quotes does nothing about `javascript:` - the two defences
are orthogonal and both were missing somewhere.

Verified by extracting the three helpers and executing them (rejects `javascript:`, `data:` and
protocol-relative `//host`; passes ordinary absolute and relative URLs). That indirection is
itself worth recording: this page carries several thousand lines of inline `<script>`, which no
test in the repository can import or exercise. The bun suite covers `frontend/ts/`; everything
inline in a template is, structurally, untested.

### Half the frontend is untestable, and it has reimplemented HTML escaping 14 times

Chunk 220 ended on the observation that inline template JavaScript cannot be reached by any test.
This chunk measured it, because "some untested code" and what is actually there are different
claims.

**21,543 lines of inline JavaScript across 101 templates**, against 22,684 lines of TypeScript in
`frontend/ts/` (which `tsc --noEmit` and 394 bun tests do cover). So roughly half the frontend is
outside every automated check the project has. It is concentrated: the top five templates are
10,736 lines, 49% of the total, led by `pages/map/index.html` at 5,175.

The consequence is measurable rather than theoretical. 44 function *names* are defined in more
than one template, and the worst case is the one that matters most: **14 separate HTML-escaping
helpers under 9 different names** (`escapeHtml` x5, `_escHtml`, `escHtml`, `_esc`, `_escapeHtml`,
`htmlEscape`, `escapeAttr`, plus the three added in chunk 220). They do not agree:

| implementation | escapes | attribute-safe |
|---|---|---|
| `pin_lists/detail.html` `_escHtml`, `map` `_escapeHtml`, `_notification_push` `escapeHtml` | `&<>"` (+`'`) | yes |
| `map` `escapeHtml` | `&<"` - no `>` | yes |
| `location/index`, `memories/index`, `memories/photos`, `profile/index`, `_mini_calendar` | `&<>` | **no** |

Six of the fourteen are text-only. That is not a bug in itself - `textContent`->`innerHTML` is
exactly right for a text node - but there is nothing in the *name* to say so, and this audit has
now found two places where the text-only version was sitting next to an attribute interpolation
that needed the other kind (chunk 219's `memories/index.html`, chunk 220's `map/index.html`). A
developer reaching for "the escape function in this file" gets a coin flip.

The fix is not more escaping helpers. It is that this code should live in `frontend/ts/`, where it
would inherit `tsc`, the bun suite, and one shared `escapeText`/`escapeAttr` pair with the
distinction in the names. Filed in `docs/PROBLEMS.md`; the map page alone would be a meaningful
first slice.

**Methods note - the instrument was wrong twice before it was right.** The first pass reported
every helper as unsafe, including two written earlier that afternoon; the `"` pattern had been
over-escaped in a Python raw string. The second pass reported the two *best* implementations
(character-class `/[&<>'"]/g` replacements) as escaping nothing, because it only recognised
individual `/&/g` patterns. Both errors were caught only by checking the output against helpers
whose behaviour was already known. A scanner over a language you are pattern-matching rather than
parsing needs a known-answer control, or its confident output is noise.

### 46 style modifiers that render as nothing

A CSS reachability sweep: which classes do templates apply that no rule ever matches? Measured
against the **compiled** `style.css` rather than the SCSS sources, because `&--modifier` nesting
means a resolved class name frequently appears nowhere in the source. (Verified the compiled file
was current first - no `.scss` is newer than it.)

3,806 distinct classes are applied across the templates; 511 have no rule at all. Most of that
number is uninteresting - JS hooks, one-off wrappers, classes that only ever carried semantics.
The meaningful subset is BEM modifiers **whose base class is styled**: 46 of them. That pattern is
unambiguous - somebody wrote `class="card card--secondary"` intending a visual distinction, and
the distinction does not exist.

Several are user-visible states rather than cosmetic polish:

- `ul-game-hud__group--lead` - on all three game pages, marks the *leading* score. Renders
  identically to every other group.
- `badge--muted` - 5 templates, mostly site admin.
- `card--secondary` / `card--primary` - 8 and 3 templates; a visual hierarchy that is currently flat.
- `visit-list--pending`, `visit-item--pending`, `visit-source--pending` - a pending visit looks
  exactly like a confirmed one.
- `notif-item__icon-wrap--pin_shared`, `--safety_ci_due`, `--visit_suggested` - per-type
  notification icon treatments.
- `sv-img--fallback`, `trip-map-marker-num--ghost`, `dm-thread--group`.

Not fixed, and deliberately: what `--lead` or `--muted` *should* look like is a design decision,
not a defect with one correct answer. The full list of 46 is in `docs/PROBLEMS.md` so it can be
worked through or deliberately dropped.

**Methods note - the fifth bad instrument today, caught by a control.** Cross-checking a sample
against the SCSS source appeared to contradict the compiled CSS: `card--secondary` had "3 matches".
It did not - those were `&--secondary` blocks nested under `.btn` and two other parents, which the
grep happily matched because it looked for the modifier suffix under *any* parent. The compiled
CSS was right and the cross-check was wrong. That is now five sweeps in one session where the
first instrument was misleading; the pattern is that every one of them was caught by checking
against a case whose answer was already known, and none would have been caught by reading the
output alone.

### The untested-view question, finally answered with the right instrument

Chunk 196 estimated view coverage by counting how many routes appeared in test files, and said
plainly that this was an upper bound - it cannot tell "imported" from "executed" - and that the
right instrument was `coverage.py` over the real suite. Three attempts later, that measurement
exists (`docs/reports/2026-08-14-view-coverage.md`).

**80% of 22,081 statements** in `controllers/` + `external_api/` execute during the full suite.
**208 of 1,795 callables (11%) never execute at all.** And the distribution is the finding:
**100 of those 208 are HTTP write handlers** - `post`/`delete`/`put`/`patch` - totalling **1,217
statements of data-mutating code that no test reaches**. Untested read paths render a wrong page;
untested write paths lose or corrupt data, and half of what is unexercised here is a write path.

Worst files by count: `userprofile.py` (19), `safety.py` (17), `consensus.py` (16),
`site_admin.py` (13), `external_api/views.py` (12). Largest single gaps:
`PinController.upload_takeout` (39), `LabelBulkConvertView.post` (36), `SiteAdminUsersView.post`
(35), `LocationWikiDetailPinEditView.post` (34), `LabelBulkEditView.post` (33).

One result is worth more than its size: `PinController.upload_takeout` also appeared in chunk
222's list of routes with no discoverable caller. Two independent instruments - a static
reference sweep and a runtime coverage run - converging on the same 39 statements is much stronger
evidence than either alone, and makes it the clearest candidate for either deletion or a first
test.

Three attempts were needed because the first two were spoiled by my own doing: one wrote its
marker without gating on the JSON step succeeding, and both had source `docker cp`'d into the
container mid-run. The third was left strictly alone for its full 1:38:35, which is the only
reason it produced a usable artifact.

### Removing the one thing two instruments agreed on

`PinController.upload_takeout` was the single item flagged by both the static caller sweep (chunk
222) and the coverage run (chunk 224). Following it properly produced a fourth, decisive signal
and one correction along the way.

The route is `pin.upload.takeout` at `.../import/upload/`, sitting beside `pin.import.form` -
which templates *do* reference. So the natural reading is "the import dialog posts here". It does
not: the wizard (`pages/location/import/csv.html`, served by `import_form`) posts to
`pin.import.preview` and `pin.import.confirmed`, and nothing anywhere - template, TypeScript,
inline script or Python - names `pin.upload.takeout`.

Mid-investigation this looked like it might be bigger than one handler. `upload_takeout` is the
only place importing `extract_archive`/`is_archive`... except it is not: `parse_for_preview`, 70
lines below, imports the same pair and is very much alive - it is what `pin.import.preview`
resolves to. So the 380-line `archive_extractor` service is fine, and the correct conclusion is
narrower and cleaner: `upload_takeout` is a **superseded duplicate** of the preview/confirmed
flow, which handles the same archives, nested archives and KML/JSON/CSV formats.

Four independent signals, then: no static reference, never executed by 10,742 tests, no UI path,
and a live sibling doing the same job. Removed - 76 lines from `pin.py` and its URL pattern.
Google Takeout import itself is untouched and still documented on the help page; it runs through
the wizard, which is the path users actually have.

Worth noting what nearly went wrong: the "only caller of `archive_extractor`" reading would have
justified deleting a 380-line security-sensitive service (it is the thing doing zip-slip and
symlink checks) on the strength of a grep that had scrolled past its second consumer.

### Answering the codebase's own "this is probably wrong"

The in-code marker survey is short and healthy - 27 `TODO`s, zero `FIXME`/`XXX`/`HACK`/`BUG` - and
17 of the 27 are the same `# TODO: Catch specific exceptions` note already acted on in the rate
limiter. Two were substantive, and both sat in `controllers/userprofile.py`, which the coverage run
independently ranked worst in the view layer (19 never-executed callables).

**`# TODO: Whatever is happening here is probably wrong.`** The code builds
`Q(labels__name="Visited", labels__kind=KIND_STATUS) | Q(last_visited__isnull=False)` inline. Two
suspicions, one confirmed and one dismissed:

- *Dismissed:* matching a status by display name looks fragile - it is exactly the case-sensitive
  name lookup that the Label uniqueness work had to fix in half a dozen places. Here it is safe,
  and for a reason worth recording rather than rediscovering: `is_protected` labels cannot be
  renamed on **either** write path (`controllers/labels.py` guards the assignment,
  `external_api/views.py` refuses the write with 403), and "Visited" is seeded protected. The name
  is a stable key because an invariant makes it one.
- *Confirmed:* `PinQuerySet.visited()` already exists, and its docstring explicitly asks callers to
  "build on it directly instead of re-deriving the `Q`" - which is precisely what this was doing.
  It was one of **four** inline copies of the same predicate (`userprofile.py`, `queryset.py` twice,
  `pin/signals.py`). Now calls `.visited()`; two imports became dead and went with it.

**A log line that contradicted its own return value.** `rate_limiter.service_is_enabled` logged
"Failed to read rate limit config for %s - **allowing call**" and then `return False`, which
refuses it. The message was copy-pasted from `check_rate_limit`, where returning `True` does allow
the call. Anyone reading logs during an incident would have drawn exactly the wrong conclusion
about why traffic stopped. The behaviour is right and stays - "is this service switched on" has no
safe affirmative answer when it cannot be read - so only the message changed, and the handler
narrowed to `DatabaseError` like its neighbours.

### A TODO plus coverage data, and a correction to an earlier finding

Two TODOs said `change_category` was "probably deprecated since the addition of Labels more
generically". The coverage run made that checkable rather than a judgement call, and the line
detail is what settled it: of the handler's four statements, 616 and 617 executed and 618-619 did
not. That shape has one explanation - `get_object_or_404` raised on every call. Something in the
suite posts to the route, and the handler body has never once run to completion.

With no template, TypeScript or inline-script reference either, that is three signals, and
`KIND_CATEGORY` labels already provide the capability through the organize and bulk-edit paths.
The action and its route are removed.

**What that turned up about earlier work in this same audit.** Following the chain,
`Pin.change_category`, `Pin.add_category` and `Wiki.add_category` all have zero callers outside
tests. `add_category` is where the label-uniqueness chunk found and fixed a
`MultipleObjectsReturned` bug - the `get_or_create` lookup was missing `profile=None`, so a
case-insensitive match could return a global and a personal label together. That fix is correct and
its tests are real, but **the method has no production caller, so the bug was not reachable in
production** - and it was reported at the time as though it were. The finding was sound; its
significance was overstated.

This is the second time coverage data has changed the reading of something already "known": it
converted "probably deprecated" into a demonstrated fact here, and downgraded a fixed bug to
unreachable. Static reading alone had produced neither.

The three orphaned methods are filed rather than deleted - removing model methods together with
their tests is a product call, and the plausible answer (categories are labels now) deserves to be
stated by someone who owns the roadmap.

### Using the coverage list: the first untested write handler had the bug it looked like it had

The point of the never-executed list is to be worked, not filed, so this chunk took the largest
untested write handler on it: `LabelBulkConvertView.post` (36 statements, top of the list, in a
subsystem this audit has already had to fix several times).

Reading it before testing it was enough to predict the failure. `Label` is unique on
`(lower(name), profile, kind)`. The single create and edit paths call `find_conflicting_label`
first and return a readable 400 - that guard was added earlier in this audit. Bulk-convert sets
`label.kind = new_kind` and saves with no check at all, so converting a tag whose name already
exists as a category is a constraint violation.

TDD confirmed it exactly: `IntegrityError: duplicate key value violates unique constraint
"uq_label_profile_name_kind_ci"`, `Key (lower(name::text), profile_id, kind)=(zzaudit collide, 3,
category) already exists`. A user hits this by having "Museum" as both a tag and a category -
which is legal, since the constraint is per-kind - and then converting one onto the other from the
organize UI.

Fixed to match the single-edit path: the whole batch is checked first and refused with a 400
naming the offending labels. Refused rather than partially applied, because converting some and
failing on others leaves the user to work out which half took effect.

Three tests, and the third is the one that makes the other two meaningful: a *non-colliding*
convert must still work. Without it, "no 500" and "the label is unchanged" would both pass if the
guard simply stopped all conversion - which is the obvious way to get this wrong.

This is what the coverage list is for. One handler, chosen because a measurement said nothing
executed it, produced a reproducible 500 on a plausible user action within an hour. There are 99
more write handlers on that list.

### Admin account deletion had no test, and a docstring saying it did not exist

Next entry on the never-executed write-handler list: `SiteAdminUsersView.post` (35 statements).
Reading it first turned up the documentation problem before any test ran - the class docstring
reads *"Read-only directory of registered users"* and documents only `GET`. The `post` it does not
mention performs **admin-initiated deletion of another person's account**. The one-line summary a
developer sees first actively misdescribes the view's most consequential behaviour. Corrected.

The guards themselves read as sound - no self-deletion, no deleting superusers or site-admin group
members, and a typed confirmation - and nothing verified any of them. Those are the guards whose
silent failure is both catastrophic and invisible, so the contribution here is locking them down
rather than hunting a bug that may not exist. Eight tests.

**The control test paid for itself on the first run.** Six guard tests passed and the two asserting
deletion *works* failed - which is the signature of guards passing trivially because the operation
never happens at all. Had the control not been there, six vacuous tests would have gone in looking
like protection.

The cause was my test, not the code, and the behaviour it exposed is worth recording.
`profile_visibility` defaults to `ANYTHING_IN_COMMON`, and a fresh admin shares nothing with a
fresh user - so `can_view_profile` is false and the handler deliberately does **not** echo a
username the admin is not allowed to see. It asks for the literal string `"hidden user"` instead.
That is a thoughtful privacy choice with a side effect worth stating plainly: for every user an
admin cannot see - which by default is most of them - the typed confirmation is a **fixed,
guessable constant** rather than a per-user string. As a speed bump against misclicks it still
works; as a "type the name to prove you mean it" check it is weaker than it looks. Both branches
now have tests.

### A failure toast that only reached one of two exit paths (fixed)

`VisitSuggestionRespondView.post`, 31 statements, never executed. Two suspicions on reading it,
and the more dramatic one was wrong - worth recording both, since the wrong one is the kind of
thing that gets "fixed" into a regression.

*Wrong:* the `blocked` branch assigns `response["HX-Trigger"]` outright, immediately after
`_trigger_label_refresh` has set that same header - which looks like it discards the label refresh.
It does not: the branch re-includes `notifCountRefresh` in its own payload. Careful code, and
"simplifying" it by using `.update()` on a header that is a JSON string would have been the actual
bug.

*Right:* the handler has two exit paths. When the response came from a pin page
(`context=pin`), it returned `_trigger_label_refresh(response)` **before** reaching the `blocked`
handling at the bottom. So a user on a pin page who accepted a suggested visit while visit logging
was switched off got their visit history re-rendered, unchanged, with no explanation - the exact
silent failure `CLAUDE.md` calls out ("Results and errors must surface as toast notifications").
From the notification dropdown, the same action explained itself properly.

The triggers are now built once and applied to whichever response is returned, so the two paths
cannot drift again. One import became dead and went with it.

### Chunk 218's "the grep comes back empty" was wrong, in two ways

Working the next never-executed write handler - `LocationWikiDetailPinEditView.post`, 34
statements - the first thing visible was three colour fields assigned straight from the request
body with no `clean_color`. That should have been impossible: chunk 218 introduced the validator
specifically to close every colour write path, and reported "all 19 now go through the validator,
and the same grep comes back empty."

The grep was incomplete twice over, and both failures are instructive:

1. **Syntactic form.** The pattern matched `color = X.get("color")` - an assignment. These are
   dict-literal entries: `"color": body.get("color")`. A quote sits between the field name and the
   colon, so the pattern could not match, and `detail_pins.py` has eight of them.
2. **Field-name coverage.** `detail_bg_color` is populated from the request key `bg_color`. My
   field list was built from the *request keys* I had seen, so a field whose model name differs
   from its request key was invisible to it - even in a file I had already edited.

Eight sites fixed. This is the second time in this audit that a claim of completeness rested on a
pattern rather than on the population it was meant to cover, and the first (8 colour paths that
turned out to be 19) was caught the same way: by looking at the code rather than re-running the
grep. Rerunning a flawed pattern only ever confirms it.

Worth stating plainly: **"the grep is empty" is a statement about the grep.**

**And it happened a third time.** The corrected sweep above matched `.get(` - so it missed
subscript access. Widening the *structural* filter (any direct field assignment from request data,
found by AST rather than regex) turned up `markup.py`'s `item.color = body["color"]` and
`item.border_color = body["border_color"]`, plus `external_api/views_labels_bulk.py` and
`external_api/views.py` writing `data["color"]` straight to a model. Three more real sites, on top
of chunk 218's 19 and chunk 231's 8 - **30 at this point**, found across three passes, each of which
believed it was complete. (Two more followed: see the fifth-pass entry below. The final count is
32, and the fact that this paragraph originally read "30 in total" is the point it was making.)

The lesson is not "write better regexes". It is that a regex encodes a guess about *syntax*, and
the property being checked is *semantic* - "does a value from the request reach this field
unvalidated". The AST pass that finally found these asks a structurally different question, and it
also correctly cleared two sites the regex flagged (`custom_layers.py` validates against an
allowlist on the following line; `labels.py:487` is fed by `_parse_bulk_payload`, which already
calls `clean_color`). A checker that produces both true positives *and* verifiable clears is
worth more than one that only produces a count.

### What nine untested handlers have in common

`LabelMergeView.post` (19 statements) was picked deliberately rather than by size: the defects so
far had clustered in `labels.py` and `site_admin.py`, so the obvious next move was to finish those
files. It is clean. The gap I expected - the GET path excludes the source from the merge candidates
but the POST path does not, so a crafted request could merge a label into itself - is guarded one
layer down, in `services/labels/merge._validate`: *"Cannot merge a label into itself."* The
controller turns that into a 400.

Where it sits is the point, and with nine handlers examined a better prioritiser than either size
or subsystem has emerged:

| handler | touches models how | outcome |
|---|---|---|
| `LabelBulkConvertView.post` | sets `label.kind` directly | **500 on collision** |
| `LocationWikiDetailPinEditView.post` | assigns fields directly | **8 unvalidated colour writes** |
| `VisitSuggestionRespondView.post` | delegates; own control flow | **silent failure on one exit path** |
| `SiteAdminUsersView.post` | delegates to `request_deletion` | clean (guards were unverified) |
| `LabelBulkEditView.post` | assigns *safe* fields only | clean |
| `AlbumEditView.post` | assigns fields, validates each | clean |
| `ConsensusPhotoUploadView.post` | delegates | clean |
| `CalendarImportView.post` | delegates | clean |
| `LabelMergeView.post` | delegates | clean |

Both model-corrupting defects were in handlers that **assign model fields directly** instead of
going through a service that owns the invariant. `LabelBulkConvertView` set `label.kind` itself and
so never met the uniqueness check that `find_conflicting_label` performs for single edits;
`LocationWikiDetailPinEditView` assigned colours itself and so never met `clean_color`. Every
handler that delegated was fine - the invariant was enforced once, at the boundary, for all
callers.

That is a sharper filter for the remaining 91 write handlers than "largest first": **look for
`setattr`/direct field assignment on a model in a view**, and check whether the corresponding
service-layer validation exists and is being skipped. The third defect is a different species -
presentation logic, not model integrity - and would not be caught by that filter, which is worth
knowing too.

### The filter found its first bug on the first run

Chunk 235's prioritiser, applied mechanically: an AST pass over every controller for
`obj.field = <request data>` or `setattr(obj, ..., <request data>)`, intersected with the
never-executed list. Four handlers. Two were already known (`AlbumEditView`, clean;
`LocationWikiDetailPinEditView`, fixed). One was a boolean preference. The fourth was a bug.

`EditProfileView._save_profile` does this, three lines after saving a form:

    request.user.first_name = request.POST.get("first_name", "").strip()
    request.user.last_name = request.POST.get("last_name", "").strip()
    request.user.save(update_fields=["first_name", "last_name"])

A form *is* used for the rest of the profile - these two fields are assigned outside it, so the
form's validation never sees them. `User.first_name` and `last_name` are `max_length=150`, which
Django enforces in `full_clean()`, which `save()` does not call. A 200-character name therefore
reaches Postgres and returns `DataError: value too long`, i.e. a 500 on the profile edit page.

Exactly the shape of the two earlier defects: a view assigning a model field directly, skipping the
layer that owns the constraint. Fixed by truncating to the column width, which is what every other
free-text field here does (`albums.py`: `.strip()[:_MAX_ALBUM_NAME_LENGTH]`), reading the limit off
the field rather than hardcoding 150.

The filter is worth keeping: it was derived from nine hand-examined handlers, expressed as a
mechanical query, and returned one real defect out of four candidates on its first run - against a
list where hand-picking by size was running at four defects in nine.

### The same bug class, three more times - and a fourth regex miss

The profile-name 500 (a `CharField` assigned straight from POST, `max_length` enforced only by
`full_clean()` which `save()` never calls) is a *class*, not an incident. Running the AST filter
across all controllers and then checking each target's actual field type separates the harmless
from the reachable:

- **`TextField` targets are safe.** Postgres does not enforce a length on `text`, so
  `visits.notes`, `labels.description`, `labels.keywords`, `safety.default_message` and
  `achievements.description` cannot fail this way, whatever `max_length` the field declares.
- **`CharField` targets are not.** Three were unbounded: `labels.icon` and `achievements.icon`
  (`max_length=50`), and `achievements.metric` (`max_length=64`). All now truncate to the column
  width read off the field.

**And a fourth syntactic miss.** `achievement.color = (request.POST.get("color") or "").strip() or
DEFAULT_ACHIEVEMENT_COLOR` is a colour write - the 31st - that all three regex generations missed
because the value begins with `(` rather than `request.POST`. The AST pass *had* listed it; I did
not act on it, because I was reading that output filtered to untested handlers and this one is
covered. So the instrument was right and the reading was wrong, which is its own lesson: a
structural checker only helps if its whole output gets read.

Left alone deliberately: `achievements.metric` declares `choices` that `save()` will not enforce
either, so an unknown metric can still be stored - it simply never matches a registered metric and
the achievement quietly never awards. That is a real gap but a different one (silent bad data, not
a crash), and it is admin-only. Noted rather than fixed.

### Where the AST-filter method stops working

The colour filter worked so well that the obvious next move was to point the same technique at
authorization: find `get_object_or_404` calls in controllers with no owner scoping - the IDOR
shape. 76 hits, and after three rounds of refinement, **zero real findings**. Every one is
authorized. That is a good result for the codebase and a better one for knowing when this method
applies.

The 76 resolve into four different ways of being safe, which is why no single check found them:

1. **In-query scoping** - `get_object_or_404(PinAlias, id=..., pin=pin)`, where `pin` was itself
   owner-resolved. Scoped transitively through a parent the checker cannot follow.
2. **Post-hoc comparison** - `image = get_object_or_404(Image, pk=image_id)` immediately followed
   by `if image.profile_id != profile.pk: raise Http404` (`photos.py`).
3. **Authorization in the caller** - `image_gallery._get_image` is genuinely unscoped; its caller
   does the check. The property is satisfied one frame up.
4. **A domain rule instead of ownership** - `trivia.py` fetches a question unscoped, then refuses
   unless `TriviaAnswer.objects.filter(round__question=question, profile=profile).exists()`:
   "you can only vote on a question you've answered". Correct, and not an ownership test at all.

The difference from the colour case is worth stating, because it predicts where this technique pays
off. *"A request value reaches this field unvalidated"* is a **local** property - decidable from one
expression. *"This object access is authorized"* is **non-local**: it can be satisfied in the
caller, a mixin, a decorator, a queryset manager, or a domain invariant three lines later. An AST
pass over one function cannot decide it, and a filter that cannot decide produces a list that must
be checked entirely by hand - at which point it has sorted the work, not done it.

Recorded so the next person does not re-run this and read 76 hits as 76 problems.

### Nineteen ways to turn a typo into a 500

Applying the filter to another *local* property, per the previous chunk's distinction:
`int()`/`float()` on request data with no guard. `int("abc")` raises `ValueError`, and a request
body is free to contain "abc" wherever a view expects a number.

Nineteen sites, across `achievements.py`, `detail_pins.py`, `labels.py`, `markup.py` and
`site_admin_costs.py`. `labels.py:614` and `744` are the ordinary label create and edit forms -
`int(request.POST.get("order", 0))` - so this is not an exotic path; it is what happens if a
client sends `order=` as anything non-numeric.

The codebase already knew the pattern, which is what makes the gap notable rather than novel:
`site_admin.py` wraps exactly these conversions in `except (ValueError, TypeError)`, and three
separate local helpers exist for the same job - `labels._safe_int`, `saved_filters._clamp_opacity`
and `map_overlays._clamped_opacity`. The answer had been written three times and applied to about
a fifth of the places that needed it.

`services/core/numbers.safe_int`/`clamp_int` now hold it once, and all nineteen call sites use it.
Deliberately conservative: each keeps the default it already had, and no clamping was introduced
where none existed, so the only behavioural change is that a malformed number produces the default
instead of a 500. `safe_int` also refuses `bool` explicitly - it is an `int` subclass, so `True`
would otherwise arrive as `1`, which is almost never what a caller means.

### A colour write behind a variable key - the fifth and last miss

A sweep for unguarded `body["key"]` subscripts (KeyError -> 500) found 42 candidates and **no
defects** - the guards are there, in forms the filter could not see: `if "lat" not in body` (the
intervening `not` broke the pattern), `if body.get("pin_type") in valid_types`, and
`_parse_bulk_payload`'s return value, which is a locally-built dict that always has its keys and is
not request data at all. Another reminder that a filter with no notion of a variable's *origin*
will conflate "came from the user" with "we just built this".

Reading the false positives turned up the real finding. `pin_bulk.py` is the most careful handler
this audit has read - it checks `if request_field not in data: continue`, enforces a per-field
length with a 400, and wraps `int()` in `try/except (TypeError, ValueError)`. It also writes
`color`, `bg_color` and `border_color` **validated by length alone**:

    ("color", "color", 20),

Twenty characters. `x" onmouse` is ten. And because the key is a *variable* (`data[request_field]`
inside a loop over field tuples), no colour sweep in this audit could ever have matched it -
not the `.get(` regex, not the subscript regex, not the AST pass keyed on field names, because the
field name only exists at runtime.

That is the **32nd** colour site, found by reading code that a filter had cleared. The count across
five passes: 19, +8, +3, +1, +1. Every pass believed it was complete; the last three were found by
looking at things, not by matching them.

The fix reuses `clean_color`, keeping the `"none"` sentinel for the two border fields where it is
meaningful and refusing it for `color` where it is not.

### Two filters, wrong in opposite directions

Sweeping request values assigned to `CharField` targets turned up two real problems, both in
`labels._parse_bulk_payload`, and both invisible to the sweep that had just been run over the same
file:

- `"icon": data.get("icon") or None` is unbounded and lands on `Label.icon`, a
  `CharField(max_length=50)`. The identical bug at `labels.py:740` was fixed two chunks earlier;
  the bulk path reaches the same column by a different route.
- `[int(x) for x in data.get("add_parent_ids", [])]` raises `ValueError` on any non-numeric entry
  in a client-supplied list.

The second is a flaw in **my own** chunk-240 filter. It excluded a function from consideration if a
guard appeared *anywhere* in its text, and `_parse_bulk_payload` contains `_safe_int` on the line
above - so the whole function was suppressed, list comprehension included.

Re-running per call site instead flipped the error the other way: 11 new "findings", all of them
guarded by `with contextlib.suppress(ValueError, TypeError)` - a `with` statement, which a filter
looking only for `ast.Try` cannot see. `site_admin.py` uses that idiom nine times.

So the coarse version had false negatives and the loose version had false positives, and neither
count was trustworthy on its own. A checker for "is this guarded" has to model **every** guard
idiom the codebase actually uses - `try/except`, `contextlib.suppress`, `.isdigit()`, a helper like
`_safe_int` - and has to evaluate them at the call site rather than the function. That is a real
piece of work, which is worth knowing before treating any such sweep's output as a list of bugs.

The two genuine findings are fixed: the icon truncates to the column width, and unparseable ids are
dropped rather than failing the request.

### A filter validated before its result was believed

The three defect species this audit found need three different searches. Two are covered: coverage
data finds *never executed*, and AST filters on local properties find *bypasses a validator*. The
third - `VisitSuggestionRespondView`'s silent failure, where an error flag was set and one of two
exit paths returned without consulting it - is neither, and had been found only by reading.

So: a filter for that shape. A function that assigns an error-ish local (`blocked`, `failed`,
`refused`, ...), consults it in an `if`, and has a `return` *between* the assignment and the check -
an exit that cannot report what was recorded. Result across every controller: **zero**.

That number is only worth something because it was calibrated first. Running the same filter
against `git show b3cad024~1` - the version of `visit_suggestions.py` from immediately before the
chunk-230 fix - produces `CONTROL HIT: post flag=blocked 1 exit(s) before the check`. The filter
provably detects the shape it claims to look for, so its silence on the current tree is evidence
rather than an absence of evidence.

This is the discipline the rest of the session had to learn the hard way. Five sweeps produced
confidently wrong output before anyone noticed - the accessibility scan at 70% false positives, the
dead-code scan where every category over-reported, the escaping-helper table that was wrong twice,
and two generations of colour regex that each declared completeness. Every one was caught by
checking against a case whose answer was already known, and every one was caught *after* the result
had been written down. Doing it first costs one command.

### A control that proved detection but not precision

Next local property: a field assigned and then omitted from `save(update_fields=[...])`, which
Django silently drops. That is worse than a crash - the write appears to succeed and the value is
gone.

Following the previous chunk, the filter shipped with a control: a synthetic handler assigning
`name` and `description` and saving only `name`. It fired. Against the controllers it reported
**seven** hits.

All seven are false positives, and they share one cause: the filter pools every `obj.field = ...`
in a function and compares it against *each* `save()`, so mutually exclusive branches accuse each
other. `e2ee.py` is the clearest example and is entirely correct - `if password_wrapped:` assigns
three fields and saves all three, while `elif bundle.password_wrapped_secret:` assigns only
`password_wrap_stale` and saves exactly that. `pin_bulk.py`'s two saves are 46 lines apart in
unrelated operations.

The lesson refines the previous chunk's, and is worth stating because I got it half right. A
known-answer control proved the filter **detects** the shape. It said nothing about **precision**,
because it contained only a true positive. A control that also included a two-branch handler which
must *not* fire would have caught the branch-insensitivity before seven findings were written down
and triaged by hand.

So: calibrate with both a case that must fire and a case that must not. Detection and precision are
different properties, and a single positive control only ever measures the first.

No `update_fields` defects exist in the controllers - established, this time, with the filter's
limits understood rather than assumed.

### Four controls passed and the filter was still wrong

A sweep for `select_for_update()` outside a transaction - `TransactionManagementError`, a hard 500 -
carrying every check this audit had accumulated: a positive control that fired, a negative control
that stayed silent, a population of 34 real calls, and the knowledge that `ATOMIC_REQUESTS` is
unset. One hit: `child_pin_boundaries.py:50`, reached from Pin's `post_save`/`post_delete` signals.

The chain looked serious, and each link checked out. `select_for_update` outside a transaction does
raise - verified directly against the database, not assumed. `Pin._meta.parents` is empty, so
`Model.save_base` uses `mark_for_rollback_on_error` rather than opening a transaction. Django's
ordinary `TestCase` wraps every test in a transaction, so 10,758 passing tests could not have caught
it either way. A textbook invisible bug.

It is not a bug. `refit_child_pin_boundary` is decorated `@transaction.atomic`. A
`TransactionTestCase` - written to prove the failure before fixing it - passed both cases, which is
the only reason the reasoning got checked at all.

The cause is worth more than the non-finding: **the filter had a dead code path I wrote myself.** It
set an `_atomic_dec` flag on functions decorated with `@transaction.atomic`, and then never
consulted it - detection looked only at `with`-block nesting. Both synthetic controls used `with`
blocks, so the dead branch was never exercised, and all four checks passed over a filter that could
not see the single most common way this codebase opens a transaction.

So the discipline needs one more clause, and it is the hardest: **controls must cover every idiom
the real code uses, not the ones the filter's author thought to write.** A positive and a negative
control prove the logic you tested. They say nothing about the branch you forgot to wire up. The
only thing that caught this was refusing to fix on reasoning alone and writing the failing test
first - which then failed to fail.

The `TransactionTestCase` is kept: it pins the guarantee that the decorator provides, and fails if
anyone removes it.

### Duplicate-row risk: one traced to a deliberate design, 22 left as candidates

A sweep for querysets filtering across a multi-valued relation without `.distinct()`, which
silently returns a row per match. This one asked Django for the relation names rather than guessing
them - 260 multi-valued names straight from `_meta`, so the "which fields are multi-valued" half
cannot be wrong. Positive and negative controls both behaved. 23 candidates after discounting the
obvious non-issues (`exclude()` across a multi-valued relation compiles to a subquery and cannot
duplicate; `.exists()`/`.count()`/`.first()` collapse anyway).

**One traced end to end, and it is correct by design.** `pin/queryset.apply_label_groups` builds
`filter(labels__id__in=...)` chains with no `distinct()`, which duplicates a pin matching several
labels - but its only caller is `filter_by_criteria`, which ends `return qs.distinct()` and
documents it ("Filtered QuerySet (distinct)"). The `distinct()` is applied once at the public entry
point rather than by every intermediate method, which is the right shape: intermediate querysets
stay composable, and the collapse happens where the result is consumed.

**The other 22, traced in the following chunk**, resolve into four groups:

- **7 already collapse.** Five sit inside `filter_by_criteria` itself (which ends `.distinct()`),
  and `answer_stalled`/`vote_stalled` each call it directly.
- **2 cannot duplicate at all**, and this is worth knowing before anyone "fixes" them:
  `__isnull=True` across a multi-valued relation is a LEFT JOIN testing for the *absence* of
  related rows, so there is nothing to multiply by. `markup.unattached` and `reviews__isnull=True`
  are safe by construction, not by a `distinct()` somewhere.
- **3 carried a latent duplicate**, now fixed: `rated`, `rated_over` and `rated_under` return a pin
  once per matching review. They have **only test callers**, so nothing in production was wrong -
  but a queryset method whose contract is "pins rated over N" silently returning duplicates is a
  trap laid for the next caller, and `.distinct()` costs nothing here.
- **2 are dead**: `Pin.by_category` and `Wiki.by_category` have no callers anywhere - not in
  Python, templates or tests. Filed rather than deleted, since removing public queryset API is a
  judgement about intent.

The remaining sites are inside `filter_by_custom_fields` and `home_dashboard_context`, both single
-caller and both feeding a `distinct()` pipeline. They are listed as *candidates* in
`docs/PROBLEMS.md` - the pattern above suggests most will resolve the same way, and "suggests" is
doing real work in that sentence. Anyone picking this up should trace rather than assume, because
the failure mode is silent: a duplicated pin in a list or an inflated count, with no error anywhere.

### A magic string shadowing its own enum

Following the most interesting reading of the 70 no-production-caller queryset methods - that some
are logic which got reimplemented inline elsewhere, leaving the same rule in two places.
`VisitQuerySet.from_takeout` is that case, with a twist.

    def from_takeout(self):
        return self.filter(source="history")

Every other site in the codebase writes `source=VisitSource.HISTORY`. This one hardcodes the
literal, so the queryset method and the twenty-odd inline filters agree only by coincidence. The
value is currently `"history"`, so nothing is broken today; change `VisitSource.HISTORY` and this
method starts returning an empty queryset while every caller of the enum keeps working. Silent, and
exactly the failure the enum exists to prevent. `manual` had the same shape.

Both now use the enum, imported lazily inside the method because `model.py` imports
`VisitManager` from `queryset.py` - the dependency runs that way, so a module-level import would be
circular. That is the codebase's established pattern for this and the reason `CLAUDE.md` lists
`TYPE_CHECKING` guards among its common patterns.

Worth noting what the sweep actually bought here. `from_takeout` was flagged as "test callers only",
which is true and uninteresting on its own. Reading *why* it had no production callers is what
surfaced the literal - the inline filters are not calling it because they were written
independently, and one of them was written by me in chunk 209 while optimising the Takeout
importer. A count of unused methods is a prompt to go and read, not a finding.

**The obvious generalisation does not work, and is recorded rather than committed.** Sweeping for
every bare string literal matching a `TextChoices` value produced 37 hits and is unusable: the map
is keyed by *value* across all 214 enum members, so `kind="user"` on a `Label` resolves to
`VisitSource.USER`, and `status="active"` on a group chat resolves to
`BillingSubscriptionStatus.ACTIVE`. Same failure as the earlier field-name collision
(`description`/`icon` matching whichever model declared them first): a value alone does not say
which enum owns it. Doing this properly needs per-field resolution -
`Model._meta.get_field(name).choices` - to ask which enum *that field* actually uses. Left undone
rather than reported as 37 findings.

### A query in `__str__`, found while doing something else

Noticed while tracing the label-kind literals rather than by any sweep. `Pin.__str__` ran
`self.labels.filter(kind="status")` and read `effective_name`, which falls through to
`self.location.display_name` - up to two queries every time a pin was rendered as a string. It also
returned a five-line f-string.

Both halves matter for different reasons. The query is invisible to profiling in the usual sense:
it never appears as a slow query, only as a lot of fast ones, once per row in an admin list and
once per log line or error page mentioning a pin. `CLAUDE.md` already forbids `save()` in `__str__`
for the same family of reasons; a read is easier to miss because nothing is written. The multi-line
return is a smaller thing that shows up in exactly the places `__str__` exists to serve - a select
dropdown renders it as a paragraph, and line-oriented log tooling splits it into five records.

Now `self.name or f"Pin {self.pk}"`. Checked first that nothing asserts the old format and no
template renders `{{ pin }}`, so the change is contained. Four tests, of which `assertNumQueries(0)`
is the one that will actually catch a regression - the others document intent, that one enforces it.

Checked whether this was systemic: **zero** other models query in `__str__`. A local lapse, not a
convention, so no sweep was warranted.

### Halving the query cost of the map's pin payload

The last filed candidate, converted into a measurement rather than left as a hypothesis.
`Pin.to_json()` builds its payload with `self.labels.filter(kind=...)` twice. `.filter()` on a
prefetched many-to-many constructs a fresh queryset and **ignores the prefetch cache**, so a caller
doing `prefetch_related("labels")` still paid per pin.

Measured with `CaptureQueriesContext` over 1 and 5 pins:

| | 1 pin | 5 pins | per pin |
|---|---|---|---|
| before | 6 | 22 | **4** |
| after | 4 | 12 | **2** |

`list(self.labels.all())` once, filtered in Python. `.all()` reads the cache when one exists and
costs a single query when it does not, so it is strictly better in both cases - there is no
trade-off to weigh here, which is unusual enough to be worth saying.

Two queries per pin remain and are **not** labels; the debug log shows a rating fetch among them.
The test therefore asserts `<= 2 per pin` rather than demanding zero: it fails if anyone
reintroduces `.filter()` here, and keeps passing when the remainder is addressed. A test asserting
an ideal nobody has reached yet is a test the next person deletes.

Two things about how this was found are worth keeping. It came from **reading `to_json()` while
placing an import** - the fourth late finding to arrive that way, after the magic string, the
bulk-payload overflow and the `__str__` query. And the first measurement attempt failed on my own
fixture (`Location` is `unique_together(latitude, longitude)`, and the second batch restarted its
loop at zero), producing an `IntegrityError` that read exactly like a bug in the code under test.
Reporting that would have manufactured a defect out of a test bug - the mirror image of chunk 248,
where a real-looking chain turned out to be safe. Both come from trusting a result before
understanding why it happened.

### 250 queries to strip 5 labels from 50 pins (fixed)

Pointing the prefetch work at `external_api/`, where the real endpoints live - the previous chunk
established that the `models/*/viewset.py` layer this audit had been scanning is nearly unused.

The layer is generally careful: 48 `select_related` and 15 `prefetch_related` calls. Of seven
list-building loops, six use `.all()` - the form that reads a prefetch cache. The seventh,
`views_pin_bulk.py:239`, was:

    for pin in pins:
        present = [label for label in to_remove if pin.labels.filter(pk=label.pk).exists()]

`.filter().exists()` inside a comprehension, inside a loop over pins: **len(pins) x len(to_remove)**
queries. Removing 5 labels from 50 pins cost 250. Both verbs also bypass any prefetch the caller
supplied, so no upstream optimisation could rescue it.

Now one set of attached ids per pin, intersected in Python - `len(pins)` queries, or none when
labels are prefetched. Semantics are identical; `present` contains exactly the same labels in the
same order.

This is the third distinct instance of the same root cause in one day (`to_json`'s `.filter()`,
`rating`'s `.latest()`, and now `.exists()`), which is why the rule is worth stating as a rule
rather than as three fixes: **only `.all()` reads a `prefetch_related` cache.** Every other related-
manager verb issues SQL, and none of them look wrong at a glance.

### Why the write-side N+1 class is mostly not fixable here

Every N+1 sweep in this audit targeted *reads* - `.filter()`, `.count()`, `.exists()`, `.latest()`
on a related manager. A loop of `.update()` calls matches none of them, so the write side stayed
invisible until `LabelReorderView.post` turned up while reading an unrelated handler. That is a
genuine gap in how the problem was framed, not a gap in the scans.

Sweeping for it (controllers, writes inside loops, with a control): **19 sites, and only one
`.update()`** - the reorder handler already filed. The rest are `.save()` (9), `.delete()` (3),
`.create()` (3), `.get_or_create()` (2), `.update_or_create()` (1).

The distribution is the finding. In most codebases those `.save()` loops would be `bulk_update`
candidates; here they largely are not, because **this application depends on `post_save` signals
for correctness**, and every bulk operation skips them. The audit has now recorded three separate
instances of that trap:

- `labels/signals.py`'s ~46 seeding `get_or_create` calls per signup look like an obvious
  `bulk_create` - `Label` has `post_save` receivers syncing a taxonomy.
- The Takeout importer's per-row `PinVisit.objects.create()` looks like an obvious `bulk_create` -
  `achievements/signals.py` subscribes to `PinVisit` under `TRIGGER_VISIT`.
- `LabelReorderView`'s per-label `.update()` wants `bulk_update` - which also skips `post_save`,
  so an order-only change needs checking against `sync_redata_taxonomy_on_save` first.

So the write-side N+1 class is real but small, and each instance needs the signal question answered
before it can be collapsed. That is a design consequence worth stating plainly: signal-driven
correctness buys cohesion at the cost of making bulk operations unavailable, and anyone optimising
writes here will meet it every time.

---

## 3. Checked and clean

Recorded so this isn't repeated. Each was actively probed, not skimmed.

- **`CeleryTaskStatusView.get`** (untested per the coverage run, and clean). Probed for the obvious
  IDOR - can one user poll another's task by id? - and the docstring had already answered it:
  Celery's result backend has no per-task owner field, so a bare `LoginRequiredMixin` *would* allow
  cross-account polling; a per-task ownership check is impossible without a schema change; so the
  view is gated behind the same `dashboard.view_site_admin` permission that its only producer
  (`BackupStartView`) requires to enqueue in the first place. The reasoning names the hole, the
  rejected alternative, and why the chosen mitigation is equivalent.

  Several of this audit's "checked and clean" entries came out that way because a docstring
  encoded security reasoning that no amount of reading the code alone would recover - the WebSocket
  consumers' scope-before-lookup ordering, the ghost viewer's explicit `render()` inside the atomic
  block, the safety-chat token route's withheld location group, and this.

  **A heuristic was proposed from that and does not survive testing.** The claim was: "where such
  reasoning is absent is a better risk signal than the code's shape." Measuring explanatory lines
  (comments + docstring) against code lines for the handlers this audit examined gives the
  *opposite* of the prediction - 0.18 for the four where defects were found, 0.07 for the five that
  probed clean.

  The measurement is also invalid, for two reasons worth naming because both are easy to repeat:
  it counts comments **as they are now**, and comments were *added to the defect handlers while
  fixing them* - so the "defect" group's density is partly this audit's own writing. And it counts
  only *function*-level docstrings, so `CeleryTaskStatusView` - the very example that prompted the
  idea, whose reasoning lives in a **class** docstring - scored zero.

  So: an appealing heuristic, unsupported by the data, measured with an instrument that was wrong
  in two ways. The underlying observation stands (those docstrings are genuinely load-bearing and
  saved real time); the predictive claim built on it does not, and is withdrawn rather than left
  standing because it sounded right.

- **Interaction risk of the nine N+1 fixes** (2026-08-14). Checked, because two of them change what
  is *available* on an object rather than only what it costs: `Prefetch(..., to_attr="own_pins")`
  puts the filtered set on a new attribute, and dropping `prefetch_related("images")` in favour of
  `annotate(image_count=...)` removes a cache other code might have relied on. Both are
  self-contained - `label.own_pins` and `message.image_count` are the only readers of those
  relations in `export.py`, and the querysets are local variables inside their own functions, so
  nothing outside consumes them. The remaining seven fixes change how a relation is fetched, not
  what it returns. Worth stating because performance changes are the least test-covered category
  in this audit: a correctness fix ships with a regression test that fails if reverted, while a
  prefetch change is invisible to every test except the one query-count assertion added for
  `to_json`.

- **Other viewsets missing a prefetch** (2026-08-14). Zero - and the number is close to
  meaningless, which is the reason for recording it. The scan covers `models/*/viewset.py`
  paired with its `serializer.py`, and that population is **2**: one serializer has related
  fields (`Pin`'s, fixed the same day) and one viewset uses `prefetch_related` (the same one).
  The DRF viewset layer under `models/` is nearly unused; the real API surface is
  `dashboard/external_api/`, which has its own view classes and was **not** covered here. A
  reader should not take this entry as evidence that the API has no N+1 problems - it is evidence
  that this particular scan looked almost nowhere. The runtime instrument
  (`test_pin_to_json_prefetch.py`: capture queries over 1 and N objects, assert the per-object
  delta) is the one to point at `external_api/` views, and that has not been done.

- **Documentation cross-references** (2026-08-14). Every `docs/*.md` path this report actually
  links to resolves - `FEATURES.md`, `NOTES.md`, `PROBLEMS.md`, `designs/plugins.md`,
  `reports/2026-08-14-view-coverage.md`, `notes/mobile_app_requirements.md`. A first pass reported
  21 of 38 as broken, which was two instrument failures stacked: the scanner could not distinguish
  a *live link* from a path *quoted as an example of a broken link* (this report contains a section
  listing exactly those), and the attribution check that was meant to separate "mine" from
  "pre-existing" ran `git show main:docs/PROBLEMS.md` against a file that does not exist on `main` -
  so the baseline was empty and every path was attributed to this session by default. The second
  failure is the more instructive: an empty baseline does not error, it just silently makes
  everything look new.

- **Side effects inside transactions** (2026-08-14). **Zero** across 760 files in `controllers/`,
  `services/` and `models/`: no Celery enqueue, email send, channel broadcast or cache write sits
  inside a `transaction.atomic()` block where a rollback would leave it already done. This is the
  most thoroughly established negative result in the audit, and the four checks behind it are the
  point. **Detection**: a positive control (an enqueue inside `atomic()`) fires. **Precision**: a
  negative control (the same enqueue wrapped in `transaction.on_commit`) stays silent - the check
  the previous chunk's filter lacked. **Population**: the scan encounters **70** real `atomic()`
  blocks, so the silence is not vacuous. **Convention**: **33** `on_commit()` calls exist, so the
  correct pattern is not merely absent-by-accident - it is known and applied. A "zero" backed by
  only the first of those four is worth very little, which is roughly the story of five earlier
  sweeps in this audit.

- **Production frontend build, and bundle sizes** (2026-08-14). `bun run build` succeeds with every
  change from this audit - all entry points bundle, including `core.js` (which carries the
  `safeColor` work) and `organize.js`. Worth noting because the session had verified `tsc --noEmit`
  and the bun tests repeatedly but never a real build until now. Two bundles are large:
  `e2ee.js` at **1.59 MB** and `article-wysiwyg.js` at **1.20 MB**. `e2ee.js` appears in
  `themes/base.html`, which looked app-wide and is not - it sits behind
  `{% if e2ee_needs_oauth_enroll %}`, so it loads only for a passwordless account that has not yet
  enrolled message-encryption keys, plus the auth pages that need key derivation. Appropriate
  placement; the *size* is still worth a look (both are almost certainly a vendored library
  bundled whole), but nothing here loads megabytes onto pages that do not need them.
- **Unguarded index access** (2026-08-14). One candidate,
  `request.META.get("HTTP_HOST", "").split(":")[0]`, and it is safe: `str.split()` always returns
  at least one element, so `[0]` cannot raise. No real `IndexError` surface in the controllers.

- **JSON body parsing** (2026-08-14). Zero unguarded `json.loads(request.body)` calls in any
  controller - every one sits inside a handler catching `JSONDecodeError`/`ValueError`, so
  malformed JSON produces a 400 rather than a 500. Worth recording both as a clean result and
  because this sweep was the first written *after* the chunk-242 lesson: it models `try/except`
  **and** `with contextlib.suppress(...)`, and evaluates protection per call site rather than per
  function. Had it been written the way the numeric sweep was, it would have produced the same
  mixture of false negatives and false positives.

- **`CalendarImportView.post`** (30 statements, never executed - 8th on the coverage list). Clean.
  The interesting surface is `invite_profile_ids`, which arrives as raw profile ids from the
  client and results in trip invitations - the shape an IDOR usually takes. It is scoped:
  `invite_members` intersects the submitted ids with `get_connections(inviter)` and silently drops
  anything that is not an accepted friend, then caps the result at `max_trip_members`. The import
  itself re-fetches each event from Google rather than trusting submitted content ("the client
  submits ids, never event content"), and auth-expiry and gateway failure are handled as distinct
  502s with different messages.

- **`ConsensusPhotoUploadView.post`** (31 statements, never executed - 7th on the coverage list).
  Clean, and it retroactively verifies an earlier change in this audit. The handler checks
  participation *and* `is_joined` before anything else, scopes the round to the session, validates
  the upload, dedupes by checksum with a 409, and takes the quota check inside
  `per_profile_upload_lock` rather than outside it. The malware scan runs synchronously here -
  `image_upload_error`'s `skip_malware_scan` defaults to `False`. That last point matters because
  chunk 211 *widened* the cases in which `malware_error_for_upload` raises: at the time only its
  three direct callers were checked, and this is an indirect one. Traced now -
  `image_upload_error` catches `MalwareScanUnavailableError` and returns a 503 "try again shortly",
  so an unscannable upload on this route is refused with a readable message rather than admitted.
  The change propagates correctly.

- **`AlbumEditView.post`** (31 statements, never executed - 6th entry on the coverage list).
  Probed and clean, which is worth recording because it calibrates that list: untested does not
  mean broken. Authorization is correct on both routes and the split is documented - pin-scoped
  requires ownership (`profile__user=request.user`), wiki-scoped goes through
  `resolve_visible_wiki`, matching the shared wiki-editing model. `cover_image_id` is deliberately
  re-scoped through the album's own contents "so a foreign image id can't be pinned as a cover".
  Each field is applied only when present, so a partial form cannot blank the rest, and both name
  and description are length-checked. The one nit: an unrecognised `kind` is silently dropped
  rather than refused, unlike the name/description paths which return a 400 - not a defect, but
  inconsistent with its neighbours.

- **`docs/FEATURES.md` accuracy** (2026-08-14). Checked because `CLAUDE.md` instructs every agent
  to consult it *before* assuming a feature is missing, so staleness here causes duplicated work
  rather than mere confusion. Cross-referenced all 79 controllers against it. The naive
  name-match said 23 were undocumented; checking each against the vocabulary the document actually
  uses (`two_factor` -> "TOTP", `webauthn` -> "passkeys", `pin_restructure` -> "detail pins", and
  so on) reduced that to **two**: `/health/` and `/thanks/`, both now added. The document is
  otherwise accurate and current - a genuinely well-maintained inventory, and worth trusting.

- **Dead weight: templates, SCSS, TypeScript, routes** (2026-08-14). Essentially none, and the
  negative result is worth as much as the scans, because every one of them over-reported and the
  reasons generalise. **Templates**: 7 of 418 are unreferenced by name, and all 7 are framework
  conventions Django or django-oauth-toolkit resolve internally (`403.html`, the five
  `registration/` password-reset templates, `oauth2_provider/authorize.html`) - deleting any of
  them silently breaks a flow. **SCSS**: 71 files, 0 unimported. **TypeScript**: 6 of 65 look
  unimported and none are - `testing/dom-setup.ts` is a `bunfig.toml` preload,
  `entries-classic/*.ts` are built by a directory glob in `bin/build-frontend.ts`, `globals.d.ts`
  is ambient types, and `tools/generate-e2ee-fixture.ts` is run directly. **Routes**: 61 of 753
  named routes are never referenced outside `urls.py`; 30 are assembled at runtime by
  `reverse(f"{prefix}.{suffix}")` (15 call sites, suffixes `add`/`delete`/`detail`/`edit`/
  `remove`/`reorder`/`upload`), 34 are in `external_api/urls.py` where the callers are API clients
  rather than this codebase, and of the 10 left over one is Django's own
  `password_reset_complete`. The residual nine are listed in `docs/PROBLEMS.md` as *candidates for
  review*, not as dead code - this codebase reaches things dynamically often enough that "no
  static reference" is weak evidence on its own.

- **Injection, deserialization, and time correctness** (whole-tree static sweeps, 2026-08-14).
  All negative, recorded so the sweeps are not repeated. **Raw SQL**: four sites outside
  tests/migrations, every one interpolating *identifiers* while parameterizing values. The
  `# nosec` comments claim the identifiers come from Django's model registry and that was verified
  rather than taken at face value - `immich/model.py` reads `_meta.db_table` and
  `_meta.get_field("profile").column` inline, and `rotate_field_encryption` receives them as
  arguments, so the call chain was followed to line 58 where they are built from `meta.db_table`
  / `pk_field.column` / `field.column` while iterating `EncryptedTextField` instances. No user
  input reaches an identifier. **Deserialization**: zero `eval`, `exec`, `pickle.loads`,
  `yaml.load`, or `marshal`; Celery is JSON-only across task, result and accept-content. Django's
  built-in `RedisCache` does pickle its values, which matters only if the cache is reachable - and
  the compose file has exactly one host port binding in the whole stack (nginx), so Postgres,
  Valkey and ClamAV are internal-only. **Time**: `USE_TZ = True` with zero naive `datetime.now()`
  / `utcnow()` / `today()` calls outside tests and migrations. **Mutable default arguments**: zero
  (ruff's B006 is enforcing it). **`assert` outside tests**: zero, so nothing validates via a
  statement that `python -O` would strip.

- **Middleware** (`dashboard/middleware.py` - `ProfilePreviewMiddleware`, the stack's only custom
  entry). Impersonation code is where privilege escalation lives, so this was probed for it and
  does not have it. The ghost viewer always has *less* access than the owner, never more: it is a
  freshly created throwaway user standing in a chosen relationship, so the worst a spoofed
  `Referer` (the one client-controlled input, used to bring HTMX fragments into preview scope) can
  achieve is running one of the owner's own GET endpoints with fewer privileges than they already
  have, inside a transaction that is force-rolled-back. Writes are refused outright - non-GET in
  scope returns 403. Identity is re-checked against `state["owner_id"]` on every request rather
  than trusted from the session alone, so a stale preview state cannot outlive the session it
  belongs to. `TemplateResponse.render()` is called explicitly inside the atomic block, because a
  lazily-rendered response would evaluate its querysets after the ghost's rows were gone.
  Side-effect escape from the rollback was the specific thing checked, since cache writes, emails
  and Celery enqueues are not transactional: the only non-trivial receiver on the path is
  `promote_first_user_if_needed`, which returns `False` as soon as any other user exists - always
  true during a preview, since the owner is one - and everything else the path touches is ordinary
  DB rows. Worth knowing for anyone reasoning about this: Postgres sequences are non-transactional,
  so the ghost's `id` is consumed and never reissued, and a cache entry accidentally keyed on it
  can never later collide with a real profile.

- **WebSocket consumers** (`dashboard/consumers.py`, 1332 lines; `routing.py`; the ASGI stack).
  Probed for the failure modes this layer usually has, and it has none of them. Origin checking is
  on (`AllowedHostsOriginValidator` wraps the whole websocket router). The seven routes split into
  three authorization models and each is enforced: `_ParticipantSessionConsumer` confirms
  participation in the specific session before joining its group, so the `<int:session_id>` routes
  are not an IDOR; `SafetyCheckinChatConsumer`'s token route treats the magic-link token itself as
  the authorization and deliberately withholds the live-location group from it, which the session
  route (owner or accepted partner) joins. API-key scope is checked *before* the participant
  lookup, with the reason written down - a refused connection must not be able to distinguish a
  real session from an invented one by how long the refusal takes. Group membership added before a
  failing `accept()` is unwound explicitly, because Channels only reliably fires `disconnect()` for
  connections that reached `accept()`. Permission is re-validated on a timer rather than only at
  connect, specifically so a dropped `*_access_revoked` broadcast cannot leave a revoked contact's
  chat open indefinitely. Three of the four `receive()` methods parse JSON inside a
  `JSONDecodeError`/`TypeError` guard, and the fourth takes no JSON; binary frames are accepted and
  ignored rather than raising. Write-scoped operations require a `*_WRITE` scope separately from
  the read scope that permitted the connection. The only gap found is volume, not access - no rate
  limit or frame-size cap - filed in `docs/PROBLEMS.md`.

- **Authorization** *(inherited claim - see the note below; not re-verified in the 2026-08-13
  session)*: 167 owner-scoped routes × GET/POST/DELETE/anonymous, plus nested-child
  smuggling — no leaks beyond the cover-photo bug above. Group chats route every view through one
  `_get_group()` → `active_memberships()` helper; group history is windowed per membership stint so
  a re-added member can't back-read.
- **WebSocket consumers** (7 total, enumerated by AST rather than by grep): every one authenticates
  in `connect()` and every one revalidates its credential on a timer — 4 via
  `CredentialScopeMixin.start_credential_revalidation`, `SafetyCheckinChatConsumer` via its own loop
  (deliberately, since it must also re-check partner status; the two would otherwise be duplicate
  timers), and the 3 game consumers by inheritance from `_ParticipantSessionConsumer`. This pairing
  matters because a socket authenticates once at connect: without it, revoking a leaked key blocks
  the next HTTP call while the open socket keeps streaming. `SafetyCheckinChatConsumer` looked like
  an outlier (no auth markers in `connect()`) but delegates to `_resolve()`, and its docstring's
  claim of a per-message re-check is real — it lives in `_create_message`, not `receive`.
- **XSS surface**: all 12 `|safe` template sites plus the single `mark_safe` traced to origin.
  *(Re-verified 2026-08-13: the first pass filtered 4 of the 12 out by pattern as "obviously safe"
  rather than tracing them, which is the same silent-sample error found elsewhere in this report. All
  four check out - three are JSON blobs built by `safe_json_for_script` (`filter_labels_json`,
  `tags_data_json`, `common_pins_json`), and the fourth, `button.spec.extra_html`, has exactly one
  assignment in the codebase: a hardcoded literal in `templatetags/map_components.py`. No plugin
  or service sets it, so despite looking like a plugin-extensibility hook it carries no external
  data.)* User
  -authored HTML (article bodies, visit notes) goes through `nh3.clean` against an explicit tag and
  attribute allowlist; Wikipedia extracts through the same, with the plain-text fallback branch
  `escape()`d precisely because it is rendered with `|safe` downstream; chart payloads through
  `safe_json_for_script`, which translates `<`, `>`, `&` so a label cannot close the enclosing
  `<script>`; the lone `mark_safe` wraps `urlize(..., autoescape=True)`. No gaps.
- **Bulk writes vs `save()`-derived fields**: 12 models derive or sanitize their own fields inside
  `save()` (slugs, `sanitize_name`, `normalized_email`, `Place.domain_root_id`,
  `TriviaQuestion.answer_normalized`). Cross-referenced against all 19 `bulk_create`/`bulk_update`
  sites and all 8 `queryset.update()` calls writing one of those fields: no bypass. The
  slug-bearing models are never bulk-created; `Label.bulk_update` touches only icon/color/
  description/order *and* explicitly re-invalidates the pin cache that the skipped `post_save`
  would have; `tasks.py`'s `Wiki.name` compare-and-set calls `sanitize_name` itself with a comment
  naming the bypass. The codebase had already internalised this class.
- **Signal hygiene**: all 50 `@receiver`s and every `.connect()` pass `dispatch_uid`; zero `save()`
  calls inside a `pre_save`/`post_save` handler or `__str__`. Both are stated CLAUDE.md rules and
  both hold.
- **The `add` template-filter trap** (`"prefix-"|add:obj.id` silently yields `''`): all 38 `|add:`
  uses checked. Every string concatenation takes a `|stringformat`-produced operand; the rest are
  int+int arithmetic or list+list. Several sites carry a comment citing the failure.
- **Work inside transactions**: zero external/network calls inside any `transaction.atomic()` block,
  and zero Celery enqueues inside one without `on_commit` — checked directly, via the
  `@transaction.atomic` decorator, and one level indirect (helpers that enqueue, called from inside
  someone else's atomic block). `ATOMIC_REQUESTS` is off, so views are not implicitly wrapped. The
  indirect pass initially reported 69 hits; all were false positives from matching bare method
  names, where ORM `.objects.create()`/`.get()` collided with service functions named `create`/`get`.
  Re-run resolving calls through each module's imports: zero.
- **`LabelledModel`** (added this audit): re-derived the query cost rather than trusting the
  docstring. Unprefetched, three accessors cost three queries - the same as the three filtered
  queries they replaced; prefetched, zero. The claim that the no-prefetch case is unchanged holds.
- **Celery beat and queue routing** (verified at runtime, not by reading config): all **24** beat
  entries name a task that is actually registered (checked against `app.tasks` after
  `import_default_modules()`). Routing is sound end to end: `task_default_queue` is `celery` with no
  `task_routes` or `task_queues` overrides; `celery-worker` runs without `-Q` so it consumes
  `celery`; `celery-worker-panels` consumes `panel_fetch`; and the only two queue values any task
  requests are `panel_fetch` (the `PanelSource` default) and `celery` (two explicit overrides, for
  CPU-heavy sources that must not occupy the 20-thread panel pool). Nothing is dispatched to a queue
  no worker consumes - the failure mode where a task simply sits in the broker forever, without
  erroring anywhere. The beat-lock TTL invariant already carries a completeness arm plus a
  non-vacuity guard, so it maintains itself.
- **SQL injection surface**: every raw-SQL entry point enumerated by AST - 8 `cursor.execute`, 8
  `RunSQL`, and zero `.raw()`/`.extra()`/`RawSQL`. All 8 `RunSQL` are static literals. Seven
  `cursor.execute` calls interpolate **identifiers** (table/column names) while parameterising every
  *value* with `%s`; each identifier traces to Django's model registry (`apps.get_models()` ->
  `_meta.db_table`/`field.column`) or to hardcoded literals passed by the caller, never to request
  data. Each already carries a `# noqa: S608 # nosec B608` naming that reasoning.
- **Upload path traversal**: verified empirically at both layers rather than from Django internals.
  All but one `upload_to` is a static prefix; the callable (`pin_image_upload_path`) interpolates the
  uploaded filename's stem. `UploadedFile.name` basename-strips before the callable ever sees it
  (`../../etc/passwd` -> `passwd`, `a/b/c.jpg` -> `c.jpg`), and `validate_file_name` independently
  raises `SuspiciousFileOperation` on any surviving `..`. URL-encoded traversal (`..%2f..%2f`) is
  neutralised by `get_valid_name`. Calling `generate_filename` directly *looks* like it preserves
  subdirectories - that path just is not reachable from an upload, which is why the first layer had
  to be checked rather than assumed.
- **Timezone handling** (read-only sweep, counted before analysed): zero `datetime.now()` and zero
  `utcnow()` outside tests; all four `datetime.fromtimestamp()` calls pass `tz=UTC`; the nine
  `.replace(tzinfo=...)` sites split into deliberate tz-stripping for naive comparison (trips, admin
  chart keys), correct attachment of a *parsed* offset (`my_activity.py`), and two form-input paths.
  Of those two, `safety.py` guards on `tzinfo is None` and `visits.py` does not - safe only because
  its ISO string is built from separate date/time inputs that cannot carry an offset. Folded into the
  existing `date.today()` entry in `PROBLEMS.md`, since all of it rests on the same single
  dependency: `TIME_ZONE = "UTC"` with no `timezone.activate()` anywhere.
- **Email surface** (header injection and spam relay): no raw SMTP sending anywhere - all 19
  `smtplib` references are `except (smtplib.SMTPException, OSError)` around Django's backend, which
  is easy to misread as 19 hand-rolled senders. Every send goes through `EmailMultiAlternatives` or
  `send_mail`, so Django's `forbid_multi_line_headers` blocks header injection by construction.
  Recipients classified by source: admin-configured (`notify_admin_email`), the user's own verified
  address, or arbitrary user-supplied. **Corrected 2026-08-13:** this originally said "only three
  paths take arbitrary addresses, and each is bounded", which was wrong - the pass read three of the
  nine recipient expressions and reported on all of them. A fourth path exists and is *unbounded*:
  secondary-email verification (`controllers/userprofile.py`) mails any address a user types, with no
  rate limit, no resend cooldown and no cap on addresses per profile. Filed separately. The three
  originally examined are bounded - friend invites and visit invites both call
  `email_rate_limit_error` + `has_sent_join_email` + `record_email_sent` (per-profile hourly/daily/
  monthly caps, and at most one join email per address ever), and safety-contact alerts are capped
  per check-in by `max_safety_checkin_contacts`, carry a `SafetyContactOptOut` check on the send
  path, and fire on a schedule rather than on demand.
- **Django's own deployment audit** (`manage.py check --deploy`), run under *production* settings
  rather than the container's defaults: exactly **one** security warning, `W021`
  (`SECURE_HSTS_PRELOAD` not set), and the settings comment already explains that as deliberate -
  preload submission is the domain owner's decision and is painful to reverse on a self-hosted
  project. Everything else is set: `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS` (a year, with subdomains), `SECURE_PROXY_SSL_HEADER`,
  `X_FRAME_OPTIONS`. Worth recording *how* this was run: the naive invocation reports three security
  warnings (no HSTS, no SSL redirect, `DEBUG=True`), all of which are artifacts of `TESTING` being
  true in the test container - the settings gate each of them on `not TESTING`. Reporting that run
  would have produced three false findings about production configuration. The remaining 237 issues
  are drf-spectacular schema-naming warnings, filed separately in `PROBLEMS.md`.
- **Dead templates and bundles**: all **419** templates checked for any reference by full path or
  basename across every `.py` and `.html` in the package; all 10 TypeScript entry bundles checked
  against templates, Python and `package.json`. One genuinely dead file found and removed
  (`partials/trips/_plan_hero_suffix.html` - 1 byte, committed in `3f12e875`, and the string
  `hero_suffix` appears **zero** times anywhere, so nothing passes it to `_page_hero.html`'s
  `title_suffix_template` variable). Zero unused bundles.

  The first run of this reported **9** dead templates, and acting on it would have deleted live
  files. Two separate flaws: it scanned only `dashboard/**/*.py`, missing `UrbanLens/urls.py` which
  renders `errors/500.html`; and it had no notion of templates Django resolves *by convention*
  (`403.html`, and the six `registration/*` password-reset templates that `PasswordResetView` and
  friends look up by default name), which cannot appear in project source at all. Both flaws
  produce confident, deletable-looking false positives - and this is the one sweep in this audit
  whose output would have been acted on destructively.
- **Template and route cross-references** (the same "does the tree contain what it references?"
  question, applied beyond the commit): all `{% url %}` names in all 418 templates resolve - 1001
  registered route names, **zero** unresolvable, checked against the live resolver including
  namespaced entries rather than by grepping `urls.py`. All literal `{% include %}`/`{% extends %}`
  targets resolve too; 4 use a variable target and cannot be checked statically, which is recorded
  rather than guessed at. Neither check was added to the pre-commit hook: both found nothing, and
  the import/template guard that *was* added exists because a real failure motivated it. A hook that
  has never caught anything is maintenance cost and false-positive risk with no evidence behind it.
- **`{% static %}` references**: every literal reference in all 418 templates resolves against the
  configured `STATICFILES_DIRS` - zero missing, one dynamic argument that cannot be checked
  statically. This is worth more than a 404 check: production uses
  `whitenoise.CompressedManifestStaticFilesStorage`, so a missing entry is a **render-time
  `ValueError` and a 500**, not a broken image - and the test suite deliberately falls back to plain
  `StaticFilesStorage` (no manifest), so no test can ever catch one. Instrument checked both ways
  before trusting the result: a fabricated path is correctly unresolvable, and the availability set
  genuinely contains the compiled `dashboard/style.css` rather than being empty.
- **`settings.X` references**: every attribute read through `django.conf.settings` resolves - 17
  distinct settings (`SECRET_KEY`, `SITE_URL`, `MEDIA_ROOT`, `OAUTH2_PROVIDER`,
  `CELERY_BEAT_SCHEDULE`, the `SECURE_*`/`SESSION_*`/`CSRF_*` family,
  `UL_MAP_SHARE_ZOOM_THRESHOLD`), **zero** undefined. An undefined one is an `AttributeError` on
  whatever rare path reads it, so it survives exactly as long as that path is untested.
- **Celery dispatch arity**: every `safely_enqueue_task` call site checked against the target task's
  real `inspect.signature` - **68** sites resolved to one of the 84 registered tasks, zero mismatches
  in positional count or keyword names. This matters because the failure is asymmetric: a
  wrong-arity dispatch succeeds at the call site (the broker accepts anything) and raises inside the
  worker, so the request that triggered it returns 200 and the error lands in a worker log. Five
  sites pass the task as a variable rather than a literal and are not statically resolvable; that is
  recorded rather than counted as clean. Rule validated against real bounded tasks before the result
  was trusted - `fetch_panel_source` (2..3 positional) and `resolve_deferred_pin_locations` (2..7)
  each correctly flag too many positional, too few, and an unknown keyword. The first probe attempt
  picked a Celery built-in taking `*args`, which could not fire and demonstrated nothing.
- **Model validation that never runs**: Django's `save()` does not call `full_clean()`, so a model's
  `clean()` and its fields' `validators` are enforced only where something invokes them. Checked
  both halves. Only two models define `clean()` (`SubscriptionRole`, `Achievement`) and both are
  enforced on *every* write path - each model is written from exactly one admin controller, and both
  of those funnel through a helper ending in `full_clean()`. For field validators the exposure is
  larger on paper (63 `validators=` on models, **zero** on forms or serializers, and only one
  `ModelSerializer` in the external API - so DRF does not re-derive them for hand-written
  serializers), but the project addresses it deliberately: `services/core/text_limits.py` defines 18
  shared limits, sets each as the field's `max_length` so Forms/DRF pick up a `MaxLengthValidator`
  automatically, *and* exposes `text_length_error()` for controller paths that build models directly
  and never run one. All 18 constants are referenced outside that module and the helper has 44 call
  sites, so both tracks are genuinely in use rather than aspirational.
- **N+1 inside Celery tasks**: the route-scaling sweep covers web endpoints only, and a task's N+1
  has no user-facing symptom - just a slower worker - so `tasks.py` was checked separately for loops
  over an unprefetched queryset that dereference a relation. Five candidates, all fine.
  `upgrade_placeholder_pin_names` reads `pin.location.display_name` per pin, but
  `with_placeholder_names()` already does `select_related("location__wiki")` *inside the queryset
  method*, which a static check on the call site cannot see. The four `sweep_stalled_*` tasks issue
  one `session.rounds.filter(...)` per session, which prefetching genuinely cannot fix (chaining
  `.filter()` onto a related manager defeats it - the same trap fixed in `LabelledModel` earlier in
  this audit), but they iterate only sessions abandoned mid-round past a timeout, and
  `stalled()`'s own docstring establishes at most one unrevealed round per session. Bounded by
  design rather than by luck.
- **`select_for_update` correctness**: all **30** calls sit inside a `with transaction.atomic()` or an
  `@transaction.atomic` function - outside one, Django raises `TransactionManagementError` at
  runtime, on whichever contended path happens to reach it first. The detector was self-tested on
  four constructed cases (bare call, `with` block, decorator, nested helper) before its zero was
  believed. Also checked: no site combines `select_for_update()` with `select_related()` on a
  nullable FK, which Postgres rejects outright ("FOR UPDATE cannot be applied to the nullable side of
  an outer join"), and every call uses the plain blocking form - no `nowait=True` or
  `skip_locked=True`, so none can silently skip rows it was meant to lock.
- **Undo restore vs unique constraints**: every handler's restored field list cross-referenced
  against its model's real unique fields (single-field, `unique_together`, and constraint
  `fields`/expressions). Of the 8 handlers, 5 restore nothing unique - `pin`, `pin_list` and `trip`
  deliberately exclude `slug` and let it regenerate on save, which their own comments name. The 3
  that do restore a unique field all refuse gracefully: `pin_list` and `saved_filter` were already
  raising `UndoExpiredError` when the name has been reused since the delete, and `label` now does
  too. Worth noting where that fix came from: both existing handlers describe the refusal as "the
  same contract every other handler follows", so the convention predates this audit - `label` was the
  one handler breaking it, with a docstring asserting "Nothing else can block: `Label` has no unique
  constraints". Adding the constraint made that sentence false, and the fix was to join the existing
  convention rather than invent one.
- **External API writes vs unique constraints**: the external surface uses hand-written serializers
  (one `ModelSerializer` in the whole module), so DRF adds **no** automatic `UniqueTogetherValidator`
  - a create that reaches a constraint is an uncaught `IntegrityError` and a 500. Three
  `.objects.create()` sites target a model with a user-facing unique constraint, and all three
  pre-check: `SavedFilter` via `name_taken_for` (400), `CustomField` via
  `filter(profile, entity_type, name__iexact).exists()` (400), and `Label` via the 409 added by this
  audit. Worth stating which direction is dangerous: a check *stricter* than the constraint is safe
  (it refuses things the database would accept - `CustomField`'s `iexact` against a case-sensitive
  constraint is exactly that, and deliberate). A check *looser* than the constraint is the bug, and
  is what `Label` had before this audit: the constraint was case-insensitive while every caller
  matched exactly.
- **HTML controller writes vs unique constraints**: the same check as the external API, across the
  session-authenticated surface. **23** `.objects.create()` calls target a model with a user-facing
  unique constraint. **22 are guarded; one is not** - `pin_edit.py`'s detach branch, which is the
  guaranteed 500 recorded below.

  *This entry originally claimed all 23 were guarded, and that was an overclaim worth naming.* The
  triage script hand-listed **14** sites to inspect, not 23, and the write-up reported the conclusion
  as if it covered the full set. The detach bug sits in one of the 9 that were never examined. Those
  9 were checked afterwards: `EmailVerification` x2 (safe by construction - the user is created in
  the same request, so no prior row exists), `Wiki` (guarded by `_location_for_child_wiki`, which
  raises before the create), `MessagingKeyBundle`/`ConversationKey`/`GroupKey` (all catch
  `IntegrityError` and return 409), `MarkupMapShare`, and the two bare `Location` creates - of which
  `maps.py`'s is safe from `visits.py` (which checks `get_for_point` first) and broken from
  `pin_edit.py`'s detach, which passes coordinates that provably already have a Location.

  The guarded 22 use three different mechanisms, which is why a single pattern-match cannot audit
  this:
  1. a pre-check before the create (most sites - `PinList`, `SavedFilter`, `CustomField`, `Album`,
     `PinOwner`, `PinShare`, `ProfileEmail`, `Label`);
  2. `except IntegrityError` returning 409, wrapped in its own `transaction.atomic()` so the failed
     insert cannot poison the outer transaction (`aliases.py`);
  3. a guard inside a *called* function - `resolve_child_pin_location` raises `PinCreationError`
     before `detail_pins.py` ever reaches its create, and the controller turns that into a 400.
  That third one also documents something the constraint sweep alone gets wrong: child pins are
  *deliberately exempt* from `db_pin_unique_location_per_profile`, with the narrower exact-point rule
  enforced in Python instead, because a child pin must be able to share a parcel with its parent.

  Method note: the heuristic (scan the 22 lines *before* each create for a guard pattern) reported 2
  of the 14 it looked at as unguarded. Both were false - one guard sits *after* the create in an
  `except` clause, the other inside a called function; a backwards-looking window sees neither. The
  more costly error was not the false positives but the silent sample: hand-listing a subset and then
  reporting on "the 23" turned an incomplete check into a clean bill of health, and the one real bug
  in the set was in the unexamined remainder. A sample is a sample even when the number next to it is
  the population.
- **Management commands** (7, none previously swept - they run with full database access outside
  every web test): each checked individually rather than sampled.
  `delete_low_engagement_wikis` is **report-only by default** and needs `--yes` to delete, which is a
  stronger design than an opt-in `--dry-run`; its counts use `distinct=True` on both annotations with
  a comment about cross-product fan-out - exactly the bug class that once made it delete every wiki.
  `rotate_field_encryption` has `--dry-run`, wraps the rewrite in `transaction.atomic()`, and
  deliberately opens no transaction during a dry run so a rollback cannot mark an enclosing (or test)
  transaction. Its `CommandError` on undecryptable values fires *outside* the atomic block, so partial
  progress commits - correct here, because `EncryptedTextField` reads under any fallback key, so a
  mixed state is valid and re-running finishes the job; the error text says exactly that and warns
  against dropping a key early. Both backfills that write have `--dry-run`.
  `provision_mobile_oauth_client` uses `update_or_create`, so re-running cannot mint a duplicate
  OAuth client. `diagnose_places_api` is read-only.

  One gap, minor: `backfill_redata_labels` iterates **every profile** and pushes to the REData API
  with no `--dry-run` and no preview of scale - the only command whose side effects are entirely
  external, which is also why a grep for local writes reports it as harmless. Not filed as a defect
  (the sync is idempotent and nothing is destroyed) but worth knowing before running it on a large
  instance.
- **Inline handlers calling undefined globals**: every `window.<symbol>()` invoked from an
  `onclick`/`onchange`/`onsubmit` attribute in all 418 templates - 31 distinct symbols - resolves to
  a definition. None is a permanently dead click. (The guarded-vs-unguarded split, 12 to 40, is a
  style difference rather than a defect: an unguarded call fails only when the bundle is absent, in
  which case the page is broken regardless, and several "unguarded" matches are assignments like
  `window._safetySkipLeaveWarning = true`, which cannot throw.)

  This check produced one apparent finding - `window._coverHeroNav`, called from `location/wiki.html`
  and seemingly defined nowhere. It is defined, in `frontend/static/js/cover-hero.js`: the scan
  covered `frontend/ts/**` and `templates/**`, and this project also ships five hand-written JS files
  under `frontend/static/js/**`. That directory was outside the previous two frontend sweeps as well;
  re-checking it found their `JSON.parse` and `!resp.ok` handling sound, plus one further unhandled
  `fetch` now added to the existing filing. Third scope error of this kind in the audit, and the
  cheapest tell each time was a "missing" symbol that the codebase plainly uses.
- **External API scopes**: AST audit resolving `required_scopes_by_method` through base classes for
  all `ExternalApiView` subclasses — **191** at the time of that pass, **197** now — zero gaps. `credential_grants` genuinely fails closed.
  Scoped to *endpoint-level* declarations: the per-entry secondary maps some views layer on top
  are a separate mechanism, and one of those (undo) did have a gap — see above.
- **E2EE / messaging**: group key rotation is signalled by comparing envelope sets to live
  membership; `group_member_token` is HMAC-scoped per group so it can't correlate members or be
  enumerated; DM retention correctly excludes `NEVER` and unread.
- **2FA**: constant-time TOTP compare, atomic step claiming (replay-proof), hashed backup codes,
  rate-limited code entry, and lockout keys that don't leak account existence.
- **Import archives**: bounded chunked reads against *actual* decompressed bytes (not the forgeable
  `file_size` header), symlink entries skipped, zip-slip guard that handles the sibling-prefix case,
  member cap, then malware + magic-byte scanning before any importer opens a file.
- **Undo framework**: 137 fields across 8 handlers round-trip intact; restore runs inside
  `transaction.atomic`.
- **Synchronous outbound calls**: instrumented the gateway chokepoint and walked 15 page endpoints —
  **0** call out inline. Panel data goes through Celery. (The exception is write paths that create a
  `Location`; see `PROBLEMS.md`.)
- **Encrypted fields**: key derivation, fallback ordering, and rotation reviewed; `ProfileEmail`
  correctly keeps a plaintext `normalized_email` for matching since Fernet ciphertext can't be
  queried.
- **Lint exclusions**: `settings/`, `tests/`, `migrations/` are excluded from ruff but hide nothing
  — two unused imports total.
- **Every documented template trap in `templates/CLAUDE.md`**, re-verified independently across all
  418 templates late in the audit: zero multi-line `{# #}` comments (Django's is single-line, so an
  unterminated one renders to the page), zero `next_page_number()` calls in a template with no
  `has_next` branch, and all 19 `"prefix-"|add:<id>` sites correctly pre-stringified with
  `{% with x_str=obj.id|stringformat:"d" %}` on the preceding line — the fix is applied by
  convention through `*_str` context variables rather than inline, which a same-line scan reads as
  19 violations. (each produces a user-visible bug when
  violated, and none is currently violated):
  - no multi-line `{# … #}` comments anywhere (Django only treats them as comments on one line, so
    the text would render to the user);
  - all five `page_obj.next_page_number` uses sit inside `{% if page_obj.has_next %}`, so none can
    raise `EmptyPage` on the last page;
  - `_pagination_controls.html`'s `request.path` assumption holds even for `_photo_gallery.html`,
    which *is* rendered from two modules (`image_gallery.py`, `safety.py`) — because all three
    render sites return it as a standalone response from its own endpoint, never embedded in a
    page, so `request.path` is always the partial's own URL;
  - `_page_hero.html`: every page that can receive an out-of-band hero swap passes `id=`, none
    wraps the include in a div, and the OOB variants are rendered from Python
    (`_pin_hero_oob`, `_trip_hero_oob`) against matching ids.
- **Account deletion**: the mirror of the export question, and this one *is* complete. Of 147 FKs
  to `Profile`/`User`, 118 are `CASCADE` and 29 `SET_NULL`. Empirically verified rather than read:
  seeded a profile with 54 rows across 8 models, ran `hard_delete_profile`, and **0 rows survived**
  (User row gone too). Every `SET_NULL` case was reviewed and is deliberate — either data owned by
  *another* user (someone else's emergency-contact entry), or shared/community content where only
  the attribution should disappear (`Wiki`, `WikiEdit`, `Article` revisions, `TripComment`,
  `GroupChat.creator`). `DeviceScanUpload.profile` is explicitly nullable for the same reason in
  normal operation. **Scope of the empirical half**: it exercised own-data CASCADE paths; the
  community-attribution `SET_NULL` paths were verified by reading, not by fixture.
- **Export/import round trip**: lossy in both directions, filed together in `PROBLEMS.md`. Export
  omits 11 user-authored content types; import omits `profile`, so bio, area, and every contact
  handle are written into the archive and never read back.
- **Data export coverage**: measured, and it is *not* complete — 11 kinds of user-authored content
  (safety check-ins, markup maps/overlays, saved filters, routes, pin aliases, profile notes,
  social links, secondary emails, wiki edits) have no representation in the archive. Filed in
  `PROBLEMS.md` as a feature gap, with the categories that are *correctly* omitted (credentials,
  key material, derived bookkeeping) called out so they don't get "fixed".
- **Cross-file constant couplings**: three findings this session came from the same shape — two
  values in different files that must move together, held in agreement only by comments (the
  pin-cache version, the notification preference stem, the panel flight TTL). Swept for more
  systematically: extracted every `UPPER_CASE` constant, found each file whose *comments* name a
  constant defined elsewhere (~95 hits), then filtered to comments asserting an actual constraint
  ("must match", "must stay larger", "keep in sync"). **`FLIGHT_TTL_SECONDS` was the only
  unguarded cross-file *value* coupling** — the rest are either name-only references (harmless),
  colocated (e.g. Overpass's `ql_timeout: 25` / `timeout: 30` are adjacent lines in one dataclass,
  with no production override, so nobody can edit one without seeing the other), or explicitly
  solved by importing instead of duplicating (`views_social.py` imports `ProfileDetailView`'s
  payload builder rather than restating it, and says why).
  The distinction worth keeping: a guard test earns its place when the coupled values live in
  *different files*. Colocated ones don't need one.
- **Docker system packages all justified**: `libreoffice-writer`, `tesseract-ocr`, `ffmpeg` and
  `poppler-utils` are heavy, and a first grep suggested they were referenced only from tests —
  which would have made them pure image bloat. That was a truncation artifact of `head -2`. The
  media services do invoke all four, guarded by `shutil.which(...)` so an image without them
  degrades rather than crashing (`documents.py::soffice_available`, `videos.py`). No change.
- **`get_or_create` constraint backing**: audited all 348 `get_or_create` call sites for lookups
  the database doesn't enforce. The first pass reported 14; **nine were false positives** and the
  correction is the useful part. Four were related-manager calls whose implicit FK the scan
  couldn't see (`ProfileActivityDay`, `ProfileStreak`, `PlaceAccessGrant`, `Fact` all have covering
  constraints). The other five included `PinAlias`/`WikiAlias`, which I came within one step of
  reporting as a *documented-but-unenforced* guarantee — `add_pin_alias` catches `IntegrityError`
  to raise `AliasExistsError`, which looks like dead code if the constraint is missing. It isn't:
  both models carry `UniqueConstraint(Lower("name"), F("pin"))`, an expression-based constraint
  that supplies exactly the case-insensitive promise the docstring makes. Expression constraints
  leave `.fields` empty, so a scan reading only that field cannot see them — worth recording as a
  reusable trap, since the naive scan makes correctly-constrained models look unprotected.
- **Management commands and OAuth provisioning**: the one destructive command
  (`delete_low_engagement_wikis`) is **report-only by default** and needs an explicit `--yes`;
  backfills offer `--dry-run`. `provision_mobile_oauth_client` gets the native-client shape right
  in every detail that matters — `CLIENT_PUBLIC` with an empty secret (a distributed app cannot
  keep one), `skip_authorization: False` so consent is still required, no secret written to stdout
  — and its claim that PKCE is enforced globally checks out (`PKCE_REQUIRED: True`).

  One documentation correction: the `urls.py` comment described the OAuth include as
  "authorize/token/revoke plus the logged-in application-management views". It actually mounts
  django-oauth-toolkit's entire URL set, which also includes token introspection, the RFC 8628
  device-code endpoints, and OpenID discovery. Neither introspection nor the device grant is
  reachable today — no application is registered with the device grant, and `introspect` is not
  among the configured `SCOPES`, so no token can carry it — but someone auditing this surface by
  reading the comment would not know they were mounted. Comment corrected to describe what is
  actually exposed, and to say what makes the extra endpoints inert.
- **Every auto-escape bypass in the templates**: all 12 `|safe` uses (and the single `mark_safe`)
  traced to source, after the SVG finding made this thread worth following.
  - User-authored HTML — visit notes and pin/wiki articles — goes through one `render_article`
    pipeline built on `nh3` with a fixed tag allowlist. Verified by feeding it 15 real payloads
    (`<script>`, `onerror`, `svg onload`, `javascript:` links, `iframe`, `object` with a data: URL,
    `base`, `meta refresh`, `ontoggle`, mXSS via `<math><mtext>`): **all 15 neutralised**. The one
    my detector initially flagged was a false positive — markdown-it refuses to build a link for a
    `javascript:` target, so the payload survives only as inert escaped text with no `href`.
  - The Wikipedia extract is third-party HTML that anyone can edit, and it is sanitised properly:
    `nh3` with a tag allowlist *plus* `attributes={}` *and* an `attribute_filter`, with a comment
    explaining that both are needed because ammonia otherwise keeps a hardcoded generic attribute
    set. Attribute-based XSS cannot survive it.
  - Every JSON-into-`<script>` site (`tags_data_json`, `common_pins_json`, `filter_labels_json`,
    and the admin/cost charts) routes through `safe_json_for_script`, which escapes `<`, `>` and
    `&` exactly as Django's own `{% json_script %}` does — so a `</script>` inside user data cannot
    break out of the enclosing block.
  - The remainder are not user data: GEOS-generated GeoJSON, and plugin-supplied toolbar HTML
    (plugins are code, not input).
- **Django admin and site-admin exposure**: of the 16 models registered in the Django admin, only
  `SiteSettings` carries an `EncryptedTextField`, and it appears in no `list_display`,
  `search_fields`, or `list_filter` — which matters because a search on an encrypted column is a
  SQL `LIKE` that cannot match ciphertext anyway. It *is* editable in the fieldsets, which is
  correct: a superuser has to be able to set the Gotify token, and it is not a privilege boundary
  against someone who already holds the database and `SECRET_KEY`. All 18 site-admin view classes
  are fully gated (login + `dashboard.view_site_admin`), verified at runtime rather than by reading
  base-class lists — five of them inherit the pair through a local `_AdminPermissionMixin`, which a
  literal AST scan reports as unprotected.
- **Index coverage for the retention sweeps** — investigated, and deliberately **not** changed.
  `ApiCallLog` is the highest-volume table in the app (a row per external API call, kept 400 days)
  and its only index is `(service, created)`, whose leading column none of its three
  `created`-only consumers filter on. That looks like a missing index until the selectivity of each
  consumer is checked:

  - `monthly_cost_series` turned out to be **one** grouped aggregate (`TruncMonth` + `Sum`), not
    the twelve per-month scans its call shape first suggested, and it selects ~365 of the 400
    retained days — Postgres would correctly seq-scan that regardless of any index.
  - `api_total` selects the whole table. Same.
  - Only `prune_older_than_days` has a selective predicate (in steady state ~0.25% of rows: one
    day's worth out of 400), and it runs once daily.

  So a `created` index would speed exactly one daily delete, at the cost of write amplification on
  the app's hottest insert path. That is a marginal trade at best, and not one to make without
  production volume to measure against. Recorded here so the "obvious missing index" is not
  re-derived and added on the strength of the index list alone. `PinTombstone` has the same shape
  (`(profile, created)`, pruned on `created`) on a far smaller table, so the argument is weaker
  still.
- **Shared mutable state**: **zero** mutable default arguments (`def f(x=[])`) across the whole
  codebase, and zero class-level containers mutated through `self`/`cls` — the pattern where
  `self.items.append(...)` on a class attribute leaks state between instances and, in a long-lived
  worker, between requests. Both were checked by AST. The naive version of the second check reports
  543 "mutable class attributes", essentially all of them Django/DRF declarations (`list_display`,
  `fields`, `readonly_fields`) that are configuration and never mutated; narrowing to "declared on
  the class *and* mutated through self *and* never reassigned per-instance" is what makes the
  answer meaningful rather than a wall of noise.
- **`select_for_update()` transaction coverage**: all 28 calls are lexically inside a
  `transaction.atomic()` block or an `@transaction.atomic`-decorated function. Outside one, Django
  raises `TransactionManagementError` at query time — a 500 that only fires when the path actually
  executes, so static coverage is the only cheap way to know. Checked by AST rather than grep,
  which matters: grep reported 32 hits, four of which were comments *describing* the pattern.
- **Raw SQL**: only four sites outside migrations, and all four are safe. Values are always
  parameterized (`%s`); the f-string interpolation is identifiers only, and I traced each to its
  source rather than trusting the `# nosec` annotations — `db_table` / `Field.column` /
  `pk_field.column` off Django's own `_meta`, enumerated from the model registry, with no path from
  user input. The fourth is a fully static PostgreSQL introspection query with no interpolation
  at all.
- **E2EE crypto primitives**: nonces are 24 random bytes from `randombytes_buf` per operation with
  XSalsa20-Poly1305 (`crypto_secretbox`), so nonce reuse is not a practical risk even with a
  long-lived conversation key; identity keys are `crypto_box` keypairs, anonymous sealing uses
  `crypto_box_seal`, and the wrap format is `nonce || ciphertext` with the nonce sliced back off on
  open. Textbook libsodium usage. The weakness was in the *parameters* around it, not the
  primitives — see the enrolment KDF floor above.
- **Frontend listener lifetime**: looked for the accumulation pattern that actually bites in an
  HTMX app — a `document`-level listener registered *inside* a per-swap handler, so it doubles on
  every swap. A scan flagged six candidates; two were read in full and both are one-time init or
  `wireHtmxHooks()` wiring with *sibling* registrations, not nested ones, and the remaining four
  match the same shape. No accumulation found. Raw `addEventListener`/`removeEventListener` ratios
  (64:7 in `map-annotations.ts`) are **not** evidence of anything on their own — listeners on
  page-lifetime elements never need removal — and reporting that ratio would have been the same
  mistake as the fetch/catch ratio recorded above.
- **Frontend build and test health**: `tsc --noEmit` is clean and all 383 TypeScript tests across
  27 files pass on the host toolchain. A scan for `fetch()` calls lacking error handling was
  started and **abandoned as unsound rather than reported**: file-level fetch/catch ratios say
  nothing about whether any individual call is guarded (several modules route through their own
  local `postJson` wrapper), and the one file the scan flagged hardest, `shared/csrf.ts`, contains
  no `fetch` at all — the match was the word inside a docstring. Recording the dead end because the
  same shape of mechanical scan has produced roughly seven near-miss false findings across this
  audit; the honest result here is "not measured", not "clean".
- **Password reset**: the E2EE interaction is handled carefully and completely — a reset sets
  `password_wrap_stale`, and that flag round-trips (written on reset, serialized to the client,
  read in two places to skip the now-undecryptable wrap, cleared on re-wrap), so it is live state
  rather than a write-only field. `AccountKdf` is deleted when the client's derivation fails so the
  account reverts to legacy auth instead of being locked out by a mismatch. The reset form also
  keeps its anti-enumeration property while fixing the SSO-only case that used to say "check your
  email" and send nothing.

  One gap, filed: a reset kills every session but revokes **no** `ApiKey` or OAuth token, and
  minting an API key requires no current-password proof — so a session-only compromise can leave a
  credential behind that the victim's password reset does not remove. Same asymmetry as the
  rate-limiting finding: the current-password proof already exists in this codebase, guarding the
  E2EE key-replacing endpoints, for reasoning that applies equally here.
- **Login rate limiting**: the account lockout (5 failures, 15 minutes by default) is keyed on the
  submitted identifier alone, with no IP dimension, no nginx `limit_req`, and no throttle on the
  login view — so knowing a username is enough to hold that account out of password login
  indefinitely at ~5 requests per 15 minutes. Two details of the current design are *good* and
  worth not regressing: a non-existent identifier is rate-limited identically with identical error
  text (so the lockout is not a user-enumeration oracle), and a success clears the counter. Filed
  rather than changed, because the threshold is an ops judgement — but the asymmetry is worth
  naming: `_client_ip()` and the cache-counter pattern already exist in the *same module* and are
  applied to passphrase suggestions and password-policy checks, two endpoints of far lower value
  than authentication.
- **All 11 per-field visibility settings are enforced** — swept after the "Visited Together" leak,
  on the theory that a declared-but-unenforced setting might not be alone. It was: every one has a
  real enforcement site. Two intermediate results were my own false alarms, both from
  pattern-matching: `online_status`/`read_receipt`/`typing_indicator` looked unenforced only because
  a `head -6` truncated the grep past their enforcement sites, and `trip_pin_location_visibility`
  scored zero because it is enforced by direct `VisibilityChoice` comparison rather than through
  `visibility_permits`. Enforcement idioms vary; counting pattern hits does not detect them.
  Read receipts are rendered in exactly one template, gated; group threads do not surface read
  state to other members at all.
- **The journal's fail-closed claim holds; its completeness had no guard.** `JOURNAL_SOURCES`
  carries a comment promising that "the view fails closed on an unmapped key, so a new domain cannot
  be exposed by forgetting the second half". Verified: `filter_sources_by_grants` iterates the
  *scope mapping*, so a source absent from it is never granted, and an empty scope set is explicitly
  omitted rather than granted. The safety direction is correct by construction.
  The completeness direction was unguarded, and is the mirror of the notification-preference drift in
  chunk 115: a source added without a scope entry is silently unreachable through the API forever -
  no error, no warning, just a domain that never appears. A test now pins both directions, plus two
  subtler invariants the mapping's own docstring states: no entry may declare an empty scope set
  (which reads as "mapped" but behaves as "never served"), and every entry must include the
  endpoint's base scope, since `filter_sources_by_grants` deliberately does not assume the view's own
  `required_scopes` were satisfied.
  Teeth-checked by deleting the `articles` entry.
- **Location-mention visibility is sound, and well placed.** A comment can embed a location's *name*
  (`@[Name](loc:uuid)`), so the interesting question is whether every render path checks that the
  viewer has pinned it. It does not have to: `render_comment_text` calls `is_visible_to` internally
  and returns `None` when any mentioned location is unpinned, so the six production callers
  (`controllers/comments`, `trip_comments` x3, `comments` x2) inherit the check rather than each
  remembering it. The rule is fail-closed on the whole comment - one unpinned mention hides all of
  it - and `services/comments/comments.py` documents it as the third layer of its visibility chain.
  `filter_visible_comments` has no production caller; a leftover helper, noted not removed.
  **I nearly reported the opposite.** A `grep | head -10` returned only test files, which reads as
  "the protection is never wired up" - a serious privacy hole. The production callers were on line 11
  onward. This is the second truncation error of the session (chunk 105 was the first, on the same
  `head` idiom), and the pattern is worth naming: truncating a search turns absence-of-evidence into
  what looks like evidence-of-absence, and negative findings are exactly where that is most
  dangerous.
- **Notification delivery is properly isolated.** Traced the WhatsApp/SMS path end to end because a
  third-party carrier is exactly the sort of dependency that ends up failing a user's action:
  `NotificationLog` post_save -> `schedule_notification_text_alerts` -> a Celery task
  (`autoretry_for=(OSError,)`, three retries with backoff) -> the dispatch helpers. Nothing sends
  inside the request, so an outage costs a retried background task rather than a 500. An
  unconfigured Twilio is caught as `ValueError` and debug-logged; a provider API error fails just
  that task, leaving the in-app notification - the actual record - intact.
- **Game areas were the last user-drawn geometry with the same bug; fixed at the source.** After
  chunk 139 showed the sweep had missed a field, the remaining `__within` sites were re-triaged by a
  sharper question - *can this polygon be drawn by a user?* - rather than by which feature I thought
  I had already covered. That leaves the SpotGuessr and Trivia area restrictions: a player drawing a
  game area across the date line gets "no eligible locations" for an area full of their own pins.
  Fixed in `GameConfig.geo_bounds`/`TriviaConfig.geo_bounds` - where the GeoJSON becomes a geometry -
  rather than at the three-plus query sites, so eligibility counts, round selection and the external
  API's eligible-pins endpoints all inherit it. That placement is deliberate: patching query sites is
  exactly how the `smart_boundary` paths got missed twice.
  Teeth-checked against the container's file. Everything else using `__within` takes provider or
  derived geometry (parcels, building footprints, the static USA boundary) that cannot straddle the
  line the way a hand-drawn box can.
- **Chunk 125's antimeridian fix was incomplete - smart-list boundaries had the same bug.** Auditing
  smart-list membership turned up `PinList.smart_boundary`, a *separate* field from
  `include_regions`, queried by its own two `__within` calls and missed by that fix.
  Both had to change, and they answer the same question at different times: `_pin_in_boundary`
  decides membership on every pin save, `_boundary_matching_ids` resolves the whole list on resync.
  Fixing only one would have been worse than fixing neither - a pin would join the list on save and
  vanish on the next resync, which looks like data loss rather than a filter bug. A test asserts the
  two paths agree.
  Teeth-checked against the container's actual file: removing both splits fails exactly the two
  cross-line tests.
  Worth recording as a limit of the sweep method. Chunk 125 swept `__within` call sites and triaged
  them by whether the polygon could plausibly cross the date line - and I judged smart boundaries as
  covered because I had fixed "the region filter", when `smart_boundary` is a different field
  entirely. The sweep found the right *class*; my mental model of which sites belonged to it was
  wrong.
- **Cross-module private imports swept; one more fixed, the rest judged not worth churning.**
  Chunk 137's finding was a layering violation, so the class was swept: 17 in `controllers`, a
  handful across `services` and `external_api` (and 76 in tests, which is normal - tests are
  supposed to reach inside).
  `controllers/flickr.py` was the same bug as epa_echo - importing `_haversine_km` from
  `models.profile.model` to filter photos by radius, then multiplying by 1000 to get metres. It now
  calls `services.geo.distance.haversine_meters` directly, dropping the km round-trip. Verified
  identical (1390.357164866m both ways).
  The rest are a different shape: reuse of a genuinely useful private helper
  (`_create_pin_from_confirmed`, `_format_duration`, `_get_compiled`) where the honest fix is to
  promote it to public API - a rename touching every call site. Most of the remainder are
  controller-to-controller, which is same-layer sharing rather than a layering breach. Filed as an
  observation rather than churned: unlike the haversine, there is no correctness risk here, only
  a naming one, and a rename sweep would touch far more code than it clarifies.
- **A plugin was reaching into a model's private helper to redo two shared conversions** — fixed.
  Following the session's own finding that defects cluster in re-derived primitives rather than in
  auth, the sweep moved to unit conversion. Four constants exist and all are numerically correct, but
  `epa_echo._miles_between` imported `models.profile.model._haversine_km` - a *private* helper, from a
  model module, into a plugin - and multiplied by an inline `0.621371`, duplicating both
  `services.geo.distance` (consolidated in chunk 131) and `services.core.units`.
  Now uses both shared helpers. Verified identical before and after (0.863928 miles for the same
  pair), and pinned against an *independent* reference rather than a restatement of the
  implementation: one degree of latitude is ~69.09 miles. One test also asserts miles come out
  smaller than kilometres, which catches an inverted conversion - the failure that reads perfectly
  plausibly until somebody checks a number.
  Also noted: `_METERS_PER_FOOT = 0.3048` is defined twice (a plugin and a controller). Left alone -
  it is a defined physical constant with no derivation to get wrong, which is the same judgement
  applied to the coordinate bound checks in chunk 132.
- **TOTP and backup codes audited; nothing to change.** Both use the compare-and-set discipline
  this audit spent several chunks *adding* elsewhere - and here it was already right, with the
  reasoning written down. A TOTP step is claimed by a conditional `UPDATE` filtering on
  `last_used_step__lt=step` and accepted only when `claimed == 1`, so two submissions of one
  intercepted code (the docstring names the phishing-proxy case) cannot both pass. Backup codes are
  hashed and consumed the same way, filtering on `used_at__isnull=True`.
  One thing checked because it looked like a denial-of-service lever: a wrong backup code runs
  `check_password` against *every* unused code, so ~10 deliberately-slow hashes per failed attempt.
  It is bounded - the 2FA fallback reuses `SiteSettings.login_max_attempts` (default 5), so an
  account tops out around 50 hashes per lockout window. The `max_attempts <= 0` disable path is
  intentional and documented on the field itself, with a `>= 0` check constraint behind it.
- **Three consecutive security subsystems - passkeys, TOTP/backup codes, and the docstring claims
  before them - audited with no defects found.** Recorded as a signal rather than filler: the
  authentication paths are the most carefully built code in this codebase, and the audit is hitting
  real diminishing returns there. The defects this session found cluster in *geometry* and in
  *duplicated primitives*, not in auth.
- **WebAuthn/passkeys audited; nothing to change.** Checked against the standard failure modes:
  the challenge is `session.pop`-ed so it is single-use, the credential lookup is scoped to the
  authenticating user, RP ID and origin are derived per-request (and Django's `ALLOWED_HOSTS`
  bounds what a Host header can claim), and clone detection is complete - the stored sign count is
  passed into verification *and* the returned counter is written back, which is the half that is
  usually missed.
  `user_verification=PREFERRED` looked like a weak setting until the flow was traced: passkeys here
  are a *second* factor issued "after a successful password login", not a passwordless primary, so
  PREFERRED is the appropriate choice rather than a gap.
  Existing coverage is genuinely thorough - `test_challenge_is_consumed_and_cannot_be_reused`,
  duplicate-credential rejection, cross-user credential rejection, sign-count persistence, and the
  full two-factor login flow. No test was added because every property worth pinning already was.
- **Absolute claims in security-relevant docstrings, spot-audited; these ones hold.** Chunk 133 found
  documentation asserting a rule the code did not implement, so the same question was put to other
  strong claims ("never", "only ever", "cannot") in privacy-adjacent code.
  *"A pin article is only ever visible to its owner, so it's fully covered by exporting it"* - holds.
  `Article.is_private` is true for any pin-attached article, `editable_by` restricts pin articles to
  the pin's owner, and the exporter reads only `Pin.objects.filter(profile=profile)`, so it exports
  the owner's own articles and nobody else's.
  *"A single piece of evidence should never read as 100% confidence"* - holds, and measured rather
  than reasoned: one unanimous categorical submission scores **0.60** (below the 0.75 confirm
  threshold) and a single number observation **0.20**, while 1000 units of unanimous weight asymptote
  to 0.998 without reaching 1.0. The Beta(2, 2) prior does exactly what its comment claims.
  That second one is now pinned. It is a *designed* property - one person should not settle a
  question - held up by two independent constants (the prior and `MIN_EVIDENCE_FOR_ESTIMATE`), and a
  future tuning change to either could have removed it silently with every existing test still
  passing. The new tests assert bounds, not exact values, so the numbers stay free to move but not
  through 1.0 and not past the confirm threshold on one submission.
- **Wiki access control is sound - and the documentation describing it was wrong.** Audited because
  it is privacy-critical *and* geometry-adjacent, after four chunks of geometry bugs. The rule turns
  out not to be geometric at all: `CLAUDE.md` and `CLAUDE.local.md` both described "a pin within the
  bounding box of the wiki's location", but `wiki_access` grants visibility by *place domain* - a pin
  on the exact `Location`, or a pin whose place resolves to the same domain root (the parcel, or any
  building on it), extended by aggregate places and explicit `PlaceAccessGrant` rows.
  The implementation is the stricter and more precise of the two, so this is a documentation defect
  rather than a security one - but a dangerous kind: someone adding a new access check from the docs
  would write a bbox query, which grants *more* than the real rule and would read as consistent with
  the written spec. Both files now describe what the code does.
  The code itself is well built: one implementation (`_domains_given_pins`) serves both the live
  check and the pin-move preview, explicitly so the preview cannot drift from what is enforced, and
  the aggregate-earning fixed point is bounded by `MAX_EARNING_ROUNDS`.
- **Three risk areas checked, all clean; one guard added.**
  *Coordinate validation* is duplicated across 12+ sites and every copy agrees (-90..90, -180..180,
  and NaN rejected for free since `-90 <= nan <= 90` is False). Deliberately **not** consolidated
  like the haversine: a two-comparison bound check is hard to get wrong, so the drift risk that
  justified that churn does not apply here.
  *Plugin loading* is properly isolated - import, instantiate, register and hook invocation each
  catch and log, so one broken plugin cannot take the registry with it.
  *Rate-limit coverage* is complete: all 46 registered services declare at least one of per-minute,
  per-day or per-30-day. That one is now guarded, because the failure mode is invisible - a plugin
  that declares no defaults does not get a lenient limit, it gets **none**, and calls out as fast as
  the code asks until the provider throttles or bills instead. The test asserts only that *a* limit
  exists; the numbers are per-provider judgements that belong with each plugin.
- **Five haversine implementations consolidated into one.** Sweeping for the shape behind three
  earlier findings - a primitive implemented more than once - turned up five independent copies of
  the same eight-line formula: profile map centring, public-pin clustering, consensus answer
  scoring, Overture boundary matching, and markup geometry (plus a sixth frozen in a migration).
  They were compared numerically first: **0.00m spread** across short, long, and
  antimeridian-crossing pairs. Nothing was broken, so this is a consolidation, not a fix - all five
  now delegate to `services/geo/distance.py` and keep their existing signatures, so no caller
  changed.
  The justification is the codebase's own history: duplicated geometry primitives here have already
  drifted twice, into four independent longitude averages (one of which put a map centre in the
  Atlantic) and a "nearest pin" lookup that ordered by a geometry column while a correct
  distance-ordered helper sat a few lines away in the same file. Nothing was wrong with these five;
  the sixth copy is where the next bug goes.
  A property test now asserts all five agree for arbitrary points, so they cannot silently diverge
  again.
- **Dwell detection and the History consent gates are sound.** All three toggles
  (`visit_logging_allowed`, `route_import_allowed`, `geolocation_tracking_allowed`) are consulted at
  the entry point of the feature they govern - checked because chunk 104 found a setting that was
  collected and stored but never consulted. `record_geolocation_visits` refuses outright when live
  tracking is off, dedupes per calendar day before creating anything, and pre-filters candidate pins
  with an indexed 5km distance query whose comment records the production incident that motivated it.
  One contrast worth recording: the same queryset module contains `near_point`, which annotates with
  `Distance` and orders by it - exactly what `find_nearest_pin` (chunk 127) failed to do. The correct
  pattern already existed a few lines away; the broken function reimplemented it by hand and got the
  ordering wrong. That is the more useful lesson from that bug than the bug itself.
- **Billing and subscriptions audited; sound.** Three things were checked because each is a classic
  source of money bugs:
  *Webhook idempotency* is properly built - the event row is claimed with `select_for_update` and
  its `processed_at` checked inside the same transaction that runs the handler, so a Stripe redelivery
  (which happens on any non-2xx *or timeout*) cannot double-credit `total_paid_cents`. The reasoning
  is written down at the call site.
  *Access predicates* evaluate paid access as "active/trialing with the threshold met, **or** unexpired
  banked coverage", the latter deliberately status-agnostic so paid-ahead runway survives a
  cancellation.
  *The usage ledger* advances one 30-day period per iteration and terminates on either affordability
  or reaching now.
  I suspected the ledger's per-iteration `cost_per_user(as_of)` - which aggregates over `ApiCallLog`,
  a table retained for 400 days - would make a catch-up expensive inside the webhook's row lock.
  Measured instead of assumed: **3 queries** for the normal one-period case and **14** for a
  subscription dormant a full year. That is not a performance problem, so nothing was filed. Recording
  the disproven hypothesis because the reasoning was plausible and wrong, which is the same failure
  mode as the routes "bug" in chunk 124.
- **Full suite: 10,579 passed, 0 failures** (plus 1,437 subtests, 80 minutes) across all 190 changed
  files - the first end-to-end run of the audit, confirming no cross-cutting breakage that the
  per-chunk targeted bands could have missed.
- **"Arbitrary row where a specific one is meant" swept; no further instances.** The nearest-pin bug
  was one case of a general shape: a query that *looks* like it selects a particular row but does
  not. Two sweeps:
  no other `order_by` targets a geometry column (the three apparent hits are `-total_points`,
  matched on a substring); and of the `.first()` calls whose own variable names promise a specific
  row - `current_round` in all three game engines - every one is reached through a `for_session()`
  that orders by `sequence_index` or `created`, so "the current round" really is the earliest
  pending one.
  Worth recording *why* that needed checking: 109 of the 165 dashboard models define no
  `Meta.ordering`, and the abstract base deliberately sets none, so `.first()` is arbitrary far more
  often than it looks. The four sites that matter happen to be ordered upstream - which is not
  visible at the call site.
- **`find_nearest_pin` did not return the nearest pin** — fixed. It ordered by
  `location__point`, the geometry column itself, which sorts by PostGIS's internal representation
  and has nothing to do with distance from the query point; the function returned an arbitrary pin
  inside the radius while its name and docstring both promised the closest. Measured: with pins 11m
  and 75m from the query point it returned the one at **75m**.
  It matters because of where it is used - matching Google Location History and My Activity
  coordinates against a user's own pins at `VISIT_MATCH_RADIUS_M` = 100m. Any profile with two pins
  inside that radius (a building and its neighbour, a pin and one across the street) could have an
  imported visit attributed to the wrong place, silently and permanently, on a path that runs
  unattended over a whole history file. Now annotated with `Distance` and ordered by it.
  Teeth-checked against the container's actual file: restoring the geometry ordering fails three of
  seven, including the three-candidate case.
  Worth noting how it read: `.order_by("location__point")` next to a `distance_lte` filter looks
  deliberate, and the docstring asserts the behaviour it does not have. Only running it exposed the
  gap.
- **Wiki nesting checked and sound** — the one consolidating operation left unexamined after the
  pin and label merges. It turns out not to be a destructive merge at all: `_absorb` sets
  `parent_wiki` and never deletes, so the relation-loss question that produced the album/overlay bug
  does not arise. Its cycle guard is called in *both* nesting directions before absorbing, and
  `Wiki.would_create_cycle` carries a `visited` set so it terminates even against data already
  corrupted with a pre-existing loop.
- **Full-suite run started** — the audit has been verified by targeted bands throughout, which is
  fast but cannot catch cross-cutting breakage across ~180 changed files. Recorded here so the
  distinction between "the affected band passes" and "everything passes" stays explicit.
- **Saved-filter and smart-list regions had the same date-line bug as the viewport** — fixed.
  Generalising chunk 124's real finding (that `__within`/`ST_Within` has no geography
  implementation and is evaluated as flat degrees, unlike `bboverlaps`), the 19 `__within` sites
  were triaged: most take small parcel or user-drawn polygons where planar point-in-polygon is
  correct. `filter_by_criteria`'s `include_regions`/`exclude_regions` are not, because a user can
  draw one across the line.
  Measured both arriving shapes: a region drawn across the antimeridian arrives *unwrapped* from
  Leaflet (179 to 181) and matched only the pins west of the line; the folded form (179 to -179)
  matched a pin 180 degrees away and none of the intended ones. `exclude_regions` is the worse half
  - a region that matches almost nothing excludes almost nothing, so a filter meant to hide an area
  quietly stops hiding it.
  `split_at_antimeridian` now folds the overhanging part back to the coordinates points are stored
  at. A polygon whose vertices are already folded but which spans more than 180 degrees is
  deliberately left alone: written literally those coordinates *do* describe the long way round, and
  silently reinterpreting them would be guessing at intent. Teeth-checked - removing the splitter
  fails three of seven, verified against the container's actual file after chunk 124's lesson.
- **The antimeridian sweep's fourth site turned out not to be a bug — and I nearly shipped a fix
  for it.** `within_bounds`' docstring names its siblings ("the same `Polygon.from_bbox` +
  `within` idiom"), which pointed at `RouteQuerySet.intersecting_bbox` and
  `locations.base.default_bbox`. The route filter builds the identical box, so I fixed it the same
  way and wrote tests.
  The teeth-check then passed when it should have failed. Confirming the container really had the
  disabled version (it did), the naive single box *still returned the correct routes*. The reason:
  `Route.path` is `geography=True` and the filter uses `bboverlaps` (PostGIS `&&`), which is
  evaluated geodetically and handles the wrap itself. `within_bounds` differs by using `__within`
  (`ST_Within`), which has no geography implementation and is evaluated as planar geometry - which
  is exactly why it broke and this did not.
  The route change was reverted. Its tests were kept but reframed as characterisation: they record
  *why* no splitting is needed here, and fail if `path` becomes a plain geometry column or the
  filter moves to `__within` - which is when the logic would be needed. `default_bbox` has no
  production callers at all, so its near-±180 clipping is noted and left.
  Worth stating plainly: the pattern matched, the reasoning was sound, and the conclusion was wrong.
  Only the teeth-check caught it, and only because a passing teeth-check was treated as a failure to
  investigate rather than a result.
- **A map viewport crossing the date line showed the whole world except itself** — fixed, and the
  worst of the family. `within_bounds` built one `Polygon.from_bbox((west, south, east, north))`.
  When a viewport crosses the antimeridian its west edge exceeds its east edge, and that rectangle
  is drawn the *long* way round: measured, a 2-degree window became a **358-degree** box that
  excluded every pin on screen and included everything on the far side of the planet. A user
  panning across the line saw their Fiji pins vanish and unrelated pins appear.
  The second arriving shape is unwrapped bounds - Leaflet's `getEast()` returns 181, not -179 -
  which built a plausible box that simply never matched stored coordinates, since those are always
  folded into [-180, 180]. Both are handled: edges normalise through a new
  `normalize_longitude`, and a crossing viewport queries its two real halves.
  Teeth-checked: restoring the single box fails four of six, including the "excludes the far side of
  the planet" case that describes the original symptom exactly.
- **Two more antimeridian bugs, found by sweeping the class** — including one of my own. The fact
  centroid fix (below) prompted a sweep for every other place longitude is averaged or compared as
  an ordinary number. Three sites, each written independently:
  the fact centroid; the **profile's saved map centre**, which clusters pins with haversine (wrap-
  correct) and then averages the cluster's longitudes arithmetically, so a user whose pins straddle
  the date line is centred at longitude 0 - in the Atlantic; and the import-failure location guess
  added earlier today, whose `abs(lng_a - lng_b)` proximity check reads two points a kilometre apart
  as 359.98 degrees apart, so an S2 cell could never corroborate a candidate near the line.
  All three now share `services/geo/longitude.py` - `circular_mean_longitude` and `longitude_delta`.
  Both return answers identical to the naive arithmetic everywhere except within a hair of the date
  line, which the "ordinary longitudes" tests pin explicitly: a fix that shifted the rest of the
  planet would be far worse than the bug. Property tests bound the outputs for arbitrary inputs.
  The lesson is the repetition rather than any single site: three separate authors reached for
  `sum(...)/len(...)` on a value that wraps, which is why the primitive now exists rather than a
  fourth correct-by-inspection copy.
- **Fact POINT evidence averaged longitude arithmetically, breaking at the antimeridian** — fixed.
  `_aggregate_point` computes a weighted centroid from every piece of POINT evidence. Longitude
  wraps, so two observations of one place either side of the date line (179.99 and -179.99) averaged
  to **0.0** - a centroid in the Atlantic, ~20,000km from either observation, which `recompute` then
  stored as the fact's value. Measured before the fix, not inferred.
  Confidence collapsed to 0 in that case, which limited the damage but did not stop the wrong point
  being written - and the second half is worse: two observers who genuinely *agreed* were scored as
  disagreeing against the bogus centroid, so a fact anywhere near the date line could never reach
  confidence at all. Now averaged as unit vectors, which returns an identical answer for every other
  longitude on Earth; the ordinary case is asserted alongside, because a fix that shifted every other
  centroid would be a far bigger bug than the one it corrects. Teeth-checked - reverting fails four
  of the seven.
  Reach is narrow by construction (only ±180: Fiji, NZ, Kiribati, Chukotka, the Aleutians), which is
  why it survived: nothing else in the app cares about that meridian.
- **The same photo-orphaning bug existed for wikis and safety check-ins** — found by sweeping the
  class rather than waiting to hit it again, and fixed identically. `Image` points at `Pin`, `Wiki`
  and `SafetyCheckin` with `SET_NULL`, so deleting any of them detaches the user's photos instead of
  destroying them; none of the three handlers recorded which object the photos had been on, so every
  undo restored an empty object with the link unrecoverable. Verified against a real database for
  both new cases before changing anything (`photo survived=True detached=True`, `re-attached=False`),
  and re-verified after (`re-attached=True`).
  A single completeness test now covers all three and asserts it against `Image`'s own `SET_NULL`
  owners, so a fourth owner cannot repeat this silently. It also pins the "still detached only" rule
  for each, since an undo that reclaims a photo the user has re-filed would be a worse bug than the
  one being fixed. Teeth-checked.
  Also enumerated, not fixed: the other undo handlers leave *associations* orphaned the same way
  (`MarkupMap.pin`, `TripActivity.pin`, `SafetyCheckin.trip`, `PinList.source_saved_filter`, and the
  eight relations pointing at `MarkupMap`). Those are links rather than content, and restoring them
  blindly could resurrect associations the user has since changed - a different judgement from
  photos, which are irreplaceable and unambiguous.
- **Undoing a pin delete now restores its photos** — fixed, and the wider limitation filed. Applying
  the chunk-117 question to the *delete* path rather than the merge: `Image.pin` is `SET_NULL`, so a
  deleted pin's photos deliberately survive, detached. But `PinUndoHandler` serialised only fields,
  FK ids and label ids, so an undo brought the pin back **empty** while its photos sat unattached -
  and nothing else recorded which pin they had been on, so the link was unrecoverable the moment the
  delete committed. The ids are now captured at stash time (the only moment the link exists) and
  re-linked on restore, but only for photos *still* detached: one the user has since filed elsewhere
  stays there. Old payloads without the key still restore. Teeth-checked.
  What is not fixed and is filed instead: everything that CASCADEs (comments, albums, overlays,
  links, notes, visits) is gone permanently - measured at `comments=0 albums=0` after a delete and
  immediate undo. Restoring those means serialising whole object graphs with internal references
  intact, which needs a decision about how deep undo reaches; the cheaper alternative is to stop the
  delete dialog promising "all of it restorable".
- **`merge_labels` checked the same way and is correct** — and this is a case where acting on the
  scan would have *introduced* a bug. Enumerating Label's relations found one CASCADE relation the
  merge never moves: `LabelCustomization`. It is reachable (a user can customize a label they own,
  despite the service describing customizations as being for labels "they do not own"), and
  confirmed empirically: merging such a label away leaves zero customization rows.
  It is still correct. A customization holds display overrides *for the label being deleted*; it
  carries no content, and moving it onto the target would silently restyle a label the user never
  customized and overwrite any override the target already had. Letting it cascade is the right
  outcome, so it is recorded as a documented exemption rather than "fixed". The guard also asserts
  exemptions stay real relations, so a stale entry cannot mask a relation that later starts
  mattering. Teeth-checked.
- **Merging a pin destroyed its albums, map overlays and custom layers** — fixed. `merge_pins`
  reassigns the loser's relations and then deletes it, and its module docstring asserts that "every
  relation FK'd to Pin falls into one of three buckets". Enumerating Pin's related objects through
  Django found 29 relations, 22 of them CASCADE, and three that appear in none of the buckets:
  `Album.parent_pin`, `MapImageOverlay.parent_pin`, `CustomLayer.parent_pin`. Confirmed against a
  real database before changing anything — an album and an overlay on the loser both came back
  `exists() == False` after a merge.
  It is drift, not a decision: `pin_merge` was added 2026-08-02, `Album` 2026-08-05 and
  `MapImageOverlay` 2026-08-06, so the completeness claim quietly stopped being true.
  Overlays and layers move straight across (no uniqueness constraint). Albums cannot: 
  `uq_album_pin_slug` is unique on `(parent_pin, slug)` and two pins each holding a "Photos" album
  is the ordinary case, with real images on both sides — so the loser's album is re-slugged rather
  than dropped, since dropping it would be the same silent data loss in a new place.
  The guard is the point: instead of listing the three models found today, the test asserts that
  *no* CASCADE relation to Pin is unmentioned by the merge, so the next model to grow a
  `parent_pin` fails here rather than deleting user data. Teeth-checked by removing one
  reassignment — both the specific test and the completeness arm fail.
- **External API scope enforcement is complete, and now guarded.** All 189 external endpoints were
  enumerated through the URL resolver: 188 enforce a scope permission, and the one that does not
  (`AuthSessionView`) is a deliberate, documented exception that reports only the calling
  credential's own grant - gating it behind a scope would be circular.
  The design is already careful: `HasApiKeyScope` fails closed *per method*, so an endpoint that
  gains a new HTTP method without declaring a scope becomes a dead 403 rather than an open door.
  What that does not cover is the dangerous mistake - dropping the permission entirely, or
  inheriting `UnscopedExternalApiView`, whose own docstring warns that doing so for a data endpoint
  "would silently grant it to every credential". That failure is invisible: the endpoint works, for
  every credential, forever. A test now pins the exemption list to exactly one named entry with a
  stated reason. Teeth-checked by reparenting a real data endpoint (`PinTombstonesView`) onto the
  unscoped base - it is named in the failure.
  Key verification itself was read and is sound: indexed prefix lookup then `check_password` on the
  secret half, revoked keys and inactive users excluded, `last_used_at` written with `.update()` so
  no `save()` side effects fire. API keys have no expiry field at all - they are revoked, never
  aged out - which is a product gap rather than a defect, and is stated as such in the endpoint that
  reports credential metadata.
- **Notification preference drift now has a guard** — the risk was identified in the codebase's own
  prose and left unenforced. `services.notifications.notification_center` introspects
  `NotificationPreference` to derive its stems precisely because "a hardcoded list here would
  silently omit it - which is exactly how the controller's `_PREF_FIELDS` and the model can drift
  apart". The controller keeps that hardcoded list anyway, for good reason (it carries display
  labels the model has no place for) — so the drift is possible and nothing checked for it. The two
  agree today (13 entries, all real fields, no orphans); a test now pins both directions, since each
  fails quietly: a model stem missing from the list is a preference the user can never change, and a
  listed name that is not a field renders a control that saves nothing. Teeth-checked.
- **Email uniqueness is enforced on the normalised value, not the raw one** — checked as the
  highest-stakes instance of the chunk-114 class (validate one value, store another). The partial
  unique constraint is on `normalized_email` where `is_verified=True`, and `mark_verified()`'s raw
  `.update()` can violate it — which the secondary-email path correctly catches, answering "already
  verified on another account" instead of a 500.
  Two of my own inferences here were wrong and were corrected by introspection rather than reading:
  the preference field count (a regex found zero) and an apparent `safety_checkin_*`/`safety_ci_*`
  mismatch (those were types lacking preferences, not misnamed fields).
- **A name that sanitized away was stored as a blank alias** — fixed, found by sweeping the class
  the underscore bug belonged to (silent transformation of user input on save). Alias creation
  checked that the *raw* submitted name was non-empty and then let `save()` sanitize it, so an
  emoji-only or `<>` name passed "Name is required" and persisted as an empty-string alias. That
  blank row shows in the pin's alias list *and* consumes its one free slot under the
  case-insensitive unique constraint, so the next such attempt fails with a duplicate-key error
  rather than a useful message. Both creation paths now validate the sanitized value — the one that
  will actually be stored — and reject it with the message they already had. The pin-name sync path
  was never affected; it guards on `is_meaningful_name` first.
- **All 12 save-time field mutations audited; the rest are sound.** Slug minting, PostGIS point
  sync, normalized email, normalized trivia answers, fact subject-type and invitation expiry are all
  legitimate derivations. `Profile.save()` forcing community-gated visibility to `NO_ONE` while
  Community is off is a deliberate single enforcement point, and fails closed. `Location.save()`'s
  place resolution is correctly gated to creation-with-coordinates and is a local geometry lookup,
  not a provider call.
  Also checked and disproven: that `bulk_create` might bypass a `save()` that mints slugs or uuids.
  None of the 13 bulk-created models has a slug, and the two with uuids take them from field
  defaults, which `bulk_create` honours.
- **Underscores were silently stripped from every user-facing name** — fixed. `sanitize_name` runs
  from the `save()` of Pin, Wiki, Location and alias rows, normalising to an allowlist. That
  allowlist kept `"`, `#`, `/` and `&` but dropped `_`, so "Site_7" was persisted as "Site7" with no
  indication - and it applies on *every* write path, so imported names (KML/GPX exports, names
  derived from filenames) were quietly rewritten. Underscore is a word character: not
  markup-significant, not a URL or query-string delimiter, not a homograph, and category `Pc` rather
  than a control character - so permitting it violates none of the existing property tests
  (`never_produces_markup_significant_characters` forbids `< > \` { } \\ | ;`). Verified: all 16
  pre-existing sanitizer tests still pass, plus 4 new ones.
  Found by accident, which is worth recording: a search-semantics probe returned pin names that
  didn't match what I had written, and the mismatch was the sanitizer, not the search.
- **Search criteria verified correct** — `filter_by_criteria` was read and then exercised against a
  real database rather than reasoned about. `has_visits=yes/no` is right in both directions
  (including the `exclude()`-across-a-multi-valued-join case, the classic Django footgun here),
  multiple tags AND correctly, and no duplicate rows are returned. The docstring's "(distinct)"
  promise holds, and `apply_label_groups`, which documents that its caller must add `.distinct()`,
  has exactly one caller, which does.
- **mypy is clean across all 780 files** — run after ~140 files of audit changes. It found exactly
  one error, and it was mine: `LabelledModel._labels_of_kind` reached for `self.labels`, which every
  subclass supplies but the mixin never declared. Fixed by declaring the contract
  (`labels: Manager[Label]` under `TYPE_CHECKING`) so the requirement on subclasses is explicit and
  checked — not by casting or ignoring, per the project's standing instruction that mypy exists to
  find real problems.
- **Two more bug classes swept to exhaustion, both already clean.** The prefetch-defeating idiom
  fixed in chunk 99 (`.all().<queryset_method>()`) survives in only two places, both harmless: a
  `.delete()` (which ignores the prefetch cache anyway) and `Pin.rating`, left deliberately because
  nothing prefetches `reviews`. `Album.photo_count` and `PinList.pin_count` were already written
  prefetch-aware, with docstrings explaining why. Query-scaling coverage is now complete too:
  `/dashboard/lists/` redirects to `/dashboard/organize/?tab=lists` (flat at 38 since chunk 107) and
  the albums page is flat at 18, so every reachable page has been measured.
  A third sweep — for unserialised check-then-create, the shape behind the article, calendar-sync and
  undo findings — produced **no reliable signal**: a loose predicate matched 125 mostly-benign
  "fetch, modify, save" functions, and a tightened one matched zero. Recorded as inconclusive rather
  than reported either way; the three instances of that class were each found by reading a
  subsystem, not by pattern-matching.
- **Group-chat removal cuts off live delivery correctly** — verified and now pinned. This was
  checked because chunk 52 found the same shape broken in safety check-in chat, where a revoked
  emergency contact kept receiving over an already-open socket. Group chat is architecturally
  immune: `broadcast_group_message` resolves `active_memberships()` at send time and addresses each
  member's *own* per-profile channel, so a member removed mid-session is simply no longer in the
  recipient list. HTTP access is already covered by existing tests (404, and messages sent during an
  absence stay hidden).
  What was missing was a test on the delivery path, and it is worth having precisely because the
  safe design is the expensive one: the payload is rebuilt per member so a masked display name is
  resolved through each viewer's visibility, and the docstring calls that "the accepted price". The
  cheap optimisation - one shared payload to a single per-group channel - would silently restore
  delivery to anyone still connected. The new test pins the *recipient set* rather than the
  transport, so it survives a transport change but not a loss of per-member resolution.
  Teeth-checked by broadcasting to all memberships instead of active ones.
- **Every cache-based lock swept for the release defect; a third instance found and fixed.** After
  hitting it in the beat sweeps and again in the upload lock, all 21 `cache.add` sites were
  classified. Most are debounce/throttle markers with no release (correct by construction), and two
  — `boundaries.schedule` and `schedule_panel_fetch`'s broker-down path — delete microseconds after
  adding, to roll back a failed enqueue, so they have no overrun window.
  The real third instance is the panel single-flight marker. It is added by the *enqueuer*, so its
  150s TTL covers queue wait **and** execution: on a backed-up `panel_fetch` queue it can lapse
  before the task starts. The worker then cleared it unconditionally in a `finally`, deleting the
  *next* schedule's marker and letting the poll after that dispatch a third fetch — each duplicate a
  real, paid upstream call. The token now travels with the task, and `flight_token=None` keeps the
  old unconditional clear for tasks enqueued before the token existed, so a deploy does not strand
  in-flight markers for a full TTL. Teeth-checked.
  Three existing tests asserted the exact dispatch args and so failed on the added parameter; they
  now accept the token with `mock.ANY`, keeping what they actually test (source key, pin, queue
  routing) intact rather than being loosened wholesale.
- **Detail pages are flat too** — extended the scaling sweep to parameterized routes, scaling each
  along its own child dimension: pin detail against comments (with replies) and against labels, trip
  detail against activities. All constant (pin detail 36 queries at 2 and 20 comments; trip detail
  24). Combined with the parameterless sweep, the only two N+1s in the app were the template/property
  accesses found in chunks 107-108.
- **The upload quota lock released a lock it no longer held** — fixed, the same defect as the beat
  sweep locks and now sharing their fix. `per_profile_upload_lock` released with a bare
  `cache.delete` guarded only by "did I acquire it", which is not "do I still hold it": an upload
  slower than the 30s timeout has already lost the lock to its successor, and its release dropped
  *that* upload's lock. It now uses `services.core.locks`' token-checked release.
  Separately filed: six check-then-create sites skip the lock entirely, and they are all the
  background ones (`tasks.py` never imports it) — the paths where a bulk import fans out one task
  per image and contention is highest. Not fixed, because the lock is deliberately fail-open, so
  wrapping those sites would look like protection while changing almost nothing under real fan-out.
  The docstring already names the real fix (a running-total column), which is a design decision.
- **Every parameterless page swept for query scaling; one more N+1 found and fixed.** Using the
  technique from the Organize fix, all 232 reversible routes were rendered at two data sizes; 98
  returned 200 at both. Exactly one grew: `/dashboard/spotguessr/pins/`, at **7 queries for 4 pins
  and 35 for 32** — precisely one per pin. The cause is the trap the map payload's own comment
  documents: each pin's label falls through to `Location.display_name`, which reads the reverse
  OneToOne `wiki`, and the queryset joined `location` but not `location__wiki`. Fixed by adding the
  join; the endpoint is now flat. Teeth-checked by reverting it.
  Also verified the background `rebuild_map_pin_cache` path, which passes a `select_related("location")`
  queryset into the cache: flat at 2 queries for 5 and 40 pins, because `prepare_queryset` adds the
  `location__wiki` join downstream. The remaining bare `select_related("location")` sites are
  single-object `.first()` fetches where the join is immaterial.
- **The Organize page issued a query per label card, three times over** — fixed. Its templates ask
  `{% if not label.profile %}` to decide whether to show the "global" chip, and accessing `.profile`
  fetches the owning row; the card template asks it three times, with more in the priority list and
  row partials. Measured end-to-end through the view: **134 queries for a profile with 5 pins, 244
  for one with 60** — 206 of those 244 were the identical `dashboard_profiles` SELECT. `profile_id`
  answers the same question for free, so a documented `Label.is_global` property now reads that.
  After: **38 queries at both 5 and 60 pins** — flat instead of ~2 per label.
  Swept the other hot pages the same way: map, trips, memories and lists are all already flat
  (the map payload is a constant 3 queries whether it serialises 10 pins or 50).
  Guarded by a new test asserting the count does not *grow* with data size rather than pinning a
  number — growth is the defect; a fixed constant would just get bumped. Teeth-checked by reverting
  one template: `89 queries for 4 pins, 117 for 32`.
  **The measurement trap is recorded in the test**, because it caught me first: the first user in a
  fresh database is auto-promoted to site admin, whose page renders differently, so my initial
  reading ("5 pins → 15, 40 pins → 29") was comparing an admin against a non-admin, not two data
  sizes. Controlling for it showed the map was flat all along and the real regression was elsewhere.
- **Deleted-account fallout, swept** — the chunk-105 bug generalised: which other `SET_NULL` FKs
  to `Profile` leave a permission check degraded? Enumerated all 44. Only `trip_visibility` ever
  reads a *visibility setting* through such a relation, and that is the one already fixed;
  everything else is attribution (`created_by`, `editor`, `submitter`), and the access checks that
  touch nulled FKs are positive matches (`contacts.filter(contact_profile=profile).exists()`), which
  fail closed. The single other "restriction guarded by owner non-null" shape in the codebase is a
  reply notification, where a NULL author correctly means nobody to notify.
- **Settings export/import round-trip now has a completeness arm** — added. The existing round-trip
  test names three fields by hand, so a field added to `_export_settings` and forgotten in the
  importer passes: the export carries it, the restore drops it, and the setting reverts to the model
  default — which for a privacy field is the *more permissive* value. The new test exports a profile
  whose settings are deliberately non-default, imports that into a fresh profile, re-exports, and
  diffs, so drift surfaces whatever the field is called. Teeth-checked by removing one field from
  the import allowlist: it reports `privacy.common_pins_visibility: ('no_one' -> 'friends')`.
  Worth noting *why* this was worth building: I had "found" a round-trip gap by comparing the export
  block against the importer's `_PRIVACY_FIELDS` tuple by eye, and was wrong — booleans are handled
  on a separate path. That was the third false alarm this session from comparing lists or counting
  pattern hits. The test does the comparison properly and permanently.
- **A deleted account's trip locations became visible to everyone** — fixed, and found by
  accident. `apply_trip_visibility_filter` opens with `hidden_out.update(a.id for a in sensitive if
  a.added_by is None)` under the comment "Activities where the adder's account was deleted: treat as
  most restrictive". That line could never fire: the only production caller,
  `viewer_hidden_activity_ids`, built `sensitive` with `... and act.added_by_id and ... and
  act.added_by and ...`, filtering those very rows out first. `added_by` is `on_delete=SET_NULL`
  and every production creation path sets it to a real profile, so NULL means the adder deleted
  their account — and their activities, whose locations they may have restricted to `NO_ONE`, fell
  through as fully visible to every trip member. Four consumers were affected (trip map, AI
  suggestions, calendar export, activities panel).
  It survived because every existing test calls `apply_trip_visibility_filter` *directly*, passing
  activities that bypass the `sensitive` filter — so the branch looked exercised while being dead in
  production. There was no test for the deleted-adder case at all. Fixed by letting those rows reach
  the filter rather than duplicating the rule in the wrapper, so it stays in one place.
- **`trip_visibility` re-implements the shared gate and is stricter than it** — filed, not changed.
  It buckets activities and resolves each bucket with its own queries (a deliberate choice, to
  answer for a whole list in a fixed number of queries). Measured against a live database, it
  diverges from `Profile.visibility_permits` in two ways: it ignores pending friend requests, which
  the canonical evaluator honours, and it reads `COMMON_PIN` as "has a pin at *this* location"
  rather than "shares any pinned location". Both fail closed, so neither leaks — which is exactly
  why it is filed: making them agree would *reveal* currently-hidden locations. The danger is a
  future cleanup unifying them onto `visibility_permits`, as that method's own docstring invites,
  silently widening access with no test failing.
- **"Visited Together" leaked past the privacy gate its neighbour respects** — fixed.
  `_add_common_context` gates `common_pin_count` behind `Profile.can_view_common_pins_with`, a
  deliberately *mutual* check whose docstring explains why: "revealing which locations a pair of
  users have both pinned exposes information about both of them, not just this profile". The
  comment above the gate goes further, noting the count itself had to be gated, not just the link.
  `shared_visited` was then computed and put in the context unconditionally, and the profile
  template renders it as a "Visited Together" stat whenever it is non-empty — so a profile whose
  `common_pins_visibility` forbade the viewer (or a viewer whose own setting forbade it) still
  disclosed how many locations the two had both **visited**. That is strictly more than the thing
  deliberately protected: a shared pin means two people bookmarked a place; a shared visit means
  both were physically there. There is no separate visits-visibility setting, so the common-pins
  gate is the applicable one. Found by following an author's own `# TODO: Whatever is happening
  here is probably wrong.` Written test-first, including an anchor proving the stat still shows
  when both sides opt in.
- **Outbound HTTP timeouts are complete** — all 12 raw `requests`/`urlopen` call sites pass an
  explicit `timeout`, and every gateway/plugin call goes through `_RateLimitedSession._do_request`,
  which sets a `(5, 30)` default precisely because `requests` has none. My first scan reported the
  Google OAuth token exchange as missing one; that was a false positive — its `timeout=` sits nine
  lines below the call, past the window my line-based scanner used. Re-run with `ast`: zero misses.
- **Two `TODO: Don't hardcode 'user' string` markers closed** in the label queryset, now using
  `KIND_USER`/`KIND_MEDIA` — the same cleanup chunk 99 applied to the tag/category/status siblings.
- **A double-clicked Undo restored everything twice** — fixed. `restore_undo_action` checked
  expiry, called the handler's `restore()`, then deleted the entry, with nothing claiming the row
  in between. Both halves of a double-submit fetch the entry while it still exists, so both pass
  the check and both restore. All three entry points (web undo view, pin bulk-delete, external API)
  look the entry up and hand it straight to the service, so a double-click is the ordinary way in.
  What made this worth fixing at the service layer rather than per handler: whether a duplicate
  actually appeared depended on whether the *handler* happened to hit a unique constraint on the
  way. `PinUndoHandler` is saved by `db_pin_unique_location_per_profile` for root pins (not child
  pins), and the saved-filter, label, wiki, pin-list, markup-map and safety-checkin handlers each
  re-check something before recreating — but `TripUndoHandler.restore` does an unconditional
  `Trip.objects.create` and duplicates outright. A new handler without a convenient unique
  constraint would silently inherit the bug. The entry is now claimed with `select_for_update()`
  inside the existing transaction; a second request waits, finds the row gone, and raises
  `UndoAlreadyRestoredError` — a subclass of `UndoExpiredError`, so all three callers keep working
  unchanged while still being able to tell the two apart. Written test-first.
- **Keyset cursors and pagination are correct** — checked and clean, recorded so it is not
  re-derived. Both cursor implementations (the shared `keyset_cursor` module and `pin_sync`'s older
  private copy) pair the right predicate with the right ordering — `(stamp > s) OR (stamp = s AND
  pk > pk)` with ascending order in `pin_sync`, the `<` mirror with `-created, -pk` in the
  friendship feed — and all three sites trim the extra probe row *before* deriving `next_cursor`,
  so no row is skipped between pages. All 15 `get_page` call sites paginate an ordered queryset,
  several via the model's `Meta.ordering` rather than an explicit `order_by`, so Django's
  inconsistent-pagination trap is avoided.
- **Rate-limit rejections consumed the budget that rejected them** — fixed, in two places.
  Every blocked attempt writes an `ApiCallLog` row so the rejection shows up in usage reporting
  (`was_rate_limited=True`, or `was_service_disabled=True`). `check_rate_limit` then counted rows
  for the service excluding only `was_geo_filtered=True` — so those rejection rows were charged
  as though requests had gone out. The per-minute limit is what makes it bite: a burst produces one
  rejection row per over-limit attempt, and each is charged against the *daily* and *30-day*
  budgets, which are much larger and far slower to recover. A caller retrying into a per-minute
  wall therefore spends a day's allowance without a single request leaving the process, and the
  harder it retries the faster its real quota disappears.
  That `was_geo_filtered` was already excluded is what makes this a bug and not a design choice —
  skipped calls were understood not to count; two of the three skip reasons were missed. Both
  counts now use a documented `ApiCallLogQuerySet.billable()`. A call that went out and *failed*
  is still billed, since the remote service counted it.
  The same count drove `compute_service_budget`, which decides how many enrichment calls a sweep
  may still make. Measured with the fix reverted: after a burst of 10 rejections against a daily
  limit of 10 with only 2 real calls made, the computed budget is exactly **0** — enrichment stops
  for a service whose quota is 80% unspent. Found by TDD (tests written failing first), and the
  enrichment case separately teeth-checked.
- **`_reserve_call` itself is sound** — the check-then-insert race is properly closed with
  `select_for_update()` on the service's `ApiRateLimit` row, and the blocked branches deliberately
  exit the `atomic()` block before raising so the rejection row is not rolled back with it.
- **Sweep locks released unconditionally, so an overrun degraded exclusion** — fixed. Ten beat
  tasks took an overlap lock with `cache.add(key, ..., ttl)` and released it with a bare
  `cache.delete(key)` in a `finally`. That is correct only while a run finishes inside its own TTL.
  When it does not: the lock expires, the next tick acquires it, the slow run's `finally` then
  deletes *that* run's lock, and the tick after acquires immediately - exclusion degrades with each
  overrun instead of recovering. Overrun is structural rather than hypothetical here:
  `send_due_checkin_reminders` sends SMTP **inline, per row**, so its runtime grows with the data
  while its TTL is a fixed 270s. The consequence is user-visible because the notification and the
  email are both sent *before* the status compare-and-set that makes the row stop matching, so two
  concurrent runs can notify a user twice about one check-in. Now `services/core/locks.py` stamps a
  token into the lock and deletes only if that token is still present, logging a warning when it is
  not (an overrun means a mistuned TTL and should be visible). The read-then-delete is not atomic -
  Django's cache API has no compare-and-delete - but it narrows the window from the whole sweep to
  the gap between two cache calls. The existing `test_beat_lock_intervals` covers TTL-vs-interval
  drift; this covers what happens once a TTL is exceeded.
  The refactor was caught by the existing suite in the best possible way:
  `test_beat_lock_intervals` discovers locks by scanning `tasks.py` for `cache.add(...)`, and it
  carries a deliberate self-guard ("guard against the AST scan quietly matching nothing after a
  refactor") that failed the moment the idiom changed. Its predicate now matches both forms, and
  was re-verified by dropping an entry from the coverage map to confirm it still reports the gap
  rather than passing vacuously.
  Note what was *not* converted: the debounce markers (`_debounce_key`, flight keys) deliberately
  have no release, and `generate_boundaries_for_location` releases a lock acquired by its
  *enqueuer*, a cross-process handoff the token scheme does not fit.
- **`send_checkin_reminder` is correctly guarded against repeat sends** — checked because
  `due_for_reminder()` filters only on status and a time window, with nothing excluding
  already-reminded rows. It is safe: the send ends in a conditional `update()` filtered on
  `status=SCHEDULED`, a proper compare-and-set, and the queryset selects on that status.
- **Signal and transaction discipline, swept exhaustively** — clean, recorded so it is not
  re-derived. All 50 `@receiver` handlers pass `dispatch_uid`; none calls `save()` inside a
  `post_save`/`pre_save` (the two hazards `CLAUDE.md` calls out); and no enqueue sits inside an
  `atomic()` block without `transaction.on_commit`. The 57 enqueues outside any atomic block are
  safe because `ATOMIC_REQUESTS` is unset, so each write autocommits before the task is queued.
  Worth noting the method: my first two scans returned 0 by being *broken* — the real idiom is a
  nested `def _enqueue()` handed to `on_commit(_enqueue)`, which no indentation heuristic catches.
  Redone with `ast`, validated against known-positive sites (13 deferred enqueues) before the zero
  was believed.
- **`PIN_CACHE_VERSION` is consistent** — TypeScript source and both compiled bundles all read 8,
  so the client cache is not silently stale. A contract test already guards the payload shape.
- **`date.today()` reads the OS clock, not Django's `TIME_ZONE`** — 9 sites, against 10 that
  correctly use `timezone.localdate()`. Currently harmless and *only* by coincidence: `TIME_ZONE`
  is `"UTC"`, the container's OS clock is UTC, and there is no per-user timezone field, so the two
  agree. They would diverge silently the moment any of those three changes. Not converted: the fix
  is behaviour-neutral today, and several of the sites have shadowed or function-local `date`
  imports that make a mechanical rewrite more likely to introduce a bug than to prevent one.
  Filed rather than churned.
- **One unused import removed** (`export.py` imported `datetime.timezone` and never used it, while
  shadowing Django's `timezone` for anyone later editing the file). Found only because ruff
  disables `F401` project-wide — deliberately, per the comment in `pyproject.toml`. That decision
  is defensible and was left alone: 147 unused imports exist, but a blanket `--fix` would delete
  side-effect imports such as `undo/service.py`'s `handlers`, which silently registers every undo
  handler. Any future re-enabling needs per-file ignores for those first.
- **Object-level authorization, swept across the whole controller layer** — a negative result
  worth recording. Every lookup keyed on a URL-supplied id was checked (35 candidates across 18
  controllers): all are scoped, either by the queryset itself (`between(profile, partner)`,
  `group=group`, `checkin=checkin`, `wiki=wiki`, `suggested_to=profile`), by a helper that scopes
  (`_participant_session`, `resolve_visible_wiki`), by an ownership check on the next line, or by
  `PermissionRequiredMixin` on the admin views. The REST surface is deliberately tiny — two
  viewsets, both filtered to `profile__user=request.user`, under a global `IsAuthenticated`
  default. No IDOR found.
- **`pin.categories`/`.tags`/`.statuses` silently defeated every caller's prefetch** — fixed.
  They were `self.labels.all().categories()`, and chaining a queryset method onto `.all()` builds
  a new queryset, which ignores the prefetch cache. Measured: 25 pins cost 2 queries to load and
  **77** once the three accessors were touched — three extra queries per row, for callers who had
  explicitly prefetched. `services.map_pins.payload` already filtered the prefetched list in
  Python; the same three accessors on the model did not. Now a shared `abstract.LabelledModel`
  mixin (Pin and Wiki had duplicate copies), filtering in Python: **77 → 2**. No migration — the
  mixin declares no fields, confirmed with `makemigrations --check`. Unprefetched callers pay
  exactly what they did before. Nothing in the codebase consumed these as querysets, so the
  return-type change to `list` reaches no caller; `map/data.html`'s `pin.categories` reads a
  payload dict, not the model.
- **Calendar sync**: token storage is encrypted (`refresh_token` is an `EncryptedTextField`), the
  export enqueue correctly defers to `transaction.on_commit`, and trip membership creation is
  idempotent through `get_or_create`. One gap found and filed: the "already imported this event"
  guard checks `(profile, google_event_id)`, but the model's unique constraints are on
  `(trip, profile)` and `(trip, profile, activity)` — so a double-submit imports one calendar event
  as **two trips** without violating anything — confirmed against a real database, not inferred.
  It is the same check-then-write shape as the article conflict, but unlike that one the obvious
  remedy is wrong: a plain unique constraint on that pair would reject every profile's second
  *timed* import, because those deliberately store a blank event id on the trip-level link and
  blanks are not distinct to a unique index. The fix has to be a partial constraint, and its
  migration must delete rows — choosing which link survives is a decision about live user data.
- **Check-then-write races, swept generally** after the article TOCTOU. A scan found 42 functions
  that read state (`.exists()`/`.count()`) and then `create()` with no `atomic()` or
  `select_for_update()` in the same function — but almost all are benign, and the reason is the
  useful part: the ones that matter are closed by a **database** constraint, so the application
  check is a friendly-error path rather than the safety mechanism.

  Verified individually for the cases with real consequences: `ApiKey.prefix` is unique (a
  colliding prefix loses at insert, so key lookup can never be ambiguous); `ProfileEmail` is unique
  on `normalized_email` rather than the raw string, which is the right key for case- and
  dot-insensitive identity; `CustomField` on `(profile, entity_type, name)`; `PinList` on
  `(profile, name)` and `(profile, slug)`; `WebAuthnCredential` on `credential_id`. The upload
  paths in the list are false positives — they serialise on `per_profile_upload_lock`, which the
  scan did not know to look for — and the import paths are single-user and sequential.

  That is what made the article conflict check exceptional: `ArticleRevision` has no revision number
  and no unique constraint, so there was nothing underneath the check. It is the one place in this
  sweep where a row lock was the *only* available answer.
- **Pagination ordering**: paginating an unordered queryset silently duplicates and drops rows
  across pages, and only 56 of 165 dashboard models declare `Meta.ordering` — so this looked like a
  likely gap. It is not: all 14 call sites go through one `services/core/pagination.get_page`
  helper (which also clamps out-of-range pages rather than raising, so a stale HTMX pagination link
  cannot error), and re-running the paginated-view bands with
  `-W error::UnorderedObjectListWarning` produced **zero** occurrences. Django emits that warning
  precisely for this bug, so escalating it is a direct test rather than an inference from
  `Meta.ordering` counts.

  That run *did* surface 8 failures, and chasing them was worth recording: they were **my own
  reused test database**, poisoned by an earlier `TransactionTestCase` band truncating
  migration-seeded `SubscriptionRole` rows. Same trap as item 4 in section 4 above — the tests pass
  on `--create-db` (12/12). Nothing to do with pagination, and nothing to do with the `-W` flag,
  which is what the first two hypotheses were.
- **Consensus trust math**: the Beta-Bernoulli posterior is right in the place it is usually
  wrong. Ageing is `gamma * (alpha - alpha_prior) + alpha_prior` — it decays the *evidence* toward
  the prior, not alpha toward zero, which is what keeps the posterior mean meaningful as history
  fades; the naive form drags the score to 0 regardless of accuracy. The score itself is the
  posterior mean `alpha / (alpha + beta)`, updates run under `select_for_update` inside a
  transaction, check-injection probability is linear in `1 - score` with a floor and ceiling (so a
  veteran is still occasionally re-checked and a newcomer is not swamped), and competitive rounds
  key off the *least*-trusted participant while honouring every participant's cooldown. The
  `random.random()` used for injection carries a `# noqa: S311` — defensible here: an observer
  learns one bit per round from a process-wide RNG interleaved across all users, which is not a
  path to predicting when they will be tested.
- **Exception hierarchies across the other untrusted-input parsers**: swept every third-party
  parser reachable from an upload after the Pillow findings. `zipfile.BadZipFile`,
  `LargeZipFile` and `tarfile.TarError` are all *not* `OSError`/`ValueError` subclasses — and the
  archive extractor already catches each explicitly. `gpxpy`'s `GPXException` is likewise unrelated
  to either, and the importer lists it explicitly too. `BadGzipFile` and `JSONDecodeError` do
  inherit from `OSError`/`ValueError` respectively, so the generic tuples cover them. The KML pair
  above was the only gap.
- **Image pipeline fidelity**: transparency and colour survive the downscale in every combination
  that matters — an RGBA PNG keeps its alpha through PNG→PNG *and* PNG→WEBP (the code converts to
  RGBA rather than flattening), and an ICC profile survives JPEG→JPEG and JPEG→WEBP. Animated GIFs
  are excluded from re-encoding entirely, so they cannot be flattened to a single frame.
- **Mass assignment and form validation**: checked every DRF serializer in `external_api` for a
  writable field that should not be — `profile`, `user`, `role`, `is_staff`, `scopes`,
  `total_paid_cents`, `stripe_*`, `processed_at`, `id`/`pk`. Two hits, both `uuid` on a *create*
  serializer, and both deliberate: an offline client stamps its own idempotency uuid and retries
  until acknowledged. It is scoped correctly (`Pin.objects.filter(profile=profile, uuid=...)`, so a
  replay can only ever return the caller's *own* pin), and a cross-profile collision is caught from
  `IntegrityError` and converted into a proper `PinCreationError` rather than the 500 it would
  otherwise become. The uuid space is unguessable, so the collision error is not a useful oracle.
  Forms are a thin surface — 7 modules, one `clean()`, and it correctly invokes the parent per file
  (via `single_clean = super().clean`, which a naive AST check reads as a missing `super()` call).
- **Subscription feature gating**: every `SiteFeature` is enforced server-side, not only in
  templates — `VIDEO_UPLOADS` at the upload chokepoint, `AI` across 18 sites, `PLACES` across 9.
  The one asymmetry is `ALPHA_FEATURES`, which gates the games *hub* and the nav item but none of
  the ~49 views behind it, so a non-entitled user can open any game directly. Verified by probe
  rather than by grep. A mixin applying the hub's check to all 49 views was written and then
  **reverted**: it broke 9 existing tests that play full games with non-entitled users, and no test
  anywhere asserts a game refuses one — so the suite encodes "games are open, discovery is gated",
  and tightening it is a product change rather than a defect fix. Filed in `PROBLEMS.md` with the
  measurement, both readings, and the mechanical work either would need.
- **Stripe billing**: the webhook path is careful in every respect that matters — the
  `Stripe-Signature` header is verified against the **raw** body, an unset secret fails *closed*
  (503, not "process it anyway"), each event id is idempotent under a `select_for_update` row lock
  so a duplicate delivery cannot double-credit, and the raw payload is written in its own
  transaction *before* the handler runs so a crash still leaves something to debug. The view is
  correctly `csrf_exempt` and unauthenticated, which a webhook must be. Unknown event types are
  ignored with a 200, which is right — Stripe sends more than you subscribe to. The nightly
  drift-correction sweep guards each subscription individually, including the *apply* step, with a
  comment explaining that `items.data[0]` indexing would otherwise abort the sweep for everyone
  after one oddly-shaped subscription.

  One gap, filed rather than fixed: **no refund or chargeback handling**. `total_paid_cents` is
  the pay-what-you-want entitlement and is monotonically increasing with no decrement path, so a
  refunded or disputed payment keeps the access it bought. The remedy is a policy choice (full
  claw-back? pro-rata? only on a lost dispute?), so it is in `PROBLEMS.md` with the mechanical part
  spelled out.
- **Storage quota enforcement**: every upload path — pin galleries, articles, journal visits,
  DMs, safety, consensus, tools, map overlays, the photo services, the four `tasks.py` fetchers,
  and pin suggestions — pairs `quota_error_for_upload` with `per_profile_upload_lock`, so the
  read-then-write race that check would otherwise have is closed. The two files that mention quota
  without calling it are correct: the external-API social path documents that it creates no `Image`
  row at all, and map overlays defer to the canonical upload service. Quota is aggregated live from
  `file_size`, which both media pipelines still update after a rewrite — including the two paths
  changed earlier in this audit. The community quota bonus resists the obvious abuse: self-votes
  are excluded, and its `count()` of relevance rows really is a count of distinct profiles, because
  `MediaRelevance`'s unique constraint covers exactly the four fields it filters on.
- **Celery beat and queue routing**: all 24 beat entries resolve to registered tasks; no task
  declares a queue no worker consumes (only `celery`, served by the default worker, and
  `panel_fetch`, served by its dedicated one). Every lock-guarded beat task's TTL is correctly
  under its interval today — 110s/120s for the stall sweeps, 270s/300s for the safety sweeps,
  3300s/3600s for enrichment and trivia — so the guard added above is a fence around a currently
  correct arrangement, not a fix. Worth noting the interaction with the chunk-2 safety fix: those
  sweeps have no explicit `time_limit` (they inherit the global 3600s), so a run exceeding 270s
  *could* overlap the next tick — but the compare-and-set reminder/escalation guards added earlier
  make an overlapping run idempotent rather than a source of duplicate emergency contact
  escalations.
  *(First pass reported all 24 beat entries as unresolvable. That was the probe, not the app —
  a bare `django.setup()` hadn't imported the tasks module Celery autodiscovers. 24/24 failing is
  the signature of a broken check; re-ran with the import and got 0.)*
- **WebSocket authorization**: audited all seven consumer routes — a surface the chunk-8 IDOR
  sweep never covered, since it walked HTTP routes only. The design is strong: a shared
  `_ParticipantSessionConsumer` base, credential scope checked *before* the membership lookup so a
  refused connection never briefly joins a broadcast group, group-membership cleanup when
  `accept()` fails after `group_add` succeeded, uniform 4404 closes that don't distinguish
  "not yours" from "doesn't exist", and periodic credential re-validation so revoking a leaked key
  actually stops an open socket. Every route verifies membership, not merely authentication. The
  one gap was the contact route's missing revocation path, above.
- **The rest of the tracking/privacy toggle matrix**: chased the shape of the two location-strip
  bugs across every `Profile` tracking setting, since "enforced in one place, not another" had now
  produced three findings. `track_routes` has a single save path and it is gated;
  `track_geolocation` gates its live-ping producer; `track_device_scans` correctly de-attributes
  rather than refusing the upload; the Google Takeout importer gates on `visit_logging_allowed`
  before doing anything. The one crack was the `VisitSource` mislabel above, plus an open question
  about which setting *should* own dwell visits, filed in `PROBLEMS.md` as the settings-copy
  decision it is rather than decided here.
- **Derived artefacts don't reintroduce stripped location**: `render_preview` re-encodes without
  an `exif=` argument, so previews carry no EXIF at all; no original-file backup is retained
  anywhere, so a strip cannot be undone from a kept copy; and `exif_data` is never serialized to
  a client — its one non-model reference is the pin-share path copying it onto the recipient's own
  row, which is the intended semantics of sharing a photo. There is no location-obscuring share
  mode for photo EXIF to defeat: sharing a pin shares its exact location by design, which is what
  the `LocationExposure` provenance chain exists to track.
- **Cache-key scoping**: audited all 125 Django cache call sites for the "content varies by
  viewer, key doesn't" leak. None found. The per-user caches are all correctly scoped
  (`search_hints:{profile.pk}`, `ul_immich_thumb_{account.pk}_{asset_id}`, Google Photos keyed on a
  session whose owner is separately cached and checked); the unscoped ones
  (Google Places, NPS, Wikipedia, Overpass) hold genuinely public upstream data whose content
  depends only on the key. No template fragment caching and no `cache_page` anywhere, so the two
  classic sources of this bug are absent by construction. The gap was in HTTP cache directives, not
  cache keys — see the media finding above.
- **The rest of the geospatial surface**: all 45 `Point(...)` constructions are lng-first; every
  `PointField`/`PolygonField` in the app is `geography=True, srid=4326`, so all 17 `__distance_lte`
  / `Distance()` uses are in real metres with no geometry-vs-geography unit mixing.
  `buffer_point_by_meters` correctly stretches the longitude axis by `1/cos(lat)` (with a `1e-6`
  clamp against the poles) rather than buffering isotropically in degree space;
  `site_scope.meters_between` converts to radians before `cos`; `bearing_degrees` is a true
  great-circle forward azimuth, not an atan2 over degree deltas — which matters because it gates
  arrow-markup share detection and thus the `LocationExposure` chain. The four independent
  haversine implementations (`markup`, `consensus.fields`, `device_scan.clustering`, plus the
  equirectangular `site_scope`) are duplication worth consolidating, but all four are correct.
  `Location.point`'s `default=Point(0, 0)` is a shared mutable default and would be null-island
  data if it ever reached the DB, but `save()` unconditionally derives the point from
  lat/lng, lat/lng are non-nullable and immutable, nothing `bulk_create`s a `Location`, and no code
  mutates a point in place — so it is inert rather than latent. The GeoJSON/KML/GPX exporters set
  no CRS, correctly: all three formats define WGS-84 as their only coordinate system.
- **Naive-datetime handling is otherwise correct**: the one `RuntimeWarning` in a full 10,285-test
  run is a test fixture (`test_pin_queryset.py:134` passes `date.today()`), not production code.
  Production consistently uses `timezone.make_aware(...)` or explicit `tzinfo=UTC`, and
  `memories/aggregator._date_to_datetime` builds a naive value only transiently before calling
  `make_aware`. Chasing that warning did surface a real timezone bug in trip weather matching —
  filed in `PROBLEMS.md`.
- **Exception-swallowing discipline is good.** 169 broad (`Exception`/bare/`RuntimeError`)
  non-reraising handlers exist, which sounds alarming until you look: they cluster in
  `consumers.py` and `tasks.py`, where being defensive is correct (one bad frame must not kill a
  socket; one bad row must not abort a sweep — see the safety-sweep isolation tests). **164 of the
  169 log the failure.** The five that neither log nor re-raise are all small best-effort
  fallbacks: `available = False`, `return _DEFAULT_MAX_SLUG_LENGTH`, `xmp = {}`, `return url`,
  `return False`. Nothing hidden.

  The subset that *is* worth attention is different: **ten handlers catch `RuntimeError` inside a
  tuple**, which also swallows the test network guard's own exception — so any unmocked
  integration on those paths surfaces as a green test plus an unread log line (exactly the
  chunk-44 finding). That static list is the candidate set; the ERROR-log sweep over passing tests
  is what tells you which ones actually fire.
- **Full-suite ERROR-log sweep**: ran all 10,285 tests with `-o log_cli=true --log-cli-level=ERROR`
  to surface failures that passing tests hide (the runner suppresses logs unless a test fails).
  **Every entry is either a deliberately-provoked test fixture or benign** — the giveaways are the
  names: `broken_panel`, `test_broken`, `send-explodes@example.com`, `<Mock …> failed`, a "broken"
  shapefile, a Stripe event for `id 999999`. The error-isolation design shows up clearly here:
  journal sources, memory sources, search providers, panel sources and photo-keyword providers all
  log-and-continue when one of them fails, exactly as their tests assert.

  Two entries were worth chasing. The `SiteSettings.notify_gotify_token` decrypt failures (3) are
  the fail-soft behaviour added by this audit, working as intended. The most frequent —
  `Failed to log API call for service article_safety` (7) — turned out to be
  `DatabaseOperationForbidden`: `ClassifyArticleTextTests` is a `SimpleTestCase`, and the path it
  exercises calls `log_api_call`, whose blanket `except Exception` (deliberate: "logging problems
  never break callers") swallows the refusal. Production is fine; the note is that those tests
  silently never exercise the cost logging, so an assertion about `ApiCallLog` there would be
  misleading. Left as-is — the swallow is correct, and the alternative is making a pure-text test
  hit the database.
- **Plugin system**: all **43** plugins carry complete metadata (`verbose_name`, `description`,
  `author`) and every one contributes through at least one of the seven hooks. A narrower check
  of just `get_service_defaults`/`get_panel_sources` flags four (`kartaview`, `mapillary`,
  `panoramax`, `photo_keywords_metadata`) as contributing nothing — they contribute imagery and
  street-view providers instead, so that reading is wrong. The registry also refuses a nameless
  plugin and warns on duplicate service keys.

  One number from that audit corroborates the cost-tracking fix from a different direction:
  **zero of the 43 plugins declare a `cost_per_call`**. Combined with the single priced entry in
  the static `SERVICE_REGISTRY`, that is why `api_spend_summary_30d` keying on "has a flat price"
  excluded essentially everything real.
- **Signal rules**: no `post_save`/`pre_save` handler calls `save()`, and every signal connection
  passes `dispatch_uid` (including the loop in `achievements/signals.py`, which a line-based grep
  reports as missing it).
- **Payload/serializer drift**: `build_photo_payload`/`PhotoSerializer` and
  `build_conversation_payload`/`ConversationSerializer` match exactly.
- **Achievement sweep cost**: measured, and it is *not* clean — ~30 queries per profile per night,
  killed at the 3600s task limit with a silent fixed tail of unevaluated users. Filed in
  `PROBLEMS.md` with numbers and two candidate fixes.
- **Global search cross-user isolation**: gave one user objects of 12 searchable kinds, each
  carrying a unique nonsense token, then searched as a second user for every token — **0 results**
  leaked, while the owner finds **12/12** of their own (so the probe is meaningful rather than
  passing because search returns nothing). All 11 providers scope by `profile`.

### Empty grep output is not evidence (three times in one chunk)

Chunk 163's queue audit began with three greps that all returned nothing: worker `-Q` flags in
`docker-compose.yml`, `queue=` assignments in code, and `task_routes` in settings. Read naively that
says "no queues are configured anywhere", which would have been a dramatic and false finding.

All three were pattern errors. The compose file spells the worker command as a YAML list, so `-Q`
and its value sit on separate lines and no single-line pattern matches both. `queue=["']literal["']`
cannot match `queue=source.queue`. And of the 21 `queue=` occurrences that do exist in code, 20 are
`refresh_queue=True` - a toast parameter that has nothing to do with Celery.

The lesson is the same one this audit has now recorded for `head` truncation, and it generalises
past that: a search returning nothing is evidence about the *pattern* until it has been shown to be
evidence about the *codebase*. The cheap discipline is to prove the pattern can match something -
count all occurrences of the bare term first, confirm the file is where you think it is - before
letting an empty result support any conclusion.


### One name, four objects: a scan that was 100% wrong

The first version of the `settings.X` check reported **78 undefined settings**. Every one was false,
which makes it the worst instrument in this audit and worth recording in full.

It matched any attribute access on a variable named `settings`, and this codebase binds that name to
four unrelated things:

1. `django.conf.settings` - the intended target;
2. `dashboard.controllers.settings`, a **module** - which is why `settings.SettingsView` and
   `settings.SaveMapPositionView` appeared in `urls.py`;
3. `SiteSettings` **model instances**, conventionally `settings = SiteSettings.get_current()` - which
   is why `settings.save`, `settings.pk`, `settings.max_pins_per_list` appeared;
4. the Pydantic `AppSettings` object from `settings/app.py` - which is why every lowercase
   `settings.google_unrestricted_api_key`-style name appeared.

The tell was in the output rather than the code: `settings.save` and `settings.pk` are not plausible
Django settings, and a real result would not be 78 items where the codebase defines a few hundred.
Restricting to files that import `settings` from `django.conf` *and* never rebind the name locally
drops 1586 files and leaves 17 references, all defined.

The general shape recurs throughout this audit - an instrument whose candidate set is wrong produces
confident output in exactly the format a real finding would take. What distinguishes this instance
is the direction: previous ones were falsely *empty* (nothing found because nothing could be found).
This one was falsely *full*, which is more dangerous, because an empty result invites suspicion and a
long list of plausible-looking findings invites action.


### Four flaws in one instrument, and the finding that survived them

Sweeping `get_or_create`/`update_or_create` for lookups no unique constraint protects. 371 call
sites; the first pass reported **30** unprotected. Four separate flaws, found by checking the
flagged models rather than the flagged code:

1. **Functional constraints invisible.** Reading `constraint.fields` returns `()` for
   `UniqueConstraint(Lower("name"), "pin")`, so every `PinAlias`/`WikiAlias` site - about a third of
   the list - was false. Their expressions had to be flattened for `F()` nodes instead.
2. **`field_id` vs `field`.** `get_or_create(profile_id=...)` does not textually match a constraint
   on `profile`, so `PlaceAccessGrant` and `WikiStatVote` were false.
3. **Related managers.** `obj.children.get_or_create(...)` carries an implicit filter the AST cannot
   see, so those sites cannot be judged and are now excluded rather than flagged.
4. **`**kwargs` unpacking.** `ProfileActivityDay.objects.get_or_create(kind=kind, day=day,
   **_owner_filter(profile))` supplies `profile` invisibly. Both remaining "profile-scoped
   constraint, profile missing" hits were false for this reason - and these were the two that looked
   *most* serious, since a missing profile in the lookup would mean matching another user's row.

After 1-3, sixteen remained; flaw 4 accounts for two more. What survives is a real finding, filed
separately: **`Label` has no uniqueness constraint at all**, and nine sites `get_or_create` on
`(profile, name, kind)` as though it identified a row. `PinAlias` and `WikiAlias` model the same
relationship correctly with `UniqueConstraint(Lower("name"), <parent>)`, which is what makes the
omission on `Label` look like an oversight rather than a decision.

The instrument was wrong four different ways and still found something true. Both halves are worth
recording: a check this crude is not evidence on its own, and discarding it wholesale because the
first output was mostly false would have discarded the finding with it.


### Measuring coverage by route name, and why the number is an upper bound

After the detach 500 turned out to sit on an untested route whose *sibling* was tested, the obvious
follow-up was "how many other routes are like that?". Enumerating the live resolver and exact-matching
each name against the test tree gives 301 uncovered project routes, 187 of them accepting writes.

The instrument has a specific, measurable flaw, found by checking a route that is certainly tested:
`external_api:labels` appears uncovered because its tests address the endpoint by literal path
(`_BASE = "/dashboard/api/external/v1/labels/"`) rather than `reverse()`. Route-name matching cannot
see that. The skew is bounded - 92 literal-path lines against 1,920 `reverse()` calls - but it is not
zero, so 187 is an upper bound rather than a count.

Sampling five of the flagged routes put the true rate high: three have no test mention whatsoever,
and two match only coincidental substrings in unrelated identifiers (`record_consensus_answer_evidence`
for `consensus.answer`, `lists_resynced` for `external_api:lists.resync`). All five are genuinely
uncovered.

The right instrument is `coverage.py`, which is already installed: run the suite under it and report
which view callables never execute. That is a direct measurement rather than a proxy, and it is the
step to take before anyone acts on the list. Recorded here rather than run because a full suite was
already occupying the machine, and two concurrent 85-minute runs would have made both unreliable -
the same mistake this audit made earlier when a stale run competed with a live one for 40 minutes.

### The silent sample: reporting on a population after checking a subset

Three entries in this report originally claimed to have checked a set when the work had covered part
of it. All three were found by re-reading the *unexamined* remainder, and two of the three were
hiding a real defect:

| claim as written | actually checked | what the remainder held |
|---|---|---|
| "all 23 controller creates are guarded" | 14 hand-listed sites | the `pin_edit` detach 500 |
| "only three paths mail arbitrary addresses, each bounded" | 3 of 9 recipient expressions | unbounded secondary-email verification |
| "all 12 `|safe` sites traced to origin" | 8; 4 filtered as "obviously safe" | nothing - the claim held |

The shape is identical each time: an automated pass produces a population (23, 21, 12), a manual pass
inspects the interesting-looking members, and the write-up states the population's number beside the
subset's conclusion. Nothing in the output looks wrong, which is what separates this from the
instrument failures recorded above - a bad instrument produces a suspicious result, while a silent
sample produces a confident one.

Two practical rules came out of it. Enumerate to a list, then tick the list, rather than eyeballing
"the interesting ones" - the detach bug was in the boring-looking remainder both times. And when a
claim says "all N", the N must be the number actually inspected, not the number the grep returned.

### Why the authorization claim was not re-verified statically

Having found three silent samples in this report, the inherited authorization claim - "167
owner-scoped routes, no leaks" - was the largest assertion nobody in this session had checked. A
static attempt to re-verify it does not work, and the reason is worth recording so the next person
does not repeat it.

Scanning all 246 object-fetching handlers in `controllers/` for a requester-scoping token in the
handler body flags **36** as unscoped. Sampling four - `PinOwnerUpdateView.post`,
`MapOverlayEditView.post`, `MarkupJsonView.get`, `ArticleRevisionView.get` - all four are correctly
scoped, each through a different delegation: `_get_pin(request, slug)` then a lookup keyed on that
pin; `_resolve_owner(request, ...)` returning an already-scoped **queryset** that the
`get_object_or_404` then filters; `self.resolve(request, **kwargs)` returning a scope object. Several
more of the 36 are legitimately not owner-scoped at all - the safety contact portal is authorized by
a magic-link token, the `SiteAdmin*` views by permission, `VerifyEmailView` by a token.

So the flag list is dominated by a false-positive mode that no body-level scan can eliminate, because
scoping in this codebase is overwhelmingly delegated. The claim is neither confirmed nor refuted
here, and it is now marked as inherited rather than presented as this session's finding.

The instrument that *would* answer it is behavioural, which is what the original pass describes:
request each owner-scoped route as a second user and assert 403/404. That is a real piece of work
(167 routes × 4 methods) and belongs in its own pass, not in a chunk that has already spent its
budget discovering that the cheap version does not work.

---

## 4. Audit methods that mislead on this codebase

### Regex over templates: a 70% false-positive rate

The accessibility sweep in chunk 219 reported 7 `<img>` tags missing `alt` and 2 icon-only buttons
missing a label. The true counts were 2 and 0.

- **5 of the 7** `<img>` hits were the string `<img` inside a **JavaScript comment** - prose like
  "(including `<img onerror>`)" explaining an XSS guard. A regex scanning `.html` files cannot tell
  a comment inside an inline `<script>` from markup.
- **Both** button hits had a perfectly good `<span>{{ tab.label }}</span>`. The scanner stripped
  `{% %}`/`{{ }}` before checking for text, which is right for deciding "is this icon-only" and
  wrong here: the label *is* the template variable.

The two real findings were JS-generated `<img>` tags built by string concatenation - which the
regex found only by accident, since they are not literal markup either. A template-aware or
DOM-based instrument would have inverted both error directions. Worth remembering before reporting
a count from a template regex as if it were a finding list.


Same spirit as the note at the top of `code_audit_status.txt`. Each of these produced a
confident-looking wrong answer during this sweep.

1. **"Used in a template but not in the SCSS" proves nothing.** Styles live in three places —
   `frontend/sass/`, inline `<style>` blocks inside individual templates, and compiled CSS.
   The SCSS-only version of this check reports ~75 undefined classes, most of them false
   (`.tools-card--wide` is defined in its own template). Even against all three sources the
   remainder is mostly class strings assembled in template expressions (they show up with stray
   quotes, e.g. `.cal-cell--today'`) and semantic JS hooks.
2. **Write counts per view are inflated by dispatchers.** "18 writes with no transaction" in
   `settings.py` is a 15-branch `if/elif` where one branch runs.
3. **`AddIndex` ordering warnings on the big migrations are meaningless.** 0001/0002/0007/0008/0010
   are squashes; interleaving is inherent and they have already been applied.
4. **A test that passes in isolation but fails in a suite may be a poisoned DB, not pollution.**
   See the `--reuse-db` item above before hunting for test interference.
5. **Query-count probes must saturate capped lists.** The trips overview looked flat at 2→12 rows
   because its "recent" lists cap at 5; the per-row cost only appears once the cap is exceeded.
6. **Verify a probe isn't vacuous.** The first undo round-trip probe compared mostly `None`-to-`None`
   fields and would have reported success regardless. Count how many compared values were non-empty.
7. **A passing test can be asserting against a failed operation.** The suite's runner suppresses
   logs for passing tests, so an unmocked integration whose exception is caught (e.g. by
   `except (..., RuntimeError)`, which swallows the network guard's own error) shows up as a green
   test plus a log line nobody sees. Re-running a band with `-o log_cli=true --log-cli-level=ERROR`
   surfaces them; that is how the import-preview test was found to be exercising its error path.
8. **Generic fixtures can't exercise selection-criteria sweeps.** Running all 17 periodic tasks
   against seeded pins/trips/visits and diffing query counts reports every one of them flat at
   1-7 queries — because none of the seeded rows *matches* what each sweep selects (a check-in due
   in this window, an account past its deletion grace, a pin with a placeholder name). Measuring a
   scheduled task honestly needs a fixture built to its own criteria, one task at a time. A
   static "does this loop have a batch bound?" check is the cheap substitute and does carry
   signal.
9. **Baker-made fixtures can be invisible to the code under test.** A cross-user search probe
   reported that a photo's caption was unsearchable — a plausible-looking product gap. It wasn't:
   the photos provider does `.exclude(image="")` and `baker.make(Image, ...)` attaches no file, so
   the row was filtered out before matching. Attaching a real `SimpleUploadedFile` made it findable.
   Always confirm the *positive* case (can the owner find their own thing?) before believing a
   negative one.

---

## 4b. Stale guidance in `CLAUDE.local.md` (not changed — it is a private, uncommitted file)

The checked-in `CLAUDE.md` files were all verified accurate (Django 6+ matches `django~=6.0.6`,
Python 3.12 matches `requires-python`, and every `docs/…` path they cite exists). The private
`CLAUDE.local.md` has drifted, and since it steers agents it is worth correcting:

- **Four dead paths.** `docs/designs/plugins.md` (the file is `docs/designs/plugins.md`, which the
  committed `CLAUDE.md` cites correctly), `docs/prompts/completed.md`, `docs/prompts/todo.md`
  (`docs/prompts/` does not exist at all), and `TODO.md` in the project root.
- **The sass gotcha section is now obsolete.** It documents `bun run sass` crashing with
  `ERR_REQUIRE_ESM` and prescribes a manual `bun node_modules/.bin/sass …` workaround. The
  scripts now invoke sass through Bun's runtime directly, so `bun run sass` works — see the
  resolved entry in `PROBLEMS.md`.
- **The mypy section is still accurate** and worth keeping: no system GDAL on this host, so the
  django-stubs plugin dies *while mypy exits 0*.

## 4b-2. `docs/FEATURES.md` was missing an entire subsystem

`CLAUDE.md` tells agents to check `FEATURES.md` before assuming a feature doesn't exist, precisely
so work isn't duplicated. **Consensus — the wiki-data-completion game — had zero mentions in it**,
despite ~3,300 lines across 14 models, 13 service modules, a controller, a WebSocket consumer, a
beat-scheduled stall sweep, and eleven URL routes. An agent following the documented process would
have concluded it didn't exist.

Added a section covering solo vs competitive modes, the cross-session tentative-answer pool, the
Beta-Bernoulli trust posterior (and that it is deliberately not shared with SpotGuessr/Trivia's
Glicko-2 ratings), the field-kind registry that makes a new answerable field a registry entry
rather than new game code, and the link into the fact-confidence machinery. Every claim in it was
checked against the code rather than inferred from the model docstrings alone.

A follow-up sweep — comparing every `models/` subsystem against the doc's coverage — found two
more genuinely user-facing gaps (the rest of the unmentioned subsystems are internal plumbing a
features inventory reasonably omits: `email_log`, `pin_tombstone`, `auto_removals`,
`search_history`, `pin_import_failures`):

- **Public Locations** had no coverage at all. Users vote a location "public"; public locations
  are then suggested to every account, which is how a new user gets a populated map. The
  eligibility rules *are* the safety mechanism here, so they run server-side and users only ever
  see vote buttons on a place that already qualifies — worth documenting precisely because a
  reimplementation that skipped the rule engine would be a privacy incident, not just duplication.
  Voting is anonymous in the UI with no running tallies before an outcome.
- **Native-app push** was absent from the Notifications section, which described only the
  WebSocket/desktop path. A backgrounded app holds no socket and registers a push destination
  instead, with UnifiedPush (self-hostable, e.g. ntfy) as the default transport — chosen to keep
  an F-Droid build free of Play Services. The FCM row kind exists but is deliberately not
  dispatched.

That sweep took three attempts to make sound: the first matched subsystem names literally (so
`direct_messages` "missed" the "Direct Messaging" section), and the second still under-matched on
underscores. Each candidate it finally surfaced was then read and confirmed by hand rather than
trusted from the name.

Two corrections in the same file while there:

- the **Real-time (WebSockets)** section listed 4 of the 7 routes — the three game sockets were
  absent entirely;
- it described safety check-in chat as "shared between the check-in owner and emergency contacts",
  omitting accepted partners, who are a distinct audience with a *different* set of groups (only
  the session route joins the live-location group). Now states the audience correctly, including
  that removing a partner or contact force-closes their socket.

## 4b-3. `docs/NOTES.md` verified accurate

The other file `CLAUDE.md` sends agents to. Every code path it cites resolves, and every app symbol
it names still exists — checked mechanically, with the single apparent miss (`L.ImageOverlay`)
being a Leaflet API reference rather than app code. No changes needed, recorded so it is not
re-derived.

## 4c. Documentation cross-references repaired

The docs were reorganised into `designs/`, `designs/drafts/`, `reports/` and `notes/` at some
point without updating the links pointing at the old locations. **Ten references across seven
files** pointed at paths that had simply moved — `docs/plugins.md`, `docs/e2ee.md`,
`docs/api-expansion-candidates.md`, `docs/overpass-mirror-test.md`, `docs/import_formats.md`,
`docs/redata-cid-resolution.md`, `docs/designs/spotguessr.md`,
`docs/designs/mobile-app-stack-r2.md`, `docs/designs/public-pins-by-vote.md`,
`docs/external_app_api_plan.md`. Each target was located before rewriting, and the rewrite was
anchored so an already-correct path could not be double-prefixed.

**Deliberately left alone** — these reference documents that do not exist anywhere, so repointing
them would mean inventing a target. They are either deleted or never written, and deciding which
is the author's call: `docs/api-reference.md`, `docs/architecture/server-agent-split.md`,
`docs/BACKEND_CHANGES.md`, `docs/migration-0.6.md`, `docs/PARITY.md`, `docs/notes/ai/*.md`
(three), `docs/prompts/*.md` (two, from `CLAUDE.local.md`). One — `docs/PROBLEMS.md/completed.md`
— is a malformed path in the source text rather than a missing file.

## 4d. Test coverage of this changeset

Run in batches partitioned by test-file name, because a single pass exceeds the available window.
Three rounds: after the bulk of the fixes, after a further twelve production files changed, and
after the last five.

**Round 3 (final), 10,406 passed, 0 failed** — re-run after a further five production files
changed (the image-orientation, decompression-bomb and malformed-KML fixes):

| batch | scope | result |
| --- | --- | --- |
| 1 | `core/` + `test_[a-e]*` | 3763 passed |
| 2 | `test_[f-l]*` | 1392 passed |
| 3 | `test_[m-p]*` | 2427 passed |
| 4 | `test_[q-z]*` | 2824 passed |

**Round 2, 10,388 passed, 0 failed**, over the same partition after the bulk of the fixes.

**Round 1**, seven batches over all 604 test files, produced one failure — **a test added by this
audit, not existing behaviour**: `test_cookies_follow_the_same_tls_gate` asserted that
`SESSION_COOKIE_SECURE` *equals* `SECURE_SSL_REDIRECT`. It doesn't have to: both default to that
value but are explicitly overridable, and this environment sets secure cookies while still
permitting HTTP, which is *stricter* than the default rather than weaker. Rewritten as the
one-directional invariant that actually matters (HTTPS enforced must imply secure cookies).

Two earlier attempts at a single unbatched run were **abandoned rather than reported**: the first
had files `docker cp`'d into the container while it ran, which is the exact mistake section 4
warns about; the second was killed because it was blocking verification of a security fix. Neither
result was used.

mypy is worth calling out separately. Per-module runs were reported clean throughout, and that was
true but insufficient — checking `media_auth.py` alone never type-checks `immich.py`'s *call site*.
A full-project run surfaced 16 errors, 8 of them introduced by this audit. All 16 are now fixed;
see section 1.

## 5. Environment notes

- **mypy cannot run on the host** — no system GDAL, so the django-stubs plugin dies with
  `Error constructing plugin instance of NewSemanalDjangoPlugin` **and mypy still exits 0**. A CI
  step invoking it this way reports success while type-checking nothing.
- Tests run in the `urbanlens_development_main_test_runner` container (has GDAL). `docker cp` the
  tree in first, and **do not `docker cp` while a run is in flight** — it swaps files mid-run and
  makes results untrustworthy.
- `docker exec` needs `-i` to accept a heredoc; without it the command silently no-ops, which can
  make a "does this check have teeth?" validation pass vacuously.
- Full suite is ~1h25m. Narrow `-k` selections aggressively: `-k 'pin or map or label'` still
  matches ~3,200 tests.


## Chunk 303 - label reorder wrote one UPDATE per label

`LabelReorderView.post` posted the whole id list and issued one `UPDATE` per id, so
dragging a single label in a list of 50 wrote 50 statements. The row count is chosen by
the user's own label list, so it grows with no bound the code controls.

Collapsed to a `SELECT` + one `bulk_update`. Measured before the change: **7 queries for
5 labels, 27 for 25**. After: constant.

**The caveat filed in chunk 298 dissolved on inspection.** It said `bulk_update` "does not
fire `post_save`, and `Label` has receivers (`sync_redata_taxonomy_on_save`) - check whether
an order-only change needs them before switching." But the code it replaced used
`queryset.update()`, which does not fire `post_save` *either*. The receivers were already
not running. So the switch is signal-neutral, and the question that looked like a blocker was
never live. Worth noting because the caveat was correct about `bulk_update` in isolation and
still pointed at nothing - the comparison that mattered was against the *existing* call, not
against `save()`.

That does surface a real and separate question: `label_refresh_map_pin_cache` is a `post_save`
receiver on `Label`, and reordering has never invalidated the map pin cache. Whether label
`order` reaches that payload is not something this chunk established - filed, not fixed.

Two behaviours were pinned by test before the change, both of which the collapse could
plausibly have broken: ids outside the caller's profile are silently ignored rather than
erroring (the loop got this from re-filtering per row, so the filter had to move rather than
disappear), and later duplicate ids win.

**Process note:** the first run of the new test failed on a fixture collision, not the
query count - `_make_tags` restarted its naming at 0 on the second call and tripped
`uq_label_profile_name_kind_ci`. This is the *second* time in this audit a fixture that
restarts a counter has produced a failure that reads like a code bug. The tell both times was
an `IntegrityError` on a uniqueness constraint rather than an assertion failure.


## Chunk 304 - reordering labels served a stale map icon

The question chunk 303 filed is a real bug. Label `order` is not cosmetic: the map payload's
`_ordered_location_labels` sorts by `-order`, and `_winning_display_label` takes the first
label carrying an icon. So reordering two icon-bearing labels changes what a pin *looks like*
on the map without touching the pin.

`refresh_map_pin_cache_for_label` exists for precisely this hazard - its docstring says editing
a label never touches `Pin.labels.through`, so nothing else invalidates the Redis payload and
affected pins "keep serving the old baked-in icon/color". But it is a `post_save` receiver, and
reorder wrote through `queryset.update()`. **The one write that changes `order` was the one
write that skipped the invalidation.** Pre-existing; chunk 303's `bulk_update` neither caused
nor worsened it, since both calls skip `post_save` alike.

Fixed by invalidating explicitly after the write.

**Adding the fix exposed a cost worth more than the fix.** The refresh does work per *pin*
carrying each label, and the first version passed every reordered label - so a reorder could
rebuild a user's entire map. Now only labels whose `order` value actually moves are written or
invalidated, which also makes re-sending an unchanged order free. A drag typically moves a
handful of labels, not all of them.

**On the test's reach.** These assert the invalidation *contract* - that reorder asks for the
right pins to be refreshed - not a Redis round-trip. The cache needs a live client and this
suite's network guard permits localhost only, so end-to-end is unavailable here. That is a real
limit on the evidence: it proves the call is made with the right ids, not that the payload
downstream is correct.

**A test that would have passed for the wrong reason.** Tightening to changed-rows-only broke
the ownership test silently - it posted an id list under which the owned label's order happened
not to move, so it would have been filtered out and the assertion would have held while
testing nothing. Caught by re-running rather than by reading. Narrowing a write path can
quietly empty a test whose subject sat *outside* the narrowed set.


## Chunk 305 - sweeping for the rest of the skipped-invalidation class

Chunk 304's bug was found by accident, so this scanned for its shape systematically: models
with `post_save`/`post_delete` receivers, cross-referenced against writes that skip them
(`.update()`, `bulk_update`, `bulk_create`). **17 models have receivers; 26 such writes exist.**

**A false alarm worth recording.** The scan flagged two more `Label.objects.bulk_update(reordered,
["order"])` sites and I initially read them as two more copies of the same bug. They are not -
both already invalidate, with the reasoning in a comment on the next line. The scan matched the
`bulk_update` line without reading what followed it. A grep for the *defect* shape finds correct
code too, because the defect and the fix live on adjacent lines.

Re-run with a check for adjacent invalidation, 14 sites needed judgement. Most are correct by
design, and the reason is usually that the receiver's job is not wanted for that write:
`NotificationLog...update(status=READ)` deliberately skips `enqueue_native_push`, because
re-pushing a notification when the user *reads* it is precisely the bug. Same for `Wiki`'s
`viewed_by_other` and `parent_wiki` writes against `suggest_and_add_categories`. **Bypassing a
signal is a legitimate technique, so the scan's output is a question list, not a defect list.**

One real gap: `reorder_activities` (`services/trips/trip_activities.py`). `sync_trip_on_activity_save`
calls `queue_calendar_push` so an auto-synced calendar follows the trip, and the reorder wrote
each position through `queryset.update()`. The one operation whose entire purpose is changing
activity order never reached the calendar. Fixed, and the loop collapsed to one statement -
it was also one `UPDATE` per row while holding a `select_for_update` lock.

The push is queued **once per reorder**, not once per row: the receiver fires per activity but
`queue_calendar_push` takes a trip id, so the row-by-row equivalent would queue the same trip N
times for one drag. Restoring a skipped signal literally is not always right.

The existing race suite passes unchanged, which matters more than the new tests - the function
carries a long comment justifying its locking against interleaved reorders, and collapsing the
writes had to leave that reasoning intact.

**Fixture errors, third occurrence.** `baker.make(Trip, profile=...)` - `Trip` has no `profile`;
its owner field is `creator`. Three chunks, three fixture-shaped failures presenting as code
failures. The cost is one full container test cycle (~3 min) each time. Reading an existing
test's `baker.make` line for the same model first would have caught all three.


## Chunk 306 - merging pins left the survivor's last-visited date three months stale

`merge_pins` repoints the loser's `PinVisit` rows to the survivor with `queryset.update()`.
`Pin.last_visited` is a denormalized copy of the newest such row, maintained by
`sync_last_visited`. Nothing recomputed it, so absorbing a more recently visited pin left the
survivor advertising an **older** date than its own visit history supports.

Reproduced before fixing: survivor showed 2026-05-16 while its absorbed history said 2026-08-12.
User-visible on the map popup (`last_visited` is in the payload) and the pin detail page.

Fixed by calling `sync_last_visited(survivor)` inside the merge's transaction. That also settles
a second staleness found on the way: **the merge issued no cache invalidation for the survivor
at all**, despite the survivor gaining visits, images and labels. `sync_last_visited` saves the
pin, which fires the `post_save` receiver that refreshes the cached payload.

**This is the same defect as chunks 304 and 305, third instance, third model.** The generalisation
is now worth stating plainly: this codebase maintains denormalized/derived state through
`post_save` receivers, and every place that writes with `update()`/`bulk_update`/`bulk_create`
is a place that derived state silently stops tracking. Three chunks found three, in labels,
trips and pins - the shape recurs because bulk writes are reached for exactly when a loop feels
slow, which is exactly when many rows change.

**On the value of reading the whole function.** Chunk 305's lesson (a grep for a defect's shape
also matches code that already fixes it) applied in reverse here: the repoint block *looks*
uniform - fourteen consecutive `update()` calls - and its correctness varies per line depending
on what each model's receivers maintain. Uniform-looking code is not uniformly correct, and the
grep cannot tell the difference.

Not fixed, filed: the survivor also absorbs `PinMarkup`/`MarkupMap` (whose receivers maintain
pin *inferences*) and `PinLink` (`resync_pin_on_link_saved`). Whether those derived values are
recomputed anywhere after a merge was not established here - it needs reading each receiver's
actual work, not the pattern match that found them.


## Chunk 307 - the three filed merge repoints are clean; no change

Chunk 306 filed three repoints whose receivers maintain derived state. Reading them (rather
than matching the pattern that found them) resolves all three, and the reasons differ:

- **PinMarkup** - `sync_pin_inferences_on_item_save` keys off `instance.parent_map_id`. The
  merge repoints `parent_pin`. The receiver's subject is not what the merge touches, so the
  map's detected pins are unaffected.
- **MarkupMap** - `sync_pin_inferences_on_map_save` resyncs when a map's *viewport/geometry*
  changes, and explicitly skips saves touching fields detection never reads. The merge repoints
  the owning `pin`, not geometry.
- **PinLink** - `resync_pin_on_link_saved` calls `_touch_pin`, whose whole mechanism is
  `Pin.save(update_fields=["updated"])`, chosen (per its docstring) to re-fire
  `sync_smart_list_membership` and keep `saved_filter_cache`'s `Max(Pin.updated)` fingerprint
  current. This one *was* a live gap - a survivor gaining its first links would not re-match a
  "has links" smart list - **and chunk 306's fix already closes it**. `sync_last_visited` saves
  the survivor with `update_fields=["last_visited", "updated"]`, a superset of what `_touch_pin`
  writes, and `sync_smart_list_membership` does not filter on `update_fields` (checked - it
  resyncs on any Pin save).

**First clean verdict after three consecutive finds, which is the point worth recording.** The
prior chunks established a real pattern, and a real pattern generates real expectation - the
pull toward finding a fourth instance was noticeable. What distinguishes these three from the
`PinVisit` case is not subtle judgement; it is one readable fact each about what the receiver
keys off. The pattern match found all four sites and could not rank them. Only reading could.

Also worth noting: the PinLink gap was closed *incidentally*, by a fix aimed at something else.
That is luck, not design - and it means the fix's own commit message understates what it does.


## Chunk 308 - turning the derived-state defect into a guard

Chunks 304-306 found this defect three times, in three models, one chunk each. The pattern is
understood well enough to stop hunting it: a static test now enumerates bulk writes
(`update()`/`bulk_update()`/`bulk_create()`) on models carrying `post_save`/`post_delete`
receivers, and fails when the set grows beyond the 22 reviewed sites.

**It deliberately does not assert that every site invalidates.** Bypassing a signal is often
correct - `NotificationLog...update(status=READ)` must *not* re-fire `enqueue_native_push` -
and most of the 22 are right for reasons like that. A test demanding invalidation everywhere
would be wrong and would train people to suppress it. What this asserts is narrower and
defensible: **the set does not grow unreviewed.** A new entry is a prompt to decide, not an
accusation.

The list also fails on *stale* entries, so a moved or deleted site cannot rot into permanent
noise.

**Verified it can fail.** A guard that only ever passes is worse than no guard, and the two
regex sanity tests do not prove the end-to-end path. Injected a real bulk write into a source
file (in the container copy only, so the working tree stayed clean) and confirmed the test
fails naming `dashboard/services/pins/pin_geometry.py::Pin`. This is the control that was
missing from an earlier filter in this audit, which passed all four of its checks while blind
to the codebase's commonest transaction idiom.

Costs 8 seconds - `SimpleTestCase`, no database.

**Honest limit:** it is a regex over source text. It will miss writes built dynamically, writes
through a queryset variable (`qs.update(...)` where `qs` came from elsewhere), and any model
whose receivers are registered by something other than the `@receiver(...)` decorator. It
catches the literal shape that produced three real bugs, which is what it claims to do - not
"all derived-state staleness".


## Chunk 309 - judging the carried sites; all clean

Three of the eight sites the guard carried as "reviewed, not individually judged" are now
judged, each resolvable from one or two checkable facts:

- **`site_scope.py::Pin` / `::Wiki`** (bulk retype of `pin_type`). Clean on three counts:
  `pin_type` is not in the map payload, no smart filter reads it (grepped - it appears nowhere
  in `services/search` or `models/pin_list`), and `refit_child_boundaries_on_save` early-returns
  unless the pin was created or its *position* changed. Retyping is neither.
- **`pin_edit.py::Pin`**. Clean, and the clearest example in the codebase of the bypass used
  *correctly*: it parks a pin as its own parent as a transient state while re-homing children,
  with `# Bypass save() so no side effects run for this transient state` at the site and the
  affected ids tracked in `deferred_ids` for resolution. Here running the receivers would be
  the bug.

**The same construct that caused three bugs is the right tool here.** That is the argument for
the guard asserting only that the set does not grow unreviewed, rather than demanding
invalidation everywhere - a stricter test would flag this correct code, and the fix for a
false positive is usually to weaken the test.

It also sharpens what the three real bugs had in common. Not "used a bulk write", but: used a
bulk write on a field the receivers *do* read (`order`, `pin` on visits), while the code
carried no indication anyone had considered them. `pin_edit.py` shows the considered case, and
it reads completely differently at the site.

Two sites remain carried, `controllers/memories.py::Pin` and `pin_list_trip.py::TripActivity`.


## Chunk 310 - the bulk-write thread closes: 22 sites, all judged

The last two carried sites are clean:

- **`memories.py::Pin`** writes `unlogged_visit_dismissed`, which nothing derived reads - absent
  from the map payload and from every smart filter. The receivers it skips have no work to do.
- **`pin_list_trip.py::TripActivity`** already queues `queue_calendar_push` explicitly after its
  `bulk_create`, with reasoning matching the fix chunk 305 applied to trip reorder.

**Thread summary.** 22 bulk writes on receiver-bearing models; 3 real bugs (label reorder's stale
map icon, trip reorder's missed calendar push, pin merge's three-month-stale `last_visited`); 19
correct, most of them deliberately so. A guard now prevents the set growing unreviewed.

**Third false alarm of the same kind.** `pin_list_trip` looked like a fourth instance and is
already fixed - as two `Label` sites did in chunk 305. The guard's list will always contain
sites that carry the defect's shape *and* its repair, because the repair lives next to the
shape. Anything scanning for the shape must read the following lines before reporting. I have
now made this mistake three times in seven chunks and caught it three times by reading; the
reliable fix is that the scan output is never the finding.

**What the thread cost and returned.** Seven chunks. Three user-visible bugs fixed, one of them
data-level (a pin advertising a visit date three months older than its own history). The
generalisable result is not the bug count - it is that a defect class found three times by
accident became a 4-second test that cannot silently regrow, and that the class turned out to
be 14% defective, so a guard demanding invalidation everywhere would have been wrong 19 times
out of 22.


## Chunk 311 - full-suite run launched (bookkeeping entry, added in 324)

Six code changes had landed across labels, trips and pins (chunks 303-310) without a full-suite
run. Launched one against a freshly synced container snapshot, and inventoried the session's new
tests: 17 across five files, all previously passing individually, plus the adjacent suites they
could disturb (157 merge tests, the trip-activity race suite, the label organize suite).

What a full run adds over those is the only thing they cannot cover: whether the fixes interact
with anything outside the areas under examination. The pin-merge change is the one that warrants
it - it now saves the survivor *inside* the merge transaction, firing receivers no merge test was
written to anticipate.

Two process notes, both mistakes:

- The run was launched as `pytest ... | tail -6`, which buffers until completion. That discarded
  all interim visibility for a 70-minute job - no progress, no early failure signal. `tee` to a
  file would have cost nothing.
- This entry was missing until chunk 324 found 21 commits against 20 documented chunks. The
  chunk that *launched the verification* was the one that went unrecorded, which is a fair
  illustration of where attention goes: the interesting work gets written up, the plumbing does
  not.


## Chunk 312 - select_for_update: 34 sites, clean

Read-only chunk (a full suite is running; no source syncs). Scanned every `select_for_update()`
against an enclosing `transaction.atomic()`, since outside one Django either raises or fails to
lock depending on autocommit state - a lock that silently does nothing is the worst version of
this bug.

**All clean.** 34 matches, of which 3 are *comments* describing the pattern (in `trivia/session.py`,
`spotguessr/session.py`, `rate_limiter.py`) and 31 are real calls, every one inside an atomic
block. The three flagged lines are prose explaining why the lock is needed - e.g.
"``select_for_update()`` inside ``transaction.atomic()`` - so a second..." - which to a regex is
indistinguishable from the call itself.

Worth noting alongside chunks 305 and 310, where a scan for a defect's shape matched code that
already *fixed* it. Here it matched code that merely *talks about* it. Same root cause: the scan
locates candidates and cannot read. This one is a cheerier version of the finding, though -
these three files documented their concurrency reasoning at the point of use, which is what made
the false positives instantly resolvable.


## Chunk 313 - two AST-decidable classes, both clean

Read-only (full suite still running). Two classes chosen because AST answers them exactly:

- **Mutable default arguments** (`def f(x=[])`, `x=dict()`, ...): **0**.
- **Bare `except:`**: **0**. This is the one that matters most - a bare handler catches
  `KeyboardInterrupt` and `SystemExit` too.
- **Handlers whose entire body is `pass`**: 26, and **0 of them catch `Exception`/`BaseException`**.
  Every one names specific types - `asyncio.CancelledError` in the consumers (the standard
  cleanup idiom), `(ValueError, TypeError)` around optional query-param parsing in site_admin,
  `(AttributeError, DatabaseError)` in context processors. Silently swallowing a *named*
  exception you expect is a different act from swallowing everything.

**The tool determined the failure mode.** Chunks 305, 310 and 312 all produced false positives
from regex matching text *about* a defect - fixed code twice, explanatory prose once. These two
scans produced none, because an AST cannot match a comment or a docstring. That is the same
lesson as the colour-literal sweep earlier in this audit, which took five regex generations
(19->8->3->1->1 sites, each generation claiming completeness) and was settled in one AST pass.

Where a class is structurally decidable, parse it. Reach for regex only when the property is
not in the syntax - and expect to read every hit when you do.


## Chunk 314 - naive-date sweep finds 4 real sites; filed, not fixed

Scanned for timezone-naive date/time construction. Four hits, all `datetime.date.today()`. Not
the naive-*datetime* bug the scan was aimed at - a `date` carries no timezone - but wrong anyway
in an app with `USE_TZ = True`: `date.today()` uses the server's timezone, Django's
`timezone.localdate()` uses the active one, and they disagree for part of every day.

`trip_activities.py:819` is the one with consequence: completing an activity computes
`effective_date = min(completed_date, today)`, so completing late in the user's day can clamp
the date to yesterday, and that date feeds the visit entries created for the activity.

**Filed to `docs/PROBLEMS.md` rather than fixed**, because a full suite is running against a
synced container snapshot and syncing source mid-run corrupts it - a rule this audit learned by
destroying two runs. The fix is one call per site, but it wants a boundary test with
`override_settings(TIME_ZONE=...)` and a frozen clock, which is not a change to make while
unable to run tests.

Also worth recording: the scan was aimed at a class the codebase does not have (naive datetimes:
zero) and found a neighbouring class it does. Aiming at a precise, decidable property makes the
near-misses legible instead of drowning them.


## Chunk 315 - asserts and prints in production code: clean

Read-only (suite still running, ~19 min in). Two more AST-decidable classes:

- **`assert` in production code: 0.** This matters because `python -O` strips assertions
  entirely, so any `assert` doing validation silently stops validating in an optimised run. A
  codebase that uses them for *checks* rather than *invariants* has a latent, deployment-flag
  -dependent bug. This one uses none.
- **`print()` in production code: 5, all in `manage.py`** - a CLI entry point, where printing is
  the correct behaviour, not a leftover debug statement.

Both were single-pass AST scans with no false positives, continuing chunk 313's pattern. Four
consecutive structurally-decidable classes have now come back clean or near-clean (mutable
defaults, bare excepts, asserts, prints), which is itself information: the mechanical defect
classes are largely absent here, and the real findings in this audit have all needed a fact the
syntax does not carry - what a receiver keys off, whether a field reaches a payload, whether a
timezone is the server's or the user's.


## Chunk 316 - applying the localdate fix under a testing constraint

The constraint from chunk 314 turned out to be narrower than I stated. The rule is *do not
`docker cp` mid-run* - the container tests its own synced copy, so **editing the host tree is
safe** while a suite runs. I had generalised "cannot change code" from "cannot sync code", and
lost a chunk of working time to it.

So the four sites are now fixed: `datetime.date.today()` -> `timezone.localdate()` in
`controllers/tools.py` (x2), `controllers/trip.py`, `services/trips/trip_activities.py`, with a
`django.utils.timezone` import added to `tools.py` (the other two already had it). Ruff clean,
all three compile, zero `date.today()` calls remain outside tests.

**Explicitly unverified.** No test has run against this change - the container is busy, and by
the rule above it will stay busy for another ~45 minutes. The substitution is mechanical and
`localdate()` is the documented tz-aware equivalent, but "mechanical" is exactly the claim this
audit has caught itself making wrongly before (five regex generations on colour literals, each
claiming completeness). Treat as pending until the suite frees the container and this is run.

The boundary test is still owed: it needs `override_settings(TIME_ZONE=...)` plus a frozen clock
to pin the case where server-local and active timezones fall on different dates. Writing it
blind would risk a test that passes vacuously, which is worse than no test - this audit has
already produced two of those.


## Chunk 317 - the completeness claim was wrong, one chunk after warning about it

Chunk 316 reported "zero `date.today()` calls remain". That was measured by grepping the literal
string `datetime.date.today()` - which **cannot** match `date.today()` written against a
`from datetime import date` import. The check could not have found the misses.

Five more real sites: `controllers/pin_edit.py`, `controllers/pin.py`,
`services/import_export/export.py` (x2), `services/ai/link_extraction.py`. All now fixed; zero
remain by an AST scan across every spelling.

**This is the exact failure the previous chunk described and then committed anyway.** It named
the colour-literal sweep - five regex generations, each claiming completeness - as the reason to
distrust "mechanical", and then made the same shape of claim from a narrower instrument. Writing
the caveat is not the same as applying it.

A sixth hit was a **false positive that would have been a real bug to "fix"**:
`rate_limiter.py:380` is `ApiCallLog.objects.for_service(service).today()` - a queryset method
that happens to be named `today`. Substituting there would have replaced a database query with a
date.

Two guards paid off. A pre-write assertion aborted the first attempt before any file was touched
(`pin_edit.py` has no top-level datetime import - both it and `pin.py` import `date` inside the
function, and both also call `date(...)` as a constructor on the next line, so the import had to
stay and only the call could change). And ruff caught `F821 Undefined name 'timezone'` in
`link_extraction.py`, where the substitution landed but the import insertion silently did not
match. Without that lint, this chunk would have shipped an import error into a live code path.


## Chunk 318 - what the running suite will and will not verify

The full suite launched in chunk 311 is running against the container snapshot synced **at that
moment**. Chunks 316 and 317 edited the host tree afterwards, and syncing mid-run is forbidden -
so the nine `timezone.localdate()` substitutions **are not in the code being tested**.

Stating this before the result arrives, because a green run is exactly the kind of evidence that
gets over-read. When it passes it will establish that chunks 303-310 hold together - the label
reorder collapse, the two cache/calendar invalidations, and the pin-merge `sync_last_visited`
call that now fires receivers inside the merge transaction. It will say nothing whatsoever about
the localdate work, which needs its own run afterwards.

This is a general hazard of long-running verification against a snapshot: the artifact under
test silently ages away from the working tree, and the result keeps the authority of a full-suite
pass while its scope quietly shrinks. The same trap as this audit's stale-container problem,
except the drift is in *time* rather than in files.

Outstanding, both owed before the localdate change can be called done:
1. A container run of the current tree.
2. A boundary test with `override_settings(TIME_ZONE=...)` and a frozen clock, pinning the case
   where server-local and active timezones fall on different dates.


## Chunk 319 - the owed boundary test, written against the vacuity objection

Chunk 316 declined to write this test on the grounds that authoring one you cannot run risks a
vacuous pass. That objection is answerable rather than blocking: build the vacuity check *into*
the test.

`test_the_chosen_instant_really_does_straddle_a_boundary` asserts that the frozen instant
produces different dates under the two zones. If that ever stops holding - a changed constant, a
tzdata update, a different default - the suite fails loudly instead of going quietly green while
testing nothing. This is the same control discipline that caught two vacuous tests earlier in
this audit, applied *before* the fact rather than after.

The premise is verified without Django: 2026-08-14 23:30 UTC is the 14th in UTC and the 15th in
Pacific/Auckland, confirmed with plain `zoneinfo`. So the test is blind only on whether
`override_settings` + `timezone.deactivate()` + a patched `timezone.now` compose as expected -
a much narrower unknown than "does this test test anything".

The third test pins the *old* behaviour: `date.today()` is insensitive to the active timezone. It
deliberately asserts insensitivity rather than a specific date, so it does not depend on the host
machine's own zone - a test that only passes on a UTC CI box would be a trap for whoever runs the
suite on a laptop.

Still unrun; the container is busy. `SimpleTestCase`, so it needs no database when it goes.


## Chunk 320 - the new test was wrong, caught without the container

Chunk 319 said the boundary test was blind on one narrow axis: whether `override_settings` +
`timezone.deactivate()` + a patched `timezone.now` compose as expected. That axis turned out to
be testable on this host after all - `settings.configure()` builds a minimal Django without
touching the project settings, so nothing imports GDAL.

Three assumptions held. **The fourth was wrong.** `test_date_today_does_not_follow_the_active_timezone`
asserted `date.today()` is unchanged across two `override_settings(TIME_ZONE=...)` blocks. It is
not: Django's `setting_changed` receiver for `TIME_ZONE` also rewrites `os.environ["TZ"]` and
calls `time.tzset()`, so the process clock moves with the setting and `date.today()` moves with
it. The test would have failed on its first container run.

The real axis is `timezone.activate()` - the per-request zone the locale middleware sets. Under
it, `localdate()` moves and `date.today()` does not, which is precisely what the nine call sites
were ignoring. Rewritten accordingly and re-checked on the host: `2026-08-14 -> 2026-08-15` for
`localdate()`, unchanged for `date.today()`.

**The lesson is not "write more careful tests".** It is that "I cannot verify this here" was
false, and unexamined. Two chunks reasoned about how to *hedge* a blind test - one declining to
write it, one building in a vacuity guard - when ten lines of `settings.configure()` would have
removed the blindness. The constraint was inherited from `CLAUDE.local.md`'s note that pytest
needs the container (true, because the project's settings import GeoDjango) and over-generalised
to "no Django on the host at all".

That is twice in five chunks: chunk 316 over-generalised "no `docker cp` mid-run" into "no edits
at all". Both times the false constraint survived because it was never tested, only reasoned
from - and both times the test was cheap.


## Chunk 321 - the host-side Django path, used

Ran all three boundary-test bodies against a `settings.configure()` Django on this host. **All
pass**, including the corrected third one: `localdate()` moves `2026-08-14 -> 2026-08-15` under
`timezone.activate()` while `date.today()` stays on the 14th.

The test is now verified in *logic*. What remains untested is only harness integration - that
`SimpleTestCase` and the project's conftest do not interfere - which is the part the container
run will cover.

**This path is worth keeping.** Anything not touching the database or GeoDjango models can be
checked here in seconds, against a real Django, instead of waiting minutes for a container cycle.
Three of this audit's fixture-shaped failures (chunks 303, 305, 306) each cost a full ~3-minute
container run to discover a wrong keyword argument; none of them needed GDAL to find. The
project note that "pytest needs the container" is accurate and was never the whole picture.

Recorded in `docs/PROBLEMS.md` as a developer-workflow note rather than a defect - it is the kind
of thing that stays lost precisely because the existing constraint is real enough to sound
complete.


## Chunk 322 - spot-checking pure services on the host; the failure was mine

Used the host-side Django path on two of this audit's own helpers. Both behave correctly:

- `clean_color`: accepts `#abc`/`#AABBCC`, rejects `red`, `""`, `#12345`, `javascript:alert(1)`,
  `#ff00ff; x`, `None`.
- `safe_int`: `"5"->5`, `"  7 "->7`, `"-4"->-4`, and `"x"`/`None`/`True`/`"1e3"` all fall to the
  default. Bool is refused explicitly (`safe_int(True, 9) == 9`), which is the point of it -
  `int(True)` is `1` and would silently pass as a valid id.
- `clamp_int`: clamps `"500"->100`, `"-5"->1`, and `"x"->default`.

**The first run reported a failure that was not one.** It asserted `safe_int("x") is None`; the
function returns `default: int = 0`. It also called `clamp_int(lo=..., hi=...)` when the
parameters are `low`/`high`. I wrote both of these helpers earlier in this audit and still
mis-stated their contracts from memory a few hundred commits later.

That is a small instance of the thing this audit keeps finding at larger scale: the failure was
in the *expectation*, not the code, and it looked exactly like a defect report until the
signature was read. Chunk 305's false alarm, chunk 310's, chunk 312's - all the same shape. The
check that resolves it is always the same and always cheap: read the definition before believing
the diff.

The 8-second feedback loop is what made this a footnote rather than a chunk. On the container it
would have cost three minutes to discover I had misremembered a keyword argument.


## Chunk 323 - boundary behaviour of the shared validators

Host-side checks of the two remaining shared helpers this audit introduced or leaned on:

- `text_length_error`: the limit is **inclusive** - exactly `MAX` characters is accepted and
  `MAX+1` errors. Empty string and `None` are both non-errors, so a blank optional field does not
  become a validation failure. That off-by-one is the one worth pinning: it decides whether a
  user who fills a field to exactly the advertised limit gets rejected.
- `clean_color`: an invalid value falls to the caller's supplied default (`#000000` stays
  `#000000`, not `None`), which is what the typing overloads promise - a non-None default
  narrows the return to `str`. The `none` keyword passes only when `allow_none_keyword=True`.

No defects. Recorded because these two are now called from dozens of write paths (32 colour
sites alone), so their boundary behaviour is load-bearing in places far from their definition,
and "inclusive or exclusive?" is exactly the question a future caller will assume rather than
check.


## Chunk 326 - reading the OPEN list first, as chunk 325 said to

23 OPEN items in `PROBLEMS.md`, several concrete and higher-value than another scan: a reproducible
500 on pin detach, ~187 write routes with no test naming them, no CSP set anywhere, login lockout
usable as a targeted DoS, refunds never reversing paid access.

Took the detach 500. Its fix is explicitly a **product decision** with three defensible answers
(nudge coordinates, use the pin's own marker fields, or refuse coherently), so patching it would be
choosing for the project. But the entry itself names a part that is not a decision: "a test posting
to `reverse('pin.link', ...)` belongs with it - that single request is enough to catch this class
permanently."

Written as `xfail(strict=True)`, which is the only marker that behaves correctly here:

- asserting the 500 would **cement the bug** as intended behaviour;
- `skip` would go silent forever;
- strict xfail passes while broken, and **fails the moment detach stops raising**, telling whoever
  fixed it to replace the marker with a real assertion.

A second, plain test reverses the route independently. A strict xfail that errors during *setup* -
a `NoReverseMatch` after a route rename - still counts as an expected failure, so the xfail alone
could rot into a permanent green that exercises nothing.

**The larger point about the last twenty chunks.** Most were self-generated: scans I invented, then
audited, then corrected. This file had 23 items already triaged by earlier work, and chunk 325 found
me overriding one of them precisely because I had not read it. Cheap-to-run scans crowd out
expensive-to-read backlogs, and the backlog was where the higher-value work was sitting the whole
time.


## Chunk 327 - verifying the xfail actually works on this project's TestCase

Chunk 326's detach test depends entirely on `xfail(strict=True)` behaving a specific way, and this
project's `TestCase` inherits from `django.test.TestCase` -> `unittest.TestCase`. Pytest documents
real limitations on unittest subclasses (fixtures, parametrize), and I half-remembered `xfail` as
among them. If that were true the test would be decorative.

**Checked instead of assumed.** A two-method probe - one failing, one passing, both marked
`xfail(strict=True)` - gives:

```
XFAIL  T::test_that_fails        <- suite stays green while the bug exists
FAILED T::test_that_passes  [XPASS(strict)]  <- loud the moment it is fixed
```

Both halves work, which is exactly the contract chunk 326 relied on. The recollection was wrong.

Two notes on running it. The probe had to be run from outside the project, with an explicit empty
`-c` config: from the repo root, pytest picks up `[tool.pytest.ini_options]`, loads Django settings
and dies on GDAL - so an isolated probe needs isolating from the project's config too, not just its
imports. And the file landed in the scratchpad rather than `/tmp` because a `cd A || cd B` guard
succeeded on the first branch; the follow-up command then looked in the wrong place. Both cost a
round trip, neither cost a container cycle.

This is the third memory-sourced claim this session to be checked rather than trusted, and the
first of the three to survive being wrong at no cost - the earlier two (`safe_int`'s default,
`clamp_int`'s keyword names) were caught only after producing a bogus FAIL line.


## Chunk 330 - independently re-deriving the duplicate-index count, and deferring to the filed one

Did the column-level AST comparison promised in chunk 329: per model, single-field
`Index(fields=[...])` against columns Django already indexes (FK/O2O, `db_index=True`,
`unique=True`). Result: **64**.

The filed entry says **58**, and **the filed number is the better one.** It was verified against a
fully-migrated database by comparing `pg_index.indkey` column lists rather than names, explicitly
excluding partial indexes (`indpred IS NULL`), unique indexes, and `varchar_pattern_ops` variants -
noting the `_like` indexes Django creates for prefix matching are *not* redundant with a plain
btree and must not be swept up.

My 64 is a source-level approximation that over-counts, mainly because I counted `unique=True` as
"already indexed" - true of the column, but a unique index is exactly what the DB-level analysis
deliberately set aside. Six of my extra hits are that.

**A number that looks like a refinement of a prior finding, and is not.** Third time this session:
chunk 305's two "extra" Label sites (already fixed), chunk 328's phantom 23rd guard entry (my own
f-string), and now this. Each time the newer, cheaper instrument produced a number that differed
from careful prior work, and each time the prior work was right. The tell is consistent - the cheap
method cannot see the exclusions the careful one was built around.

Not attempting the removal. It is a migration change against a database I cannot currently test
against, and the entry's exclusion list is precisely the kind of detail that makes a "mechanical"
sweep dangerous - the same argument that proved correct about the `date.today()` sweep in chunk 325.


## Chunk 332 - the suite found a regression I introduced, in a guard I had duplicated

Full suite: **1 failed, 10781 passed, 1457 subtests** (1:09:50). The failure was mine, and it
exposes a larger error.

**The failing test was `test_bulk_write_signal_guard.py` - not the guard I wrote in chunk 308.**
A guard for this exact defect class already existed, and it is better than mine on every axis:

- keyed by `(path, model, operation)` rather than line numbers, explicitly "not by line number,
  which would churn on every edit above the call and train people to update it blindly";
- it resolves receivers through `django.apps` and the real signal registry, so it sees receivers
  connected **dynamically** - `Image`'s achievements `post_save` is wired via `_SUBSCRIPTIONS`, not
  a decorator, and my regex-based version was structurally blind to it;
- every entry carries a *reason string*, so the record is the reasoning, not a checkbox.

Its docstring also already described the stale-map-icon-after-label-reorder problem, and its
`pin_list_trip.py` entry already documents "calls `queue_calendar_push(trip.pk)` once afterwards
rather than once per activity" - the exact design decision I reached independently in chunk 305 and
recorded as though it were novel.

So chunk 308's "turning the defect into a guard" was reinventing a better wheel, and chunks 303 and
305 then added two bulk writes that the real guard correctly flagged as unregistered. **My duplicate
could not catch that**, because I had written my own list from a fresh scan that included them.

Fixed: both new call sites registered in the real `REVIEWED` with their reasoning; my duplicate
deleted. The real guard passes (4 tests, 15s).

**The notification said exit code 0.** The suite was launched as `pytest ... | tail -6`, so the
pipeline reported *tail's* status, not pytest's. A red build was announced as green, and only
reading the output caught it. Chunk 311 already flagged that pipe for destroying interim
visibility; the exit-code masking is the more dangerous half and I did not notice it then.

**Three chunks, three variations of the same root error** - 325 (overrode a documented decision
without reading it), 330 (re-derived a number careful prior work already had), and now 332
(rebuilt a test that already existed). Each time the prior work was better, and each time it was
sitting in a file I had open or adjacent. The loop's bias is toward generating new artifacts over
reading existing ones, and generation feels like progress in a way that reading does not.


## Chunk 333 - the owed verifications, run

Both debts from chunks 316-319 are settled now the container is free:

- **localdate boundary tests: 3 passed.** The corrected `timezone.activate()` axis behaves in the
  real harness exactly as the host-side `settings.configure()` probe predicted, including the
  precondition guard against vacuity.
- **Detach route: 1 passed, 1 xfailed.** The `xfail(strict=True)` reports XFAIL, confirming the 500
  still reproduces exactly as the 2026-08-13 entry describes, and that the marker will flip to a
  hard failure the moment anyone fixes it.
- **Suites covering the nine localdate files: 942 passed** (trip, pin_edit, export, link_extraction,
  tools), 58 subtests.

The localdate change is therefore no longer "unverified" - the qualifier it carried through six
chunks. It remains **behaviour-neutral** under current settings and still overrides a documented
deferral, so the standing offer to revert is unchanged; what has changed is only that it is now
known not to break anything.

Worth noting what the host-side probe bought: every prediction it made about the boundary test held
in the container. The 8-second loop was not a substitute for the 3-minute one, but it was an
accurate preview of it - and it caught the one genuinely wrong assertion (the `override_settings`
axis) before it ever reached a container run.


## Chunk 334 - reading a backlog entry found a flaw in my own fix

Took the trip-activity weather timezone entry, since my localdate work touched a line it names.
The entry's own bug (Open-Meteo returns naive *local* time while the comparison target is naive
UTC, so slot matching is out by the location's offset) needs the provider timezone threaded
through the resolution chain - more than could be finished safely here, and left alone.

But checking whether my change interacted with it found that **my change is half a conversion**:

```python
today = timezone.localdate()               # active timezone's date, after chunk 316
... act.scheduled_at.date() >= today       # still the UTC date of an aware datetime
```

Before, both sides were UTC. Now one side follows the user and one does not. Latent rather than
live - they agree while nothing calls `timezone.activate()`, which is true today.

**This is precisely what the deferral argument predicted** and what I dismissed by not reading it:
"the sites are not uniform". I had read that as a claim about *import styles* - which is how the
entry illustrates it - and the real hazard is semantic. Converting one side of a comparison is a
different and less visible failure than a `NameError`, and lint cannot see it.

Only this one of the nine converted sites has been checked this way. Recorded in `PROBLEMS.md` as
work owed, rather than quietly assumed fine - the other eight deserve the same question, and I do
not have grounds to claim they are clean.


## Chunk 336 - re-verifying a filed product question rather than deciding it

Took the games feature-gate entry. It is explicitly a **product decision**: a mixin applying the
hub's check to all 49 views was written and reverted, because it broke 9 tests that exercise full
gameplay with users who do not hold the feature. That is the behaviour the suite currently encodes,
so tightening it would lock out anyone playing today - not a defect fix.

Deciding it is not mine to do, so the contribution is confirming it is still true: an AST pass finds
**50 game view classes, 1 gated (the hub), 49 with `LoginRequiredMixin` alone** - the entry's count,
unchanged since 2026-08-12.

**Deliberately did not add a test here**, unlike the detach case in chunk 326. There, a strict xfail
worked because the current behaviour (a 500) is wrong under *every* candidate fix. Here the current
behaviour is what one of the two plausible answers wants, so any test I wrote would encode a guess
about the product question - and a test asserting "a non-entitled user can play" would actively
obstruct the fix if the gate is meant to apply. The absence of a test is the honest state.

That distinction is worth keeping: an unresolved product question sometimes takes a guard and
sometimes must not. The test is safe only when every candidate answer agrees the present behaviour
is wrong.


## Chunk 337 - a stale line number, and honest arithmetic on the coverage gap

Went to read the secondary-email entry by line number and landed on a different one. The numbers I
recorded in chunk 326 are stale because **I have been appending to this same file ever since**. A
line number into a document you are actively growing is a reference with a short half-life; the
headings are stable, the offsets are not. Worth noting since this audit has cited `PROBLEMS.md` by
line repeatedly.

The entry I landed on was the right one to look at anyway - the ~187 uncovered write routes, which
was *prompted by* the detach 500 that chunk 326 covered. Confirmed `pin.link` is now genuinely
referenced by `reverse()` in the test tree, so the count is **186**.

**One out of 187 is the honest size of that dent**, and recording it that way matters more than
recording the decrement. This entry describes a systemic gap; closing it route by route is not a
strategy, and a changelog of individual routes would make steady-looking progress against a number
that barely moves. What the detach case genuinely demonstrates is the *unit*: one request against a
never-executed route was enough to pin a 500 permanently. That is an argument for a sweep, not for
another 186 chunks.


## Chunk 338 - attempting to tighten a filed estimate, and failing to

Chunk 337 argued the uncovered-write-route gap wants a sweep rather than route-by-route chipping.
The cheapest useful sweep is not writing tests - it is tightening the estimate, since the entry
flags a known false-positive mode (tests addressing endpoints by literal path rather than
`reverse()`).

Result: literal-path matching adds only **8** routes. That is a genuinely useful datum - it says
the skew the entry worried about is small.

**But the same probe enumerated 971 routes where the entry has 841, and 419 uncovered where it has
301.** So my route set is not the entry's route set - different namespace attribution on nested
resolvers, and I searched only `dashboard/tests` rather than the whole tree. I cannot claim to have
tightened a number I cannot reproduce.

**Fourth time this session.** Chunks 305, 328, 330 and now 338: a quick instrument produces a
number that differs from careful prior work, and the prior work is better every time. The pattern is
stable enough to be a rule - when a cheap re-derivation disagrees with a careful one, the cheap one
is wrong until proven otherwise, and the burden is on the new number. I recorded the 8 as
indicative and left the entry's figures standing.


## Chunk 339 - the backlog is blocked on decisions, not on effort

Fourth backlog item examined in as many chunks (calendar-import duplicates), and the fourth whose
deferral is correct on inspection:

| item | why it is not an engineering task |
|---|---|
| pin detach 500 | three defensible behaviours; picking one is a product call |
| games feature gate | tightening it locks out anyone currently playing |
| 58 duplicate indexes | migration against live schema, with a careful exclusion list |
| calendar duplicates | needs a partial unique index **plus** deleting rows - choosing which link survives decides which of two real trips keeps the user's calendar |

Each was already analysed to the point where the remaining work is a *decision*, not
implementation. The calendar entry even names the correct constraint
(`condition=~Q(google_event_id="")`) and explains why the obvious plain unique constraint is wrong:
a timed import deliberately stores an empty `google_event_id` on the trip-level link, and empty
strings are not distinct to a Postgres unique index.

**This reframes what this audit can still contribute.** The 23 OPEN items are not a queue of
unfinished engineering that more chunks will drain - they are largely a queue of questions for the
project owner, several with the implementation already specified. Continuing to "search for bugs"
past this point has sharply diminishing returns compared to the four product decisions already
filed, plus these.

The honest summary of the last several chunks: chunks 303-310 found and fixed three real bugs;
chunks 311-339 have mostly been verification, self-correction, and confirming that prior work was
already right. That is worth something, but it is not the same activity, and reporting it as
continued bug-finding would misrepresent it.


## Chunk 340 - template rules from CLAUDE.md, both clean

Looked outside the OPEN backlog at two project-specific template rules, each with a concrete
user-visible failure mode:

- **Multi-line `{# ... #}` comments** (they render straight to the page): **0** across 418
  template files.
- **`"prefix-"|add:obj.id`** (silently yields `''`, because `str + int` fails both the
  int-coercion and concat paths - the bug that collapsed per-item DOM ids and made every DM map
  bubble render the first map): **0 real instances.** No `|add:` anywhere takes a `.id` or `.pk`
  operand.

23 raw matches narrowed to 0. Most operands end in `_str` - the documented fix, already applied.
The remaining five (`ns`, `label_url_kind`, `pin.slug`, a pre-built `visit_map_id`) are strings by
nature, so concatenation is well-defined for them.

**Fifth time a scan's raw output was not the finding**, after chunks 305, 312, 328 and 338. The
ratio here is stark - 23 matches, 0 defects - and the reason is the same as always: the pattern
that expresses a bug also expresses its fix and its safe uses. A grep for `|add:` cannot see the
type of the operand, which is the entire question.

Both rules being clean is itself worth recording. They read like the kind of convention that
erodes quietly, and they have not.


## Chunk 341 - two signal-discipline rules, both clean

Continuing outside the backlog with `CLAUDE.md` rules that AST can decide exactly:

- **`save()` inside a `post_save` receiver or `__str__`**: **0**. The failure mode is recursion -
  a receiver that saves re-fires itself - and `__str__` doing a write is the kind of thing that
  turns a debug print or an admin listing into a write storm.
- **`dispatch_uid` on signal registration**: **57 registrations, 0 missing.** Without it, a module
  imported twice registers the receiver twice and every signal fires it twice; the project has
  already been bitten by duplicate Celery side effects, so this is a rule with history behind it.

Perfect compliance on both, which is a different result from the earlier sweeps. Chunks 313 and 315
found mechanical classes largely *absent* (mutable defaults, bare excepts, asserts); this is a
project-specific discipline maintained at 57 out of 57. That is not the same as an absence - it is
evidence of a convention actually being followed, in a codebase where nothing enforces it
automatically.

Which suggests where the remaining value is: not another convention scan. Three consecutive
convention checks (chunks 340-341, five rules total) have come back perfectly clean, and the
prior probability of the next one finding something has dropped accordingly.


## Chunk 342 - closing the incidental-invalidation claim from chunk 306

Chunk 306 asserted that calling `sync_last_visited(survivor)` also refreshes the merged pin's
cached map payload, since the merge had no invalidation of its own. Asserted, not checked - and
chunk 307 then leaned on the same claim to declare the `PinLink` repoint clean. Verified now:

- `refresh_map_pin_cache` is a plain `post_save` receiver on `Pin` with **no `update_fields`
  filter**, so `pin.save(update_fields=["last_visited", "updated"])` fires it.
- Its only guard is `if instance.profile_id`, which a merge survivor always satisfies.
- `_refresh_cached_pin` defers through `transaction.on_commit`, so it runs *after* the merge
  transaction commits rather than against half-applied state.

The claim holds on all three points, including one I had not considered - the on_commit deferral,
which matters because the merge does its work inside `transaction.atomic()` and an immediate
refresh would have cached a survivor whose relations were still moving.

Worth noting that two chunks' conclusions rested on this for six chunks before anyone checked it.
An unverified claim does not stay isolated; chunk 307 built on it within one chunk. Verification
debt compounds in the same direction as the reasoning that created it.


## Chunk 343 - auditing my own load-bearing claims; one was wrong

Chunk 305 marked the two `NotificationLog...update(status=READ)` sites clean, and used them as the
headline example that "bypassing a signal is often correct - re-pushing a notification when the
user *reads* it is precisely the bug". That reasoning was asserted from the receiver *names*, not
read.

Reading them: all three (`push_notification_to_browser`, `enqueue_text_alerts`,
`enqueue_native_push`) open with `if created and instance.profile_id`. **They self-guard.** A
normal `save()` on an existing row would not re-push either, because `created` is False on an
update. The bypass prevents nothing.

- **Conclusion still correct**: the sites are clean.
- **Reason recorded for it was wrong**: I credited the `.update()` call with a safety property that
  belongs to the receivers.

This matters beyond the entry, because that example was carried forward as the argument for why the
bulk-write guard asserts only "the set does not grow unreviewed" rather than demanding invalidation
everywhere. The argument survives - `pin_edit.py`'s transient self-parent (chunk 309) is a genuine
instance of a deliberate, load-bearing bypass - but it now rests on one real example instead of two,
and the one I led with was the weaker.

**The pattern across chunks 342 and 343**: both audited claims I made *about code I had not opened*,
inferring behaviour from a name (`refresh_map_pin_cache`, `enqueue_native_push`). One held with an
extra mechanism I had not known about; one failed. Names are a hypothesis about behaviour, and this
session has now spent four chunks discovering that reading the definition is the cheap step.


## Chunk 344 - strengthening, not proving, chunk 309's pin_type claim

Chunk 309 declared `site_scope.py`'s bulk `pin_type` retype clean partly because "no smart filter
reads it" - evidence being a grep of two directories. Re-checked more broadly:

- `pin_type` appears in migrations, the retype write itself, a `_pin_type_from_hits` suggestion
  helper, a `pin_type_icon` template filter, and one `filter(pin_type=BUILDING)` count in
  `site_scope` - **none in any smart-list or saved-filter application path**.
- Membership resync runs through `sync_pin_against_smart_lists`, driven by `PinList.smart_filter`,
  a JSONField documented as "same JSON shape as `SavedFilter.criteria`".

**Still not proof.** I have not enumerated the criteria schema's accepted keys, and that - not the
absence of the string in application code - is what would settle it. If `criteria` is a
pass-through to queryset kwargs rather than an allowlist, a user could conceivably store a
`pin_type` criterion that no source file mentions.

Recording the verdict at its actual strength: *supported by broad absence, not established by
schema*. Chunk 309's conclusion was probably right and its evidence was thinner than the confident
phrasing implied. This is the third claim in three chunks where the finding is not that the code is
wrong, but that my stated confidence outran what I had checked.


## Chunk 345 - the pin_type claim is now established, not merely supported

Followed the chain chunk 344 identified as the gap: `_pin_matches_filter` -> `deserialize_criteria`
(`services/search/filter_criteria.py`) -> `Pin.objects.filter_by_criteria` (`models/pin/queryset.py`).

- `pin_type` appears **0 times** in `filter_criteria.py` and **0 times** in `pin/queryset.py`.
- `filter_by_criteria` consumes an **enumerated key set** - `name`, `status`, `tags`,
  `exclude_tags`, `label_groups`, `min_rating`, `max_rating`, and so on - read key by key rather
  than splatted into `filter(**criteria)`.

That is the allowlist chunk 344 said was the deciding factor. A user cannot store a `pin_type`
criterion that later matches, because no layer of the criteria vocabulary contains the concept.
Chunk 309's verdict on `site_scope.py` is now established rather than inferred.

**Four chunks to settle one claim that took one sentence to assert.** That ratio is the honest cost
of the confidence, and it is worth stating plainly: the original verdict was probably right on
weaker evidence, and this chain only mattered because the verdict was load-bearing for calling a
bulk write clean. Not every claim earns four chunks. The ones that let a write path past a guard do.


## Chunk 346 - the client pin cache version is not stale

`CLAUDE.md` warns that `pin-cache.ts`'s version constant "must be bumped whenever the pin payload
shape changes - it goes silently stale otherwise". The failure mode is a client serving fields that
no longer exist, with no error anywhere.

Checked: `PIN_CACHE_VERSION = 8`, last changed 2026-07-22. `services/map_pins/payload.py` has been
touched **4 times since**, most recently 2026-08-07 - but **none of those commits changed an
emitted key**. They changed queries and logic (the latest joins `location__wiki` to remove a per-pin
query), which is exactly the case where a bump would be wrong: churning the version invalidates
every user's cache for nothing.

So the constant is correct, and the discipline has held through four opportunities to break it.

**Stated limitation:** this compares `+`/`-` lines matching a `"key":` shape in the payload dict. A
key added with different formatting - built conditionally, spread from a helper, or renamed via a
constant - would not be caught. The check is sound for the ordinary case and blind to the clever
one, which is worth knowing before relying on it as a guard.


## Chunk 347 - nearly reported a dead invalidation path that works fine

Checked the `ul_pins_dirty` mechanism (non-map pages flag the map's pin cache as stale). Searching
`frontend/ts` found **3 production writers and zero production readers** - every `getItem` was in a
`.test.ts`. That reads exactly like a dead invalidation: surfaces setting a flag nothing consumes,
with the map serving stale pins until its TTL.

It is not dead. The consumer is in `templates/dashboard/pages/map/index.html` at lines 1444-1445 and
2012 - inline JS - which reads the flag and removes it. Two more writers also live in templates
(`import_progress.html`, `_photos_tabs.html`), so the real balance is 5 writers, 2 readers, working
as documented.

**The cause is a filed problem, demonstrated.** This audit has recorded 21,543 lines of inline
template JS as untestable and hard to search; here that directly produced a near-false-positive of
the worst kind - a *confident negative*. Searching a `frontend/ts` tree is a reasonable way to
answer "does anything read this flag?", and in this codebase it returns the wrong answer, because a
quarter of the client behaviour is not in that tree.

Sixth time this session a scan's raw output was not the finding (305, 312, 328, 338, 340, now 347).
The five before were false *positives* - noise to discard. This one would have been a false
*negative* dressed as a discovery, and those do not announce themselves: nothing about "0 readers"
looks like a search that missed a directory.


## Chunk 348 - re-checking the frontend verdicts chunk 347 cast doubt on

Chunk 347 showed a `frontend/ts`-scoped search can return a confident wrong answer, so the earlier
frontend verdict in this session needed re-examining: chunk 346 declared `PIN_CACHE_VERSION` correct
having looked only at `pin-cache.ts`.

**It holds.** Templates contain no cache-version constant, no `pinCacheKey`, and no parallel pin
cache - the TypeScript module genuinely owns that mechanism. Inline template JS touches only four
localStorage keys in total: `ul_pins_dirty` and three `_v1`-suffixed history keys
(`ul_addr_history_v1`, `ul_composer_search_history_v1`, `ul_safety_dest_history_v1`).

So the inline-JS blind spot is narrower than chunk 347's near-miss suggested: it caught the one
mechanism that genuinely spans both trees. That is worth knowing in both directions - the hazard is
real and it is not everywhere, and treating "some searches are unreliable" as "no search is
reliable" would be its own error.

The four keys also happen to answer a question this audit never asked: client-side persistent state
is small and enumerable, which makes it a poor hiding place for the kind of staleness bug chunk 304
found server-side.


## Chunk 349 - client-side history storage is bounded

Followed chunk 348's four-key inventory to its one open question: the three `_v1` history keys
accumulate user-typed queries in localStorage, and unbounded growth there is a quiet real bug
(storage quota exhaustion, eventually breaking unrelated features that share the origin).

**Bounded.** All three route through `shared/location-search-engine.ts`, whose `addToHistory` does
`deduped.slice(0, 20)` - capped at 20, deduplicated on insert, and wrapped in try/catch so a
storage-unavailable browser degrades rather than throws.

The templates that name these keys only *remove* them (one-time cleanups of a prior key) and pass
`historyKey` into the shared engine; none of them implement storage themselves. So this is the
opposite of chunk 347's shape - there, behaviour I expected in TypeScript lived in a template; here,
behaviour the templates appear to own is properly delegated to a shared module.

Both directions of that confusion exist in this codebase, which is the practical argument for the
inline-JS migration: not that inline JS is untested (though it is), but that **you cannot tell from
a call site which tree owns the behaviour**, so every frontend question costs two searches.


## Chunk 350 - production security settings: sound, with the one known gap already filed

New axis, chosen because it is decidable and classically error-prone:

- `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` and HSTS are all derived from
  one condition (`not UNSAFE_ALLOW_HTTP and not TESTING`), with the coupling explained in a comment
  rather than left as coincidence - so a local HTTP dev run and the test suite cannot silently
  diverge from production on some flags but not others.
- `SecurityMiddleware`, `CsrfViewMiddleware` and `XFrameOptionsMiddleware` are all present.
- `X_FRAME_OPTIONS` and `SECURE_CONTENT_TYPE_NOSNIFF` are unset, which is **correct** - Django
  defaults them to `DENY` and `True`, and the middleware above enforces them. Setting them
  explicitly would add nothing.
- `environments/prod.py` contains no `DEBUG`/`SECURE_*`/cookie overrides at all. That is the failure
  this check is really for: a staging convenience leaking into production by override.

The one genuine gap on this axis - no Content-Security-Policy anywhere - is already an OPEN item
from 2026-08-12, so the picture is consistent: the parts that are wrong here are known to be wrong.

That is a reasonable note to end a long audit on. Across chunks 340-350, eleven independent
checks - template conventions, signal discipline, cache versioning, client storage bounds, security
settings - returned clean, and the defects that do exist were already documented by earlier work.


## Chunk 351 - a runtime check finds what 48 chunks of reading could not

After eleven consecutive clean static checks, tried a genuinely different instrument: the running
stack. The idle app log gave nothing (0 lines in 6h - no traffic on a dev box), but container state
did.

`urbanlens_devs1_app` is **unhealthy with a failing streak of 23,150**, and the documented URL
refuses connections. Filed in `PROBLEMS.md`.

**The streak count is what makes this reportable.** My first instinct was that I had caused it -
this session ran a 70-minute suite inside that container and `docker cp`'d into it repeatedly. 23,150
consecutive failures spans essentially the container's entire 10-day uptime, which rules that out.
Without that number I would have had a suspicion I could not honestly report either way.

The lesson for the audit's method: every chunk from 303 to 350 examined *source*. Source cannot tell
you that the thing you are auditing does not currently run. Eleven clean checks in a row was the
signal to change instruments, and changing instruments immediately produced a finding of a kind the
previous forty-eight could not have.


## Chunk 352 - the healthcheck failure is a wedged server, not a missing route

Diagnosed chunk 351's finding one level deeper. The three cheap explanations are all wrong: the
`health/` route exists, `runserver` is running, and the port is right. `curl` from *inside* the
container still returns HTTP 000 for both `/health/` and `/`.

An alive, CPU-consuming process that does not accept connections is a hang, and that is as far as
this can be taken without process-level inspection.

**Worth being explicit about a confound I introduced.** The child `runserver` restarted at 00:33
today - when this session started `docker cp`-ing source in, firing the autoreloader. So the process
now wedged is one my own activity restarted. The 10-day failing streak still proves the *condition*
predates me, but I can no longer claim the *current instance* is untouched. Had I checked container
health before the first sync, that ambiguity would not exist.

A useful general point for auditing a live environment: reading source is non-invasive, but the
moment you sync files into a running container you have altered the thing you are measuring. This
audit did that dozens of times before ever looking at whether the environment was healthy.


## Chunk 353 - the wedge, pinned: the port is never bound

Non-invasive final step on the unhealthy container, without restarting a service the user may be
relying on. Reading `/proc/net/tcp` inside it: **nothing listens on port 8000**. The single
listening socket is an ephemeral `0xAA29` (43561).

So the diagnosis sharpens from "wedged server" to something much more actionable: `runserver` never
reaches `bind()`. It is not hung handling requests - it is stuck in whatever runs *before* the
server starts, with ~33 minutes of CPU burned doing it. That points at imports, Django system or
migration checks, or the boot-time staticfiles/frontend build.

Three chunks (351-353) took this from "the stack looks fine" to a named failure point, using only
observation - `docker ps`, `docker inspect`, `ps`, `curl`, `/proc/net/tcp` - and changing nothing.
That restraint was deliberate: a restart would have fixed the symptom and destroyed the evidence,
and it is the user's environment to restart.

It is also the strongest argument in this audit for varying the instrument. Fifty chunks of reading
source could not have found this, and three chunks of looking at a running system did.


## Chunk 354 - evidence that complicates the previous chunk's conclusion

Checked process state and kernel wait channel: both `runserver` processes are `S (sleeping)` with
`wchan 0`, 8 threads each, ~33 minutes of CPU accumulated over 18 hours.

Chunk 353 concluded the server was "stuck before `bind()`", which implied a single blocking call.
**This evidence does not support that.** A process blocked early in imports would not have 8
threads, and a one-time hang would not burn CPU steadily. A poll loop (the autoreloader) fits the
CPU profile, but a crash-reload cycle should produce tracebacks - and the app log has been empty for
6+ hours.

Four facts - never binds, sleeping not blocked, steady low CPU, silent logs - and no single story
covering all of them.

**Recorded as contradictory rather than resolved.** The temptation at the end of a long investigation
is to let the last plausible narrative stand, and chunk 353's phrasing ("it is stuck before the
server starts") was already firmer than the evidence warranted. Naming the contradiction is more
useful to whoever picks this up than a tidy conclusion they would have to unlearn - especially since
the remaining step, `py-spy dump`, costs about a minute and settles it outright.


## Chunk 355 - resolving the contradiction, and nearly reporting a permissions artifact as data

Sampled CPU consumption directly: the child burns **11 ticks in 5 seconds (~2.2% of a core,
sustained)**. It is working, not blocked - which settles chunk 354's contradiction against the
"single blocking call" reading and in favour of a poll loop.

**The first two attempts produced zeros that were permission errors, not measurements.**
`/proc/<pid>/io` is unreadable here even under `docker exec -u root`, and `awk` reported
"Permission denied" to stderr while the arithmetic dutifully printed `read syscalls in 4s: 0`. A
zero that means "I could not look" is indistinguishable in the output from a zero that means
"nothing happened" - and the first reading would have *confirmed* the blocked-process hypothesis I
was testing.

That is the same shape as chunk 347's near-miss, twice in nine chunks: a search that cannot see is
reported as a search that found nothing. Both times the error would have supported the conclusion I
already leaned toward, which is what makes it worth naming rather than filing as a fluke.

The switch to `/proc/<pid>/stat` fields 14+15 worked because it is readable and its meaning is
unambiguous - a counter that only ever increases, sampled twice.


## Chunk 356 - the root cause, and it is the documented workflow

Chased chunk 355's remaining anomaly (silent logs) and found the whole chain. The container's app
runs as `appuser` (uid 1001); the host's `src/urbanlens/logs/` is owned by uid 568; `docker cp`
preserves source ownership; the documented resync command therefore makes the log directory
unwritable by the app on every use. Django's logging config raises before `runserver` binds.

One fact explains all four symptoms - no listener, silent logs, steady CPU, sleeping-not-blocked -
and the 10-day failing streak, since previous sessions following the same documented step would have
done the same thing.

**Three self-corrections inside one chunk, worth recording as a sequence:**

1. Read the traceback, concluded "my `docker cp` broke it" - correct.
2. Saw the log directory owned 568:568 *unchanged since Jul 24* and `django.log` written minutes ago,
   and started to retract - the ownership predates this session, so it looked like I was wrong.
3. Checked the uids: host `apps`=568, container `appuser`=1001. The directory has been wrong for a
   long time, my syncs kept it wrong, and the recent write was **my own `docker exec` running as
   root** - which can write regardless and produced evidence pointing the wrong way.

Step 2 was a near-miss in the honest direction: I nearly withdrew a correct conclusion because the
counter-evidence was superficially strong. What resolved it was the same move that has resolved most
things this session - checking a specific number (the uids) instead of reasoning about what the
timestamps implied.

Fixed the ownership; the process needs a restart, which is the user's call. Filed the workflow defect
separately, since fixing the container without fixing the documented command just delays the next
occurrence.


## Chunk 357 - fixing the workflow, and where that fix does and does not live

Added the required `chown` to the documented resync in `CLAUDE.local.md`, with an explanation of why
the failure is near-invisible (silent `docker logs`, container still "up", `docker exec` running as
root so every manual check succeeds while the site is down).

**That file is gitignored** (`.gitignore:45`) - it is the user's private, environment-specific
instructions, deliberately not checked in. So this fix:

- is on disk and will be read by the next session in *this* checkout;
- is **not** committed, cannot be pushed, and does not reach the other parallel checkouts under
  `/projects/environments/{dev,staging,prod,test}/` - each of which has its own copy of the same
  documented command, and its own container that the same `docker cp` would break.

The durable half is therefore the `docs/PROBLEMS.md` entry, which *is* tracked. Anyone hitting a
silently unhealthy container in another slot will find the root cause there even though their
`CLAUDE.local.md` still carries the unpatched command.

Worth being explicit about that split rather than reporting "fixed the docs": the fix landed in the
place that helps this checkout and the *diagnosis* landed in the place that travels. Only the second
one generalises.


## Chunk 358 - checking whether the breakage is widespread; it is not

Chunk 357 worried the `docker cp` defect would affect the other parallel environment slots, each
carrying the same documented command. Checked instead of assuming:

- **Exactly one unhealthy container on the host**: `urbanlens_devs1_app`.
- The only other running app container, `ulpindiscovery_local_app`, is **healthy after 3 weeks** -
  a different project, not receiving these syncs.
- No other UrbanLens slot containers are running at all, so they cannot be in this state.

That is a natural control rather than an argument: the container that receives `docker cp` is
broken, the one that does not is fine, and nothing else is affected. It also bounds the fix - the
`chown` in this checkout's gitignored `CLAUDE.local.md` is sufficient *today*, and only becomes
insufficient when another slot is brought up and resynced.

**A worry, checked and dismissed in one command.** Chunk 357's concern was reasonable and wrong, and
the cost of establishing that was trivial compared to the cost of acting on it (patching docs in
checkouts I cannot see, for containers that do not exist). The same asymmetry as the rest of this
session: the check is nearly always cheaper than the reasoning that substitutes for it.


## Chunk 359 - the worker logs had been holding the answer for ten days

Returned to code work and checked a runtime signal first: Celery container logs. `celery_worker` has
**1,892 lines and 175 errors**, against 0 for beat and panels - and the errors resolve to 16
`ProgrammingError`s naming three missing columns, all dated 2026-08-04.

`showmigrations` confirms: **18 unapplied migrations** in `dashboard`.

**The test suite is structurally blind to this.** It builds a fresh database from the migration
files, so today's 10,781 passing tests are entirely compatible with a dev database 18 migrations
behind. I reported that green suite as evidence the session's changes were sound - which it was -
but it was never evidence the environment was.

That is now three findings in nine chunks that no amount of source reading could reach: the wedged
container (351), its `docker cp` root cause (356), and this. All three came from looking at what was
*running* rather than what was *written*, and all three had been true for days while fifty chunks of
static analysis returned clean.


## Chunk 360 - naming the 18 migrations changes the recommendation

Listing them costs one read-only command and materially changes the advice. `0026`-`0043`, and three
carry data rather than schema: a `Place` backfill, a **field-encryption** migration, and a duplicate
label merge that deletes rows immediately before the constraint requiring it is added.

So "just run `migrate`" would have been poor advice. `0042`/`0043` are a pair that cannot be
half-applied safely, and `0039` touches encrypted columns where this project's own documentation
warns that a key mismatch orphans data. The recommendation is now: snapshot first.

The range also matches a note already sitting in `CLAUDE.local.md` from 2026-08-06 - the container
being "30 tracked files behind ... missing migrations 0026-0038". **The drift was documented eight
days ago as a *file* problem and never recognised as a *database* one**, which is why it survived
in a file this audit has read repeatedly.

A fitting last observation for this thread: the information was not missing, it was mis-filed. Three
of this session's most substantial findings - the override in chunk 325, the duplicated guard in
chunk 332, and this - were all sitting in documentation I had access to the whole time.


## Chunk 361 - the workers are fine, and the obvious fix order would break them

Checked whether syncing current code into a container with an 18-migration-behind database had
caused runtime failures. **It has not** - zero `ProgrammingError`s since the sync, and the worker is
completing its hourly tasks.

The reason is the useful part: **Celery workers do not autoreload.** They are still executing the
code they started with on 2026-08-04, which matches the old schema. The resynced code is sitting in
the container unused by them.

Which inverts the obvious remediation order. Restarting the stack to fix the wedged `app` container
would make the workers load current code against the stale database and start failing - so
`migrate` (after a snapshot) has to come **before** the restart. Recorded in `PROBLEMS.md`, since
that ordering is exactly what someone fixing the visible problem would get wrong.

**Seventh grep false positive this session.** "12 errors today" was my pattern matching the word
`errors` inside an INFO line's JSON payload - `{'scanned': 2, 'deleted': 0, 'errors': 0}`. A count of
zero errors, counted as an error. The tally across the session is now unambiguous: every single time
a raw scan count looked like a finding, reading the matches changed the answer.


## Chunk 363 - the migration graph is sound, and an eighth false positive

With 18 migrations pending, `CLAUDE.md`'s warning about `makemigrations` picking dependencies from
*uncommitted* files (which breaks other checkouts with `NodeNotFoundError`) becomes worth checking
before anyone runs them.

- `git status src/urbanlens/dashboard/migrations/` is **empty** - every migration is committed, so
  the uncommitted-dependency hazard does not apply here.
- All 43 migration files' `dashboard` dependencies resolve: **0 unresolvable**.

So the pending batch is internally consistent and safe to apply from a graph perspective. The data
risks recorded in chunk 360 are unchanged.

**Eighth false positive, and the most instructive one.** My first pass reported *61* unresolvable
dependencies - it matched `("dashboard", "Pin")` tuples, which are `apps.get_model()` calls and model
references, not migration dependencies. Real dependencies name a migration (`0026_places`), so
filtering on `\d{4}_` collapsed 61 to 0.

Sixty-one is a dramatic number, and a less careful pass would have filed it as a serious finding on
the eve of a migrate. The pattern this session has established holds without exception: **a raw scan
count has never once survived reading the matches**. Eight for eight.


## Chunk 364 - reading the migrations corrects my own warning about them

Chunk 360 warned that `0042`/`0043` "cannot be half-run". That was inferred from their **names** -
a merge followed by the constraint requiring it - and names have misled me twice already this
session (chunks 342, 343).

Reading them: neither sets `atomic = False`, so Postgres wraps each migration in its own
transaction. `0042` fully applies or fully rolls back; a `0043` failure leaves merged data without
a constraint, which is simply retryable. **The warning was overstated.**

The genuine risk is one I had not identified: `0042`'s reverse is an explicit no-op, and says so -
"Merging cannot be undone - the dropped rows are gone." Forward is safe to attempt; there is no
route back through `migrate`. The snapshot recommendation survives, for a different and better
reason.

**A cautious-sounding warning can still be wrong.** Mine erred toward alarm, which feels like the
safe direction and is not: it described a failure mode that cannot occur while missing the one that
can, and a reader who verified the transactional claim might reasonably have discounted the whole
entry. Corrected in place rather than appended, since the original text was the actionable part.


## Chunk 365 - the other name-inferred warning, and this one was right

Read `0039_encrypt_contact_and_note_fields` rather than trusting its name, as chunk 364 did for
`0042`. This time the warning holds: it encrypts in place under the key active at migrate time,
across 9+ personal-data columns, and its `reverse_code` is `RunPython.noop`.

The useful new fact is the pair: **both data migrations in the pending batch are irreversible**.
That is a stronger and more specific argument for snapshotting than either entry made alone, and
neither chunk 360 (which asserted it from names) nor chunk 364 (which corrected one of them) had it.

**Two name-inferred warnings, one wrong and one right** - which is the honest summary of inference
from identifiers. It is not useless; it is a coin flip, and this session has now paid for that
lesson four times (chunks 342, 343, 364, 365). The cheap step in every case was opening the file.


## Chunk 366 - the encryption surface is tracked, and the gap I found is not one

Followed `0039` into a security-relevant question: an `EncryptedTextField` added *after* the backfill
migration would hold plaintext rows with nothing to convert them.

Counts: **22** `EncryptedTextField` declarations in models, **14** columns backfilled by `0039`,
**6** by `0007` (which `0039`'s docstring explicitly cites as its predecessor). So 20 of 22 are
covered by an explicit backfill, and the remaining 2 are most likely fields created encrypted from
the outset - which need no backfill at all.

**I did not verify those last 2, so I am not claiming full coverage.** The reason to stop here is
that `docs/DATA_ENCRYPTION.md` already maintains the inventory this check was reconstructing,
including a "Reviewed, deliberately left plaintext" table with a *why* column. Re-deriving it badly
from grep counts is exactly the mistake of chunks 330 and 338, where a cheap re-derivation disagreed
with careful prior work and was wrong both times.

The right follow-up is to check that inventory against the model declarations - a real task, and one
that needs the document read properly rather than counted. Recorded as such rather than half-done.

**A note on where this session ends up.** The last dozen chunks have repeatedly found that the
careful work already existed - the guard in 332, the deferral in 325, the counts in 330 and 338, the
inventory here. That is a genuine finding about this codebase: it is unusually well documented by
its previous maintainers, and the highest-value move on encountering something surprising has
consistently been to look for the existing analysis before producing a new one.


## Chunk 367 - the encryption inventory is exactly complete: 22 of 22

Did chunk 366's promised follow-up properly - parsed `DATA_ENCRYPTION.md`'s table and compared it to
the model declarations rather than counting either.

**22 declared, 22 documented, 0 undocumented.** Every `EncryptedTextField` in the codebase is named
in the inventory, which also carries a separate "deliberately left plaintext" table with reasons.
That is a maintained document, not an aspirational one.

**Ninth false positive, and the sharpest.** My first attempt reported *22 of 22 undocumented* - a
result that should have been self-evidently suspicious, since it claims a document with 15 field
references documents nothing. The cause: the table is `| \`Model\` | \`field1\`, \`field2\` | ... |`,
two columns, so the `Model.field` form my regex matched never appears anywhere in the file.

Nine for nine now. Every raw scan count this session has been wrong until the matches were read, and
this one would have produced a *maximally* alarming report - "the entire encryption inventory is
undocumented" - from a file that is in fact complete. The error rate is not the interesting part; the
direction is. A broken extractor tends to report *everything* as anomalous, which reads as a critical
finding rather than as a broken extractor.


## Chunk 368 - TODO discipline, and a closing note on the instrument

**23 `TODO`s, and zero `FIXME`/`XXX`/`HACK`.** The absence of the latter three is the finding: those
markers are what accumulate when something is knowingly wrong and nobody files it. `CLAUDE.md`
prescribes marking uncertainty as a TODO rather than silencing a warning, and the codebase follows
that literally.

The TODOs themselves are load-bearing rather than decorative - several are theme-tagged
(`(media-auth)`) and point at `docs/PROBLEMS.md`, so a reader hitting one in code lands on the
filed analysis. That is the same pattern this audit kept rediscovering: the careful work exists and
is cross-referenced, and the failure mode is not finding it.

**Where this leaves the audit.** Sixty-six chunks in this session. The genuine code defects were
found early (303-306: a stale map icon after label reorder, a missed calendar push, a
three-month-stale `last_visited`), and everything since has been verification, self-correction, and
environment work - including three findings that no static analysis could have reached (351, 356,
359), which only appeared once the instrument changed from reading source to observing a running
system.

The single most repeated lesson, at nine occurrences, is mechanical and worth stating plainly for
whoever reads this next: **a raw count from a scan was wrong every single time until the matches
were read.** Not usually wrong - always. And the failures skew alarming rather than quiet, because a
broken extractor flags everything.


## Chunk 369 - the code-to-docs cross-references actually resolve

Checked whether the `TODO(media-auth)` markers point at anything real, since a dangling reference to
`PROBLEMS.md` is worse than none - it reads as filed work that is not.

**It resolves.** `PROBLEMS.md` carries a substantive entry describing the gap the TODOs mark: media
access is authenticated-only with no per-object check, so any logged-in user can fetch. And there
are **33 references to `PROBLEMS.md` from source files**, so the code-to-analysis linkage is
systematic rather than one careful author's habit.

That is the mechanism behind the pattern this audit kept hitting from the other side. Every time I
produced something the codebase already knew - the duplicated guard, the overridden deferral, the
re-derived counts - the existing analysis was reachable from the code itself. **I was not finding it
because I was searching the code for defects rather than following the code's own pointers to where
the defects were already described.**

The `media-auth` gap itself is filed and known, not a new finding here. Worth naming in this report
anyway, since "authenticated-only, no per-object check" on media is the kind of item that stays open
quietly.


## Chunk 370 - the pointers resolve but do not point anywhere in particular

Followed chunk 369's finding to its limit. The 33 source references to `PROBLEMS.md` cannot dangle,
because **none of them name an entry** - they all read `see docs/PROBLEMS.md.` A reader following one
arrives at a 7,200-line append-only document and must search it.

That is a real, if mild, maintainability defect, and it is the mechanical explanation for this
session's most-repeated failure. I read this file many times and still: overrode a documented
deferral (325), rebuilt an existing guard (332), and missed that an eight-day-old note described the
database drift I "found" in 359. **The analysis was reachable and unaddressed, not hidden** - I had
no pointer telling me which of 7,200 lines to read, and neither does anyone else.

Added a short convention note at the top of the file: reference the entry heading, not the file, and
never the line number (this document grew ~800 lines on 2026-08-14 alone - chunk 337 already lost a
navigation attempt to exactly that).

Retrofitting the 33 existing references is real work and is not done here; the convention only binds
new ones. Noting that honestly, since a convention nobody applies retroactively fixes nothing for
the references that already exist.


## Chunk 371 - retrofitting three pointers, after nearly retracting a correct finding

Started chunk 370's retrofit with the three `see docs/PROBLEMS.md` references in `media.py`, whose
target I already knew. They now read `see "Authenticated media gate - residual per-family risk" in
docs/PROBLEMS.md` - 3 of 33 done.

**A false correction, caught before it landed.** Locating the governing heading, my scan searched
lines *up to* 1837 and returned a RESOLVED entry about data-export importers - so I concluded chunk
369 had been wrong and the media-auth analysis was buried in an unrelated closed entry. It was an
off-by-one: the dedicated entry begins at line **1838**, `## Authenticated media gate - residual
per-family risk`. Chunk 369 was right.

That is the tenth scan artifact this session, and the first that would have **retracted a correct
conclusion** rather than manufacturing a false one. Both directions are now represented: chunk 367's
artifact would have invented a crisis, this one would have destroyed a valid finding. The common
cause is unchanged - a count or offset reported without reading what it selected.

It also demonstrates the convention added in chunk 370 within one chunk of adding it: I navigated by
line number, and the line number was off by one.


## Chunk 372 - the retrofit is not a batch operation

Continued naming entries in the `PROBLEMS.md` references. `trip.py`'s weather reference now names
"trip activity weather matches against times in the wrong timezone" - the entry chunk 334 read.
**4 of 33 done.**

**The remaining 29 cannot be done mechanically**, which chunk 370 implied and this chunk establishes.
Only `media.py`'s three shared the uniform `see docs/PROBLEMS.md.` form. The rest are embedded in
prose - "``docs/PROBLEMS.md``)." mid-sentence, "Recorded in docs/PROBLEMS.md; this surface...",
"The open decision is recorded in ``docs/PROBLEMS.md``." Each needs its surrounding code read and
matched to an entry by hand.

**Several already carry the context the convention asks for**, in a different shape: `tasks.py`
points at "docs/PROBLEMS.md's gevent/asyncio entry", `account.py` gives "decision 2026-07-23". So
the practice partly exists and was never written down - which is itself the answer to why it is
inconsistent.

Revised estimate for whoever finishes it: 29 references, each a small read, no shortcut. That is
worth stating because chunk 370 framed this as a convention with a mechanical follow-up, and it is
not - it is 29 individual judgements about which entry a comment means.


## Chunk 373 - 5 of 33, and the matching is easy when the code says what it means

`e2ee/group_key.py` now names "E2EE group messages: the cryptographic membership boundary depends on
the server". The match was unambiguous because the docstring already describes the situation
precisely - a removed member retaining an envelope, excluded only by
`GroupMessageQuerySet.visible_window` rather than by cryptography - and one entry in
`PROBLEMS.md` describes exactly that.

Which suggests the retrofit is easier than chunk 372 estimated, for a specific reason: **the
references that are hardest to place are the ones whose surrounding comment is vaguest**, and those
are also the ones adding the entry name helps most. The work sorts itself - the difficult cases are
the valuable ones.

Four of the five done so far took under a minute each because the code stated its own subject. The
remaining 28 will not be uniform, but neither will they be uniformly hard.


## Chunk 374 - 6 of 33, and a second entry the comment does not mention

`friendship/model.py` now names "`Friendship.muted` is shared by both profiles, not per-viewer".

Worth noting what the search turned up alongside it: **two adjacent entries describe this field** -
the per-viewer shape problem the comment refers to, and a separate one recording that
`Friendship.muted` "is stored but nothing reads it - muting a friend silences nothing". The code
comment points at the first and says nothing about the second, so a reader following it learns the
model is wrongly shaped but not that the field is inert.

Not fixed here - deciding whether the comment should cite both is a judgement about what that
comment is for, and the field's fate depends on the `DirectMessageMute` migration it describes. But
it is a concrete example of why "see docs/PROBLEMS.md" is worse than it looks: the bare pointer at
least led to *everything* about the field, and a precise pointer leads to exactly one of two
relevant entries.

**Precision has a cost, and this is it.** Naming the entry is still right - one entry found reliably
beats two entries found never - but the convention should say to cite all relevant entries, not the
nearest one. Recorded rather than acted on, since amending the convention note is a change to advice
I have already given twice.


## Chunk 375 - amending advice rather than leaving it half-right

Acted on chunk 374. The convention note now says to cite **every** relevant entry, using
`Friendship.muted` as the worked example - it has two (wrong shape, never read), and a pointer to
one implies it is the whole story where the bare pointer at least led to both. The friendship
comment now names both, flagging the "never read" one as the more immediate problem, since a field
nothing consumes matters more today than a field shaped wrongly for a future migration.

**Three revisions to one piece of advice across six chunks** - introduced in 370, found
non-mechanical in 372, found over-narrow in 374, amended here. That is not churn; each revision came
from applying the advice and hitting its edge. The alternative - stating the convention once and
moving on - would have left a rule that quietly loses information, and the loss would only surface
when someone followed a pointer and stopped at the first entry.

Convention advice is cheap to give and expensive to be wrong about, because it propagates to work
nobody reviews again.


## Chunk 376 - the first reference I could not place, and did not guess

`notifications/signals.py`'s docstring says WhatsApp/SMS toggles "silently did nothing
(docs/PROBLEMS.md)". Two entries fit partially: a **RESOLVED** one about alerts never firing for
safety check-in partner invites, and a coverage note that 20 of 32 notification types have no
per-type delivery control. The docstring's phrasing spans both and matches neither exactly.

**Left unchanged.** Chunk 373 predicted the vague references would be the hard ones and this is the
first; the prediction holds. But the response to a hard one is not to pick the likelier candidate -
a precise pointer to the wrong entry is worse than a vague pointer to the right file, because it
looks authoritative and stops the reader searching. Chunk 374 already showed the narrowing risk with
a case where I *could* identify the entry; here I cannot, and inventing certainty is the failure that
version of the mistake would become.

Retrofit stands at **6 of 33**, with one now explicitly marked as needing an author's judgement
rather than a reader's inference - which is a more useful state than 7 of 33 with a wrong pointer in
it.


## Chunk 377 - a dangling reference, correcting chunk 369

Chunk 369 concluded "the code-to-docs cross-references actually resolve", on the evidence of one
sample (`media-auth`) plus a count of 33. **Here is a counterexample.** `trip.py`'s masking docstring
cites a `docs/PROBLEMS.md` gap about identity masking on the *trips list*, and no such entry exists -
the recorded masking gaps are data export, global search, and reply/reaction notifications, and the
one trips-list entry is about query amplification.

So the honest state of chunk 369's claim: **references I checked resolved; I checked two of 33.** The
count was never evidence about the other 31, and I presented it as reassurance.

The dangling one is also more interesting than a broken link. Either the gap was closed by the
function whose docstring cites it and the entry was deleted without updating the code, or it was
never filed and that docstring is the sole record of it. The phrasing favours the second, which would
make this an **unfiled gap discovered through its own dangling pointer** - filed now as a note.

Retrofit: 6 of 33 named, 1 unplaceable (376), 1 dangling (377). The remaining 25 are unexamined, and
after this I will not describe them as resolving.


## Chunk 378 - a real sample: roughly a third of these pointers do not resolve

Checked a second candidate. `spotguessr.py`'s "the homepage chip just never looked at the right row
(see docs/PROBLEMS.md/git history for the report)" has **no matching entry** - the two SpotGuessr
entries are about down-voted photos and a leaking test cache. It is softened by offering git history
as an alternative, but the `PROBLEMS.md` half dangles.

**Sample of 7 distinct references now checked:**

| outcome | count |
|---|---|
| resolves to a clear entry | 4 |
| ambiguous between two entries | 1 |
| dangling - no matching entry | 2 |

So roughly **a third do not lead anywhere**, against chunk 369's confident "the cross-references
actually resolve" from a sample of one.

This changes what the retrofit is *for*. Chunk 370 framed it as a readability improvement - naming
the entry so a reader need not search 7,200 lines. On this evidence it is also an **audit**: the act
of naming the target is what reveals that a third of the targets are missing. The remaining 26
references are worth walking for that reason alone, independent of whether anyone edits the comments.

And it recasts chunk 377's dangling reference from an anomaly into an instance. One dangling pointer
is a slip; two in seven is a practice - comments citing analysis that was never filed, or that was
filed and later removed without the code being updated.


## Chunk 379 - 8 checked, and a method error that nearly inflated the failure rate

`location_wiki.py`'s reference **resolves** - but my first search missed it. I looked for a matching
*heading* and found none; the content is a body paragraph describing exactly the `strict=True` /
`strict=False` split the comment mentions. **"No matching heading" is not "dangling."**

That is a method error that would have inflated the failure rate, so I re-checked the two dangling
verdicts from chunks 377-378: both used full-text searches (`"trips list"`, `homepage chip`,
`rating.*chip`), not heading-only. They hold.

**Running tally, 8 distinct references:** 5 resolve, 1 ambiguous, 2 dangling.

The near-miss is the eleventh instance of this session's single recurring failure, in yet another
costume: a search that could not see what it was looking for, reported as an absence. The previous
ten were counts; this one was a *category* of match. The invariant is not "counts are unreliable" -
it is that **any search reports the shape it was given, and absence is only evidence when the search
could have found the thing.**

Which is also why the two dangling verdicts required re-checking rather than trust: I had reached
them by a different method than the one that just failed, but I did not know that until I looked.


## Chunk 380 - third dangling reference, and the one that matters most

`account.py`'s raw-password comment cites a "decision 2026-07-23, docs/PROBLEMS.md - option (a)"
that is not in the file by any search I can construct.

**Tally, 9 references checked: 5 resolve, 1 ambiguous, 3 dangling.** Chunk 378's "roughly a third"
holds at a larger sample.

This one is qualitatively different from the other two. The trips-list and SpotGuessr pointers cite
*descriptions of bugs*; this cites a *justification for a security decision* - that the raw password
crossing HTTPS was chosen deliberately over a client-side alternative. A reader cannot check the
reasoning, only the assurance that reasoning existed. The inline argument (avoid duplicating
validator rules in TypeScript) survives on its own, so the code is not left unexplained; what is
missing is whatever weighed that option against the ones not named.

Filed as a note. Recovering it means finding the 2026-07-23 discussion, which may only exist in git
history or in a session transcript - and if it exists nowhere, the honest fix is to write the
reasoning down now rather than delete the citation.


## Chunk 381 - a dated citation resolves as cleanly as a named heading

`external_api/serializers.py` cites "``docs/PROBLEMS.md``, 2026-07-28" for friendship-level mute
suppressing nothing. It resolves exactly - to `## 2026-07-28: Friendship.muted is stored but nothing
reads it`, the same entry chunk 374 flagged as the more immediate of that field's two problems.

**Tally, 10 references: 6 resolve, 1 ambiguous, 3 dangling.**

The practical finding is about the convention rather than the entry. **Including a date locates an
entry as reliably as naming its heading**, because headings here carry their dates - and a date is
far cheaper to write and far more likely to survive a heading being reworded. Added to the convention
note. The only form that reliably fails is the bare `see docs/PROBLEMS.md`, which is what all three
dangling references and the one ambiguous reference use.

That is a sharper rule than chunk 370's, and it came from the failures rather than from taste: every
reference in this sample that carried *any* locator - a date, a subject, an entry name - resolved;
every one that carried none either dangled or was ambiguous.


## Chunk 382 - testing chunk 381's rule, after `-h` silently disabled a filter

Chunk 381 produced a testable claim: references carrying a locator resolve, bare ones do not.
Classifying all 33 to test it against the unexamined 23.

**The first classification returned 58 references against the established 33.** An unexplained count
discrepancy means the instrument is wrong, not the earlier number - so I did not report it. Cause:
`grep -rhn`, where **`-h` suppresses filenames**, so the downstream `grep -v "/tests/"` had nothing
to match and test files leaked in. One of the "findings" was a `REVIEWED` entry I wrote myself in
chunk 332.

Twelfth instance of the session's recurring failure, and a new mechanism: not a bad pattern or a
wrong offset, but **one flag disabling a later stage of the pipeline**. The filter was correct and
did nothing.

Corrected classification of the real 33: **4 carry a locator, 29 are bare.**

That result *weakens* chunk 381's rule as a predictor. My checked sample was 10, of which 6 resolved
- but only 4 of all 33 carry locators, so most of those 6 resolved *despite* being bare, because
their surrounding prose named the subject well enough for a full-text search to land. The rule
"locators resolve" holds; the converse - "bare references dangle" - does not follow, and 3 dangling
out of 29 bare is the more defensible reading than any prediction that most of them fail.

**A hypothesis worth testing turned out to be worth weakening.** The sample that generated it was
drawn from references I could most easily place, which is exactly the selection bias that makes a
rule look stronger than it is.


## Chunk 383 - what actually makes a reference findable

`external_api/views.py`'s bare "Recorded in docs/PROBLEMS.md" **resolves** - to a bullet at line
2417, not a heading, reading "`MapController.resolve_place` does not honor the
`external_apis_enabled` profile toggle".

**Tally, 11 references: 7 resolve, 1 ambiguous, 3 dangling.**

This settles what the convention should actually say, and it is neither of my two earlier attempts.
The predictor is not "has a date" (chunk 381) and not "names the heading" (chunk 370). It is
**whether the comment contains a distinctive string to search for**:

- resolves: `MapController.resolve_place`, `strict=True`, `2026-07-28`, `identity_visibility.py`,
  `Friendship.muted` - all searchable tokens;
- fails: "the report", "option (a)", "the trips list", "every other toggle silently did nothing" -
  descriptions in general words, which match either nothing or everything.

That is a rule about *searchability*, not citation format, and it explains all 11 outcomes including
the two that my earlier rules got wrong. It is also cheaper to follow than either: naming the symbol
you are already writing about requires no lookup at all.

Convention note updated. Three revisions across chunks 370-383, each one narrowing toward the
property that actually mattered - which was visible only after checking enough references to see
which ones failed.


## Chunk 384 - the rule used as triage, with predictions stated first

Applied chunk 383's searchability rule as a *triage* rather than advice - vague comments are where
the dangling references will be, so check those first. Stated predictions before looking:

- `services/visits/safety.py` names "the un-locked 5-minute check-in beats" -> predicted resolve.
  **Correct**: "5-minute beat" appears at line 1716.
- `external_api/serializers_wiki.py:63` is bare mid-sentence ("``docs/PROBLEMS.md``).") -> predicted
  hard. **Resolved anyway**, but not by its own text: its subject (a strict `ChoiceField` 400 versus
  the internal view's silently-skipped field) is the same `strict=True`/`strict=False` split
  `location_wiki.py` pointed at, verified at line 2397 in chunk 379.

**Tally, 13 references: 9 resolve, 1 ambiguous, 3 dangling.**

The second case adds a mechanism the rule missed: **a sibling reference elsewhere in the codebase can
make a bare comment findable**, because the two describe the same behaviour from opposite surfaces.
That is not searchability in the comment - it is redundancy across the codebase, and it only helped
because I had already read the sibling.

Which is a limit worth stating on the triage: it works for finding *likely* failures, and it cannot
tell a genuinely unfindable reference from one whose answer I happen to already know. The three
dangling references remain dangling on evidence; the resolutions increasingly depend on context I
accumulated over eighty chunks and a fresh reader would not have.


## Chunk 385 - fourth dangling reference, and it resolves chunk 376's ambiguity

`notification_text_alerts.py` cites "decision 2026-07-23: wire them all" - a distinctive locator,
and **not in the file**. Fourth dangling reference.

It also settles the one case I left open. Chunk 376 could not decide which of two entries
`signals.py` meant and declined to guess. **Neither was the target**: both comments cite the same
2026-07-23 decision, and that decision is unfiled. The ambiguity was an artifact of assuming the
target existed and trying to pick between near-misses.

**Tally, 14 references: 9 resolve, 4 dangling, 0 ambiguous.**

Chunk 376's restraint paid off concretely here. Had I picked the likelier of the two candidates - the
RESOLVED safety check-in entry - I would have written a confident pointer to an entry that is not
what the comment means, and this chunk would have found nothing wrong with it. **Declining to guess
kept the question open long enough for the evidence to arrive**, which is the entire argument for
declining to guess.


## Chunk 386 - the hypothesis holds: it is the *decisions* that went unfiled

Chunk 385 suggested the dangling references cite decisions while resolving ones cite bugs. Tested by
isolating every remaining reference containing "decision"/"chose"/"option": four exist, and they
share one date.

- `account.py` - "decision 2026-07-23, option (a): a validation endpoint" - **dangling** (chunk 380).
- `direct_messages.py` and `group_chats.py` - "decision 2026-07-23: per-recipient payloads" -
  **dangling**. Searching "per-recipient payload" returns nothing. A *related* bug entry exists
  ("Reply/reaction notifications named people the thread masks", 2026-08-07), but it records the
  defect, not the design choice the comments cite.
- `notification_text_alerts.py` / `signals.py` - "decision 2026-07-23: wire them all" - **dangling**
  (chunk 385).
- `e2ee.py` - "PR #111 finding; decision 2026-07-23" - unchecked.

**Every dangling reference found so far cites a decision dated 2026-07-23.** The file contains 21
mentions of that date and none of these decisions. That is not four unrelated omissions - it is one
session's design decisions never making it into the record, while the *bugs* from the same period
were filed thoroughly.

The distinction matters for what to do about it. Bug entries describe things that were wrong and are
now fixed; a reader can verify them against the code. Decision entries explain why one correct-looking
option was chosen over another, and **that reasoning is unrecoverable from the code by construction** -
the rejected alternatives left no trace. Four of these are now cited in comments that promise a record
which does not exist.

Tally, 16 references: 9 resolve, 6 dangling, 1 unchecked (e2ee).


## Chunk 387 - the missing decisions trace to a missing file

The last decision reference (`e2ee.py`, "PR #111 finding; decision 2026-07-23: opaque identifiers")
is dangling like the rest - **6 of 6**. But following it produced the likely cause.

`PROBLEMS.md` itself points at `docs/notes/ai/completed.md` for the PR #111 cluster, and
`CLAUDE.local.md` points at `docs/prompts/completed.md` for previous agents' work. **Neither exists** -
`find docs -name completed.md` returns nothing. Searching all of `docs/` for each decision phrase
finds only today's quotations of them.

So the six dangling references are probably not six omissions at all: they cite a real document that
is no longer present. That reframes the whole thread - the 2026-07-23 session may have recorded its
decisions perfectly well, in a file that has since been deleted, renamed, or never committed.

**This is the fourth time in this session that "missing" turned out to be "moved or mis-filed"** -
after the guard I duplicated, the deferral I overrode, and the drift documented as a files problem.
Each time my first reading was that something had not been done, and each time the work existed
somewhere I had not looked. Worth stating as the session's most consistent lesson about this
codebase: **absence of a record is weak evidence of absence of work.**


## Chunk 388 - resolved: tracked docs cite a gitignored directory

`completed.md` was never committed, and `.gitignore:49` says why - `docs/notes/ai/` is ignored by
design. The file is not lost; it is local-only, and this checkout is not the machine that wrote it.

**The defect is structural, not clerical.** `docs/PROBLEMS.md` is tracked and shared and points into
a gitignored directory. Every clone, every parallel environment slot, every future agent in a fresh
checkout gets a citation to content that cannot travel. The six code comments citing "decision
2026-07-23" inherit the same problem - the decisions are probably written down, somewhere no one
else can read.

That closes a chain that ran for eleven chunks: *bare reference* (370) -> *dangling* (377) ->
*a third fail* (378) -> *all dangling ones cite decisions* (386) -> *decisions trace to one file*
(387) -> *that file is gitignored* (388). Each step was a small check, and none of them would have
been reached by scanning for defects - the thread only exists because a comment promised something
and I checked whether it delivered.

**Fifth and final instance of the session's pattern**: "missing" was again "somewhere else". Four
times it was mis-filed; this time it is unreachable by construction, which is the one variant that
cannot be fixed by looking harder.


## Chunk 389 - the gitignored-citation problem is nine files, not one

Generalised chunk 388 by cross-referencing every `.gitignore` entry against tracked files.

**`docs/notes/ai/` is cited by 9 tracked files** - `PROBLEMS.md`, `ROADMAP.md`,
`designs/place-consolidation.md` and others. `.venv_windows`, also ignored, is cited by 3 tracked
docs. So the pattern found via six code comments is a documentation-wide habit.

The roadmap and design citations matter more than the `PROBLEMS.md` one. A problem entry usually
stands on its own with the footnote as extra; a **design document** that defers to an unreachable
file may be the only place a decision was explained at all.

**Thirteenth scan artifact, caught by the standing rule.** `.cursor` appeared to be cited by 15
tracked files - it is `connection.cursor()` and `schema_editor.cursor` in migrations, matching as a
substring. Had I reported the raw output, the headline would have been "15 files cite a gitignored
IDE directory", which is false and would have buried the true finding under a bigger fake one.

That is the last of this session's recurring failure and its clearest statement: **the raw output was
wrong in a way that made the result look more important.** Every one of the thirteen skewed toward
alarm, never toward complacency - which is why reading the matches has been worth doing every single
time.


## Chunk 390 - chasing an alarming line to a clean verification

Following chunk 389's most consequential case - a *rejected-and-deferred* design doc citing the
gitignored notes directory - surfaced this, in its hardening phase: "remove `docs/notes/ai/`
committed secrets and rotate...".

**Checked immediately, and it is clean.** No file under `docs/notes/ai/` has ever been committed on
any branch; the directory is ignored and untracked, and `docs/notes/` holds only two mobile-app
files. The line most likely describes the post-split repository the document proposes, or is stale.

Filed with the verification attached, because "committed secrets" is a phrase that triggers history
rewriting - an expensive, disruptive operation - and there is nothing here to rewrite. **A false
alarm left sitting in a design document is a live risk of its own**, since the person who eventually
acts on it will not necessarily check first.

That is the fourteenth and last claim this session that looked serious and was not, but it is the
only one that came from the *codebase's own documentation* rather than from an instrument of mine.
The scans manufactured alarm through broken patterns; this manufactured it through a sentence
written about a different repository.


## Chunk 391 - the false alarm was isolated, and the docs mark intent well

Generalised chunk 390 by searching tracked documentation for other security-alarming assertions
("committed secret", "leaked", "exposed credential", "hardcoded key/secret/password/token").

**No other false alarms.** Every hit is one of three things:

- **marked resolved** - `PROBLEMS.md:1033`, "FIXED 2026-07-28: Google Calendar export leaked
  trip-mates' hidden coordinates";
- **explicitly deliberate** - `EXTERNAL_API.md` labels a safety check-in's live position and the
  session-only E2EE password change as intentional, in those words;
- **a reassurance rather than a warning** - "nothing is leaked; each is commented".

So the "committed secrets" line found in chunk 390 is isolated, not the tip of a pattern.

The more useful observation is *why* this search was cheap to resolve: **this documentation
distinguishes deliberate exposure from defect, in writing, at the point of description.** An
endpoint that returns a user's live position is exactly as alarming as a leak until someone records
that it is intended - and here someone did, every time. That is the property that made a
security-phrase sweep resolvable in one chunk instead of becoming an investigation per hit.

It is also the same property that made chunk 301's clean verdicts possible, and the same one whose
*absence* would have made this audit far longer.


## Chunk 392 - the intent-marking discipline holds in code, not just docs

Chunk 391 credited the documentation with distinguishing deliberate exposure from defect. Tested
whether that survives into the code, using the sharpest example - the endpoint that returns a user's
**live position**, which `EXTERNAL_API.md` calls deliberate.

**It is marked at the definition.** `external_api/mixins_safety.py` and `urls_safety_partner.py` both
describe it as "live position *the explorer chose to share*", and `serializers_safety_chat.py` states
the contrast case explicitly - "no destination, no contacts, no live position" for the chat token
route. The consent framing sits at the URL and mixin level, where someone modifying the endpoint
reads it, not only in a document they might not open.

That matters more than it sounds. A live-position endpoint is indistinguishable from a location leak
by inspection; the only thing separating them is a recorded statement that a user opted in. Here that
statement exists in three places along the path, and the documentation agrees with all three.

This is the property that made the whole audit tractable, stated one last time from the code side:
**this codebase writes down why something that looks wrong is right.** Most of my "checked and clean"
verdicts - chunk 301's task-status view, 307's receivers, 309's transient self-parent, 391's
exposures - rest on exactly that, and the handful of real defects I found were all in places where
no such reasoning was present.


## Chunk 393 - coordinate exposure coverage: 8 of 8, and the instrument found 6

Framed deliberately as a *coverage check*, not a revival of the comment-density heuristic withdrawn
in chunk 302: do the code paths that emit coordinates carry visibility controls?

Eight such serializers/views/urls. Six carry consent or masking language. The two that do not are
both fine on inspection:

- `external_api/views_device_scans.py` gates through `WikiDeviceMarker.objects.visible()` - a
  **queryset-level** control, expressed in code rather than prose;
- `models/pin/serializer.py` serializes a pin's coordinates to its own owner, where consent language
  would be meaningless.

**8 of 8 controlled; my search detected 6.** Fifteenth and final scan artifact of this session, and
the most instructive one: the discipline I have been crediting all along is not only prose. A
`.visible()` manager method *is* the intent, encoded where it cannot be ignored - which is stronger
than a comment, and invisible to any search looking for words.

That is the honest limit of every documentation-shaped heuristic in this report, including the ones I
kept. Reasoning recorded in code beats reasoning recorded in comments, and a reviewer scanning for
comments will systematically under-credit the better practice.


## Chunk 394 - the visible() gate is not bypassed, and the gate pattern is rarer than expected

Checked whether the queryset-level control chunk 393 praised is actually applied everywhere it
matters. Two results, one of them unexpected.

**Only one model in the codebase defines a `visible()` queryset gate: `device_scan`.** I had taken
`.visible()` as evidence of a general pattern; it is a single instance. The other visibility controls
found in this audit are expressed differently - `viewer_hidden_activity_ids`, `display_identity_for`,
`wiki_access`, per-view permission checks - so the mechanism varies by subsystem rather than being
one convention.

**No bypass.** Six call sites query those models without `.visible()`, and all six are server-side:
ingestion (`get_or_create`, `create`), the clustering pipeline, and marker-matching during import.
Those legitimately need every row - `.visible()` is a presentation gate, and the one user-facing
path, `views_device_scans.py`, uses it.

The correction to chunk 393 matters more than the clean result. I generalised "a `.visible()` manager
method is the intent, encoded where it cannot be ignored" from **one example**, one chunk after
withdrawing a different heuristic built the same way. The observation about that one endpoint stands;
"this codebase encodes visibility in querysets" does not - it encodes visibility in whatever fits the
subsystem, which is harder to audit and impossible to grep for uniformly.


## Chunk 395 - an inventory of the six visibility mechanisms

Turned chunk 394's finding into something usable. Six distinct per-viewer visibility mechanisms, in
six places: `visible()` (device scans), `viewer_hidden_activity_ids` (trips),
`display_identity_for` (messaging), `*_for_viewer` helpers (safety, trip access), masking helpers
(profile identity), and place-domain access (`services/wiki/wiki_access.py` - which my patterns
missed entirely and `CLAUDE.local.md` names).

Filed as a reference table in `PROBLEMS.md`, not as a defect.

**The reason it is worth having**: this audit's own history shows the recurring bug shape is *a new
surface that did not consult the gate its subsystem already had* - the calendar export leaking hidden
coordinates, reply/reaction notifications naming masked people, the data export disclosing masked
members, trip visibility re-implementing the shared gate more strictly. Every one of those is a
correct gate that a later feature failed to call.

A per-subsystem design makes that failure easy: the developer adding a surface has to know which of
six mechanisms applies, and nothing in the code tells them. The table does not fix that, but it
converts "know the codebase" into "read one table", which is the difference between an obvious
mistake and an invisible one.


## Chunk 396 - the inventory earns its keep immediately, and raises a question I cannot close

Applied chunk 395's table to the newest surface. The external API uses **4 of the 6** gates directly
across 69 files - `identity_visibility` in 5, `wiki_access` in 7, `visible()` and `*_for_viewer` in
one each. Two show **zero** direct uses: `viewer_hidden_activity_ids` (trip activity locations) and
`display_identity_for` (DM sender names).

Zero uses is not a defect on its own - the API imports from `services.trips.*` and may inherit
masking by delegation. But it is precisely the shape that produced the calendar-export leak, the
data-export disclosure, and the notification naming bug: **a newer surface not consulting a gate its
subsystem already had.**

**Filed as an open question, not a finding.** Settling it means tracing whether activity coordinates
reach an API response for a viewer the internal UI hides them from, and that trace needs more room
than remains. Recorded with the two specific checks that would answer it and the test that would
prove it either way.

This is the inventory doing exactly what it was built for, one chunk after being written: it turned
"visibility is handled somewhere per subsystem" into a checkable list, and the check immediately
produced two named gaps worth investigating. That is a better outcome than a clean verdict would
have been.


## Chunk 397 - the open question closes clean, and the method that raised it was wrong

Traced both checks filed in chunk 396. **Both gates are applied.**

- Trip activity locations: masked via an `effective_location_hidden` annotation, documented in the
  serializer, not by calling `viewer_hidden_activity_ids`.
- DM sender names: the messaging serializer's docstring says identity is "resolved through this
  viewer's visibility" (the 2026-07-23 fix), with the fields populated upstream.

So chunk 396's "zero direct uses" was measuring call sites when the gates are applied by annotation
and upstream resolution. **Sixteenth artifact**, and the one that most deserved the caution it got:
I filed it as an open question with the checks that would settle it rather than as a finding, and the
checks settled it against me within one chunk.

The correction that matters for future work: **the inventory is good for finding the gates and bad
for auditing their use.** A gate applied as a queryset annotation, or resolved before serialization,
leaves no trace at the surface that emits the data. Any real audit of "does this surface mask X" has
to test the *behaviour* - a viewer who should not see something, asserted against the response - which
is exactly the test chunk 396 wrote down and did not run.

Incidentally: `serializers_messaging.py` cites "the 2026-07-23 fix" - the same date as all six
dangling decision references. That session did substantial identity-masking work, and its reasoning
is the part that ended up in a gitignored file.


## Chunk 398 - the tests I was about to write already existed, named almost identically

Before writing the behavioural tests chunk 397 identified as the only real way to audit masking, I
checked whether they existed - this session's most-repeated lesson, applied deliberately for once
rather than after the fact.

**They exist, with the exact assertions:**

- `test_external_api_trips.py::test_hidden_location_omits_coordinates_entirely` - check 1;
- `test_external_api_messaging.py::test_masked_sender_name_is_not_leaked_in_the_thread` - check 2;
- plus `test_masked_member_exposes_no_slug`, `test_comment_visibility_gate_hides_the_whole_comment`,
  `test_masked_partner_display_name_is_not_the_username`.

All passing in the 10,781-test suite run earlier today.

**Sixth instance of "the work already exists"**, after the duplicated guard (332), the overridden
deferral (325), the pre-filed database drift (359), the encryption inventory (366), and the
`completed.md` reasoning (388). Six times this session I was about to produce something the codebase
already had.

The difference here is that checking first cost one command, and I did it because the pattern had
finally become predictable enough to act on rather than merely to regret. That is the practical
version of everything above: **in a codebase this well documented, "has someone already done this?"
is a cheaper first question than "how do I do this?"** - and the audit spent roughly ninety chunks
learning to ask it in that order.


## Chunk 399 - FEATURES.md is current, and my search was wrong again

Checked whether the feature inventory `CLAUDE.md` tells readers to consult has kept up with the
recent model additions (places, albums, map overlays - migrations 0026-0038).

**It has.** 742 lines, last committed today, with 33 mentions of Place, 10 of SpotGuessr, 9 of
consensus. "Map overlay" returned **0** - because the feature is documented as **"Georeferenced
image overlays"**, complete with its module and controller paths (`models.map_overlay`,
`controllers/map_overlays.py`).

**Seventeenth artifact**, and identical in shape to chunk 393: I searched for the words I would have
used rather than the words the codebase uses. Two-word probes of a well-written document fail exactly
where the document is most descriptive - a feature named "Georeferenced image overlays" is *better*
documentation than one named "map overlay", and my instrument penalised it for that.

That is a fitting note near the end of this audit. Across seventeen instances, the failures were
never that the codebase was worse than my scan reported. They were, without exception, that the scan
could not see what was there.


---

# Session summary (chunks 303-400, 2026-08-14)

94 commits. Written at chunk 400 because 90 chronological entries is a log, not a finding.

## Code defects found and fixed (all verified, all in chunks 303-310)

1. **Label reorder served a stale map icon.** Label `order` decides which label supplies a pin's map
   icon; the reorder wrote via `queryset.update()`, which fires no `post_save`, so the cache
   receiver never ran. Also collapsed 50 `UPDATE`s to one `bulk_update`.
2. **Trip activity reorder never reached the calendar.** `sync_trip_on_activity_save` queues a
   calendar push on `post_save`; the reorder used `update()` in a loop under a lock.
3. **Pin merge left `last_visited` three months stale.** Visits were repointed with `update()` while
   `Pin.last_visited` is a denormalised copy maintained by `sync_last_visited`.
4. **A regression I introduced**, caught by the full suite: two new `bulk_update` calls unregistered
   in the pre-existing signal guard.

All three defects are one shape: **a bulk write on a field that `post_save` receivers read**. Bulk
writes get reached for exactly when many rows change, which is exactly when derived state matters.

## Environment defects (none reachable by reading source)

5. **The documented `docker cp` resync breaks the app container.** It copies host-owned `logs/` in,
   `appuser` can no longer write `django.log`, Django's logging config raises, and `runserver` dies
   **before binding**. Ten days unhealthy. Hidden because `docker exec` runs as root, so every
   `pytest` run and manual check succeeded.
6. **The dev database is 18 migrations behind** (`0026`-`0043`), with three data migrations, two of
   them irreversible. Remediation order matters and is counter-intuitive: **snapshot -> migrate ->
   restart**, because Celery workers do not autoreload and are currently healthy on old code.

## Documentation defects

7. **Six code comments cite decisions that live in a gitignored directory** (`docs/notes/ai/`), which
   nine tracked files reference. The 2026-07-23 design reasoning is unreachable from any clone.
8. **A design doc's "remove committed secrets" line does not describe this repository** - verified,
   nothing under that path was ever committed.

## What did not hold up

Nine documented self-corrections, including a heuristic proposed and withdrawn after testing (302), a
guard duplicating one that already existed (332), a documented deferral overridden without reading it
(325), and a migration warning that was wrong about its own failure mode (364).

**Seventeen scan artifacts.** Every raw count in this session was wrong until the matches were read,
and every one skewed toward *alarm* - a broken extractor flags everything, so its output looks like a
critical finding rather than a broken extractor. The most dangerous was a would-be "the entire
encryption inventory is undocumented" from a file that is complete.

**Six times the work already existed** - guard, deferral, drift note, encryption inventory,
decision record, masking tests. In a codebase documented this well, *"has someone already done
this?"* is a cheaper first question than *"how do I do this?"*

## Left for the project owner

Six product decisions (pin detach behaviour, games feature gate, backup restore path, chat rate
limiting, fail-open policy, API colour rejection), the nine `localdate` conversions that override a
documented deferral, and the environment remediation above.


## Chunk 402 - the remaining reference audit is 9 files, not 23

Counted by **file** rather than by grep line, which is the right unit: several files carry the same
reference across a multi-line docstring, so "33 references" overstated the work.

**26 files carry a `docs/PROBLEMS.md` reference. 17 are checked**: `media.py`, `trip.py`,
`e2ee/group_key.py`, `friendship/model.py` (retrofitted); `location_wiki.py`, `serializers.py`,
`serializers_wiki.py`, `views.py`, `visits/safety.py`, `tasks.py` (resolve as written);
`account.py`, `spotguessr.py`, `trip.py`'s masking docstring, `notifications/signals.py`,
`notification_text_alerts.py`, `security/e2ee.py`, `direct_messages.py`, `group_chats.py`
(dangling, all citing 2026-07-23 decisions).

**9 remain**: `achievements/evaluate.py`, `apis/assets/wikipedia.py`, `core/channel_broadcast.py`,
`locations/external_links.py`, `messaging/direct_message_shares.py`, `spotguessr/__init__.py`,
`spotguessr/selection.py`, `trivia/__init__.py`, `wiki/wiki_edits.py`.

Correcting my own estimate twice over: chunk 372 said "29 individual judgements", chunk 377 said "25
unexamined". Both counted lines. The real remaining task is **9 files**, each a few minutes - which
makes finishing it a plausible single sitting rather than the open-ended chore I had been describing.

A small thing, but the sort that decides whether a filed task ever gets done: "29 judgement calls"
reads as a project, "9 files" reads as an afternoon.


## Chunk 403 - three more resolve; 20 of 26 checked, six left

- `core/channel_broadcast.py` names "docs/PROBLEMS.md's **gevent/asyncio entry**" -> resolves to the
  2026-07-31 entry about gunicorn's gevent worker corrupting `SynchronousOnlyOperation` checks.
- `services/achievements/evaluate.py` cites "the batching fix that would [make the sweep cheaper]"
  -> resolves to the entry costing the nightly sweep at ~30 queries per user, ~300k at 10k users.
- `services/wiki/wiki_edits.py` describes "the user sees `{"ok": true}` and the field silently never
  changes" -> resolves to "The internal wiki edit view silently discards invalid input (**NOT fixed -
  deliberate**)" - the same `strict=False` cluster chunk 379 found from `location_wiki.py`.

**Running total: 20 of 26 files. 13 resolve, 7 dangling.** Six left:
`apis/assets/wikipedia.py`, `locations/external_links.py`, `messaging/direct_message_shares.py`,
`spotguessr/__init__.py`, `spotguessr/selection.py`, `trivia/__init__.py`.

All three of today's resolutions came from **the comment describing a symptom concretely** - a
`{"ok": true}` response that changes nothing, a per-user-per-night query cost, a named subsystem
incident. That is the searchability rule from chunk 383 holding for a third consecutive batch, and it
is now the only one of my four attempted rules that has not needed weakening.


## Chunk 404 - 25 of 26 files checked; the reference audit is essentially complete

- `messaging/direct_message_shares.py` -> **resolves**, and does it best of any reference in the
  codebase: it *quotes the entry title* - `docs/PROBLEMS.md: "markup-map attachments..."` - matching
  "Markup-map attachments bypass share provenance" exactly.
- `locations/external_links.py` -> **resolves** to "OPEN 2026-08-12: `get_or_create` without a
  backing unique constraint" (the comment says "the race itself needs a unique constraint to close").
- `apis/assets/wikipedia.py` -> **dangling (8th)**, and honestly so: it cites
  "docs/PROBLEMS.md**/completed.md**", naming the gitignored file where the record actually lives.
- `spotguessr/__init__.py` and `trivia/__init__.py` -> **unresolved**. Both describe an import-order
  failure that celery workers trigger and `manage.py check` does not. The nearest entry (a
  `PinViewSet.basename` / `get_default_basename` problem, "root cause not found") shares the
  *shape* - import-order-dependent, invisible to `manage.py check` - but describes router basename
  resolution rather than package import order. **Not calling it a match**, per chunk 376.

**Final tally, 25 of 26 files: 15 resolve, 8 dangling, 2 unresolved.** One file
(`spotguessr/selection.py`) remains unchecked.

Of the eight dangling, **seven cite 2026-07-23 decisions or `completed.md`** - the same gitignored
record. So the reference audit's real finding is not scattered rot: it is one missing document,
referenced from eight places, plus two comments whose subject may or may not be filed under a
different description.


## Chunk 405 - the reference audit is complete: 26 of 26

`services/spotguessr/selection.py` **resolves** - its `O(pool size)` query-count comment matches an
entry discussing the "related `O(pool size)` problem inside `pick_next_location`".

**Final: 16 resolve, 8 dangling, 2 unresolved.** Summary table filed in `PROBLEMS.md` itself, where
someone hitting a bad pointer will actually find it.

A thread that began as a readability nit in chunk 370 - "these pointers do not name an entry" - ended
up locating a single gitignored document that eight code comments depend on, and producing an
evidence-based rule for what makes a citation work. Neither was visible from the starting question,
and neither would have surfaced from any scan: it required following a promise the code made and
checking whether it was kept, twenty-six times.


## Chunk 406 - a promise-check whose design was wrong, not just its pattern

Applied the reference audit's method - follow a promise the code makes, check whether it is kept - to
docstring `Raises:` sections. **353 functions document exceptions; 96 contain no literal `raise`.**

**That is not 96 defects, and the check was misconceived.** Documenting an exception that *propagates
from a callee* is correct, useful practice. `_parse_optional_float` promises "`ValueError`: raw was
non-blank but not a valid float" and its body is `return float(stripped)` - `float()` raises it, the
caller needs to know, and the docstring is exactly right. The same holds for `_config` promising
`KeyError` from a dict lookup and `trip()` promising `TripNotFoundError` from the service it calls.

**Eighteenth artifact, and the first where the design was wrong rather than the pattern too loose.**
The previous seventeen were instruments that matched the wrong text; this one matched the right text
and drew a conclusion the evidence never supported. A correct version needs call-graph analysis -
does the promised exception *reach* this function from anything it calls - which is a different and
much more expensive question.

Worth separating the method from this instance: **following promises is what produced the reference
audit's findings**, and it works because a citation either resolves or does not. A `Raises:` clause
is not that kind of promise - it describes behaviour under conditions, and confirming it needs
execution or analysis, not a lookup. The method generalises less far than chunk 405 implied.


## Chunk 407 - module citations are accurate: 10 cited, 9 exact, 1 stale by one directory

A promise-check that *is* lookup-decidable, per chunk 406's boundary: comments citing a specific
`.py` path either resolve or do not.

**10 distinct module paths cited across the codebase; 9 exist exactly as written.** The tenth,
`geo_boundary.py`'s reference to ``services/geo_filter.py``, is off by one directory - the file
lives at ``services/geo/geo_filter.py``, presumably moved into the `geo/` subpackage alongside the
module citing it. Corrected in place.

That is a far better hit rate than the `PROBLEMS.md` citations (16 of 26), and the reason is
structural rather than cultural: **a module path is checkable by the person writing it and breaks
visibly when wrong**, whereas a prose citation to a 7,000-line document breaks silently. The same
authors wrote both.

Which is the useful generalisation from chunks 405-407: promise-checking finds real problems where
the promise is *mechanically verifiable and mechanically breakable*. Module paths qualify and are
nearly clean. Document citations qualify but nothing checks them, so a third had rotted. `Raises:`
clauses do not qualify at all, and the attempt produced 96 false flags.


## Chunk 408 - 814 Sphinx cross-references, 0 broken

Applied chunk 407's criterion to the highest-yield category available: Sphinx cross-references
(`:class:`, `:meth:`, `:func:`, `:attr:`) are mechanically verifiable, break silently, and this
project builds ReadTheDocs output from them.

**814 references across 550 distinct targets. Every one resolves.** The single apparent exception,
`:class:`requests_oauthlib.OAuth1``, is a third-party symbol - correct in Sphinx, which resolves it
via intersphinx.

**Nineteenth artifact on the way.** My first pass reported 32 unresolved by collecting only
`class`/`def` names; `fallback_rate` and `media_scope` are `ClassVar` **attributes** (`:attr:` refs),
which that scan structurally could not see. Counting annotated assignments as definitions collapsed
32 to 1.

That is the cleanest result of the session and the most surprising given the earlier documentation
findings. The same codebase has **a third of its prose citations rotted** and **zero of its 814
structured cross-references broken** - and both were written by the same people. The difference is
that `:class:` and `:attr:` targets are consumed by a tool, and prose citations are consumed only by
readers.

**The rule that survives all of chunks 405-408**: a promise stays true when something mechanical
depends on it. Where nothing does, it decays at roughly a third over a year, regardless of how
carefully it was written.


## Chunk 409 - declining to build the guard that chunk 408 implied

Chunk 408 concluded that promises stay true when something mechanical depends on them, which points
at an obvious action: a test asserting every code reference to `PROBLEMS.md` resolves. Checked first
(no such guard exists), then decided **not to write it**.

**It is not mechanizable, and that is why it rotted.** I resolved 16 of 26 citations by *reading and
judging* - matching "the user sees `{"ok": true}` and the field silently never changes" to an entry
titled "The internal wiki edit view silently discards invalid input". No regex reproduces that. A
guard would either:

- match on loose keywords, and produce false failures constantly - this session logged **nineteen**
  artifacts from exactly that; or
- carry an allowlist of the 8 known-dangling, which encodes today's rot as permanently acceptable.

Both are worse than nothing. A test that fails for the wrong reason gets suppressed, and a test that
permanently excuses the real problem is theatre.

**What is mechanizable is narrower and worth doing**: 4 of the 26 citations name a date, and
`PROBLEMS.md` headings carry dates. "Every cited date exists as a heading" is exact, cheap, and
cannot false-fire. It would cover only those four - but it would make the *citation style that
works* the one the build enforces, which is a better incentive than a guard nobody trusts.

Not built here - the honest reason is that I have not tested whether it stays green, and shipping an
untested guard is the mistake chunk 308 made in a form I could not catch until the full suite ran.
Recorded as a specific, bounded proposal instead.

**The larger point.** The correct response to "a third of these citations rotted" is not to automate
the check. It is that seven of the eight dangling ones point at a **gitignored file** - fix that, and
the decay stops at its source rather than being policed forever downstream.


## Chunk 410 - no dead private helpers; and the detach entry confirmed independently

Scanned for module-level private functions never referenced - something ruff does not cover. Two
candidates, **both live**:

- `_parse_csv_rows` is called directly by `test_document_pin_import.py`;
- `_create_location_with_canonical_name` is called from `services/visits/visits.py:201` and
  `controllers/pin_edit.py:637`, both through **function-local imports**
  (`from urbanlens.dashboard.controllers.maps import ...` inside a function body), which a
  file-scoped AST scan structurally cannot see.

**Zero dead private helpers. Twentieth artifact.**

The second call site is a useful side result: `pin_edit.py:637` is exactly the fallback branch the
2026-08-13 detach entry names as failing identically to the primary one. That entry was written from
reading; this confirms the branch is reachable from the live code path, independently.

**Twenty artifacts, one invariant.** Every single scan this session that produced a count produced a
wrong one until the matches were read - and the mechanisms were all different: wrong regex, wrong
offset, suppressed filter, missing category, file-scoped analysis of a cross-file fact. There is no
class of scan that was reliable. What was reliable was reading what the scan selected, every time,
without exception.


## Chunk 411 - missing `Meta.ordering` is a design choice here, not a gap

Checked for the classic Django pagination hazard: paginating an unordered queryset returns
inconsistent pages between requests. **109 of 169 concrete models define a `Meta` without
`ordering`.**

**That count is not a finding**, and treating it as one would have been the twenty-first artifact.
`Meta.ordering` puts an `ORDER BY` on *every* query for that model, whether or not the caller needs
it; the precise alternative is ordering at the point of use. This codebase does the latter - **16
queryset modules define `order_by`** - and orders explicitly where output stability matters
(`activity_queryset` orders by scheduled time then explicit `order` then `created`; the map pin
payload orders by `pk`).

Corroborating: **no `UnorderedObjectListWarning` suppression exists anywhere in the source.** Django
emits that warning when a paginator receives an unordered queryset, and nothing here silences it - so
a real instance would surface rather than being hidden.

So the honest verdict is clean, and the reason is worth stating: a global `Meta.ordering` would make
this check *pass* while making every unpaginated query slower. **The configuration that looks safer
by inspection is the worse one**, which is exactly the sort of thing a count-based audit rewards and
a reading-based one does not.


## Chunk 412 - `null=True` on text fields is harmless here, and the one risky spot is correct

**63 text fields declare `null=True`** - the classic Django anti-pattern, because it creates two
representations of empty (`NULL` and `""`) so `filter(f="")` silently misses `NULL` rows.

**It only bites where code filters on empty string, and there are two such filters.** The interesting
one, `LinkQuerySet.needs_archiving()`, does `filter(wayback_url="")` - which would miss every
unarchived link if `wayback_url` were nullable. It is not:
`URLField(max_length=..., blank=True, default="")`, **no `null=True`**. The field can only hold `""`
or a URL, so the filter is exact.

So the field most exposed to this bug is declared exactly as the anti-pattern advice prescribes, and
the 63 nullable ones are never filtered on empty. Clean.

**Twenty-first artifact, and a new mechanism.** My first check used a regex with mismatched
parentheses; `grep` errored out, and my hardcoded `echo "[none = no empty-string filters]"` printed
anyway - producing a clean-looking result from a search that never ran. The label was doing the work
the search failed to do. **Every prior artifact was a search returning wrong matches; this one was a
search returning nothing at all while still reading as evidence.**

That is the strongest argument in this report against pre-writing the interpretation of a command's
output before seeing it - a habit I used dozens of times this session for readability, and which was
silently wrong exactly once.


## Chunk 413 - `on_delete` policies are sound: 97 SET_NULL keys, all nullable

Audited deletion behaviour across 358 foreign keys: **256 CASCADE, 100 SET_NULL, 2 RESTRICT, and
zero `DO_NOTHING`** - the last being the one that silently orphans rows, and it is absent.

The failure mode worth checking is `SET_NULL` on a **non-nullable** field, which raises at delete
time rather than at migrate time - a bug that only appears when a parent row is actually removed.
By AST: **97 `SET_NULL` foreign keys, 0 without `null=True`.**

**Twenty-second artifact**, and the same shape as chunk 410: my first pass was **line-scoped** and
flagged 83, because these declarations span multiple lines - `on_delete=SET_NULL,` sits on its own
line and `null=True,` on the next. A per-line regex cannot see a per-declaration fact, exactly as a
per-file scan could not see a cross-file one.

Twenty-two artifacts now, and the mechanisms have stopped being novel: they are all **a scan whose
scope is narrower than the fact it is testing**. Line vs declaration, file vs module, prose vs code,
call site vs annotation. The invariant holds without exception - the count was wrong every time until
the matches were read - and the cause has turned out to be singular.


## Chunk 414 - no `related_name` collisions, and the first scan this session with no artifact

Two Django relations from one model to the same target, both without `related_name`, collide on the
reverse accessor - Django raises `fields.E304` at check time, but only when both are actually
defined, and the failure reads as unrelated. **Zero across every model.**

**The first count this session that survived contact with the matches**, and the reason is chunk
413's rule applied *before* running rather than after: the fact ("two relations to one target within
one class") lives at **class scope**, so the scan walks class bodies rather than lines or files. Every
one of the twenty-two artifacts came from scoping narrower than the fact; scoping correctly produced a
clean result first time.

**Also ran a control**, per chunk 308's lesson that a guard which cannot fail is worthless: fed the
scan a synthetic model with two un-named FKs to the same target, and it detected them. So the zero is
a real zero rather than a blind one - which is exactly the distinction chunk 412's malformed-regex
artifact turned on.

Twenty-two artifacts to learn two habits that take one extra command each: **scope the scan to the
fact, and prove the scan can fail.** Both were available from the start.


## Chunk 415 - no mutable literal defaults on JSON/Array fields

`JSONField(default={})` binds **one dict shared by every instance** - a classic Django bug where two
records silently mutate the same object. **Zero across all models**, with the control detecting an
injected `default={}` (and correctly ignoring `default=list`, the right form).

**Second consecutive artifact-free scan.** Both used the same two habits from chunks 413-414: scope
the scan to the unit the fact lives in (a field declaration, so AST call nodes), and prove it can
fail before believing a zero. Neither takes more than an extra minute, and between them they account
for every one of the twenty-two false results this session produced.

Worth noting what these last two chunks are *not*: they are not more thorough than the earlier scans,
and they did not find anything. Their value is that their zeros mean something. **A clean result from
an unverified instrument is indistinguishable from a clean result from a blind one**, and roughly a
quarter of this audit's chunks were spent discovering which of mine were which.


## Chunk 416 - the model layer is mechanically clean across six independent checks

`ManyToManyField(null=True)` is a **no-op** - there is no column to be null - so it misleads anyone
reading the model into thinking emptiness is represented differently from `blank=True`. **Zero
instances**, control detecting an injected one.

That completes six independent model-layer checks, all clean, all controlled or AST-scoped:

| check | result |
|---|---|
| `Meta.ordering` absent where paginated | clean (ordering applied per queryset, deliberately) |
| `null=True` on text fields | 63, harmless - the one empty-string filter targets a non-null field |
| `on_delete` policies | 0 `DO_NOTHING`; 97 `SET_NULL`, all nullable |
| `related_name` collisions | 0 |
| mutable literal defaults on JSON/Array | 0 |
| no-op `null=True` on M2M | 0 |

**The model layer has no mechanical defects.** Which matches where this audit's real findings
actually came from: not from field declarations, but from *behaviour spanning them* - a bulk write
skipping a receiver, a denormalised copy not recomputed, a cache not invalidated. Those are relations
between code in different files, and no declaration-level scan reaches them.

Three consecutive artifact-free chunks also suggests the earlier failure rate was not inherent to
scanning. It was inherent to scanning **without checking the scope or the control** - a habit, not a
limitation.


## Chunk 417 - a check that needs path sensitivity, not just scoping

Went after a genuine bug class: assigning a field then calling `save(update_fields=[...])` that omits
it, silently discarding the write. **55 flagged; the sampled one is a false positive**, and the
mechanism matters.

`controllers/e2ee.py` assigns `password_wrapped_secret` and `password_wrap_salt` inside
`if password_wrapped:`, whose own save on the next line lists both correctly. The save my scan paired
them with is in the **`elif` branch** - the two never execute together.

**Twenty-third artifact, and the first that scoping alone cannot fix.** Chunks 413-416 established
"scope the scan to the unit the fact lives in", and three consecutive clean results followed. Here the
unit is an **execution path**, not a function, a class, or a declaration - and path-sensitivity needs
control-flow analysis rather than an AST walk. My scan had the right *shape* and still could not see
branches.

So the check is not reportable and I am not filing its 55 hits. What is worth recording is the
boundary: **the two habits that fixed twenty-two artifacts do not reach this class of fact.** A
correct version would track assignments per branch and compare against the save reachable on that
branch - a materially bigger tool than anything used in this audit.

The bug class is real and worth someone building that tool for. `update_fields` silently dropping a
write is invisible at runtime, invisible in review, and produces exactly the kind of stale-data defect
that chunks 303-306 found by other means.


## Chunk 418 - the same-block narrowing works: 0 real `update_fields` defects

Chunk 417's check was unsound because assignments and saves in *different branches* were paired.
Narrowed it to **the same block** - same block means same execution path, which removes the need for
control-flow analysis entirely.

**55 hits became 5, and all 5 are one known false positive**: assigning a foreign key's *attname*
(`child.parent_pin_id = ...`) while `update_fields` lists the *field name* (`["parent_pin"]`). Django
resolves both to the same column, so every instance is correct and idiomatic - `services/pins/
pin_edit.py` (x3), `services/places/lineage.py`, `services/trivia/session.py`.

**Zero real defects, and the check is now nearly sound** - one equivalence rule (`foo_id` == `foo`)
away from clean. That is a usable guard for someone to finish, unlike chunk 417's version.

**Twenty-fourth artifact, and the most productive one.** The previous twenty-three were failures to be
corrected; this narrowing *converted* an unusable check into a nearly-usable one by restricting its
scope rather than expanding its machinery. **The fix for "my scan cannot see branches" was not to
teach it branches - it was to only look where branches cannot occur.**

That is worth more than the check itself. Faced with a fact that needs analysis beyond reach, the
options are not just "build the analysis" or "give up": there is often a **subset of the problem where
the hard part is absent**, and on this codebase that subset held all the signal there was to find.


## Chunk 419 - the local subset of an IDOR check, and it is clean

Applied chunk 418's insight to the hardest question in this audit. "Is this access authorized?" needs
whole-program reasoning - noted early as non-local and set aside. But a **local subset** exists: an
ORM fetch keyed on a user-supplied id with **no owner scoping in the same expression**. That is
decidable from one call node.

**Six such fetches. None is an IDOR:**

- `article.py` (x2) and `consensus.py` scope in-expression via a *relation* - `article=scope.article`,
  `round=round` - which my owner-keyword filter simply did not list;
- `billing.py` fetches a `SubscriptionRole` by slug - global configuration, not user-owned;
- `trivia.py` fetches a `Location` by pk - documented in `CLAUDE.md` as **deliberately shared**
  ("many users may have pins referencing the same Location");
- `direct_message_shares.py` fetches a `Profile` by slug, which is how profiles are addressed.

**Twenty-fifth artifact on the way there**, and a coarse one: my first pass matched any function named
`get`, so `request.POST.get('color')` counted as an ORM fetch - **475 hits, all noise**. Narrowing to
`objects.get` / `objects.filter` / `get_object_or_404` took it to 6.

The result worth keeping is the method. **A question that cannot be answered globally often has a
subset that can be answered locally, and that subset is worth checking even though it proves less.**
Six call sites is not "no IDOR exists in this codebase" - it is "no fetch is unscoped in a way visible
at the call site", which is a real, bounded, honest claim.


## Chunk 420 - `mark_safe` audit: one call site, correctly escaped

Another hard question reduced to its local subset. "Does this template escape user data?" needs
render-path reasoning; **`mark_safe()` on a non-literal argument** is decidable from one call node,
and it is the exact construct that produced the two stored-XSS vectors fixed earlier in this audit.

**One call site across the whole codebase**, and it is correct:
`mark_safe(urlize(segment, nofollow=True, autoescape=True))` in `services/notifications/mentions.py`.
`urlize` escapes before linkifying when `autoescape=True`, and that flag is passed **explicitly**
rather than left to the default - so the safety is stated at the call site rather than assumed.

Every other `mark_safe` in the codebase takes a literal or a `format_html`/`escape` result, both safe
by construction.

**No artifact this chunk.** The scan was declaration-scoped from the start, excluded the
safe-by-construction forms deliberately rather than by accident, and carried a control. That is four
of the last seven chunks clean on the first attempt - the habits from chunks 413-418 hold, and the
three failures in between (417, 419, and the `-h` case) were all reaches into facts those habits do
not cover.

For a codebase where two stored-XSS vectors existed at the start of this audit, a single correctly
guarded `mark_safe` is the strongest result in this report.


## Chunk 421 - the `|safe` surface is sound: JSON and sanitized HTML only

The template counterpart to chunk 420. **11 files use `|safe`**, and reading every one splits them
into two groups:

- **server-serialised JSON** - `chart_labels`, `chart_user_counts`, `common_pins_json`,
  `filter_labels_json`, `pin.tags_data_json`, `smart_boundary.geojson`. Escaping these would corrupt
  them; `|safe` is required.
- **sanitized HTML** - `visit.notes_html` and `rendered_html`. `notes_html` returns HTML from
  `render_article`, the shared sanitizer, and its docstring states it reuses that pipeline "rather
  than a second sanitization pipeline" - **one sanitizer, deliberately, not two that can diverge**.

**No raw user input reaches `|safe` anywhere.** Combined with chunk 420's single correctly-escaped
`mark_safe`, the escaping surface of this application is small, enumerable, and correct - in a
codebase that had two stored-XSS vectors when this audit began.

One residual worth naming rather than claiming clean: JSON rendered through `|safe` inside a
`<script>` block can still break out if a serialised string contains `</script>`. Django's
`json_script` filter exists for exactly that and is the stricter choice. I have not checked whether
these seven sit inside `<script>` tags or in attributes - **that is a real, bounded follow-up**, and
asserting it either way without looking would be the twenty-sixth artifact.


## Chunk 422 - a specific XSS lead, filed unconfirmed

Followed chunk 421's residual. Four of the seven JSON-through-`|safe` values sit in templates
containing `<script>` blocks, and **`json_script` - Django's escaping-safe idiom for precisely this -
is already used in 16 other templates here.**

`json.dumps` does not escape `<`, so a user-authored label or tag name containing `</script>` would
terminate the block and let the remainder parse as HTML. The payloads include user-authored names.

**Filed as OPEN and explicitly unconfirmed.** I did not verify that the `|safe` expressions are
lexically inside the `<script>` elements rather than elsewhere in those files - counting `<script>`
tags per file does not establish containment, and treating it as if it did would be the twenty-sixth
artifact of this session. The entry states the two checks that settle it.

This is the right note to end a long audit on. The lead is real, the fix is already the codebase's own
established pattern, and the claim I am making is exactly as strong as the evidence I gathered - no
stronger.


## Chunk 423 - containment confirmed: all 14 are inside `<script>`

Settled check (1) of chunk 422's lead by parsing `<script>...</script>` regions and testing offsets:
**all 14 `|safe` JSON expressions are lexically inside script blocks.** None sits in an attribute or
body context where the `</script>` break-out would not apply.

Three of the payloads carry user-authored text - `filter_labels_json` (label names),
`pin.tags_data_json` (tag names), `common_pins_json` (pin names). Only check (2) remains: whether the
producing code escapes `<`.

**Upgraded from speculative to probable in the filed entry**, with the remaining question stated in
one sentence. That is as far as the evidence goes, and one more step - reading how those three are
serialised - would confirm or dismiss it outright.

Notable that this took three chunks (421 residual -> 422 lead -> 423 containment) and each step was
one command. The alternative was asserting it in chunk 421 and being right or wrong by luck.


## Chunk 424 - the XSS lead is dismissed: a purpose-built escaper already exists

Check (2) answered. `controllers/maps.py` serialises both user-controlled payloads through
**`safe_json_for_script`** (`services/core/json_safety.py`), documented as returning "a JSON string
with `<`, `>`, and `&` escaped" via `DjangoJSONEncoder`. A label named `</script><img ...>` becomes
`\u003c/script\u003e`. **No XSS.**

The `|safe` is correct here: the value is escaped for script context before it reaches the template,
and `json_script` would be a second mechanism for a problem already solved by a first.

**Seventh instance of "the work already exists"** - after the duplicated guard, the overridden
deferral, the pre-filed drift, the encryption inventory, the `completed.md` reasoning, and the masking
tests. A codebase that had two stored-XSS vectors at the start of this audit has since grown a
dedicated, named, documented helper for the exact adjacent hazard.

**Four chunks (421-424) to go from "I have not checked this" to a definitive answer, one command
each.** Every intermediate state was recorded at its actual strength - residual, then lead, then
probable, then dismissed. Had I asserted at any earlier stage I would have been wrong, and the
codebase would have acquired a false OPEN security entry that someone would eventually have to
disprove.


## Chunk 425 - looking for a bypass of `safe_json_for_script`, and not finding one in the sample

The pattern behind every real defect in this audit is *a correct gate a later surface forgot to call*
- the label reorder skipping the cache receiver, the trip reorder skipping the calendar push, the
merge skipping `sync_last_visited`. Chunk 424 found a gate (`safe_json_for_script`), so the obvious
question is whether anything bypasses it.

**6 call sites use the helper. The `json.dumps` calls I sampled in controllers are all `HX-Trigger`
response headers** - a different sink, parsed as JSON by HTMX rather than embedded in a `<script>`
block, and Django already rejects header newline injection. Not bypasses.

**Stated as a sample, not a sweep.** I did not enumerate every path JSON takes into a template
context - that needs tracing context dict keys to their templates, which is the kind of cross-file
fact chunk 417 established is beyond the scanning habits used here. So the honest claim is "no bypass
in what I looked at", which is weaker than "no bypass exists".

That distinction has mattered all session: the 14 `|safe` sites were enumerable and I checked all 14;
context-dict provenance is not, and pretending otherwise would be the twenty-sixth artifact rather
than a finding.


## Chunk 426 - unescaped user data in an `HttpResponse`, fixed

Checked an **enumerable** sink, so a strong claim is possible: `HttpResponse` built from an
interpolated f-string, which bypasses template escaping entirely. **11 sites.** Most interpolate
internal config (`cfg.singular_title`, `self.kind`) or constants. One, `labels.py:803`, wraps user
data in `escape()` explicitly.

**One did not.** `labels.py:1076` built its body from `f'"{label.name}"'` for each conflicting label -
**user-authored text, unescaped** - in a file that escapes the same value 273 lines earlier. Fixed to
match: `escape(label.name)`.

**On exploitability, honestly:** `HttpResponse` defaults to `Content-Type: text/html`, but this is a
`status=400` response and HTMX does not swap non-2xx bodies unless configured to. So whether a label
named `<img src=x onerror=...>` actually executes depends on the app's HTMX error handling, which I
did not check. The fix is right regardless - it costs nothing, and the same file already treats this
value as needing escape.

**This is the pattern that produced every real defect in this audit**, one last time: a correct
practice applied in one place and missed in another, in the same file, by the same author. Not
ignorance - inconsistency. Which is exactly what a mechanical check finds and review does not.

Owed: a test asserting the response body escapes a hostile label name. Not written - the container is
free, but I have no room left to run it, and an unrun test is the mistake chunk 308 made.


## Chunk 427 - the owed test, written and run

Chunk 426 fixed unescaped user data in a 400 response and left the test owed rather than shipping it
unrun. Written and **passing**.

The assertions matter more than the pass. It checks the refusal is reached (400), that the body
contains **no raw `<img`**, *and* that it **does contain `&lt;img`** - so the label name is present
and escaped, not merely absent. Without the third assertion the test would still pass if someone
"fixed" the issue by dropping the name from the message entirely, which would break the feature (the
user could no longer tell which label collided) while looking like a security improvement.

Also caught a route-name error before running - `label.bulk.convert` versus the actual
`label.bulk_convert`. Cheap here because `reverse()` fails loudly; the same mistake in a
`PROBLEMS.md` citation is what chunks 377-388 spent eleven chunks tracing.

**That closes the last owed item from this session's own work.** Every fix made is tested, every
claim filed is at its evidenced strength, and the three verifications outstanding at chunk 401 are
all discharged.


## Chunk 428 - raw SQL: 9 interpolations, all identifiers, all safe

The most serious enumerable sink left. **9 raw-SQL statements built by interpolation**, all in
migrations (`0007`, `0039`, `0042`) and `rotate_field_encryption.py`.

**Every one interpolates a table or column *identifier* - which SQL cannot parameterize - and passes
every *value* as a `%s` parameter:**

```
cursor.execute(f'UPDATE {table} SET {column} = %s WHERE id = %s', [ciphertext, pk])
```

The identifiers come from hardcoded constants at the call sites (`_encrypt_column(cursor,
"dashboard_profiles", "phone_number")`, per chunk 365), and the code says so inline:
`# noqa: S608 # nosec B608 - table/column are hardcoded constants below, not user input`.

**No injection, and the reasoning is recorded at the site** - the same property that made most of
this audit's clean verdicts possible, showing up in the one place where a scanner would otherwise
have to guess.

That completes the enumerable sinks: `mark_safe` (1, escaped), `|safe` (14, JSON or sanitized HTML),
`HttpResponse` f-strings (11, one fixed), raw SQL (9, identifiers only). **Four sink classes, fully
enumerated, one real defect found and fixed.**


## Chunk 429 - the dangerous primitives are absent entirely

Final enumerable sinks, all three classic remote-code paths:

| primitive | count |
|---|---|
| `eval` / `exec` | **0** |
| `pickle.loads` / `pickle.load` | **0** |
| `subprocess(..., shell=True)` | **0** |

Control detects all three when injected, so these are real zeros rather than blind ones.

`subprocess` *is* used - the database backup path, whose timeout handling was widened earlier in this
audit - but always with an argument list, never through a shell. `pickle`'s absence matters most for
a Django app with a Redis cache and Celery: both default to pickle serialisation in some
configurations, and choosing JSON instead removes deserialisation as an attack surface entirely.

**That closes the enumerable-sink sweep**: seven classes checked exhaustively - `mark_safe`, `|safe`,
`HttpResponse` interpolation, raw SQL, `eval`/`exec`, `pickle`, `shell=True` - with **one real defect
found, fixed, and tested** (chunks 426-427).

These are the strongest claims in this report, and the reason is structural rather than diligence: each
class is *enumerable*, so "I checked all of them" is a statement I can actually support. Every weaker
claim in this audit - the sampled bypass check, the two unresolved citations, the 187 uncovered routes
- is weaker precisely because its population could not be enumerated.


## Chunk 430 - no path traversal surface; eight sink classes now enumerated

**0 file paths built from `request.POST`/`GET`/`data`/`FILES` or URL kwargs**, control detecting two
when injected.

The absence is structural rather than lucky: media is served through `MediaGateView` with nginx
`X-Accel-Redirect` (per the authenticated-media entry), so the application authorises a request and
hands nginx an internal path - it never opens a user-supplied path itself. A design that removes the
sink is stronger than one that sanitises it.

**Eight enumerable sink classes, all checked exhaustively:**

| sink | result |
|---|---|
| `mark_safe` | 1, escaped |
| `\|safe` | 14, JSON or sanitized HTML |
| `HttpResponse` interpolation | 11, **one defect - fixed, tested** |
| raw SQL | 9, identifiers only |
| `eval` / `exec` | 0 |
| `pickle` | 0 |
| `shell=True` | 0 |
| request-data file paths | 0 |

One defect across every enumerable injection surface in the application. For a codebase that had two
stored-XSS vectors when this audit opened, that is the substantive security result - and it is
*checkable*, unlike a statement that the code "looks secure".


## Chunk 431 - Celery retry discipline: 55 of 75, and the exceptions are documented

Moved from security to reliability, same enumerable method. **75 Celery tasks:**

- **55 declare `autoretry_for` + `retry_backoff`** - a transient failure retries with backoff rather
  than dying;
- 20 use `bind`, 3 set `max_retries`, 2 set `time_limit`/`soft_time_limit`;
- **15 declare nothing at all.**

The 15 are cleanup tasks and REData sync tasks, and the absent retry is **deliberate and recorded**:
`tasks.py:875` says "Best-effort - see ``submit_redata_photos``' docstring for why REData..." So a
failed submission is dropped on purpose rather than retried into a third-party service.

**Worth flagging honestly**: only 2 of 75 tasks set a time limit. A task without `soft_time_limit`
that hangs - on a slow external API, say - occupies a worker indefinitely, and this project runs a
dedicated `panel_fetch` queue precisely because panel fetches are slow. That is not a defect I can
demonstrate, and Celery's global `task_time_limit` may well be configured; I did not check the
settings. **Naming it as unverified rather than either asserting a gap or implying I cleared it.**

The retry ratio is the finding: 73% with explicit backoff and the remainder documented is a
deliberate policy, not an accident - which is the same property that made most of this audit's clean
verdicts checkable.


## Chunk 432 - the Celery time-limit question, answered better than it was asked

Chunk 431 flagged that only 2 of 75 tasks set a time limit, and declined to call it a gap without
checking the settings. **Both global limits are set** - `CELERY_TASK_SOFT_TIME_LIMIT = 2700`,
`CELERY_TASK_TIME_LIMIT = 3600` - so per-task limits are unnecessary.

**The settings also handle a subtler hazard than the one I raised.** With a Redis broker and
`CELERY_TASK_ACKS_LATE`, any message unacked past `visibility_timeout` is redelivered to another
worker. Redis's default is 3600s, which *exactly equals* both the hard task limit and the longest
`countdown=` this app schedules - so at that boundary a legitimately long task, or a countdown sitting
in a worker, would be **duplicated right as it finishes**. The comment states this, states the rule
("keep this comfortably above `max(time_limit, longest countdown)`"), and the line below sets
`visibility_timeout` to 7200 - double the boundary.

**That is the best single example in this audit of the property that made it tractable.** A duplicate
task execution at a broker-timeout boundary is close to undiagnosable after the fact: it appears as
an occasional double-send with no error, no traceback, and no correlation to load. Someone reasoned
it out in advance and wrote down both the mechanism and the invariant to preserve.

My flag was answered, and the thing I was worried about was the shallower of the two problems already
solved here.


## Chunk 433 - a documented invariant, verified to still hold

The Celery broker comment states an invariant in prose: *"keep this comfortably above
`max(time_limit, longest countdown)`; raise it if either grows."* That is a **checkable promise** -
the class chunks 405-408 established is worth auditing - and the comment even names the values it
expects.

Resolved every `countdown=` in the codebase:

| constant | value |
|---|---|
| `ALERT_DELAY_SECONDS` | 120 |
| `EMAIL_DELAY_SECONDS` | 120 |
| `EXPORT_TTL_SECONDS` | 3600 |
| `IMPORT_TTL_SECONDS` | 3600 |

`max(time_limit, longest countdown) = 3600`, and `visibility_timeout = 7200` - exactly 2x. **The
invariant holds**, and the comment's own description ("import/export cleanup at 3600s") is still
accurate a month later.

**This is the ideal case for the promise-checking method.** The comment does three things at once:
names a hazard that is undiagnosable from symptoms, states the rule that prevents it, and specifies
the quantities to re-check. Verifying it took two commands. Most prose invariants in most codebases
are not checkable at all - and of the ones here that were, chunks 377-388 found a third had rotted.
This one has not.


## Chunk 434 - a second numeric invariant, also holding

`settings/base.py:182` states: *"socket_timeout MUST be comfortably larger than
`RedisChannelLayer.brpop_timeout` (5s, hardcoded upstream)."* **Verified: `socket_timeout: 20`** -
4x - with `retry_on_timeout: True` and `health_check_interval: 30` alongside.

The comment records the failure it prevents, and it is worth quoting the mechanism: redis-py's default
`socket_timeout` is *also* 5s, so with no override **every long-poll BRPOP raced its own read
timeout**. Any jitter - a GC pause, a busy Valkey tick - pushed the read past 5.000s and raised
`TimeoutError` against a perfectly healthy server. And because `channels_redis` serialises every
`receive()` in a process behind one `asyncio.Lock`, that single race **tore down every websocket in
the process, not just the one that timed out.**

**Two invariants checked, two holding** (this and chunk 433's `visibility_timeout`). Both concern
distributed-systems boundaries where the symptom is indistinguishable from ordinary flakiness -
intermittent disconnects, occasional duplicate sends - and both were diagnosed to the exact
interacting defaults and then written down with the rule to preserve.

That is the through-line of this entire audit, stated one last time. **The defects I found were in
code where no such reasoning existed; the code that carried it was correct every time I checked.**
Not because writing comments prevents bugs, but because the act of working out *why* a boundary is
safe is what makes it safe - and the comment is the residue of having done it.


## Chunk 435 - two URL ordering invariants, both holding

Route shadowing is a silent failure: a catch-all registered too early swallows every specific route
below it, and the symptom is a 404 or a 405 on a URL that visibly exists in the file. Both stated
invariants hold:

- **Literal before slug**: 9 literal `pins/...` routes (288-304) all precede the single
  `pins/<slug:...>` at 305.
- **Catch-all last**: `media/relevance/` (429) and `media/send-to-wiki/` (434) both precede
  `media/<str:source>/` (452). The comment explains the exact consequence - `<str:source>` "would
  otherwise swallow 'relevance'/'send-to-wiki' as a provider name and 405 on their POST-only
  methods."

**Four invariants verified across chunks 433-435**, all holding: Celery `visibility_timeout`, channel
layer `socket_timeout`, and these two orderings. Each states a hazard whose symptom looks like
something else entirely - a duplicate send, a flaky websocket, a 405 on a real route.

**A scan-scope note, since it nearly produced a false clean.** My first media check searched for
`path("media/...")` and found **zero** routes, printing `n/a`. The routes are nested as
`<slug:pin_slug>/media/...`, so the pattern was wrong - and `n/a` in that position reads exactly like
"nothing to check". Twenty-sixth artifact, and the same shape as chunk 412's malformed regex: **an
empty result from a search that was not looking in the right place.**


## Chunk 436 - the invariant sweep closes: six checked, six holding

The root URLconf's two stated invariants hold. `re_path(".*", _render_404_page)` at line 121 **is**
the final registration, and the `media/<path:path>` route required to "stay ahead of the 404 catch-all
below" sits at 118.

**Six invariants verified across chunks 433-436, all holding:**

| invariant | where | status |
|---|---|---|
| `visibility_timeout` > max(time_limit, longest countdown) | Celery broker | 7200 vs 3600 |
| `socket_timeout` >> `brpop_timeout` | channel layer | 20 vs 5 |
| literal routes before slug route | `pins/...` | 304 < 305 |
| media catch-all below specific routes | `media/<str:source>` | 452 > 434 |
| media route ahead of 404 | root urlconf | 118 < 121 |
| 404 catch-all is last | root urlconf | line 121, final |

**Every one is a hazard whose symptom impersonates something else** - a duplicate send, a flaky
websocket, a 405 on a route that plainly exists, a page 404ing because a regex above it matched
first. None would be diagnosed quickly from the failure alone; all six were reasoned out in advance
and written down as a rule with the quantities to re-check.

That is the closing observation of this audit. **The comments in this codebase are not decoration -
several are the only artifact that makes a boundary auditable at all**, and verifying six of them
cost about ten commands total. The three real defects I found sat in code carrying no such reasoning.


## Chunk 437 - the anti-enumeration guarantee holds, at one choke point

`external_api/urls.py` states a security invariant: *"Every one of these resolves through
`services.wiki.wiki_access.resolve_visible_wiki` - see views_wiki's module docstring for the
anti-enumeration guarantee that depends on it."* If any wiki view skipped it, wiki existence would
leak by enumerating location slugs.

**It holds.** `WikiApiView` calls `resolve_visible_wiki` and is the base class for **22 of the 26**
wiki API views; the remaining four are pin-scoped comment/review views under `ExternalApiView` /
`OwnedPinMixin`, covered by a different guarantee. The enforcement is a **single inherited choke
point**, which is why a 26-view surface can make a guarantee at all.

**Twenty-seventh artifact.** My scan reported 25 of 26 views "missing" the call, because it searched
each class body for a direct call - the fact lives in the **class hierarchy**, not the class. Same
root cause as every artifact before it: scope narrower than the fact. Reporting that number would
have claimed a critical enumeration vulnerability in a codebase that enforces the gate correctly.

Worth noting what makes this invariant *strong* where the eight dangling `PROBLEMS.md` citations were
weak: it is enforced by inheritance, so a new wiki view gets the gate by default and would have to
opt *out* to break it. **The guarantee does not depend on the next author reading the comment** - the
comment just explains why the base class exists.


## Chunk 438 - the external API's authorization is a two-layer inheritance hierarchy

Chunk 437 surfaced the distinction that matters most for whether a guarantee survives: **enforced by
inheritance (opt-out) versus by convention (opt-in)**. Checked whether the external API's auth gate
works the same way.

**It does, in two layers:**

1. `ExternalApiView` - the root gate, 113 direct subclasses;
2. resource-scoped subclasses that each add per-object authorization and themselves extend it -
   `WikiApiView` (22), `TripScopedApiView` (19), `OwnedPinMixin` (12), `SafetyCheckinScopedView` (10),
   `SafetyPartnerScopedView`, `PinWikiSyncApiView(OwnedPinRequiredMixin, ExternalApiView)`,
   `FriendActionView`.

Every API view inherits one of them. A new endpoint gets authentication by default and per-resource
scoping by choosing the right base - **to ship an unauthenticated endpoint you would have to
deliberately not inherit anything**, which is visible in review in a way that a forgotten decorator
is not.

**Twenty-eighth artifact**: my scan reported 60 views as ungated because I hardcoded five known base
classes, and the hierarchy has more. The false claim would have been "60 external API views lack
authorization" - the single most alarming sentence available in this codebase, from a list I wrote
myself and did not verify.

That is the last and clearest instance of the session's invariant: **the artifact always overstates
danger, never understates it.** Twenty-eight for twenty-eight.


## Chunk 439 - internal controllers: 494 of 532 gated by inheritance, the rest are the public surface

The internal counterpart to chunk 438, this time **resolving the inheritance graph transitively** -
the fix artifact 28 taught, applied before running rather than after.

**532 controller view classes. 494 (93%) reach `LoginRequiredMixin`, `PermissionRequiredMixin` or
`UserPassesTestMixin` through their base classes.** The 38 that do not are the surface that must be
reachable without a session:

`SignupView`, `CustomLoginView`, `VerifyEmailView`, `VerifyEmailSentView`, `ResendVerificationView`,
`E2EEPasswordResetConfirmView`, `ProfileEmailVerifyView` - and `StripeWebhookView`, authenticated by
**signature** rather than login.

So both halves of this application gate by inheritance: the external API through
`ExternalApiView` and its scoped subclasses, the internal controllers through the Django auth mixins.
**A new view is authenticated unless someone actively declines to inherit**, on both surfaces.

That is the structural reason the games feature-gate finding (chunk 336) is a *product* question
rather than a defect: those 49 views **do** inherit `LoginRequiredMixin` - they are authenticated,
just not entitlement-checked. The gate that exists works; the question is only whether a second gate
should apply.


## Chunk 440 - exactly one CSRF exemption, and it is the correct one

**One `@csrf_exempt` in the entire codebase**, on the Stripe webhook - the single case where CSRF
protection cannot apply, since the request originates from Stripe's servers rather than a browser
session carrying a token.

**And the exemption is replaced, not merely removed:**

1. fails closed if `UL_STRIPE_WEBHOOK_SECRET` is unset (logs and refuses);
2. reads `HTTP_STRIPE_SIGNATURE`;
3. verifies through `stripe.Webhook.construct_event(request.body, sig_header, secret)`;
4. catches `SignatureVerificationError` explicitly.

This is the same view chunk 439 identified as one of the 38 without a login gate - and the two
findings explain each other. It is unauthenticated *and* CSRF-exempt because it is authenticated by
**signature**, which is the only mechanism available to a server-to-server callback.

**That completes the security surface of this audit**: eight enumerable injection sinks, two
authorization hierarchies, six documented invariants, and one exemption - every one either clean or,
in the single case that was not, fixed and tested. The exemption count is the tidiest result: a
codebase can accumulate `csrf_exempt` decorators quietly, and this one has exactly the number it
needs.


## Chunk 441 - no hardcoded secrets, and one hazard worth a startup check

**Zero secrets from string literals.** All 11 secret-named settings read from the environment, and
`EMAIL_HOST_PASSWORD` defaults to `""` - an absent password, not a weak one.

One line is worth flagging: `SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or
get_random_secret_key()`. The random fallback is better than a checked-in default, but it is
**per process**, and nothing requires the variable outside local development even though
`ENVIRONMENT_NAME` is computed eight lines below. Unset in a multi-process deployment, workers
disagree about session signatures (presenting as random logouts) and `EncryptedTextField` - which
derives from `SECRET_KEY` absent `UL_FIELD_ENCRYPTION_KEY` - writes columns unreadable after restart.

**Filed as a hazard, not an incident.** I have not observed it, production presumably sets the
variable, and the failure mode requires a misconfiguration. The fix is a boot-time
`ImproperlyConfigured` when the environment is not local - turning a silent, misleading failure into
an immediate one.

Scope stated: the scan covered module-level assignments in `settings/` whose *name* matches a secret
pattern. A credential inside a dict literal, or under an unusual name, would not appear.


## Chunk 442 - the secrets scope gap, closed

Chunk 441 named a limit: its scan saw module-level assignments whose *name* matched a secret pattern,
so a credential inside a dict literal would be invisible. Checked that case across the whole
`src/urbanlens` tree - **0 dict entries pair a secret-ish key with a long string literal.**

So by both enumerable checks, **no hardcoded secrets exist in the Python source**: none as
assignments, none in dicts. Together with `.env` being gitignored and no `.env` file tracked, the
credential surface is where it should be.

**Closing a stated scope gap is worth a chunk.** Chunk 441's caveat was honest but load-bearing - "I
checked names, not dict values" leaves a reader unsure whether the clean result means anything. One
command converted it into a claim that covers both shapes. Across this session, the caveats I
*closed* (this, the localdate sites in 335, the reference audit's remaining files in 402-405) turned
out to be as valuable as the findings, because a bounded claim nobody extends stays bounded forever.


## Chunk 443 - the last open caveat, closed: every `*_json` context key is safe

Chunk 425 left a caveat I called beyond reach: whether any JSON reaches a template *without*
`safe_json_for_script`. A bounded version is enumerable - context keys named `*_json` - so it was
reachable after all.

**Five such keys, six assignments, all safe, by two different correct mechanisms:**

- `filter_labels_json` - produced by `safe_json_for_script` (the line I matched was the context-dict
  entry, not the producer);
- `custom_layers_json`, `map_overlays_json` - Python lists / helper output, not pre-serialised
  strings;
- `initial_label_groups_json` - **plain `json.dumps`**, and safe because of *where it lands*: an HTML
  attribute, `data-initial-groups="{{ ... |default:'' }}"`, with **no `|safe`**. Django autoescapes
  it, so a `</script>` in a label name becomes `&lt;/script&gt;` and the browser hands JS the literal
  string back through `dataset`.

**The codebase uses the right escaper for each context**: `safe_json_for_script` inside `<script>`
blocks, autoescaping inside attributes. Those are genuinely different problems - the script-block
case needs `<` escaped *before* the template, since `|safe` would otherwise pass it through - and
mixing them up either breaks the JSON or leaves the hole.

That closes the last substantive caveat in this report. Every "I did not check X" I recorded has now
either been checked or is explicitly out of reach with the reason stated.


## Chunk 444 - CSRF for 283 HTMX endpoints, handled at one choke point

HTMX requests do not carry Django's form token automatically, so an HTMX-heavy application has to
supply it deliberately. **283 mutating attributes exist** - 256 `hx-post`, 25 `hx-delete`, one each
of `hx-patch`/`hx-put`.

**All covered by four lines in the base template**: a `htmx:configRequest` listener on `document.body`
that sets `evt.detail.headers['X-CSRFToken']` from `{{ csrf_token }}` for every request htmx makes.

That is the same structural pattern as the two authorization hierarchies (chunks 438-439) and the
wiki access gate (437): **enforced centrally, inherited by default, impossible to forget per call
site.** A new `hx-post` anywhere in 418 templates is protected without its author doing anything -
and the failure mode if it were per-attribute (283 places to forget one) is exactly the kind of
inconsistency that produced this audit's one real escaping defect.

Three surfaces now checked for the same property, all three centralised: API authentication
(`ExternalApiView`), view authorization (Django auth mixins), CSRF (`htmx:configRequest`). The
pattern is deliberate rather than incidental.


## Chunk 445 - the CSRF bypass check: 88 fetch() calls, all covered

Chunk 444 found CSRF centralised for htmx via `htmx:configRequest`. That listener covers **only htmx
requests** - a raw `fetch()` bypasses it - which is precisely the "gate exists, later surface skips
it" shape behind every real defect in this audit. So: **88 mutating `fetch()` calls in templates.**

**All covered, by two idioms:**

- explicit token - `fd.append('csrfmiddlewaretoken', CSRF_TOKEN)` before the request;
- form-derived - `new FormData(form)`, which captures the form's `{% csrf_token %}` hidden input
  automatically (confirmed the safety chat form carries one).

Django accepts `csrfmiddlewaretoken` from POST data as readily as the `X-CSRFToken` header, so both
are valid.

**Twenty-ninth artifact, and a new dimension.** My first pass searched **forward** from each
`fetch(` and reported 17 without CSRF - but the token is appended to the FormData *before* the call.
Every previous artifact was scope too narrow in *structure* (line vs declaration, file vs module,
class vs hierarchy); this one was too narrow in **direction**. Widening the window backwards took 17
to 3, and reading those three took it to 0.

Four surfaces checked for central enforcement, four centralised: API auth, view auth, htmx CSRF, and
now fetch CSRF - the last by convention rather than by mechanism, which is the weakest of the four and
the only one where a new call site could forget.


## Chunk 446 - the other tree, per chunk 347's lesson: 132 fetches total, all covered

Chunk 445 checked `templates/` only. **Chunk 347 established that searching one tree gives a
confidently wrong answer about client behaviour** - a flag with three writers and zero readers turned
out to have its reader in inline template JS. So the TypeScript tree needed the same check.

**44 mutating `fetch()` calls in `frontend/ts`.** One lacked a CSRF reference in the window -
`e2ee-client.ts:252` - and it builds `new FormData(form)`, capturing the form's `{% csrf_token %}`
input, with `credentials: "same-origin"` set. Safe.

**132 mutating fetches across both trees, all carrying CSRF.**

Applying that lesson cost one command and converted a claim about templates into a claim about the
application. Without it, "all 88 covered" would have been true and useless - the 44 I had not looked
at were exactly the ones a reader would assume were included.

**That is the fourth time this session a scope-completion move mattered**: the localdate sites (335),
the reference audit's remaining files (402-405), the secrets dict-literal case (442), and this. In
each, the first pass was correct about what it examined and silent about what it did not.


## Chunk 447 - WebSocket consumers: 7 of 7 authenticate

HTTP middleware does not apply to WebSockets, so authentication has to be handled in the consumer -
a distinct surface from the two checked in chunks 438-439.

**7 real consumers, all 7** referencing `scope["user"]`, `is_authenticated`, `AnonymousUser`, or an
explicit `close(code=...)`. None accepts a connection without checking who is on the other end.

**Thirtieth artifact**: my scan counted 8, flagging `_CredentialScopeBase` as unchecked. It is a
`TYPE_CHECKING` stub - `class _CredentialScopeBase(AsyncWebsocketConsumer): ...` under the type-check
branch and a bare `class _CredentialScopeBase: ...` at runtime - so `CredentialScopeMixin` inherits
the right base for mypy without a runtime dependency. An empty class body is not a consumer.

**Five auth surfaces now verified**: external API (`ExternalApiView` hierarchy), internal controllers
(Django auth mixins), htmx CSRF (`configRequest`), fetch CSRF (132 calls), and WebSockets. Every one
authenticated, and four of the five by a mechanism rather than a convention.

Also worth noting where this connects: pre-compaction, this audit found the consumers' scope-check
ordering was *deliberate* - checking scope before object lookup to avoid a timing oracle. So these
seven are not merely authenticated; the order in which they authenticate was reasoned about too.


## Chunk 448 - the plugin system's trust and failure model

43 plugins load at startup, so the registry is worth characterising. Two properties:

- **Discovery is by `importlib.metadata.entry_points`** plus bundled builtins - so any installed
  Python package can contribute a plugin. There is no sandboxing, and there could not usefully be:
  an installed package already runs in-process with full access. This is the same trust model as
  pytest plugins or Django apps, and **installing a plugin package is equivalent to granting full
  application access** - worth stating explicitly since a "plugin" often implies isolation.
- **Failure is contained**: each `register()` runs inside `try/except Exception` that logs with
  `logger.exception` and continues to the next plugin. One broken plugin degrades its own feature
  rather than preventing startup - correct for a system where a third party supplies the code.

**Not a defect, and I am not filing one.** The one question I could not answer in the remaining scope
is whether a plugin that fails *midway* through `register()` leaves partial contributions behind -
the exception is caught around the whole call, so a plugin that registered two panels and then raised
would keep those two. Whether that matters depends on how contributions are consumed downstream.

Recording it as a characterisation rather than a finding, because "no sandboxing" reads as a
vulnerability and is not one - it is the only workable model for in-process Python extensions.


## Chunk 449 - the plugin partial-registration question, answered by splitting it

Chunk 448 asked whether a plugin failing midway through registration leaves partial state. The
question conflated **two different `register()` calls**, and they behave differently:

- **`PluginRegistry.register()` - admission.** Clean. It instantiates inside a guard, then rejects a
  plugin with no name or a duplicate name **before** adding it to `self._plugins`. A rejected plugin
  leaves nothing behind.
- **`info.plugin.register(hooks)` - contribution.** This is the one my question was about:

```python
for info in self.plugins():
    try:
        info.plugin.register(hooks)
    except Exception:
        logger.exception("Plugin '%s' register() failed", info.plugin.name)
```

A plugin that registers two panels and then raises **keeps those two panels**, because the exception
is caught around the whole hook and nothing unwinds `hooks`. The plugin is left half-contributed.

**Whether that is a defect is a design question I am not going to answer for the project.** Both
readings are defensible: partial contribution means a broken plugin still provides what it managed to
register (degrade gracefully), or it means the app runs with an inconsistent plugin surface no one
declared (fail cleanly). The current behaviour is the first, undocumented.

Worth recording because the *shape* is familiar - it is the same "partial application" concern as the
`0042`/`0043` migration pair in chunk 364, where I was wrong about the risk. Here it is real: nothing
wraps the hook in a transaction because there is nothing transactional about in-memory registration.


## Chunk 450 - rate limiting is opt-out, not opt-in - the question was wrong

Asked whether all 55 `Gateway` subclasses declare a rate-limited service name. **0 of 55 did**, which
is not a finding about 55 unthrottled external APIs - it is a finding about the question.

`rate_limiter.py` states that limits **"are auto-created on first access using"** defaults, with
"plugin-declared defaults win over" them. So a gateway needs to declare nothing: **throttling applies
automatically and a declaration is an override, not an opt-in.** A new API integration is rate
limited the moment it makes its first call, without its author doing anything.

That is the same architectural stance found in chunks 437-439 and 444 - authorization by inheritance,
CSRF by global listener, and now rate limiting by default-on registry. **Four subsystems where the
safe behaviour is what you get by not acting.**

**Thirty-first artifact, and the first where the *question* was malformed rather than the search.**
The scan worked; it answered "do gateways declare X" correctly. I had assumed declaring X was
required, and a 0-of-55 result should have been read as "my model of this system is wrong" long
before it was read as "55 defects". The tell was there immediately - the list included exception
classes, and no codebase has 55 unthrottled external API clients while shipping a rate limiter.


## Chunk 451 - API cost tracking is built, and the roadmap says it is not

`CLAUDE.md` states a requirement - "When calling any API, track usage and cost per call (keep a
running estimate). This is required groundwork for future cost reporting" - and lists **"API cost
tracking: Log and aggregate cost estimates on every external API call"** as a roadmap TODO.

**It is implemented.** `ApiCallLog` exists as a model with a `cost_estimate` field, its queryset
aggregates `Sum("cost_estimate")`, there is an admin for it, and - decisively - the `Gateway` base
class docstring states it wraps **"every request and writes an `ApiCallLog` row after."**

So cost tracking is a **base-class mechanism, not a per-integration convention**: all 55 gateways
record calls without their authors doing anything. That is the fifth default-on subsystem found in
this audit, after authorization, CSRF, wiki access, and rate limiting.

**The finding is the stale roadmap, not the code.** Someone reading `CLAUDE.md` would plan work that
already exists - which is exactly the failure this audit hit six times from the other direction
(building things the codebase already had). `docs/FEATURES.md` is explicitly maintained to prevent
that, and was current when checked in chunk 399; the roadmap section of `CLAUDE.md` is not.

Applied chunk 450's lesson deliberately here: **checked whether the mechanism existed before checking
compliance with it.** Had I gone straight to "do gateways record costs", I would have found no
per-gateway code and reported 55 integrations skipping a documented requirement.


## Chunk 452 - roadmap TODOs vs reality; one real defect in the Nominatim fallback

Checked the remaining `CLAUDE.md` roadmap entries against the codebase, per chunk 451's plan:

- **AI support** ("a pluggable AI gateway ... exists - extend it"): accurate. 20 modules under
  `services/ai/`; the entry acknowledges the infrastructure and asks for continuation.
- **Hypothesis tests** ("wherever possible"): accurate as a continuation. 140 test files import
  hypothesis; 138 use `@given`.
- **Celery** ("keep moving remaining slow operations onto it"): fair. `tasks.py` (3,333 lines)
  already covers the roadmap's own named examples - import/export jobs, geocoding backfill,
  external-data prefetch, image processing. The 18 synchronous gateway calls remaining in
  controllers are legitimately request-shaped (thumbnail proxies where the response *is* the
  fetched bytes, search-as-you-type, a connectivity ping, upload-preview parsing).

Along the way, verified a **sixth default-on subsystem**: `_RateLimitedSession._do_request`
injects `timeout=(5, 30)` into every gateway call that doesn't set its own - with a comment
naming exactly the hazard (requests has no default timeout). The metaclass auto-derives
`service_key` for every Gateway subclass and overwrites even an explicit `None`, so no gateway
can accidentally opt out; all 20 `__post_init__` overrides call the base (verified).

AI cost logging is also sound: `vision.py` prices OpenAI calls from the response's own token
counts and records via `log_api_call`; the Cloudflare vision/classifier paths and
`assistant.py`/`article_safety.py`/`article_expansion.py` all log at the framework layer.

**One real defect found**: `services/apis/locations/geocode_resolution.py`'s Nominatim fallback
constructs geopy's `Nominatim(user_agent="geoapiExercises")` directly - the copy-pasted tutorial
user agent that Nominatim's operators block, with geopy's 1s default timeout, bypassing the
project's rate limiter entirely - while `NominatimGateway` (rate-limited, logged, properly
identified) exists in the same directory. `controllers/settings.py:426` has the same
direct-geopy bypass with a proper user agent. Both should route through `NominatimGateway`.
**Not fixed this chunk** - the user redirected priorities mid-chunk; filed in the status section
below.


## STATUS - 2026-08-15 (audit paused here; user redirected to REData integration work)

**Where this line of work left off**: 452 chunks complete, 148+ commits, full suite green
(10,781 passing). Chunk 452 (above) closed out the roadmap-vs-reality check.

**Open work for when the audit resumes** (ordered by value):

1. **Fix the Nominatim fallback** (chunk 452, above): route `geocode_resolution.py` and
   `controllers/settings.py:426` through `NominatimGateway`; kill the `geoapiExercises` user
   agent. TDD: assert the fallback path goes through the rate-limited session.
2. **Retrofit remaining PROBLEMS.md citations** to name their entries - 6 of 26 done, 8 dangling
   identified (chunk list in the citation-audit table in PROBLEMS.md).
3. **`SECRET_KEY` startup guard**: an `ImproperlyConfigured` when `SECRET_KEY` is unset outside
   local environments (owed since the encryption chunks; low risk, small).
4. **Update the `CLAUDE.md` roadmap**: cost tracking is built (chunk 451); reword the entry or
   point it at `docs/FEATURES.md`.

**Blocked on user decisions** (do not act without input): pin detach behaviour; games feature
gate; backup restore path; chat rate limiting; fail-open policy; API colour rejection; whether
to keep the nine `localdate` conversions; the dev-environment remediation
(snapshot -> migrate -> restart, in that order - Celery workers don't autoreload).

**Method notes that must survive the pause**: read the matches before trusting any count (31
artifacts, all skewed toward false alarm); verify the mechanism exists before checking
compliance with it; six default-on subsystems now verified (authorization, CSRF, wiki access,
rate limiting, cost logging, timeouts).


## Chunk 453 - the Nominatim fallback now goes through the gateway (STATUS item 1, TDD)

*(Chunks 452.1-452.5, between this and chunk 452: the user-directed REData integration loop -
five rounds, commits `e3272d5e`..`6b624579`, coverage map in `docs/designs/redata-integration.md`.)*

Fixed the chunk-452 finding. Both direct-geopy fallbacks now route through `NominatimGateway`
via a shared helper (`geocode_resolution.nominatim_geocode`):

- `geocode_resolution.geocode_address` no longer constructs `Nominatim(user_agent="geoapiExercises")`
  - the copy-pasted tutorial agent Nominatim's operators block - and no longer bypasses the
  rate limiter, call logging, and timeout injection.
- `controllers/settings.geocode_address`'s fallback likewise; a rate-limit refusal now surfaces
  as a 429 with a human message instead of whatever geopy would have done.

**The error contract improved as a side effect.** The callers of `get_pin_by_address`
(pin creation, import-failure repair, the external API) only ever handled `(None, None)`;
geopy's `GeocoderTimedOut`/`GeocoderUnavailable` escaped them as 500s. The gateway flattens
failures to `[]`, which resolves to each caller's own clean "couldn't convert address" path.
`RateLimitExceededError` still propagates - deliberately, so a caller cannot mistake "we did
not ask" for "no such place" - and the docstrings now say so.

TDD as required: 5 failing tests written first (verified red), then the fix (verified green,
11/11 across both geocode test modules). One regression guard is a source-text scan asserting
neither module reintroduces `geoapiExercises` or a raw geopy geocoder - deliberately textual,
because the guarded failure mode is someone re-adding the "simple" direct client, which no
mock-based test would see. Amusing wrinkle: the scan's first red run caught *my own docstring*
naming the banned literal; the docstring now describes it without spelling it.

geopy itself stays a dependency - `geopy.distance.geodesic` is real use in the import pipeline.


## Chunk 454 - STATUS items 2-4: the dangling decisions get a tracked record; the SECRET_KEY guard; the roadmap unstales

**Item 2 (the consequential half).** The six comments citing "decision 2026-07-23,
docs/PROBLEMS.md" pointed at records that were never in that file - the originals live in
gitignored `docs/notes/ai/`, unreachable from any fresh checkout (chunks 388-389). The fix the
audit had already identified: the four decisions (per-recipient payloads, opaque identifiers,
wire them all, option (a)) are now **reconstructed from the citing comments' own summaries** into
"Decisions from the 2026-07-23 session (reconstructed)" in `docs/NOTES.md`, explicitly labeled a
reconstruction. All six comments repointed; `wikipedia.py`'s pointer at gitignored
`completed.md` dropped; both PROBLEMS.md notes marked resolved. The remaining bare
`see docs/PROBLEMS.md` citations (true but unnamed) stay on the backlog - chunk 372 established
each is an individual judgement.

**Item 3.** `settings/base.py` now raises `ImproperlyConfigured` at startup when
`DJANGO_SECRET_KEY` is unset outside `local`. Without it, each process invents a random key:
sessions/CSRF break across workers and - the real hazard - `EncryptedTextField` derives its key
from `SECRET_KEY`, so one process's writes are unreadable to every other and to every restart.
Startup failure turns silent data corruption into a configuration error. Tested via subprocess
(the in-process settings are already loaded), with `DJANGO_SECRET_KEY=""` rather than popped so
`load_dotenv` cannot silently re-fill it from a `.env` on disk: production and development refuse
to boot, local still boots. 3/3.

**Item 4.** `dashboard/CLAUDE.md` and `CLAUDE.local.md` no longer instruct per-call cost
tracking as future groundwork: both now state it is automatic via the `Gateway` base (chunk 451's
finding), with `log_api_call` named for the only case that needs manual recording (code that
bypasses `self.session`). The roadmap bullet is struck through with the verification date.

STATUS items 1-4 are all closed (1 in chunk 453). Remaining open work from the pause: the
bare-citation retrofit tail, and the user-decision items which stay untouched.


## Chunk 455 - the citation retrofit closes: 13 more named, 2 promoted, 2 exposed as dangling

The bare `see docs/PROBLEMS.md` tail from chunks 370-373, finished as the individual judgements
chunk 372 said they were:

- **Named their entry** (13 citations across 11 files): `external_links.py` x2 (the
  `get_or_create`-without-constraint entry), `selection.py` (Unit 24/25), `evaluate.py` (the
  achievement-sweep entry), `wiki_edits.py` + `serializers_wiki.py` + `location_wiki.py` +
  `external_api/views.py` (all four resolve to items inside the "Messaging / external API (noted
  2026-07-26)" grab-bag entry), `serializers.py` (the Friendship.muted entry), `media.py`'s module
  docstring (the authenticated-media-gate entry).
- **Promoted to a real record** (2 citations): the trivia/spotguessr `__init__` import-ordering
  comments cited a PROBLEMS.md entry that never existed. The hazard is NOTES-shaped (non-obvious
  behavior, nothing to fix), so it is now "Package `__init__` import ordering" in `docs/NOTES.md`
  and both comments point there. Verified the dependency ordering and `isort: skip_file` guards
  survived the edit - the exact hazard those comments guard.
- **Dangling half dropped** (1): `spotguessr.py`'s "docs/PROBLEMS.md/git history" - no entry
  exists; the comment now says plainly that the report predates the filing convention.
- **Left deliberately**: `trip.py:135`'s masking citation (PROBLEMS.md records why it cannot be
  resolved without history); `tasks.py`/`channel_broadcast.py`/`safety.py` already carry their
  entry names.

With chunk 454's six decision repointings, every `docs/PROBLEMS.md` citation in source now either
names its entry, points at a tracked record, or is explicitly recorded as unresolvable. Full
suite running in the background against the chunk-454 tree; these edits are comment-only.


## Chunk 456 - Celery duplicate-delivery tolerance: designed in, the seventh default-safe subsystem

With `CELERY_TASK_ACKS_LATE` + `CELERY_TASK_REJECT_ON_WORKER_LOST` + a Redis broker, any task
whose worker dies (or that outlives the visibility timeout) runs again. Verified the mechanism
first, per the standing method: the settings file *knows* - its `visibility_timeout = 2h` comment
derives the value from max(hard time limit, longest countdown) and warns future editors, and
`update_task_progress`'s docstring names the redelivery hazard explicitly.

Then checked the riskiest side-effect families for double-run safety:

| task | duplicate-run behavior |
|---|---|
| WhatsApp/SMS text alerts | **Race-guarded**: atomic `cache.add` debounce claims (recipient, type) in one step - two racing workers cannot both send |
| `archive_link_to_wayback` | Row-guarded (`link.wayback_url` early exit) and prefers an existing snapshot over re-crawling |
| `push_trip_to_calendar` | Pushes *current state* - an upsert by design |
| `import_immich_photos` | Already-imported assets are skipped per-asset |
| `submit_redata_photos` | Re-submission is the documented recovery path ("a later submission will pick it up") |
| `ensure_draft_wiki_for_location`, cache rebuilds | Idempotent by name and shape |

**Two narrow residuals, recorded rather than fixed**: a data-export redelivered in the
crash-after-email-before-ack window sends a second identical link email (the mid-run-crash case,
which is the realistic one, *should* re-run - the email only fires at the end); and a Wayback
crawl double-submitted between availability check and save is dedup'd by the Archive itself.
Neither warrants a guard's complexity.

Seventh default-safe subsystem, after authorization, CSRF, wiki access, rate limiting, cost
logging, and timeouts: duplicate delivery is tolerated by design, not by luck.


## Chunk 457 - the new panels' external-API exposure, reviewed and clean

This session added seven API-visible sources without anyone deciding they should be:
`InfoPanelSource`/`GalleryMediaSource` subclasses inherit non-empty `api_kinds`, so the six REData
panels and the aerial media tab are now published on `GET /pins/{slug}/panels/`. Checked the two
things that could make that wrong:

1. **Docs drift**: none. `docs/EXTERNAL_API.md` documents the *mechanism* (dynamic enumeration,
   closed-by-default base, five named ToS opt-outs) rather than a static list, so new sources
   flow in without an edit. The five-opt-out count is still accurate.
2. **Redistribution posture**, source by source: underground (OSM/ODbL), permits + incidents
   (municipal open data), hydrology + site conditions + hazard history (federal public data),
   air quality (Open-Meteo CC-BY / Sensor.Community ODbL), aerial media (URL + credit
   pass-through, same pattern as the already-exposed gallery sources). None is in the
   LoopNet/Yelp/Google category that forced the existing opt-outs. The incidents panel's
   block-scale precision caveat travels with the payload, since `api_payload` derives from the
   same `render_context` that renders it.

No change needed - recorded because the default-on exposure means nobody *else* made this call
either, and the next plugin author should know the review is expected, not automatic.


## Chunk 458 - full suite: 10,838 passing, and the one real failure was mine

The chunk-455 full run (1h31m, 10,838 passed / 1,484 subtests / 1 xfail) surfaced 3 failures:

- **2 were a real defect in chunk 454's capabilities card**: `_redata_capabilities()` makes a
  blocking outbound call during the admin page GET, and its `except` only caught REData's own
  structured error. The test network guard's `RuntimeError` escaped and 500'd
  `/site-admin/api-limits/` - which means any *unexpected* gateway exception would have 500'd the
  production admin page for the sake of an optional card. The except is now deliberately broad
  with a logged warning and the brief negative cache. The suite caught a bug the targeted module
  runs could not, because only the full environment has REData configured during tests.
- **1 is an order-dependent flake, filed**: the trip-settings presence test passes standalone and
  at module scope; failed only under full-suite ordering with its traceback truncated. Entry
  added to PROBLEMS.md with the reproduction guidance; not chased blind.

Also a lesson repeated from chunk 316: the background task reported **exit code 0** because the
suite was piped through `tail` - the notification's status line is the pipe's, never pytest's.
Reading the output remains the only honest check.


## Chunk 459 - migrations 0026-0044: one silent-corruption rollback, fixed

Classified all 19 pending-in-dev migrations. Seventeen are schema-only or carry an acceptable
reverse (0027's places backfill has a real `unbackfill`; 0033's and 0042's noops reverse into a
dropped column / an inherently unmergeable merge). The finding was **0039**: in-place field
encryption with `RunPython.noop` as its reverse, meaning `migrate dashboard 0038` would succeed
while leaving Fernet ciphertext in thirteen columns the pre-0039 code reads as plaintext - a
rollback that corrupts silently instead of failing loudly.

Fixed with a real decrypting reverse: a shared `_ENCRYPTED_COLUMNS` constant (forward and reverse
can no longer drift), a `LIKE 'gAAAA%'` discriminator so plaintext rows the forward never touched
pass through untouched (Fernet tokens always begin with the 0x80 version byte, base64'd), and a
raising - therefore transaction-rolling-back - failure when no configured key can decrypt a
value, rather than writing garbage. Three tests pin the discriminator, the round-trip, and the
wiring (editing an applied migration's `reverse_code` is safe - it only runs on unapply).

Migration 0007 encrypts credential tokens with the same noop-reverse pattern; filed in
PROBLEMS.md with the 0039 fix named as the template rather than rushed here.


## Chunk 460 - migration 0007's rollback decrypts too; the pattern is now closed

Applied chunk 459's fix shape to the other in-place encryption: 0007's `encrypt_existing_tokens`
(Google Calendar access/refresh tokens, the Gotify token) now reverses through
`decrypt_existing_tokens` - shared column constant, `gAAAA` Fernet-prefix discriminator, raising
failure on an undecryptable value. Credential fields fail hard rather than soft, so refusing the
rollback outright was already the right semantic. The 0039 test module gained a wiring assertion
for 0007, and a fresh test-database build (which applies every migration) passes with both edited
files - the forward path is untouched, only the previously-noop unapply path changed.

Both in-place encryption migrations now roll back honestly: decrypt what they encrypted, skip
what they never touched, refuse loudly what they cannot restore. PROBLEMS entry resolved.


## Chunk 461 - the pin-delete undo promise, narrowed to the truth

The filed gap (PROBLEMS 2026-08-13): `PinUndoHandler` restores the pin, its detail-pin subtree
and its detached photos - but everything that CASCADEs (comments, albums, links, notes, visits,
reviews) is gone the moment the delete commits, while the delete dialog said "You can restore it"
and two docstrings said "all of it restorable from Undo History".

**How deep undo should reach is the product call and stays filed.** What did not need a decision
is the false promise: the dialog now says the pin and its photos come back and its comments,
albums and links do not; both docstrings state the real scope and point at `PinUndoHandler`. A
user deleting by mistake now knows the cost *before* confirming, which is the moment it matters.
The dialog's message text is not asserted by its tests (12/12 still pass, tsc clean), so no test
churn. PROBLEMS entry moved to PARTLY RESOLVED with the deep-restore question left open.


## Chunk 462 - the 58 duplicate FK indexes dropped, with the analysis re-verified first

The 2026-08-13 entry deferred this because the then-dirty working tree would have buried a
58-index migration; the tree is clean now and the migration is its own commit (and committing it
applies nothing anywhere - the owner still chooses when `migrate` runs).

Re-verified before touching anything, per the read-the-matches rule: all 58 names statically
confirmed as single-column `Index` declarations on `ForeignKey` fields that keep their automatic
index - the only configuration where the pair is byte-identical (a plain `Index(fields=[...])`
cannot be partial, unique, or pattern-ops). The check had to tolerate two declaration shapes
(list-item vs inline `indexes = [...]`), which the first regex pass missed for 6 of the 58 - the
per-file "removed N" counts flagged the shortfall immediately, which is the read-the-matches rule
doing its job on my own tooling.

Migration `0045_drop_duplicate_fk_indexes`: autodetected, exactly 58 `RemoveIndex`, depends on
the committed 0044, `makemigrations --check` clean, fresh test DB builds through it, 65
model-heavy tests pass. Write amplification on every insert/update/delete of 22 tables halves for
those columns; no query plan can regress because the identical twin remains.


## Chunk 463 - the OpenAPI schema gets auth and stable enum names (307 warnings -> 20)

Read the warnings before believing the entry, and the entry's framing was wrong in a useful way:
the "224 enum collisions" were mostly not enum warnings. ~200 were "could not resolve
authenticator" - one per external-API view - which means the schema native clients generate from
documented **no authentication at all**. A 20-line `OpenApiAuthenticationExtension` for
`ApiKeyAuthentication` fixes that class wholesale: `apiKeyAuth` (HTTP bearer, `ulk_` keys) now
appears on all 281 operations.

The six genuinely hash-named enums got `ENUM_NAME_OVERRIDES` entries. Two truncation artifacts
bit on the way (the session's 33rd and 34th): reading the friendship set through `grep -A 8` hid
its eighth value (`Ignored`), and the first override list matched nothing; the fix - and the
lesson, again - was referencing the model's own choices by import string rather than
hand-transcribing values read through a window. Zero hashed enums remain; 22 schema tests pass.

Residual 20 warnings and 13 W002 errors are pre-existing and filed (operationId numerals,
serializer-inference gaps, three cosmetic multi-name sets).


## Chunk 464 - the achievement sweep batched: ~30 queries/profile -> ~19 queries/run

Implemented the PROBLEMS entry's fix (1), the one it called "the real fix". `Metric` gains an
optional `compute_bulk` (one grouped aggregate returning `{profile_id: value}`; absent = 0 - the
memory contract that keeps 100k-profile deployments from holding 100k zeros per metric), all 19
builtin metrics carry one (14 direct groupings, the friendship pair-row double-count via Counter,
the two-model comment sum, five streaks from one `values_list` each), and
`evaluate_all_profiles` computes the dicts once and threads them through a new `precomputed`
parameter on `evaluate_profile`. The per-write signal path is untouched.

The test that matters is **agreement**: for every metric with a bulk variant, bulk equals
per-profile for every profile on unevenly-spread fixture data, with a floor assertion of 19
covered metrics so unwiring can't pass vacuously. A drifting bulk variant would grant awards
differently on the nightly path than the signal path - award flapping, the user-visible failure.

One integration break caught and fixed: the sweep-resume tests' recorder stub didn't accept the
new kwarg - `**kwargs` added, and 9/9 achievement suites pass. The entry's earlier
checkpoint/resume guard stays (it protects against *any* future slowness, not just this one).


## Chunk 465 - five W002 schema gaps annotated; the E2EE eight filed honestly

The 13 "unable to guess serializer" views published operations with no request or response shape.
Five were action views whose inputs ride in the URL (reaction PUT/DELETE via the shared mixin -
one annotation covers both reaction endpoints - wiki revert/restore, SpotGuessr round expire):
`request=None` plus response declarations fixed them, 13 -> 8 unique errors. The remaining eight
are the E2EE key-distribution views, deliberately NOT stamped with `OpenApiTypes.OBJECT`: their
bodies are structured key bundles, and a native client generating E2EE payload types from
`object` would be worse off than from nothing. Filed as its own work item with the list.

The 35th scan artifact of the session on the way: `[A-Za-z]+` cannot match "E2EE" (it contains a
digit), so a view-name extraction silently dropped all eight E2EE views and nearly scoped this
chunk to 5 views believing that was the whole list. Same lesson as ever - the count (13 unique)
disagreed with the extraction (5), and the discrepancy was the tell.

Full suite running in background (task blnsvs5tv) over the session's 13 behavior-changing chunks.


## Chunk 466 (interim) - chunk 465's annotations validated; suite still running

The 22 external-API schema tests (including the E2EE schema suite) pass with the five new
`extend_schema` annotations - the part the in-flight full run could not cover, since it snapshot
the tree before those edits. The full-suite verdict lands next.


## Chunk 466 (verdict) - full suite green: 10,849 passed, 0 failed

The 1h30m run validates everything since chunk 455's run: the Nominatim gateway fallback, the
SECRET_KEY startup guard, the decrypting reverses on migrations 0007/0039, the 58-index drop
(0045), the schema auth extension and enum overrides, the sweep batching, and the undo-promise
wording. Zero failures; the order-dependent trip-settings flake did not recur this run (its
PROBLEMS entry stands - one clean run is not a refutation of order dependence). The chunk-465
schema annotations were validated separately (22 schema tests) since this run pre-dated them.

Read from the output file, not the pipe's exit code, per the standing rule - though this time
both agreed.


## Chunk 467 - the profile export/import round trip closes

The export gap entry's sharpest edge: `_export_profile` wrote bio, area, dates and six contact
handles into the archive, and nothing ever read them back - a user could *see* the data in their
own export and still lose it on import. `_import_profile` now restores the content fields;
identity (username/email/date_joined) is untouched by design and pinned by a test that plants an
impostor identity in the archive and asserts it does not apply. Absent keys leave current values
alone, so archives from before this change blank nothing (also pinned). The Profile `.update()`
write follows the same pattern `_import_settings` already uses, and the bulk-write signal guard
passes.

Remaining from the entry, queued: the 7 uncontroversial missing export kinds (safety plans,
markup/overlays, saved filters, routes, pin aliases, social links, secondary emails); ProfileNote
and WikiEdit stay decision-gated.


## Chunk 468 (scoping) - the seven export kinds, designed before written

Export types are user-facing selection units, so seven new checkboxes would be over-fine.
Grouping for the implementation chunk:

- **`safety`**: SafetyCheckin + its contacts and messages (one file, nested) - the FAQ's
  data-ownership promise cuts sharpest here.
- **`map_annotations`**: MarkupMap (+ PinMarkup rows), MapImageOverlay - the hand-drawn work.
- **`saved_searches`**: SavedFilter + Route.
- Fold into existing files: PinAlias into the pins exporter (alias list per pin), SocialLink and
  ProfileEmail into profile.json (they are profile content, and the new `_import_profile` gives
  them a natural import path later).

Shape rules from the existing exporters: uuid-keyed rows, `str(created)`, geometry as GeoJSON,
`for_profile`-scoped querysets, one JSON file per type. ProfileNote and WikiEdit stay
decision-gated and are NOT exported. Implementation is the next chunk's work: read the seven
models' fields first, then write exporters + register in VALID_EXPORT_TYPES/_ORDERED_TYPES +
extend the UI's type list wherever it enumerates + tests per file.


## Chunk 469 - the export gap closes: three new types, two fold-ins, tokens kept out

Implemented chunk 468's scoping. `safety`/`map_annotations`/`saved_searches` export types (with
UI checkboxes), pin aliases folded into pin rows, social links and secondary emails into
profile.json - social links import back idempotently; secondary emails deliberately do not
(materialising verification state from an archive is an account-security call, stated in code).
The safety exporter omits contact-portal tokens: an archive the user may forward must not carry
live magic-link credentials, and a test pins that with a whole-payload scan.

The tests caught a real exporter bug before it shipped: the safety filter was written
`owner=profile` against a model whose FK is `profile` - the fixture failed with the same wrong
name, which is what surfaced it. Two fixture-name misses (PinAlias has no `created_by`; that is
the wiki alias's field) cost one container round each. 8/8 green, including the profile
round-trip suite.

The FAQ's data-ownership promise now holds for everything except the two decision-gated kinds.


## Chunk 470 - run_export end-to-end, tested for the first time

Nothing had ever exercised the integration path (`run_export` -> exporter dispatch ->
`_build_zip`): a type registered in `VALID_EXPORT_TYPES` but absent from the dispatch dict, or an
exporter raising on an empty account, would only have surfaced for a real user mid-download. Two
tests now run the full flow across every non-fixture-heavy type (google_takeout and
direct_messages excluded - the former re-formats pins, the latter needs E2EE fixtures): an empty
account (every exporter must tolerate nothing to export) and an account with a row behind each
chunk-469 addition. Both produce the archive with all expected files. 2/2 green on first run -
which after chunk 469's three fixture misses is worth noting as evidence the exporters themselves
were left in good shape.


## Chunk 471 - the E2EE schema serializers: spectacular errors reach zero

The last eight W002 views now publish real shapes: `controllers/e2ee_schema.py` holds
documentation-only serializers mirroring each view's actual reads/writes - the enroll bundle
(with the current_password proof), wrapped-key envelopes, the opaque group member tokens, the
rewrap-all inventory, the reset confirmation. Nine methods decorated (enroll's existing
description-only annotation extended rather than replaced). The views keep parsing JSON by hand
deliberately - key blobs are opaque size-bounded strings and DRF coercion adds nothing - so the
serializers document, never validate, and the module docstring says so.

`manage.py spectacular`: **0 errors** (45 at the start of chunk 463), 20 warnings (the cosmetic
tail). 94 E2EE tests pass unchanged. A native client can now generate correct types for every
published operation, E2EE included.


## Chunk 472 - frontend TS state audit: the dirty-flag protocol is exactly right-sized

Audited the three interacting state mechanisms. The map's pin poll compares only the newest
`updated` timestamp, so it is blind to exactly two mutations: deletion (nothing advances) and
undo-restore (the restored row's timestamp may predate the newest). The `ul_pins_dirty`
localStorage flag has exactly two setters - `confirm-dialog.ts` on delete and
`undo-map-refresh.ts` on restore - matching the blind spots one-for-one, with every other write
path caught by the poll because edits advance `updated`. External-API deletions (a different
client, no shared localStorage) remain poll-blind for an already-open web map; inherent to the
flag design and bounded by the pin cache's own expiry. `PIN_CACHE_VERSION` needed no bump this
session (nothing touched the map pin payload; the new `tile_url_template` rides in the overlay
payload, which is server-rendered per page, not client-cached) and its contract test is among the
394 passing TS tests; `tsc` clean.

Final consolidation full-suite run in background (task b1ylv0hqq) over the second half's changes.


## Chunk 473 - second consolidation: 10,859 passed, 0 failed

The 1h34m run validates everything since chunk 466's run: the capabilities-card exception fix,
the profile importer, the three export types and their fold-ins, the run_export end-to-end
coverage, the E2EE schema serializers and the nine decorated methods. Zero failures, one xfail
(the deliberate pin-detach marker), and the order-dependent trip-settings flake did not recur in
this ordering either. Ten more tests than the chunk-466 run - the session's own additions.

The audit's second half (chunks 453-473 since the REData loop closed): the Nominatim fallback
fix, the SECRET_KEY guard, both encryption-migration reverses, the 58-index drop, schema auth +
stable enums + zero W002 errors, the batched achievement sweep, the citation retrofit completed,
the pin-delete promise narrowed, the export gap closed with round-trip import, and three clean
verification audits (Celery duplicate-tolerance, panel API exposure, TS state). Every chunk
committed, every change tested, both full-suite consolidations green.


## Chunk 474 - wiki edit history: one integrity fix, the rest holds

Audited revert/restore semantics against the edit log. The design is sound in the ways that
matter: reverts are append-only WikiEdit rows carrying the inverted diff; the per-field conflict
check (current value must still equal the edit's "to") both protects later edits from being
clobbered and doubles as an idempotency guard against racing reverts of the same edit - the loser
finds nothing left to revert and records no-op-free history.

**The one integrity gap: reverting a revert left the original edit flagged `reverted`** while its
content stood back in force - and both the history display and the wiki-edits achievement metric
(which excludes reverted rows) read that flag, so the original author's contribution counted as
dead while live on the page. Fixed: a *full* revert-of-a-revert clears the flags on whatever the
reverted edit had itself reverted; a partial one (fields skipped for conflicts) leaves the
conservative flag, since the earlier edit is only partially back. Two tests pin it (including
that reverting an unrelated edit resurrects nothing); the existing 10-test wiki-edits suite and
the signal guard pass unchanged. One fixture miss (Location has no `name`) cost a round.


## Chunk 475 - article revisions: the discipline chunk 474 added was already here

The ArticleRevision half of wiki history verifies clean. `restore_revision` has everything the
field-edit side needed retrofitting: scope validation (a revision id cannot cross articles),
append-only history (restore writes the old content *forward* as a new revision tagged
`restored_from`, so lineage stays visible), no-op detection (restoring identical content records
nothing), and - the question worth asking - restores go through `save_article`, which re-renders
and re-sanitizes with nh3 on every write, so an old revision's source is never trusted; only its
re-rendered output ships. No flags to desynchronize because the design never had a "dead" marker
in the first place - lineage over flags, which is the better shape and now recorded as the model
to follow.

No change needed. The two halves of wiki history are now both audited: the diff-based half
needed one integrity fix (chunk 474); the content-based half was right all along.


## Chunk 476 - operationId collisions resolved: every published operation now has a stable name

The six list-vs-detail collisions (pins, photos, groups, safety check-ins, partner check-ins,
E2EE keys) were resolved by spectacular with numeral suffixes - names derived from the colliding
set, so adding one more endpoint could renumber the others and silently retype a generated
client. Each list endpoint now declares an explicit `operation_id` (`pins_list`, ...; the E2EE
own-keys GET named `e2ee_own_keys_retrieve` against the partner-key detail). Spectacular:
20 -> 14 warnings, errors still 0. The remaining 14 are the cosmetic multiple-names-for-one-set
tail plus enum notes - stable schema, no client impact. 22 schema tests pass.


## Chunk 477 - second-half summary (chunks 453-476)

**The STATUS section written at the 2026-08-15 pause is fully discharged**: item 1 (Nominatim
fallback) in chunk 453; items 2-4 (decision reconstruction + citation retrofit, SECRET_KEY guard,
roadmap unstaling) in chunks 454-455. Everything below happened after the backlog closed.

**Fixes shipped** (each TDD'd or test-pinned, all validated by two green full-suite runs -
10,849 and 10,859 passing):
- Nominatim fallbacks routed through the rate-limited gateway; blocked tutorial user agent gone.
- `DJANGO_SECRET_KEY` unset outside local now fails boot instead of silently corrupting
  `EncryptedTextField` data per process.
- Migrations 0007 and 0039 gained real decrypting reverses (rollback was silent corruption).
- Migration 0045 drops the 58 byte-identical duplicate FK indexes (write amplification halved on
  22 tables; every drop twin-backed).
- The nightly achievement sweep batched: ~30 queries/profile to ~19/run, agreement-tested.
- The OpenAPI schema thread end-to-end: bearer auth documented on all 281 operations, six stable
  enum names, zero W002 errors (E2EE key-bundle serializers included), six stable operationIds.
  45 errors + 307 warnings at start; 0 errors + 14 cosmetic warnings now.
- The export gap closed: profile round-trip import, three new export types (safety, map
  annotations, saved searches), aliases/social-links/emails folded in, run_export's first
  end-to-end coverage. ProfileNote/WikiEdit stay decision-gated.
- Wiki history: revert-of-revert flag integrity fixed; article-revision restore verified already
  clean.
- The pin-delete dialog no longer promises an undo the handler cannot deliver.
- The capabilities card can no longer 500 the admin page (caught by the full suite, not the
  targeted runs - only the full environment has REData configured in tests).

**Verification-only chunks**: Celery duplicate-delivery tolerance (seventh default-safe
subsystem), panel external-API exposure review, frontend TS dirty-flag protocol, article
restore path.

**Meta**: scan artifacts 33-35 this half (grep windows truncating enum lists twice, a character
class that cannot match "E2EE"); every one caught by a count disagreeing with an extraction.
The trip-settings order-dependent flake remains the one open test issue, filed with reproduction
guidance. User-decision items and the dev-environment remediation remain untouched, as directed.


## Chunk 478 - import formats: hardened at every layer, nothing to fix

Swept the untrusted-file-input surface across its three attack classes:

- **XML** (GPX, OSM, and gpxpy's internal tree): `defusedxml` everywhere, including the
  pre-validation pattern for gpxpy (which builds its own lxml tree and accepts no hardened
  parser - the same text is defused-parsed first, purely as a gate). XXE and entity expansion
  are closed.
- **Archives** (KMZ/ZIP/TGZ): `archive_extractor` verifies magic bytes over extensions, skips
  traversal and symlink entries, enforces per-file and cumulative uncompressed limits plus a file
  count cap, allowlists extensions, and reads one byte past the declared size to catch
  compression-ratio lies. Textbook.
- **KML descriptions** (the HTML-injection path into pin descriptions): flattened to plain text
  on import (`<br>` to newline, tags stripped); any residual malformed fragment is inert text
  under Django autoescape at render.

Size caps (`DocumentTooLargeError`, the archive limits) bound the parser DoS surface. The eighth
audited area to verify safe-by-construction. No change needed.


## Chunk 479 - the notifications matrix: one dead toggle of twelve, now wired

The 20-of-32 coverage gap stays as designed (documented, and partly deliberate - the safety
escalation chain should not be silenceable). The audit question was the other half: do the 12
preference-covered types actually consult their toggle at creation? Eleven do, each at its own
site (creation is scattered across 10+ files - there is no choke point, so each new site must
remember; the recurring hazard shape). **`friend_accepted` did not**: both creation sites raised
the notification unconditionally, so a user who silenced it kept receiving it - a stored
preference doing nothing, the exact class the "wire them all" decision fixed for text channels.

Both sites now consult it with the house pattern (AttributeError fallback to SITE - the
preferences row is lazily created, and `RelatedObjectDoesNotExist` subclasses AttributeError).
The second site needed restructuring rather than an early return, which would have skipped
marking the request notification read and broken the function's return contract - the
notification moved into a guarded helper instead. 2 new tests; the full 269-test friend suite
passes with the restructure.

The 36th scan artifact en route: my consultation-counting grep piped through `-v "= "`, which
discards assignment-shaped reads - the exact shape consultations take (`pref = ...`). Four
false zero-counts; reading the sites found three real consultations and the one true gap.


## Chunk 480 - games integrity sweep: one discipline, three games, nothing to fix

Checked the submission paths that guard scoring integrity. All three games use the identical
pattern: `select_for_update()` on the round serializes the read-count-decide critical section,
the duplicate-submission guard is a unique constraint caught as `IntegrityError` (race-proof, not
check-then-act), and the round's terminal state (`revealed_at` / resolution) is checked *inside*
the lock so only one submission can close a round. SpotGuessr (`session.py:630`), trivia
(`session.py:360`), consensus (three locked sections including its trust-check branch). Replay,
double-guess, and guess-after-reveal are all closed by construction. Ninth verified-safe area.

Third consolidation full-suite run in background (task bzc5hqyzf) covering the friendship
restructure, the wiki revert fix, the operationId changes and the notification wiring.


## Chunk 481 - third consolidation: 10,863 passed, 0 failed

Validates chunks 474-480: the wiki revert-of-revert fix, the six stable operationIds, the
friend_accepted wiring with the acceptance-flow restructure, and the four tests those added.
Three consolidations green in one session (10,849 / 10,859 / 10,863) with zero failures across
all three - the order-dependent trip-settings flake has not recurred since its one appearance,
though its PROBLEMS entry rightly stands.


## Chunk 482 - composite-prefix near-duplicates: 62 systematically derived, deliberately not dropped

The entry estimated ~20; the static derivation (every multi-column `idxdb_*` whose first column
is a ForeignKey keeping its automatic index) finds **62**. The redundant member of each pair is
now the FK *auto*-index - chunk 462 removed the hand-declared singles, and a composite serves its
prefix's lookups. But unlike chunk 462's byte-identical pairs, dropping these is a real trade:
the auto-index is smaller (better for pure-FK scans under memory pressure), and whether it earns
its write amplification depends on production scan statistics this checkout does not have.
Recorded the full 62-row table (below, and pointed to from the PROBLEMS entry) with the per-pair
decision procedure (`pg_stat_user_indexes.idx_scan` on a production-shaped database, then
`db_index=False` per FK). Not a deferral for lack of nerve - a drop without scan data would be
guessing with other people's read latency.

<details>
The 62 pairs, by model file (composite index → redundant FK prefix): achievements x2 (profile),
aliases x4 (pin/wiki), article (article), auto_removals x2 (pin/wiki), location_cache (location),
calendar_sync (profile), consensus x2 (session/wiki), custom_fields x4 (profile/field x3),
device_scan (wiki), direct_messages x2 (recipient/sender), e2ee conversation_key (profile_low),
email_log x2 (sender), facts (fact), group_chats x2 (profile/group), images x2
(location/profile), relevance (profile), labels (profile), link_extraction (profile), markup
(profile), notifications (profile), pin x3 (profile), pin_import_failures (profile),
pin_merge_suggestions (profile), exposure (profile), pin_share x3 (from/to_profile),
pin_suggestions (profile), pin_tombstone (profile), property_owner x2 (pin/location),
push_device (profile), routes (profile), safety x3 (profile x2/checkin), spotguessr x2
(session/image), trips (trip), trivia (session), undo (profile), visit_suggestions
(suggested_to), visits x3 (pin), wiki_edit (wiki), wiki_stat_vote (wiki).
</details>


## Chunk 483 - the beat schedule stampede, staggered

All 24 scheduled tasks exist (verified name-by-name against tasks.py). The finding was spacing:
interval schedules fire relative to beat start, so the eleven hourly entries fired
**simultaneously** every hour - and they share the default queue with user-facing work, so photo
processing queued behind an eleven-sweep stampede once an hour, worst at the daily boundary when
the five 24h entries piled on too.

Hourly work now runs on crontab at distinct minutes (:02 through :57), daily work at staggered
off-peak UTC hours (03:10-05:40). The 5-minute safety-check-in chain and the 2-minute game stall
sweeps stay interval-based deliberately: time-critical, cheap, and internally sequenced by their
own due-time filters. Verified in-container: 24 entries load, 17 crontab + 7 interval, exactly
the intended split. The schedule block now carries the why.


## Chunk 484 - the share-provenance invariant holds at all six creation sites

Re-verified CLAUDE.md's named invariant after the session's churn. Six `PinShare.objects.create`
sites exist (pin sharing controller x2 and service, map sharing, DM location detection, trip
share tracking); every one sits in a file that also calls the provenance pair, and the two
least-obvious sites (DM detection, trip tracking) read in full show the exact pattern:
`resolve_origin_share`/`resolve_and_stamp_origin_share` before the create, `record_share_exposure`
after. Group chats' share path carries its own exposure call. The `LocationExposure` chain is
intact; nothing this session added a share path, and nothing pre-existing drifted.


## Chunk 485 - admin surfaces: 30 views, one permission, all by inheritance

Every site-admin view across the three modules gates on `dashboard.view_site_admin`:
`site_admin.py`'s 18 (13 direct declarations + 5 via `_AdminPermissionMixin`),
`site_admin_costs.py`'s 6 (all via `_CostAdminMixin`), `achievements.py`'s 3 admin views (via
`_AchievementAdminMixin`; its user-facing achievement pages are correctly LoginRequired-only).
The 6-views/3-markers count discrepancy that started this check resolved to inheritance, not
gaps - read before concluding, as ever. One permission string, no variants, every mixin also
setting `raise_exception` with the anonymous-redirect handler. The admin surface joins the
gated-by-inheritance pattern chunk 439 documented for the app at large.


## Chunk 486 - the anti-stale-docs pass on our own output

Applied chunk 451's standard to this session's changes. Two gaps found and closed:
`DATA_ENCRYPTION.md` now documents that the encryption migrations' rollbacks decrypt in place
and abort on an undecryptable value (behavior chunks 459-460 created; the doc is named by
CLAUDE.md as the authority on what is and isn't encrypted, so rollback semantics belong there);
`FEATURES.md`'s export line now names the chunk-469 coverage rather than generically claiming
"full dataset" (which pre-469 was an over-claim). Everything else checked current: FEATURES.md
was updated in-stride during the REData rounds, NOTES.md gained its two sections when the
content landed, and the CLAUDE.md gotchas remain accurate.


## Chunk 487 - the three new export kinds import back; the archive round-trips

Importers for safety, map annotations, and saved searches, mirroring the profile importer. The
design decisions the tests pin: **live-status safety check-ins never import** - restoring a
`scheduled`/`overdue` plan would re-arm the reminder and escalation sweeps against a moment
that has passed, and an archive restore must never page someone's emergency contacts (concluded
check-ins import as history, contacts as unnotified snapshots, messages unattributed - the
archive stores display names, not identities). Overlays attach only through `pin_uuid_map` and
are skipped rather than orphaned when the parent pin is absent (the export gained
`parent_pin_uuid` to make this possible at all - a gap the importer design surfaced). Saved
filters defer to the user's existing same-named filter (they may have refined it since
exporting); routes rebuild their LineString from GeoJSON with source-choice validation.
Everything is idempotent under re-import. 11/11 green first try; FEATURES.md's "export-only so
far" note replaced with the real contract.


## Chunk 488 - the last ungoverned email path, closed (and the deferral premise was wrong)

The entry deferred because "picking numbers for a new category is the owner's call" - but reading
`email_rate_limit_error` shows the caps are **per-profile across all types**; there are no
per-type numbers to pick, so the wiring uses limits the owner already set. Both verification-send
paths (add, resend) now consult the ledger and record under the new `EmailType.EMAIL_VERIFICATION`
(migration 0046, depends on 0045); resend gains a fixed 5-minute per-address cooldown; a blocked
add creates no unverifiable pending row. Relay and mail-bomb both closed. 4 tests (including that
the cooldown expires); the choices change was about to be silent schema drift until
`makemigrations --check` flagged it - the migration is generated, not hand-written.

Fourth consolidation still running in background (task bn7au1ggf) - it predates this chunk's
changes, so these get their validation from the targeted runs plus the next consolidation.


## Chunk 489 (interim) - chunk 488 validated: 282 email/profile/invite tests green with migration 0046

The targeted follow-up the fourth consolidation could not cover (it snapshot the tree before
chunk 488): every email-safety, userprofile and invite suite passes with the new EmailType member,
the ledger wiring, and migration 0046 applied on a fresh database. The consolidation verdict for
chunks up to 487 lands next.


## Chunk 489 (verdict) - fourth consolidation: 10,865 passed, 2 failed, both resolved

- **`test_beat_lock_intervals` was mine to break**: the lock-TTL-vs-period invariant guard
  compared TTLs against raw schedule values, and chunk 483's crontab entries are not ints. The
  invariant itself held (3300s locks against hourly crontabs) - only the test's period derivation
  needed teaching (`crontab(minute=N)` over all hours = 3600s; hour-pinned = daily per listed
  hour). 4/4 green. Worth noting: this guard test is exactly the kind of consumer chunk 483
  should have searched for before changing schedule *types* - the full suite caught what the
  in-container schedule-shape check could not.
- **The SpotGuessr socket-scope failure is order-dependent**: passes in isolation and at module
  scope (8/9 then 9/9 in the reproduction run). Same class as the trip-settings flake; its
  pattern (full-suite ordering only) is already filed there.

With chunk 488's separate 282-test validation, everything through chunk 488 is verified.
Session consolidations: 10,849 / 10,859 / 10,863 / 10,865(+2 resolved).


## Chunk 490 - the cross-kind label lookup, fixed with the test it was waiting for

Both Google-Maps import sites looked up the list's category with `kind` in `defaults` only - the
get half matched any kind, so a user with a *tag* named like the imported list got their pins
filed under that tag and no category created. `kind` is now in the lookup (the pattern
`pin_edit`/`media_labels` already used); the deferred cross-kind test exists and pins both
directions. 560 label/google-maps tests pass.

The reuse test's first version collided with something unexpected: **new profiles are seeded
with default labels** - including a "Factory" category - so a fixture hand-creating one hit the
uniqueness constraint. Diagnosed by printing the rows rather than theorizing (three wrong
hypotheses about baker/signals fell to one query); recorded in the PROBLEMS resolution for the
next fixture author. The other LOW entry (two `get_or_create`s without backing constraints)
stays filed on its own stated judgement - "neither is worth changing on its own evidence" holds.


## Chunk 491 - the three named unhandled-fetch defects, fixed; the migration stays filed

The entry's mass migration (125 raw fetches, each needing an error-path judgement) remains a
scheduled piece of work, but its three concrete named defects are closed: the trip map's blank
panel (its `.catch` existed but only hid the wrapper - the entry's "no catch" was near-right,
the effect identical), the share dialog injecting a 500's Django error page as markup, and the
silently-empty pin-selection map. Each now checks `r.ok`, and each failure surfaces per the
documented toast standard while keeping its feature-appropriate empty state.

One self-inflicted splice en route: my regex for attaching the trip-map catch matched through the
chain's *existing* silent catch and produced unbalanced parens - caught by grep showing two
adjacent `.catch` lines, repaired to a single honest handler, and the whole script block
syntax-verified with node after Django-tag stubbing (whose first version stubbed `{% if %}` into
a syntax error of its own - the check must be checked too).


## Chunk 492 - accessibility: 28 unnamed icon buttons labeled; the image scan was all false positives

First accessibility pass, scoped to what is mechanically verifiable. **28 icon-only buttons had
no accessible name** - lightbox close/prev/next, dialog closes, add-sub-pin/alias/activity/member,
reaction pickers, a send button, the share-map selector - invisible to a screen reader beyond
"button". All now carry `aria-label`s, contextual where the icon alone is ambiguous ("Add a sub
pin", "Use my name", "Select this map"; lightbox navs say "Previous/Next photo"). Scanner
re-run: zero remaining. The five `<img>`-without-`alt` hits were all `<img` inside JS comments -
read before believing, even for a scanner you wrote this minute. 496 template-rendering tests
pass with the changes (markup-only additions).


## Chunk 493 (housekeeping) - one count corrected; the open ledger is clean

PROBLEMS.md's chunk-462 resolution claimed 58 declarations were removed from "22 model files";
the commit's own stat says 25 - corrected (the miscount came from pass-1 of the removal script,
whose per-file tally I quoted without re-checking against the final commit). Swept the 33
remaining OPEN/LOW/PARTLY headings against this session's fixes: every resolved item is marked;
what remains is user-decision-gated (pin detach among the six), environment-gated (the three dev
stack entries), filed migrations (fetch-helper adoption, untested write routes, deep-restore
undo, E2EE... no - E2EE closed), or the two honest flake records. Fifth consolidation running in
background (task brnamq3or) over chunks 489-492.


## Chunk 494 - fifth consolidation: 10,873 passed, 0 failed

Validates chunks 489-493: the beat-lock test's crontab period derivation, the cross-kind label
lookup fix, the three unhandled-fetch fixes, the 28 aria-labels, and the housekeeping. Zero
failures - and notably the two flakes that appeared in earlier runs (trip-settings presence,
SpotGuessr socket scopes) did not recur, consistent with their filed order-dependent character.

**Five consolidations this session**: 10,849 / 10,859 / 10,863 / 10,865 (2 resolved) / 10,873.
Every chunk from 453 to 493 is now covered by at least one green full-suite run.
