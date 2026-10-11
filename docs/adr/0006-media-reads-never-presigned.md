---
status: accepted
date: 2026-09-06
---

# Media may live in an object store, but reads always pass the media gate

Formerly `D6`.

User media may be stored in an S3-compatible object store (`UL_MEDIA_STORAGE_BACKEND=s3`; filesystem stays the default), but a read is never served by handing the client a URL to that store. The client always gets `/media/<key>` and always passes `MediaGateView`. A presigned URL is a bearer token that carries no session, leaks through referrers, logs and shared links, and cannot be revoked, so it is exactly the surface the product goals require to be impossible by construction.

## Considered options

- Presigned URLs: fastest, and take the app out of the data path. Rejected because they change who can read a file. A shorter expiry only shortens each leak.

## Consequences

- To keep bytes out of the app workers, nginx fetches the object from a pinned upstream using a URL Django signs per request after authorization. Only the path and query go into `X-Accel-Redirect`, never a scheme and host, so a stored path cannot become server-side request forgery.
- Without nginx in front (self-host), Django streams the object itself.
