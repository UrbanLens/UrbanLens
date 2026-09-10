# Doc/config claims the map-performance investigation found stale or false

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## N12 — `pyproject.toml`'s nplusone-rejection comment misdescribed `django-auto-prefetch`; it is wholly unwired

`id: N12` · `status: current` · `updated: 2026-09-10`

`pyproject.toml:320-322` and (before this session's edit) `docs/TOOLING.md:612-613` both justified
rejecting the `nplusone` runtime detector partly on the claim that `django-auto-prefetch` "already
suppresses the access pattern such a detector watches for." That clause is false:
`django-auto-prefetch` is listed as a direct dependency (`pyproject.toml:16`,
`django-auto-prefetch~=1.14.0`) but is otherwise entirely unwired - a repo-wide grep for
`auto_prefetch` under `src/urbanlens` returns zero imports, it is not in `INSTALLED_APPS`, and
`models/abstract/model.py` subclasses plain `django.db.models.Model`, not `auto_prefetch.Model`. It
is an installed, inert dependency, not a suppression mechanism - it cannot be quieting anything.

The independent reason given alongside it (`nplusone` "has not shipped a release since 2019")
carries the decision on its own, so nothing about keeping `django-perf-rec` as the adopted
alternative needs to change - only the false clause does.

**Corrected in place this session**: `docs/TOOLING.md`'s "Evaluated, not adopted" bullet for
`nplusone` now cites only the 2019 reason. **Not corrected**, outside `docs/` and therefore outside
this session's remit: `pyproject.toml:320-322`'s comment still carries the false clause verbatim -
whoever next edits that dependency block should delete "and because `django-auto-prefetch` already
suppresses the access pattern such a detector watches for - which would make it quiet precisely
where this codebase's N+1s actually came from" and keep the 2019-not-shipped reasoning that follows
it.

## N13 — The archived "map payload is already query-flat" claim was true and answered the wrong question

`id: N13` · `status: current` · `updated: 2026-09-10`

`docs/archive/PROBLEMS-ARCHIVE.md:8713-8732` ("CORRECTION: the `to_json()` prefetch work does not
affect the map...") states that the map's payload is "already query-flat" and that fixing
`Pin.to_json()`'s prefetch behaviour was "never the expensive part." Not edited - archived history
stands, per house style - but this investigation found that framing answered the wrong question:
query-flatness was true (the pre-fix `MapPinPayloadService.all()` ran a flat ~21 queries regardless
of pin count, per R27) and irrelevant, because the measured cost was 88% Python CPU time spent
building 63,240 Django model instances to emit 10,000 flat dicts - not query count or query shape
at all (see N11 for why the scaling mixins available at the time could not have caught this
either). A reader who greps the archive for "map" and "query-flat" and concludes the map's
performance was already a settled question would be right about the sentence and wrong about the
conclusion.

Fixed across `0fab2b35a`/`cc0040878`/`112df3dab`/`f2623a5d4` on `release/v_0_8_0` - see those commit
messages for the fix itself. This note exists only so the archived finding is read correctly
alongside them, not to reopen or restate the fix.
