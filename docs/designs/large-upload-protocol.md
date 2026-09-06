# D7 — Uploads over the ingress cap: advertise the real limit now, chunk to Django later

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D7` · `status: accepted` · `decided: 2026-09-06`

## The problem

Cloudflare's free and pro plans cap a request body at 100 MB. The infrastructure
repo's ingress decision is the free tier, reaffirmed under an explicit
no-new-subscriptions constraint, so the cap is fixed and the protocol has to
change. Production has never met this wall because it is DNS-only at
Cloudflare; the Phase 9 cutover is the first time these paths sit behind it.

Three limits in this repo are larger than 100 MB, measured 2026-09-06:

| Limit | Value | Where |
|---|---|---|
| site-wide per-file upload | 250 MB default, 900 MB ceiling | `models/site_settings/model.py` |
| data-file import form | 500 MB | `forms/upload_datafile.py` |
| export-archive import view | 500 MB | `controllers/tools.py` |
| nginx `client_max_body_size` | 200 MB | `config/nginx/django.conf` |

A body the proxy rejects is answered by the proxy. No view runs, nothing is
logged here, and the uploader watches an upload fail into an error page from a
company they have never heard of, having sent 100 MB first.

## Decided now: advertise what the ingress will carry

`UL_MAX_REQUEST_BODY_MB` (0 = nothing in front imposes one) lowers all three
limits through `services.media.storage.cap_to_ingress`. It is deliberately
*not* a limit this application wants — it is a limit it states so the user
finds out before sending the bytes. `max_upload_file_size_bytes()` is the
chokepoint, and it feeds both the server-side check and the
`data-max-file-size` the vault pages hand their upload widget, so the file is
refused in the browser. The site-admin page says the cap is in force rather
than silently ignoring the number an admin typed.

This is insurance, not the protocol. It converts "opaque 413 after a long
upload" into "we told you this file is too big", which is what makes the
cutover survivable on a date nobody has to co-ordinate. It does not let anyone
upload a 150 MB video.

## Decided now: the protocol will be chunked upload to Django, not presigned multipart

Not yet built. The reasoning, so it is not re-litigated from scratch:

**Presigned multipart straight to Garage** (the infrastructure side's
recommendation) is genuinely better on the wire — nothing over 100 MB is ever a
single request through Cloudflare, and the app is out of the data path. Two
things count against it here, and neither is about effort:

1. **It needs an object store.** `GOALS.md` makes a single-machine self-host a
   supported path, not an afterthought. An upload protocol that only works when
   a bucket exists means either self-hosters lose large uploads or the project
   maintains two upload paths permanently — which is the cost the
   infrastructure note attributes to the *other* option.
2. **It moves the bytes out of reach of validation.** `docs/MEDIA_PIPELINE.md`
   is built around never letting an unvalidated upload be reachable. A direct
   browser→bucket `PUT` lands the object before anything has looked at it, so
   it needs a quarantine prefix, an async validate, and a promote step — and a
   give-up branch that rejects rather than publishes. That is a correct design
   and a considerably larger one than "an S3 client and three endpoints".

**Chunked upload to Django** keeps the validation pipeline exactly as it is,
adds no public surface, and works on every deployment. It costs a gevent worker
per uploaded byte, which is the real objection, and a resumable implementation
is not small.

The chunk endpoint should be written so the transport is swappable — the client
asks the server where to put the next chunk, rather than assuming. If presigned
multipart is ever wanted, it becomes a second answer from the same endpoint
rather than a second client.

**Unlike D6, the presigned-URL objection does not apply here.** A presigned
*upload* URL grants write to one key; it does not read anyone's private data.
This is a portability and validation decision, not a privacy one.

## Not measured

Nothing here was tested behind Cloudflare. The 100 MB figure is the
infrastructure repo's, taken on trust; the cap machinery is tested against the
setting, not against a real proxy rejecting a real body.

## See also

- [`media-object-storage.md`](media-object-storage.md) — D6, which already
  built the signing machinery a presigned-multipart implementation would reuse.
- [`../handoffs/infrastructure-media-and-static.md`](../handoffs/infrastructure-media-and-static.md) — the reply this answers.
