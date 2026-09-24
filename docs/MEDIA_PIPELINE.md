# The media pipeline

How a user-uploaded file gets from a browser to a stored, servable asset, and
what stops a malicious one from being useful to whoever sent it.

## The threat this is built around

Not malware. ClamAV (`services/security/malware_scan.py`) catches a trojan
renamed `photo.jpg`, and it should stay - documents and archives are its actual
home turf, where macros and embedded objects are real, catalogued threats.

What it cannot catch is the thing images are actually dangerous for: a
well-formed file engineered to corrupt memory inside the library that decodes
it. CVE-2023-4863 is the reference case - a heap overflow reachable by handing
libwebp an ordinary-looking WebP file, exploited in the wild before a fix
existed. There is no payload to match and no signature to write; the file is
not "bad content", it is a legitimate file that breaks the parser.

Every format this app accepts has the same shape of risk, so the mitigation
cannot be per-format detection. It is **placement**: decode somewhere an
exploit gains nothing, and **normalisation**: never serve back the bytes that
were uploaded.

Two different things are worth distinguishing in the table below, because
conflating them overstates what is actually enforced:

- **Guarded** — the entry point carries `@untrusted_parse`, so running it
  outside the sandbox raises (under `deny`) or logs (under `warn`).
- **Routed** — the *task* that reaches it declares `queue=SANDBOX_QUEUE`, so it
  executes in the isolated container. Real isolation, but nothing stops a
  future caller reaching it from somewhere else.

| Parser | Reached by | Runs in | Guarded? |
|---|---|---|---|
| Pillow (+ pillow-heif) | photos, media previews | sandbox | yes |
| Pillow (bare `PIL.Image`) | custom icon and avatar re-encode | routed via `publish_held_upload` | yes |
| Pillow (bare `PIL.Image`) | embedded XMP/IPTC photo keywords | sandbox, read with the EXIF snapshot in `process_image_upload` | yes |
| ffmpeg / ffprobe | video | sandbox | yes |
| LibreOffice (`soffice`) | doc/spreadsheet conversion | sandbox | yes |
| poppler + tesseract | PDF text/OCR, preview render | sandbox | yes |
| `zipfile` / `tarfile` | data import, import preview | routed via `run_user_data_import` and `parse_import_preview_task` | yes |
| python-docx | AI document import, via the import preview | routed via `parse_import_preview_task`; the AI call runs on an interactive worker | yes |
| lxml / fastkml / gpxpy | KML, GPX, OSM XML | routed via `run_user_data_import` and `parse_import_preview_task` | yes |
| GDAL / GeoPandas / Shapely | shapefile, WKT/WKB | routed via `run_user_data_import` and `parse_import_preview_task` | yes |
| clamd | everything, except VirusTotal-eligible fetched assets (see below) | sandbox (its own container for the daemon) | n/a |

Not every parser is guarded (corrected 2026-09-10, P103 — this used to claim
"every parser is now guarded", which was false). The import preview was the last
*sandboxing* blocker before `UL_UNTRUSTED_PARSE_POLICY=deny`; since 2026-09-13 it
parses in `parse_import_preview_task` and finishes its lookups on an interactive
worker (P2), and the label-icon resize that decoded in the request moved there too
(P103). The embedded-metadata keyword provider decoded the stored photo in the
credentialed interactive worker until the same day (P116); the keywords are now read
in the sandbox before the rewrite and kept on `Image.embedded_keywords`. The external
API's SpotGuessr round image decoded and re-encoded the photo in the request, with no
decorator for `warn` to log (P117); it now serves the stored file through the media
gate's hand-off, since every stored photo is already re-encoded (P118).

## The tiers

### 1. The request

Fast, local, and *never* a decode. In order:

1. Size ceiling (`storage.max_upload_file_size_bytes`).
2. Extension allowlist for photos - SVG is refused here, because it has no
   magic bytes and sniffing would pass it (`security/content_sniffing.py`).
3. Magic-byte sniff against the declared kind.
4. Store the raw upload untouched and mark the row `pending_scan`. Nothing
   here decodes it - a decode is exactly the class of code the sandbox tier
   exists to keep out of this process. The quota and duplicate checks and the
   insert run inside `storage.reserve_upload`, one upload per profile at a time
   (D21).

ClamAV is *not* in that list any more. It runs in the sandbox worker
(`tasks._scan_pending_upload`), gated on `pending_scan` - the request no longer
blocks on a clamd round-trip, and the row is invisible to everyone but its
uploader until the scan clears it. The four paths that still scan
synchronously (avatars, marker icons, achievement art) store their file on
something that is not an `Image` row, so there is no `pending_scan` to gate it
with; the upload is held unserved until the sandbox worker re-encodes it
instead (see "Stored photo normalisation"); a test pins that split
(`tests/hypothesis/test_async_malware_scan.py`).

For `Image` rows fetched by our server from a public external host (Media
gallery imports from Wikimedia/Smithsonian/LOC/etc., Google Street
View/Satellite - see `malware_scan.VIRUSTOTAL_ELIGIBLE_SOURCES`),
`tasks._scan_pending_upload` tries a VirusTotal hash lookup first
(`services/security/virustotal_scan.py`) and only falls back to the ClamAV
scan above when VirusTotal has no definitive verdict (unconfigured, unknown
hash, quota exhausted, any error). A user's own upload, and a user's own
connected photo library (Immich/Flickr/Google Photos, imported with their own
credentials) never take this path - VirusTotal shares every file it is shown
industry-wide, which is only acceptable for content that was already public
before we fetched it.

Anything that opens the file with a real parser is marked
`@untrusted_parse` (`services/sandbox/guard.py`) and cannot run here.

`media/metadata_strip.py`'s byte-walk JPEG/PNG/WebP segment stripper used to
run at this step instead of `pending_scan` - see its module docstring. Still
present, still decode-free, but currently unused: `pending_scan` closes the
same "raw file briefly servable" window through access control, which also
covers every format the byte-walker didn't (HEIC, TIFF, GIF, ...).

### 2. The sandbox worker

`media-worker` in `docker-compose.yml` drains the `sandbox` queue and does all
the decoding: malware scan, EXIF read, re-encode, downscale, thumbnails,
transcode, document conversion, OCR.

`media-worker-batch` is the same container with the same isolation (both merge
the `x-sandbox-worker` anchor, so the hardening cannot drift between them),
draining `sandbox_batch` instead. The split is about duration, not trust: a
data import walks a 500MB archive for up to `CELERY_TASK_TIME_LIMIT`, and
sharing `media-worker`'s two slots with it meant two concurrent imports could
stall every upload on the site for an hour.

This is also where `Image.pending_scan` gets cleared -
`tasks.process_image_upload`, once it has scanned the file, read its EXIF and
produced a stripped/downscaled (or transcoded, or converted) copy. Until then,
`services/media/access.py::authorize_image` and `ImageQuerySet.visible_to`
restrict the row to its uploader; everyone else gets the same "not found" a
deleted file would produce. `SafetyContactPhotoView`, the one serving surface
that does not go through `authorize_image`, filters `pending_scan=False`
itself. See `Image.pending_scan` and the resolved entries in
`docs/PROBLEMS.md`.

Every media type gets that gate, not just photos: a video or document is stored
raw too, and its window is the *longer* one (an ffmpeg transcode runs for
minutes where a photo downscale runs for seconds). So do rows created from
provider bytes rather than from a request - the four import tasks and
`media_materialize.materialize_media_item`.

A stored file that cannot be opened at all is not treated as "safe to publish
unprocessed" - nothing has ever validated it, so a still-pending row that fails
this way retries a few times, then is deleted outright
(`tasks._reject_image_upload`) rather than served raw. Clearing `pending_scan`
on that path instead - "give up and fall back to visible" - was the actual
first implementation, and was wrong: it degraded straight through the leak
this whole mechanism exists to prevent. Kept as a cautionary example in
`docs/PROBLEMS.md`'s resolved entry.

What makes it a sandbox, in the order that matters:

1. **`networks: [sandbox_network]` and nothing else.** That network is
   `internal: true`, so there is no default route - verified: a container
   attached only to it cannot reach the internet, while siblings that also
   joined (db, valkey, clamav) stay reachable by alias. This is the
   load-bearing line: an exploited decoder has nothing to exfiltrate to and no
   second stage to fetch.
2. **`x-sandbox-env` instead of `x-app-env`** - no third-party API keys, no
   OAuth secrets, no mail credentials. Nothing to steal, nothing to bill.
3. **`cap_drop: ALL` + `no-new-privileges`**, plus an explicit
   `user: "1001:1001"`. The explicit uid is not decoration: `cap_drop: ALL`
   removes `CAP_SETUID`, so the image's entrypoint cannot `gosu appuser`, and
   it removes `CAP_CHOWN`, so the entrypoint's volume-ownership fixup cannot
   run either. Both are handled - the chowns are best-effort and the `gosu` is
   skipped when already unprivileged - but before that the container simply
   crash-looped on `Operation not permitted` and never started celery at all.
   `depends_on: app` orders these workers after the one container that *can*
   chown the shared volumes.
4. **`pids_limit` and tight cpu/mem**, set below `celery-worker`'s, so a
   decompression bomb or a runaway helper starves this container first.
5. **`/tmp` on `noexec,nosuid` tmpfs** - the one directory it writes freely is
   one it cannot execute from.

**Residual risk, stated plainly:** it still holds database credentials and the
field-encryption key, because the processing tasks write their results back to
the row they were handed. Removing that would mean splitting every task into a
pure parse plus an ORM write, which is a much larger refactor; it is the next
thing to do if this tier is ever hardened further.

### 3. Normalisation

`downscale_stored_image` re-encodes every photo, whatever its format and whatever
it carries, under the uploader's size and format policy (`get_stored_photo_policy`:
plan, subscription and the user's own cap; WebP unless exempt). Only the pixels and
the ICC colour profile reach the new file - `pixels_only` also drops what Pillow's
writers would otherwise copy across (JPEG and GIF comments, TIFF XMP/IPTC tags) - so
a stored photo can be served without a metadata check of its own. EXIF and embedded
keywords are read first and kept on the row. Animated GIF, PNG and WebP keep their
frames; HEIF, MPO and anything else Pillow opens are transcoded. A pending upload
that cannot be re-encoded is retried and then removed, never published as uploaded.
`tests/hypothesis/test_every_stored_photo_is_reencoded.py` plants a marker in every
carrier and format and searches every output for it.

**Waiting for storage.** The upload is already in media storage, so a failure that
outlasts a task's own retries (about 30 minutes) needs nothing from its owner: the
upload stays held or pending and gets an `UploadRetry` row, and
`services/media/upload_retry.py`'s `retry_waiting_uploads` (beat, every 5 minutes,
maintenance queue) queues it again. The first retry comes 5 minutes after it starts
waiting; each wait after that is twice as long, up to a day. One run queues at most 20. While the latest storage failure
is newer than the latest success, only one upload per run probes storage. A task for an
upload that is already waiting skips its own quick retries, so the retry sweep alone
sets the pace. A file storage says is gone (`FileNotFoundError`, or S3 `NoSuchKey`/404) is
retried once a day. It is given up on only after storage has said so for 7 days and served
another upload since, because storage pointed at the wrong bucket or prefix also reports
every file gone. An upload waiting over a day, with storage serving other uploads since it
last failed, is reported once to the admins (`NotificationEvent.UPLOAD_STUCK`, routed by
`notify_stuck_uploads_*`), since that suggests a failure retrying cannot fix. The admin's
Upload retries list has a Give up action, which drops a held upload or rejects a comment.
`adopt_stalled_comment_scans` (hourly) gives a pending comment whose scan was never
queued an `UploadRetry` row.

Comment and trip comment images, custom icons (label, pin, achievement) and avatars
from every writer go through the same encoder (`images.reencode_image_file`), under a
random name rather than the uploaded one. A comment image is re-encoded by
`stored_field.reencode_stored_field` before its `pending_scan` clears, in the same
update; one that cannot be decoded is rejected, and one storage cannot read or
write (any of `held_upload.STORAGE_ERRORS`) is retried, then left pending to wait for
storage (below). The image is read before the malware scan, so storage refusing that read is
not mistaken for the scanner being down; a scanner that stays down still rejects.

An icon or avatar cannot be hidden by a flag on its row, because the media gate
serves any icon or avatar path to every member, so the upload is held instead.
`services/media/held_upload.py` stores it under `unprocessed/`, which has no
authorizer, and names it in the row's `<field>_upload` column. `publish_held_upload`
writes the re-encoded file (WebP, at most 256px for an icon and 512px for an avatar)
into the field and clears the column in one conditional update, so the field only
ever names a file this server encoded and nothing that renders it needs a check. An
upload replaced, cleared or superseded (an emoji avatar, a removed avatar) before the
worker runs is never published; one that cannot be decoded is dropped and the field
keeps what it showed; one storage cannot read or write (an OSError, or on the S3 backend a connection or client error, or a download that
failed every attempt or its checksum; a misconfigured client, such as missing credentials, fails instead) is
retried, then waits for storage. A replaced
avatar or achievement icon is deleted; a replaced label or pin icon is left to
`sweep_unnamed_files`, and undo queues a held upload again. A publish lands
while other requests hold the row in memory, so `models/abstract/held_upload.py`'s
`HeldUploadModel` leaves the field and its `_upload` column out of a full `save()`
unless that instance changed them (a row saved with no primary key is still inserted
whole); otherwise a settings form or the external API's profile PATCH would write
back an empty field and a held name whose file is gone. `sweep_held_uploads` (beat,
hourly) queues the publish again for an upload held longer than 15 minutes, since a
failed enqueue would otherwise leave it "processing" for ever. It leaves an upload that is
waiting for storage to the retry sweep. An upload whose file is gone starts waiting there
instead of being dropped. It drops one whose
publish has started three times without finishing, so a file that kills the worker is
not fed to it every hour (a start that failed to write to storage and was handed to a
retry finished, and is not counted, and one still running, for as long as the task's
hard time limit allows, is left alone; a duplicate publish that finds one running
returns without starting, unless it is a redelivery of that same task, which takes the
mark back, so starts overlap when a broker connection loss hands a running publish
back, or while the cache is down, when a publish goes ahead unmarked and the
sweep never drops); counting starts rather than queues means a sandbox queue
backed up behind a bulk import costs nobody their upload. A file storage cannot stat
is skipped rather than ending the sweep, and a partial index on each `_upload` column
means finding the held rows never reads the pin table. It also removes `unprocessed/` files no row names once they are
older than the undo window, which is how long a deleted label or pin can still come
back with its held upload.
`sweep_unnamed_files` (`services/media/stored_field.py`, beat, hourly) deletes files in `avatars/`, the icon
directories and `comment_images/` that no file field storing in that directory names, once they are older than the
Celery hard time limit (a file saved before the row naming it commits) and no undo record inside the retention window
mentions them. A replaced label or pin icon, a file storage refused to delete after a swap, and one a row deleted
outside undo left all end there; a file it cannot list, stat or delete waits for the next run.
The owner's page says the avatar is processing, and the external API's profile
detail carries `avatar_pending` for the caller's own profile.
`test_every_icon_and_avatar_is_hidden_until_reencoded.py` fails on any stored file
carrying the fixture marker that another member could be served before the worker
runs; `test_every_stored_user_image_is_reencoded.py` and
`test_held_icon_and_avatar_uploads.py` cover the encoder and the wait.

Photos and map overlay images restored by a data import are `pending_scan` rows
handed to `process_image_upload`, like any upload. The one-off
`strip_exif_from_stored_photos` backfill re-encodes images stored before these
changes, after the photos, skipping only avatars generated from the emoji picker
(`avatar.GENERATED_AVATAR_PATTERN`).
The practical effect is the one that matters here: what gets served is bytes this
server's encoder wrote, not bytes the uploader sent. A disguised non-image
fails to decode; data appended after the end-of-image marker does not survive
re-encoding; polyglot tricks stop working because the container is rebuilt.

Video goes through ffmpeg for the same reason, and always has its container
location tags stripped.

The rewrite lands under a *new* name whenever the extension changes, and the
superseded file is **not** deleted by the function that replaced it. It returns
a `StoredFileReplacement` naming it, and the caller deletes it
(`discard_superseded_file`) only after persisting `image.image.name`. The
ordering is load-bearing: `services.media.access` authorizes a media request by
looking the path up on the row, so between an early delete and the row update
every request for that path is authorized and then missing - which for the whole
of a processing run is the uploader's own just-uploaded tile. Deleting late
leaks one file if the process dies in between; deleting early left a row that
permanently named a file which no longer existed.

Listings never name a pending upload's file at all, since a late delete is still a delete. While
`pending_scan` is set, `Image.file_url` is None and `display_url`/`thumb_url`/`marker_thumb_url` are
empty, whatever the media type (a document's `.txt` is replaced by its `.pdf` the same way).
`ImageQuerySet.servable()` drops such rows. Code that reads `image.image.url` directly bypasses this.

Where the owner sees an upload, it is a "Processing…" placeholder (`partials/ui/_processing_thumb.html`,
or `processingPlaceholder` in `shared/photo-processing.ts`) that polls `vault.photos.processing` and
swaps the file in once it settles:

- Vault Photos grid and organize queue, Vault Documents grid, Vault home recent strip
- pin/wiki/check-in gallery panel and album grids
- the pin page Media card's "My Photos" tiles
- the home page's recent-photos widget
- direct messages: the composer's attachment chip and the thread's bubbles, for the sender and for a
  recipient the message shows photos to (consented, or revealed once); `vault.photos.processing`
  answers that recipient with the file URLs only
- the manage-overlays photo picker, where a pending photo cannot be picked until it settles (the
  server refuses a pending `image_id` too)
- the comment "Choose Existing" picker, likewise not pickable until it settles
- tiles with no script of their own, which opt into a page-wide poll with `data-processing-auto`
  (`partials/ui/_processing_tile_attrs.html`, `watchAutoProcessingTiles`): the profile photo strip,
  the pin-share and wiki-share dialogs, the visit form and visit history, the pin-suggestion card

The pin-share detail page is the recipient's, who can neither fetch nor poll the photo, so its
placeholder stays still.

Comment and trip-comment images are replaced the same way (`stored_field.reencode_stored_field` names
the re-encode anew and deletes the upload), so their author sees the same placeholder, polled through
`comments.images.processing` and `comments.trip_images.processing`. "Choose Existing" copies the picked
photo's stored file onto the comment and skips the scan, so it refuses a pending photo: its file is the
raw upload, never scanned and with its metadata.

Content that embeds an image by URL cannot follow a rename, so it links to the row instead:
`media.image` (`/media/image/<uuid>/`, `controllers.media.StableImageView`) redirects to whatever file
the row names now, authorized by `authorize_media` for that file, with `private, no-cache`. While the
upload is pending its uploader gets a `no-store` placeholder SVG and everyone else a 404. The article
editor's inline-image upload answers with this link, polls, and requests the link again once the photo
settles.

Elsewhere a pending row is left out or named without a file:

- Left out: map photo layers (pin and wiki `gallery.json`, album maps, memories), the pin's popup
  fallback photo, the wiki Media card's votable "Photos" tiles (votes are keyed by file URL), and the
  pin/wiki cover and floorplan photo pickers. The pin gallery adds a map marker when a tile settles.
- No file: the external API's photo, safety-photo and wiki-gallery rows carry `processing` and
  `processing_failed`, with `url` null until the file is ready; its pin/wiki and trip comment rows carry
  `image_processing`, with `image_url` null. The Vault home names a pending video without linking it.

An image overlay is the exception. `MapImageOverlay.source_url` names the stored file while it is
pending, because the upload-an-overlay flow opens the aligner on it at once and a placeholder there
could not be aligned. Its JSON also carries `image_link`, the photo's stable link: the map retries a
failed overlay image through it once (`followRenamedOverlayImage`), and the manage dialog's thumbnail
uses the link while the photo is pending.

P142 and P58 (archived) have the history.

### 3b. Derived copies

Three smaller copies are written from the original, all in the sandbox worker,
all under `@untrusted_parse`, all stored as ordinary `ImageField`s in the
`pin_images` media family - so the media origin serves them under the same
authorization, caching and header rules as the full-size file:

| field | size | format | for |
|---|---|---|---|
| `thumbnail` | 400px | WebP | grid display |
| `marker_thumbnail` | 88px | WebP | map markers |
| `analysis_thumbnail` | 512px | JPEG | vision models and the image classifier |

`analysis_thumbnail` is the one with a security reason to exist rather than a
display one. Photo keywording runs on the *ordinary* Celery worker -
`generate_image_keywords` declares no queue, and legitimately so: it makes a
network call to a provider, which is not work for a sandbox container. But it
used to build its 512px copy by calling Pillow on the stored upload, which put
an image decoder in the process holding REData, OAuth and database credentials
on a network with full egress. That is precisely the foothold this whole
pipeline exists to deny.

So the decode moved here, and the consumer
(`services.photos.photo_keywords.analysis_jpeg_bytes`) now only *reads* bytes.
A photo with no analysis copy is **skipped**, never decoded on demand - the
fallback would silently reintroduce the hole. `backfill_image_analysis_thumbnails`
writes the missing copy in the sandbox and re-enqueues keywording, which is
also how the library is backfilled for photos uploaded before the field
existed. `test_photo_keyword_sandboxing.py` asserts the property transitively:
every *task* that can reach the decode, through any depth of helper, must
declare `queue=SANDBOX_QUEUE`.

It is a separate file rather than a reuse of `thumbnail` because the two are
answerable to different things. The grid thumbnail is WebP at 400px because
that is right for the UI, and the UI must stay free to change it; Cloudflare
Workers AI - the default vision provider, and the only source of the
classifier - takes a bare byte array with no format negotiation, and JPEG is
the one encoding every provider accepts.

### 4. The media origin

Uploads are served from `media.urbanlens.org`, an origin with no session
cookie, so anything that does slip through executes where the same-origin
policy makes it useless: it cannot read the app's DOM, call its API as the
user, or see its cookies.

- `media-nginx` is a separate container publishing `UL_MEDIA_PORT`. Its config
  (`config/nginx/media.conf`) contains no route to an application page, so no
  Host header turns that hostname into a second front door.
- `MEDIA_URL` becomes absolute, which is what moves every existing `.url` call
  site - templates, serializers, external API - onto the new origin without
  touching any of them.
- Authorization is unchanged: every byte still goes through `MediaGateView` and
  the default-deny policy table in `services/media/access.py`.
- Authentication is a separate `ul_media` cookie - signed, `HttpOnly`, carrying
  one user id, scoped to the deepest domain both hosts share, minted and
  refreshed by `MediaOriginCookieMiddleware`. See
  `services/media/origin.py` for why this rather than signed URLs.
- Responses on that origin get `default-src 'none'` plus a `frame-ancestors`
  naming the app origin (replacing `X-Frame-Options`, which would block the
  Vault document lightbox now that it frames another origin), `nosniff`, and
  `Referrer-Policy: no-referrer`.

### Proxied third-party bytes

Some views relay bytes from elsewhere on the *app* origin: the REData proxies
in `controllers/pin.py`, the Immich thumbnails (`controllers/immich.py`,
`controllers/pin_suggestions.py`), Google Photos previews
(`controllers/google_photos.py`) and Places photos
(`controllers/media_proxy.py`). None of those bytes were normalised, and the
declared type is the upstream's word. Every one of them answers through
`services/media/proxied_media.proxied_media_response`, which serves only
allow-listed raster, video and PDF types inline, under `default-src 'none'`
and `nosniff`, and anything else as an `application/octet-stream` attachment.
The tile proxies allow-list separately (`gateway.servable_tile_type`), and a
`media_preview` response is always this app's own JPEG/PNG render.
Tests: `test_redata_media_proxy_serves_no_documents.py`,
`test_login_gated_media_proxies_serve_no_documents.py`.

## Deploying it

```bash
# .env
UL_MEDIA_PORT=21801                              # media-nginx publishes here
UL_MEDIA_BASE_URL=https://media.urbanlens.org    # what Django builds URLs against
UL_ALLOWED_HOSTS=urbanlens.org,media.urbanlens.org
UL_SANDBOX_ENABLED=true
UL_UNTRUSTED_PARSE_POLICY=warn                   # then deny, see below
```

Then point NGINX Proxy Manager's `media.urbanlens.org` host at
`<host>:${UL_MEDIA_PORT}`, with TLS and websockets off (this vhost serves only
file bytes).

`UL_MEDIA_BASE_URL` must be left empty until the vhost actually resolves -
setting it rewrites every media URL on the site.

### Where the bytes are stored

`UL_MEDIA_STORAGE_BACKEND` is `filesystem` (the default, and what a
single-machine deployment wants) or `s3` for any S3-compatible object store.
The switch changes nothing about *who may read a file* — `FileField.url` keeps
returning `/media/...` and every read still passes `MediaGateView`. That is the
point of `services/media/object_storage.GatedS3Storage`, and it is enforced by
`manage.py check`, not left to convention: plain `S3Storage.url` returns a
presigned bucket URL, which would take every media read out from behind the
gate with no code change and no test failure.

```bash
UL_MEDIA_STORAGE_BACKEND=s3
UL_S3_ENDPOINT_URL=http://garage-s3.garage.svc.cluster.local:3900
UL_S3_BUCKET_NAME=ul-media
UL_S3_ACCESS_KEY_ID=...
UL_S3_SECRET_ACCESS_KEY=...
UL_S3_ADDRESSING_STYLE=path            # Garage and most self-hosted stores
# Optional: an internal-only nginx location that proxies the store, so media
# bytes never pass through a gevent worker. Unset streams through Django.
UL_MEDIA_X_ACCEL_OBJECT_PREFIX=/_object_media/
```

Three things a deployment has to know:

- **The media volume does not go away.** `exports/`, `imports/` and
  `preview_sources/` under `MEDIA_ROOT` are reached with `os.path` rather than
  through the storage API. `preview_sources/` is deliberate — see
  [Media previews](#media-previews); it is how the sandbox worker gets a 60 MB
  file without putting it through Valkey. `manage.py check` warns and names all
  three whenever the s3 backend is selected.
- **botocore rejects a hostname containing an underscore.** A container named
  `urbanlens_garage` cannot be an endpoint host.
- **Building an export containing photos is not ported yet.**
  `services/import_export/export.py` uses `FileField.path` twice, which raises
  on a non-filesystem storage.

`docs/designs/media-object-storage.md` has the decision and why presigned URLs
to the client were refused.

### How big an upload may be

Three limits, and an ingress cap that lowers all of them.
`SiteSettings.max_upload_file_size_mb` (250 MB default) governs photos, videos
and documents; the data-file import form and the export-archive import view
each cap at 500 MB. `UL_MAX_REQUEST_BODY_MB` states what the proxy in front of
the deployment will actually pass, and
`services/media/storage.cap_to_ingress` lowers each of the three to it.

That exists so the *user* finds out. A body the proxy rejects is answered by
the proxy: no view runs, nothing is logged here, and the uploader watches an
upload fail into somebody else's error page after sending the whole cap.
`max_upload_file_size_bytes()` feeds both the server-side check and the
`data-max-file-size` the vault upload widget pre-checks against, so an
oversized file is refused in the browser before any bytes are sent. Set it to
`100` behind Cloudflare's free or pro tier.

### The parse policy

`UL_UNTRUSTED_PARSE_POLICY` decides what a non-sandbox process does when it is
about to parse an upload:

- `warn` (default) logs and proceeds. Run here first and read the logs: every
  line names an operation still happening in the wrong container.
- `deny` raises `UnsandboxedParseError`. The target state.
- `allow` disables the check. What the test settings use, because the suite
  calls the parsers directly.

The import preview, which this section used to name as the one thing left before
`deny`, parses in `parse_import_preview_task` now and finishes its lookups on an
interactive worker (P2). The earlier moves stand: `prepare_photo_upload` no longer
decodes, the two `render_preview` callers go through `tasks.render_media_preview`,
enrichment photos go through `process_image_upload`, and the one legitimate
exemption (`strip_exif_from_stored_photos`, a backfill over already-scanned files) is
written down as an `allow_untrusted_parse` block rather than left implicit. What
`warn` cannot show is an undecorated parser - P116 and P117 were the two known, both fixed.
Tracked in `docs/PROBLEMS.md`.

## Media previews

A gallery tile for something a browser cannot render (a PDF, a TIFF, a HEIC)
is a server-side render, and `render_preview` reaches Pillow and poppler - so
it runs on the sandbox queue like every other decode, not in the view.

Both endpoints (`controllers/media_preview.MediaPreviewView` for a signed
remote URL, `controllers/pin.RedataMediaProxyMixin` for an in-app proxy route)
follow the same three steps:

1. Serve it if `previews.cached_preview` has it.
2. Otherwise stage the source (`previews.stage_preview_source`) and call
   `previews.request_sandbox_render`, which queues `tasks.render_media_preview`
   at most once per key (`cache.add` of a `RENDER_QUEUED` marker).
3. Answer **404** either way.

The source travels on the **media volume**, not through the broker and not
through the cache - only a small `{name, content_type}` descriptor goes in the
cache. The cap is 60MB (`MAX_PREVIEW_SOURCE_BYTES`), and Valkey is a single
512MB instance shared with the Celery broker, sessions and Channels: one gallery
page of large scanned PDFs would evict all of it under `volatile-lru`, including
the staged sources themselves. Staged files live under
`MEDIA_ROOT/preview_sources/`, which nothing serves - every media URL resolves
through an `Image` row and these have none, so `authorize_media` refuses the
path family outright. `render_media_preview` deletes its own source;
`sweep_stale_preview_sources` (hourly) clears orphans from a failed enqueue.

The descriptor outlives `RENDER_QUEUED` deliberately (30 min vs 2 min), so when
the marker expires and the next request re-queues, the source is still on disk
and is not re-downloaded.

That 404 is the part worth understanding. The endpoint used to block until the
render finished, and keeping that would have meant waiting on a Celery result
inside a web request - one pinned worker per tile, twenty tiles per gallery
page, for as long as `media-worker` is behind. So a miss returns 404, the
gallery's `onerror` (`urbanlensMediaThumbFallback` in `themes/base.html`) shows
its icon tile, and that handler retries a preview URL twice (2s, 4s, with a
cache-busting `_r=` because the browser has already negatively cached the
first URL) before settling on the icon. A tile that misses all three still
fills in on the next page load.

## Files nothing points at any more

Django stopped deleting `FileField` files on delete in 1.3, deliberately: a
rolled-back transaction would otherwise leave a row pointing at a file that is
gone. Nothing replaced it, so two ordinary actions stranded one every time -
replacing an icon or avatar with a new upload, and deleting the row that named
it.

`services/media/file_cleanup.py` is the rule now, as `pre_save`/`post_save` and
`post_delete` receivers over `Achievement.custom_icon` and `Profile.avatar`.
Receivers rather than per-caller deletes because one rule in one place is the
shape that cannot be forgotten - and connected *per sender*, because a
sender-less receiver makes every model in the project report listeners, which
disables Django's fast-delete path repo-wide.

Three boundaries, each of which the obvious version gets wrong:

- **`Pin.custom_icon` and `Label.custom_icon` are deliberately excluded.** The
  undo framework stashes them as a stored *name* rather than as bytes
  (`services/undo/handlers/`), so unlinking on delete or replace would leave an
  undo within its window restoring a row that names a file no longer there - a
  broken icon with nothing to explain it. Those want the unlink deferred to when
  the `UndoAction` is pruned, which is a different mechanism rather than a
  longer list. A held upload (`<field>_upload`) follows the same split: deleted
  with an achievement or profile, kept with a deleted label or pin, and deleted
  by account deletion for all four.
- **`Image`'s columns are not managed here.** `services/media/images.py`'s
  `delete_stored_file` handles `image` and its three derived files, and knows
  when two rows legitimately share a file; this module does not. Its own
  `post_delete` receiver, `models/images/signals.py::remove_stored_file`, runs
  it for every deleted row, so nothing should unlink a photo's file except
  through `delete_stored_file` - account deletion did, and broke every copy it
  had shared.
- **Every unlink waits for the commit.** `post_save`/`post_delete` fire *inside*
  the transaction, so deleting there would survive a rollback that put the row
  back. `transaction.on_commit` runs it only once the write lands.

Historical orphans predating this are not swept. For pin and label icons that is
a disk cost only: `authorize_pin_icon` and `authorize_label_icon` serve a file
only to a viewer who owns (or, for a global label, can see) a row that names it,
so an orphan matches nothing and is refused. `authorize_icon`
(`achievement_icons/`) and `authorize_avatar` are unconditional, deliberately -
an award or an avatar renders on its owner's profile site-wide - so an orphaned
achievement icon or avatar is still fetchable by any authenticated user. A
one-time sweep against surviving rows closes the disk half for every family.

## Adding a parser

The whole extension point is two lines:

1. Decorate the entry point with `@untrusted_parse("family.operation")`.
2. Route whatever task calls it with `queue=SANDBOX_QUEUE` - or
   `SANDBOX_BATCH_QUEUE` if the parse runs for minutes rather than for a
   moment - and add its name to
   `EXPECTED_SANDBOX_TASKS` in `tests/hypothesis/test_sandbox_isolation.py` so
   the change of blast radius is a line in a diff.

Declare the queue on the task (`@shared_task(..., queue=SANDBOX_QUEUE)`), never
at the `apply_async` call site - one missed call site is one untrusted parse
back on the unrestricted worker, and there is nothing to notice it.

## AI inference

The assistant's tool loop follows the same shape as `media-worker` but for
the opposite reason: not "this container must not be trusted", but "this
workload holds provider credentials that must not sit next to REData/OAuth
credentials, and must not reach the internet except through an allowlisted
proxy". `Queue.AI` (`services/sandbox/queues.py`) is drained by `ai-worker`,
isolated on `ai_network`/`inference_network`; model calls leave that worker
over HTTP to a separate Django-free `ai-inference` service, which is the only
container holding provider API keys. See `docs/AI_PIPELINE.md` for the full
architecture.
