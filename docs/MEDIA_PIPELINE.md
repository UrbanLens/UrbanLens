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
| ffmpeg / ffprobe | video | sandbox | yes |
| LibreOffice (`soffice`) | doc/spreadsheet conversion | sandbox | yes |
| poppler + tesseract | PDF text/OCR, preview render | sandbox | yes |
| `zipfile` / `tarfile` | data import (routed), **import preview (request)** | mixed | yes |
| python-docx | AI document import, **import preview (request)** | mixed | yes |
| lxml / fastkml / gpxpy | KML, GPX, OSM XML | routed via `run_user_data_import` | yes |
| GDAL / GeoPandas / Shapely | shapefile, WKT/WKB | routed via `run_user_data_import` | yes |
| clamd | everything, except VirusTotal-eligible fetched assets (see below) | sandbox (its own container for the daemon) | n/a |

Every parser is now guarded, so `warn` logs a complete worklist rather than a
partial one. Two rows still say **request**: `controllers/pin.parse_for_preview`
is the last thing standing between here and `UL_UNTRUSTED_PARSE_POLICY=deny`.
See `docs/PROBLEMS.md`.

## The tiers

### 1. The request

Fast, local, and *never* a decode. In order:

1. Size ceiling (`storage.max_upload_file_size_bytes`).
2. Extension allowlist for photos - SVG is refused here, because it has no
   magic bytes and sniffing would pass it (`security/content_sniffing.py`).
3. Magic-byte sniff against the declared kind.
4. Store the raw upload untouched and mark the row `pending_scan`. Nothing
   here decodes it - a decode is exactly the class of code the sandbox tier
   exists to keep out of this process.

ClamAV is *not* in that list any more. It runs in the sandbox worker
(`tasks._scan_pending_upload`), gated on `pending_scan` - the request no longer
blocks on a clamd round-trip, and the row is invisible to everyone but its
uploader until the scan clears it. The four paths that still scan
synchronously (avatars, marker icons, achievement art) store their file on
something that is not an `Image` row, so no task will ever run over it and
there is no `pending_scan` to gate it with; a test pins that split
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

`downscale_stored_image` is called unconditionally for every photo, even where
the uploader's plan applies no size cap and no WebP conversion, because it is
also what removes EXIF and what transcodes formats browsers cannot render. The
practical effect is the one that matters here: what gets served is bytes this
server's encoder wrote, not bytes the uploader sent. A disguised non-image
fails to decode; data appended after the end-of-image marker does not survive
re-encoding; polyglot tricks stop working because the container is rebuilt.

Video goes through ffmpeg for the same reason, and always has its container
location tags stripped.

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

### The parse policy

`UL_UNTRUSTED_PARSE_POLICY` decides what a non-sandbox process does when it is
about to parse an upload:

- `warn` (default) logs and proceeds. Run here first and read the logs: every
  line names an operation still happening in the wrong container.
- `deny` raises `UnsandboxedParseError`. The target state.
- `allow` disables the check. What the test settings use, because the suite
  calls the parsers directly.

One thing left before `deny`: `controllers/pin.parse_for_preview` still parses
archives, KML/GPX/shapefiles and `.docx` inside the request. Everything else has
moved - `prepare_photo_upload` no longer decodes, the two `render_preview`
callers go through `tasks.render_media_preview`, enrichment photos go through
`process_image_upload`, and the one legitimate exemption
(`strip_exif_from_stored_photos`, a backfill over already-scanned files) is
written down as an `allow_untrusted_parse` block rather than left implicit.
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
