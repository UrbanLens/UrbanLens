# Reply: /static/, media on Garage, and the 100 MB upload cap

- **Status: SENT, 2026-09-06.** Answers both of the `infrastructure` repo's
  open outbound handoffs. Static assets and the S3 backend are built and
  merged on `release/v_0_8_0`; the upload protocol is decided and deferred.
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N8` · `status: current`

**For whoever owns `UrbanLens/infrastructure`, not this repo.** Written
2026-09-06, against `release/v_0_8_0`. Answers
`docs/handoffs/static-assets-and-uploads.md` and
`docs/handoffs/urbanlens-app-media-on-garage.md`, both marked OPEN as of
2026-09-05. Nothing in your repository was edited to produce this.

Every claim you made was checked before anything was changed. **All of them
held**, including the ones the audit had gotten wrong, so this reply is short on
corrections and long on what was built.

---

## Part 1: `/static/` — done, and your scope was right

Re-measured here, 2026-09-06, on `release/v_0_8_0`:

| Your claim | Verdict |
|---|---|
| whitenoise is a dependency, and is the staticfiles backend | confirmed |
| `WhiteNoiseMiddleware` absent from `MIDDLEWARE` | confirmed |
| the one line is **not** sufficient | confirmed |
| 62 files tracked under `frontend/static/`, 12,708,690 bytes | confirmed, exactly |
| 166 manifest entries, 51 backslash-separated, 130 admin, 12 resolvable | confirmed, exactly |

All three changes are made.

**Change 1 — the middleware, in your position, not whitenoise's.** Your
ordering argument decided it: WhiteNoise short-circuits in the request phase, so
placing it after `CorsMiddleware` keeps all four security-header layers on a
static response and skips sessions, CSRF, auth, the media-origin cookie and the
preview swap. Measured through the real WSGI stack in the built image rather
than asserted:

```
/static/dashboard/style.9aec26d38360.css
  200  cache-control: max-age=315360000, public, immutable   set-cookie: (none)
  content-type: text/css   cross-origin-resource-policy: same-site
/static/dashboard/style.css
  200  cache-control: max-age=60, public                     set-cookie: (none)
```

The absent `Set-Cookie` is there. So is `Access-Control-Allow-Origin: *`, which
you flagged: `CROSS_ORIGIN_RESOURCE_POLICY` is `same-site` here and is still
applied to static responses, so a cross-*site* embed is refused by CORP and the
media origin (a sibling subdomain) is unaffected.

**One thing to know before you test through the tunnel.** `SecurityMiddleware`
sits above WhiteNoise and `SECURE_SSL_REDIRECT` is on, so a request arriving
without `X-Forwarded-Proto: https` gets a 301 before WhiteNoise ever sees it —
including for `/static/`. Your existing 404s prove cloudflared is setting the
header today, so this should be a non-event; it is written down because it is
the failure mode that would look like "the middleware did not work".

**Change 2 — the assets are in the image.** `sass`, the bun bundle and
`collectstatic` now run in the `Dockerfile`. Your stated risk did not
materialise: `collectstatic` opens no database and no socket, and needs only a
throwaway `DJANGO_SECRET_KEY`, scoped to the `RUN` so it reaches no layer's
environment. Verified twice — once in an isolated container with
`--network none` and a wiped `STATIC_ROOT`, once in the real build:

```
193 static files copied to '/app/src/urbanlens/frontend/static', 4 unmodified, 533 post-processed.
Static manifest verified: 197 entries, all present.
```

`init.py` still runs the same sequence at container start, and that is not
redundancy: docker-compose mounts a **named volume** over `STATIC_ROOT`, and a
volume that already has content is not re-seeded from the image. One definition
(`init.py --frontend-only`), two callers, so the image build and the compose
path cannot drift.

**Change 3 — the manifest.** Fixed as the side effect you predicted: 0
backslash entries after a Linux collect. `STATIC_ROOT` is now untracked and
gitignored in full — 12.7 MB of build output that disagreed with its own
manifest, gone. A new build-time check (`verify_static_manifest`) fails the
build when an entry names a file that is not there **or** carries a backslash;
those are checked separately, because on Linux a backslash is a legal filename
character and an existence test alone passes.

**One extra fix your audit could not have seen, because everything 404'd.** Four
templates wrote a literal `/static/...` URL rather than going through
`{% static %}` - including the 1.5 MB header logo on every authenticated page.
A literal resolves, so it would not have shown up as a failure once the
middleware landed; what it loses is the content hash, so those assets were
served `max-age=60` where their hashed siblings get ten years and `immutable`.
Two of the five assets on `/` were in that state. All four now go through the
tag, and a whole-tree check (`bin/check_static_url_literals.py`, wired into CI)
stops them coming back. Measured after the fix, rendering with nothing in front
of the app:

```
/accounts/login/  4 static refs, 4 resolve, all immutable, none with Set-Cookie
/                 4 static refs, 4 resolve, all immutable, none with Set-Cookie
```

Before the change, your own measurement of `/` was five `/static/` URLs and five
404s totalling 490,790 bytes of 404 HTML per page load.

**Your verification script will now return `197 of 197`, not `166 of 166`.**
The count moved because the bun bundle output is now collected too. Please
assert "0 missing", not a specific total.

The k8s Deployment can keep running `gunicorn` directly. Nothing needs a
sidecar, and `collectstatic` at pod start would still be wrong for the reason
you gave.

---

## Part 2: media on Garage — the app half is built, and the answer is "not presigned"

Confirmed as you measured: `STORAGES["default"]` was hardcoded, and neither
`boto3` nor `django-storages` was installed.

Both are fixed. `UL_MEDIA_STORAGE_BACKEND=s3` now selects an S3 backend;
filesystem stays the default, so nothing about an existing deployment changes
until the variable is set. The full env surface is in `.env-sample`
(`UL_S3_ENDPOINT_URL`, `UL_S3_BUCKET_NAME`, `UL_S3_ACCESS_KEY_ID`,
`UL_S3_SECRET_ACCESS_KEY`, `UL_S3_REGION_NAME`, `UL_S3_ADDRESSING_STYLE`), and
`manage.py check` refuses to start when any of the required ones is missing
rather than failing on one photo, in one request, after deploy.

**The decision you asked for: the gate stays in the data path. No presigned URL
is ever handed to a client.** Recorded as `docs/designs/media-object-storage.md`
in this repo. A presigned URL is a bearer token for one object — no session, no
authorization walk, no revocation, and it outlives the check that produced it,
in `Referer` headers, history, proxy logs and shared links. `docs/GOALS.md`
requires pin data to be unreachable *by construction*, and this project's stated
direction is end-to-end encrypting private photos, which makes the object less
meaningful outside the gate rather than more.

**Your option 3 is what was built, and it is better than it sounded.** You
described it as "`X-Accel-Redirect` to an internal nginx proxy of Garage, at the
cost of an nginx config that knows how to reach Garage" — and the thing that
makes it work is that *Django* signs the request, not nginx. nginx needs no
SigV4 support at all:

1. The gate authenticates, authorizes, then signs a short-lived URL for the
   object (60s; it is consumed by the request that minted it).
2. It returns `X-Accel-Redirect: /_object_media/<bucket>/<key>?X-Amz-...`.
3. nginx fetches that from a **pinned** upstream and strips the header.

**Only the path and query go in that header, never a scheme and host.** An
absolute URL there would turn any bug that can influence a stored path into
SSRF performed by the one process inside the cluster. Because SigV4 covers the
`Host` header, your vhost has to send the host `UL_S3_ENDPOINT_URL` names:

```nginx
location /_object_media/ {
    internal;
    proxy_pass       http://garage-s3.garage.svc.cluster.local:3900/;
    proxy_set_header Host garage-s3.garage.svc.cluster.local:3900;
    proxy_set_header Authorization "";   # the presigned query is the credential
    proxy_hide_header x-amz-request-id;
    proxy_hide_header x-amz-id-2;
}
```

Set `UL_MEDIA_X_ACCEL_OBJECT_PREFIX=/_object_media/` to turn it on. **This is
the one part untested from here** — this repository ships no such vhost, and
this session had no Garage access. What *was* tested is that the signed
path+query, prefixed with the endpoint, returns the exact bytes: so the URL
nginx would fetch is well-formed. Leave the prefix unset and the gate streams
the object through Django instead, which works with nothing in front of the app
and is what a self-host gets.

**Verified end to end against a real S3 API** (MinIO on the app network, no
internet, 2026-09-06 — not against Garage): save, `exists`, `size`, a
`/media/...` URL with no signature in it, streamed bytes byte-identical,
`Cache-Control: private`, the `X-Accel-Redirect` hand-off, the signed URL
returning those bytes, and traversal payloads refused.

Two things that will cost you time otherwise:

- **botocore rejects a hostname containing an underscore**
  (`ValueError: Invalid endpoint`). `garage-s3.garage.svc.cluster.local` is
  fine; a docker container name like `urbanlens_garage` is not.
- `default_acl` must stay null. `manage.py check` errors if it is set: a
  bucket-level grant is the one way to reach a file without passing the gate.

### What did **not** move, and why it matters to you

Three subtrees under `MEDIA_ROOT` are reached with `os.path` rather than through
the storage API, so switching the backend does not move them:
`exports/<job_id>/`, `imports/<job_id>/`, and `preview_sources/`. The last is a
*deliberate* shared filesystem — the sandbox worker reads staged sources off the
same volume so 60 MB files do not go through the 512 MB Valkey that also holds
the broker. `manage.py check` emits a warning naming all three whenever the s3
backend is selected.

**So the media volume cannot be deleted after the cutover.** Sizing it for
scratch rather than for the photo corpus is fine; removing it is not.

Also still open on our side: two `FileField.path` call sites in the export
archive builder. `.path` raises on a non-filesystem storage, so building an
export containing photos needs porting before an s3 deployment does that. Not
fixed here, not tested against s3, tracked in `docs/designs/media-object-storage.md`.

### On your "worse than the eventual state" point

Taken, and it is the right framing: one unbacked 338 MB copy versus two
replicas plus a nightly off-cluster encrypted copy is not a close comparison.
Nothing in this reply argues for or against the migration or its timing — your
ADR-0008 amendment about leg 3's destination snapshots still governs that, and
the application is simply no longer on the list of things blocking it.

---

## Part 3: uploads over 100 MB — decided, partly built

Your three measurements are confirmed (250 MB default with a 900 MB ceiling,
500 MB in the import form, `client_max_body_size 200m`), and there is a fourth:
`controllers/tools.py` accepts a 500 MB export archive on its own path.

**Decided: chunked upload to Django, your option (b), not presigned multipart.**
Recorded as `docs/designs/large-upload-protocol.md`. Not because (a) is worse on
the wire — it is better — but because it needs an object store, and
`docs/GOALS.md` makes a single-machine self-host a supported path rather than an
afterthought; and because a direct browser→bucket `PUT` lands unvalidated bytes
in the bucket, which our media pipeline is built to prevent, so (a) additionally
needs quarantine-validate-promote. The chunk endpoint will be written so the
transport is swappable, which keeps (a) available later as a second answer from
the same endpoint rather than a second client.

Worth stating plainly: **the presigned-URL objection in part 2 does not apply
here.** A presigned *upload* URL grants write to one key and reads nobody's
private data. This is a portability and validation call, not a privacy one — so
if the self-host constraint ever changes, (a) is back on the table on its
merits.

**Built now, because the resumable implementation is not small and the cutover
date should not wait for it:** `UL_MAX_REQUEST_BODY_MB` lowers every one of
those four limits to what the ingress will actually carry. It feeds both the
server-side check and the `data-max-file-size` the upload widget pre-checks
against, so a 150 MB file is refused **in the browser, before any bytes are
sent**, instead of dying into an opaque Cloudflare 413 that we never see. The
site-admin page says the cap is in force rather than silently ignoring the
number an admin typed.

Set `UL_MAX_REQUEST_BODY_MB=100` on any stack behind the proxied tunnel. It is
insurance, not the protocol — nobody uploads a 150 MB video until the chunked
path exists — but it makes the cutover survivable on a date nobody has to
co-ordinate.

---

## What we need from you

1. **Set `UL_MAX_REQUEST_BODY_MB=100`** wherever a stack sits behind the
   proxied tunnel, at the same time as the cutover.
2. **Fix your ingress-watch assertion** — you already tracked this. The hashed
   asset now exists to assert against.
3. **If you want the object-store `X-Accel` path**, add the `location` block
   above and tell us the exact host you will proxy to, since the signature
   covers it. If you would rather not, do nothing: leave
   `UL_MEDIA_X_ACCEL_OBJECT_PREFIX` unset and the gate streams the objects
   itself. Correct either way, slower one way.
4. **Do not delete the media volume** after a media cutover. See part 2.
5. **Nothing else.** Neither open handoff is waiting on this repo any more.

## See also

- `docs/designs/media-object-storage.md` — D6, the read-path decision in full.
- `docs/designs/large-upload-protocol.md` — D7, the upload-protocol decision.
- `docs/MEDIA_PIPELINE.md` — the deployment shape, now including the object-store backend.
