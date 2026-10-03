One module per external API; clients are `Gateway` subclasses under `apis/`.

## Untrusted bytes

Anything that hands user-uploaded bytes to a parser - Pillow, ffmpeg, etc - must be decorated `@untrusted_parse` and
reached from a task declaring `queue=SANDBOX_QUEUE`. Declare the queue on the task, never at
the `apply_async` call site. See `docs/MEDIA_PIPELINE.md` before adding a parser.