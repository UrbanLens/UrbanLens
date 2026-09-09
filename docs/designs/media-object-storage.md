# D6 — Media on an object store: the gate stays in the data path

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D6` · `status: accepted` · `decided: 2026-09-06`

## The decision

User media may be stored in an S3-compatible object store, and **a media read
is never served by giving the client a URL to that store.** The client always
receives `/media/<key>` and always passes `MediaGateView`.

`UL_MEDIA_STORAGE_BACKEND=s3` selects
`dashboard.services.media.object_storage.GatedS3Storage`. Filesystem stays the
default and nothing changes for a deployment that does not set the variable.

## Why not presigned URLs

The infrastructure repo offered three ways to serve reads from Garage
(`../handoffs/infrastructure-media-and-static.md`, and its inbound half in that
repo's `docs/handoffs/urbanlens-app-media-on-garage.md`). Presigned URLs are the
fastest and take the app out of the data path entirely. They are also the one
option that changes who can read a file.

A presigned URL is a bearer token for one object. It carries no session, walks
no authorization policy, survives the request that produced it, travels in
`Referer` headers, browser history, proxy logs, screenshots and shared links,
and cannot be revoked before it expires. `GOALS.md` requires that pin data be
unreachable **by construction** — "structurally impossible for a bug (human or
agent-written) to leak a private pin's live data through some other surface".
An object-store URL that answers without consulting
`services.media.access.authorize_media` is exactly that other surface.

Shortening the expiry does not fix the class of problem, it fixes the duration
of one instance of it. And the project's stated direction is the opposite one:
`GOALS.md` puts private photos first in line for end-to-end encryption, which
makes the object even less meaningful outside the gate, not more.

## What was built instead

Three delivery modes, one authorization path. `MediaByteSource` in
`dashboard/controllers/media.py` is the split; a fourth backing store is a
fourth subclass, not a fourth branch in every caller.

| Backend | `MEDIA_X_ACCEL*` | How the bytes reach the client |
|---|---|---|
| filesystem | `MEDIA_X_ACCEL=true` | `X-Accel-Redirect` into `/_protected_media/`; nginx reads the volume. Unchanged. |
| filesystem | off | `FileResponse` off the disk. Unchanged. |
| s3 | `MEDIA_X_ACCEL_OBJECT_PREFIX` set | Django signs a URL for the object and puts **its path and query only** in an `X-Accel-Redirect`; nginx fetches it from a pinned upstream and strips the header. |
| s3 | unset | `FileResponse` over `default_storage.open()`. Works with nothing in front of the app, which is what a self-host has. |

The third row is the one worth reading twice. It keeps the whole access model
*and* keeps every media byte out of a gevent worker, because the credential
nginx uses is minted per request, after authorization, and never leaves the
server.

**Only the path and query go into that header, never a scheme and host.** An
absolute URL there would make any bug that can influence a stored path into
server-side request forgery performed by the one process that is inside the
cluster. nginx therefore pins its own upstream, and the response cannot move it.
Because SigV4 covers the `Host` header, nginx has to send the host
`UL_S3_ENDPOINT_URL` names:

```nginx
# Untested from the application side - this repository ships no such vhost.
location /_object_media/ {
    internal;
    proxy_pass       http://garage-s3.garage.svc.cluster.local:3900/;
    proxy_set_header Host garage-s3.garage.svc.cluster.local:3900;
    proxy_set_header Authorization "";   # the presigned query is the credential
    proxy_hide_header x-amz-request-id;
    proxy_hide_header x-amz-id-2;
}
```

## The part that is easy to get wrong

`FileField.url` is what every template, serializer and external-API response
renders, and `S3Storage.url` returns a presigned bucket URL. Swapping the
backend without overriding it would have silently replaced every gated
`/media/...` link on the site with a bearer token — no code change, no test
failure, no log line. `GatedS3Storage.url` returns exactly what
`FileSystemStorage(base_url=MEDIA_URL).url` returns, which is asserted as a
property over generated names in
`tests/hypothesis/test_media_object_storage.py`, and `manage.py check`
(`dashboard.E010`) refuses to start if `STORAGES["default"]` names the upstream
backend directly.

## What is still on local disk

Measured 2026-09-06. Three subtrees under `MEDIA_ROOT` are reached with
`os.path`/`pathlib` rather than through `STORAGES["default"]`, so switching the
backend does not move them, and the media volume cannot be deleted:

- `exports/<job_id>/` — `services.import_export.export.export_dir`
- `imports/<job_id>/` — `services.import_export.import_data`
- `preview_sources/` — `services.media.previews`, which is a *deliberate*
  shared filesystem: the sandbox worker reads staged sources off the same
  volume, precisely so 60 MB files do not go through the 512 MB Valkey that
  also holds the broker.

None is served through `/media/`; each has its own owner-checked view, and
`services.media.access` refuses all three families outright. `manage.py check`
emits `dashboard.W002` naming them whenever the s3 backend is selected, so the
switch cannot be mistaken for "media is off local disk now".

Two `FileField.path` call sites remain, both in
`services/import_export/export.py` (lines 933 and 1343 at time of writing).
`.path` raises `NotImplementedError` on a non-filesystem storage, so the export
archive builder needs porting to `storage.open()` before an s3 deployment can
build an export containing photos. **Not fixed here, and not tested against s3.**

## Verification

Run against MinIO on the app network, no internet, 2026-09-06 — not against
Garage, which this session had no access to:

```
backend             : GatedS3Storage
saved as            : pin_images/a7/Kd3xq8Lm2Zpq/2026-probe.jpg
FileField.url       : /media/pin_images/a7/Kd3xq8Lm2Zpq/2026-probe.jpg
streamed headers    : Content-Type: image/jpeg  Cache-Control: private, max-age=300
accel redirect      : /_object_media/ul-media/pin_images/.../2026-probe.jpg?X-Amz-Algorithm=...
bytes via signed URL: b'\xff\xd8\xff-real-jpeg-bytes'
traversal refused   : yes
```

Two things that cost time and will cost the next person the same:

- **botocore rejects a hostname containing an underscore** with
  `ValueError: Invalid endpoint`. Container names with underscores cannot be
  used as `UL_S3_ENDPOINT_URL` hosts. `garage-s3.garage.svc.cluster.local` is
  fine.
- `default_storage` is a `LazyObject`. `type(default_storage).__name__` is
  `"DefaultStorage"`; `default_storage.__class__.__name__` is the real backend.
  `isinstance` works either way.

## Not decided here

Whether media *should* move to Garage, and when. That is the infrastructure
repo's ADR-0007/0008 sequencing, and its own amendment says migrating before
leg 3's destination snapshots exist is "a regression dressed as progress". This
decision only removes the application from the list of things blocking it.

## See also

- [`../MEDIA_PIPELINE.md`](../MEDIA_PIPELINE.md) — the deployment shape.
- [`large-upload-protocol.md`](large-upload-protocol.md) — D7, which shares the signing machinery if it is ever chosen.
- [`../handoffs/infrastructure-media-and-static.md`](../handoffs/infrastructure-media-and-static.md) — the reply this answers.
