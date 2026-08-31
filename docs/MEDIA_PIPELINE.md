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

| Parser | Reached by | Runs in |
|---|---|---|
| Pillow (+ pillow-heif) | photos | sandbox |
| ffmpeg / ffprobe | video | sandbox |
| LibreOffice (`soffice`) | doc/spreadsheet conversion | sandbox |
| poppler + tesseract | PDF text/OCR | sandbox |
| `zipfile` / `tarfile` | data import, KMZ, archives | sandbox |
| lxml / fastkml / gpxpy | KML, GPX, OSM XML | sandbox |
| GDAL / GeoPandas / Shapely | shapefile, WKT/WKB | sandbox |
| clamd | everything | its own container |

## The tiers

### 1. The request

Fast, local, and *never* a decode. In order:

1. Size ceiling (`storage.max_upload_file_size_bytes`).
2. Extension allowlist for photos - SVG is refused here, because it has no
   magic bytes and sniffing would pass it (`security/content_sniffing.py`).
3. Magic-byte sniff against the declared kind.
4. ClamAV, still synchronous here (see the ClamAV entry in `docs/PROBLEMS.md` -
   the one piece of this tier not yet moved off the request).
5. For a photo: store the raw upload untouched and mark the row
   `pending_scan` (`services/media/images.prepare_photo_upload`). Nothing here
   decodes it - a decode is exactly the class of code the sandbox tier exists
   to keep out of this process.

Anything that opens the file with a real parser is marked
`@untrusted_parse` (`services/sandbox/guard.py`) and cannot run here.

`media/metadata_strip.py`'s byte-walk JPEG/PNG/WebP segment stripper used to
run at this step instead of `pending_scan` - see its module docstring. Still
present, still decode-free, but currently unused: `pending_scan` closes the
same "raw file briefly servable" window through access control, which also
covers every format the byte-walker didn't (HEIC, TIFF, GIF, ...).

### 2. The sandbox worker

`media-worker` in `docker-compose.yml` drains the `sandbox` queue and does all
the decoding: EXIF read, re-encode, downscale, thumbnails, transcode, document
conversion, OCR, archive extraction.

For a photo, this is also where `Image.pending_scan` gets cleared -
`tasks.process_image_upload`, after it has read the file's EXIF and produced a
stripped/downscaled copy. Until then, `services/media/access.py::authorize_image`
and `ImageQuerySet.visible_to` restrict the row to its uploader; everyone else
gets the same "not found" a deleted file would produce. See `Image.pending_scan`
and the resolved entry in `docs/PROBLEMS.md` for the full reasoning.

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
3. **`cap_drop: ALL` + `no-new-privileges`**, on top of the image's non-root
   `appuser`.
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

Not yet at `deny`: `prepare_photo_upload` still reads EXIF in the request, and
`parse_for_preview` still extracts archives and documents there. Both are
tracked in `docs/PROBLEMS.md`.

## Adding a parser

The whole extension point is two lines:

1. Decorate the entry point with `@untrusted_parse("family.operation")`.
2. Route whatever task calls it with `queue=SANDBOX_QUEUE`, and add its name to
   `EXPECTED_SANDBOX_TASKS` in `tests/hypothesis/test_sandbox_isolation.py` so
   the change of blast radius is a line in a diff.

Declare the queue on the task (`@shared_task(..., queue=SANDBOX_QUEUE)`), never
at the `apply_async` call site - one missed call site is one untrusted parse
back on the unrestricted worker, and there is nothing to notice it.

## AI inference (not yet deployed)

`Queue.AI_INFERENCE` exists in `services/sandbox/queues.py` with nothing routed
to it. The split follows the same shape as `media-worker` but for the opposite
reason: not "this container must not be trusted", but "this workload wants its
own resource envelope, and possibly a GPU host". Concretely that means a new
compose service draining `-Q ai_inference`, an `x-ai-env` anchor carrying only
the model-provider credentials, and `queue=Queue.AI_INFERENCE` on the AI tasks.
It stays on `app_network` - inference calls out to model APIs, so it needs
egress and cannot use `sandbox_network`.
