# dashboard/services/ — Service Layer

Applies to `src/urbanlens/dashboard/services/`. One module per external API or
domain concern; API clients are `Gateway` subclasses under `apis/`.

## Untrusted bytes

Anything that hands user-uploaded bytes to a parser — Pillow, ffmpeg,
LibreOffice, GDAL, `zipfile`, lxml — must be decorated `@untrusted_parse` and
reached only from a task declaring `queue=SANDBOX_QUEUE`. All 38 production
decorations live under this directory. Declare the queue on the task, never at
the `apply_async` call site. See `docs/MEDIA_PIPELINE.md` before adding a parser.

Resolve an external binary to an absolute path (`shutil.which`) rather than
passing a bare name to `subprocess`: in this tier a writable PATH entry ahead of
the real one would decide what runs on attacker-supplied bytes. `videos.py` and
`documents.py` show the shape.
