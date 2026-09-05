# How this documentation works

**With rare exceptions, everything in this directory was written by a Claude agent, not by Jess.**
It records what one automated session measured or believed on a given date. It
was not independently reviewed. Treat it as evidence, not authority.

`CLAUDE.md` is loaded into every session and every subagent, so it stays under
140 lines and agents cannot edit it. This directory is the writable surface.

## Start at `INDEX.md`

One line per document, never wrapped, so a single `grep` returns a complete
record. Read the index before reading anything else here.

```bash
grep -E '^\| P7 ' docs/INDEX.md          # one record by id
grep -i 'encryption' docs/INDEX.md       # by keyword
grep -E '\| open ' docs/INDEX.md         # everything still open
```

## The ID prefixes

| | | Status values |
|---|---|---|
| `P#` | **Problem** — a known defect, with how it was found and why it was deferred | `open` · `blocking` · `fixed` |
| `I#` | **Idea** — an unvalidated proposal, or a practice worth transferring | `unvalidated` · `actionable` · `absorbed` |
| `D#` | **Decision** — we chose X over Y because Z. Immutable once accepted; a later `D#` supersedes it | `accepted` · `superseded` |
| `X#` | **Experiment** — one measurement, with its method and its unit of analysis | `holds` · `collapsed` · `untestable` · `disqualified` |
| `T#` | **Task** — actionable and closeable | `open` · `blocked` · `done` |
| `PL#` | **Plan** — a multi-stage body of work | `live` · `superseded` |
| `R#` | **Reference** — how a subsystem currently works | `current` · `stale` |
| `N#` | **Note** — something worth recording that is none of the above: an observation, a caveat, a thing someone will otherwise rediscover | `current` · `stale` |

## Adding an entry

1. Read `INDEX.md` and take the next free ID from the header comment.
2. Write the entry under a `## <ID> — Title` heading at column 0, with the
   metadata line below it.
3. **Add the INDEX line in the same commit**, and update the next-free-ID
   comment. The index is the allocator, so a duplicate ID becomes a merge
   conflict instead of a silent collision.

## Closing a problem

A resolved `P#` **moves** to `archive/PROBLEMS-ARCHIVE.md`; it is not left in
`PROBLEMS.md` marked fixed. The live file is what still needs attention, and an
entry that says "fixed" at the top still costs every reader the time to work out
that it does.

1. Rewrite the entry as the record of what was actually wrong. Where the
   original guessed and the fix proved it wrong, say so — that correction is
   usually the most useful sentence in the entry, and the next person to guess
   the same way is the one it is for.
2. Move it under a `## RESOLVED <date>: <title>` heading in the archive, keeping
   its metadata line as `` `id: P#` · `status: fixed` · `resolved: <date>` ``.
   The id line is load-bearing twice over: a citation of `P70` still resolves
   after the entry is fixed, and `bin/check_docs_index.py` counts archived ids
   toward the next free one, so finishing the highest-numbered problem cannot
   hand its id to the next writer.
3. Delete its row from `INDEX.md`. Do **not** lower the next-free-id header —
   ids are never reused.
4. Repoint anything in `src/` that cited it. `bin/check_docs_refs.py` catches a
   path that stops resolving; nothing catches a `P#` that has moved.

## House style

These are why this directory was rebuilt; the previous version broke all of them.

- **A title is a claim, not a category.** "Path casing is not normalised on
  write, so joins silently drop rows" — not "Casing issue".
- **Never stack a correction under an old claim — rewrite the claim,** and note
  what it supersedes on the metadata line. The old documentation had entries four
  corrections deep, and a reader could not tell which layer was current.
- **Say what you did not measure.** "Not re-measured this session" is a
  complete and useful sentence. Silence reads as verification.

Cite `file:line` or the exact command, so the next reader can re-run it rather
than believe you.

## The disclaimer block

`INDEX.md`, `PROBLEMS.md` and any new standalone file open with this,
immediately after the title. Older documents predate the convention and are
being converted as they are next edited, rather than in one sweep that would
touch every file without reading it:

```markdown
> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.
```
