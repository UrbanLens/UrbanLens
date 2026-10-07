---
status: accepted
date: 2026-09-06
---

# Uploads over the ingress cap will be chunked to Django, not presigned multipart

Formerly `D7`. Detail: [`docs/designs/large-upload-protocol.md`](../designs/large-upload-protocol.md).

Cloudflare's free plan caps a request body at 100 MB, below several of the app's upload limits. Until a new protocol exists, `UL_MAX_REQUEST_BODY_MB` lowers every upload limit to what the ingress will carry, so the browser refuses an oversize file before sending it. The protocol, when built, will be chunked upload to Django: it keeps the media validation pipeline unchanged, adds no public surface, and works on a single-machine self-host with no object store.

## Considered options

- Presigned multipart straight to the object store: better on the wire, but it needs a bucket (a self-host may have none) and lands bytes before validation, which needs a quarantine-and-promote pipeline. This is a portability and validation objection, not the privacy one in ADR-0006.

## Consequences

- Chunked upload costs a worker for every uploaded byte. The chunk endpoint should tell the client where to put the next chunk, so presigned multipart can be added later as a second answer from the same endpoint.
