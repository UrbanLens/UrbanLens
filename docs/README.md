# How this documentation works

**With rare exceptions, everything in this directory was written by a Claude agent, not by Jess.**
It records what one automated session measured or believed on a given date. It
was not independently reviewed. Treat it as evidence, not authority.

`AGENTS.md` (which each `CLAUDE.md` imports) is loaded into every session and
every subagent, so it stays under 140 lines and agents cannot edit it. This directory is the writable surface.

## Start at `INDEX.md`

One line per document, never wrapped, so a single `grep` returns a complete
record. Read the index before reading anything else here.

```bash
grep -E '^\| P7 ' docs/INDEX.md          # one record by id
grep -i 'encryption' docs/INDEX.md       # by keyword
grep -E '\| (live|actionable) ' docs/INDEX.md   # plans and ideas in play
```

## Where things live

| What | Where |
|---|---|
| **Work to do** - a defect, a task, anything that will be closed | A GitHub issue, labelled per [`agents/triage-labels.md`](agents/triage-labels.md); see [`agents/issue-tracker.md`](agents/issue-tracker.md) |
| **A decision** - we chose X over Y because Z | An ADR in [`adr/`](adr/README.md) |
| **Domain vocabulary** | [`CONTEXT.md`](CONTEXT.md), per [`agents/domain.md`](agents/domain.md) |
| **Knowledge** - measurements, how things work, plans, ideas, notes | A document here, indexed in `INDEX.md` |

Problems (`P#`), tasks (`T#`) and decisions (`D#`) used to be index records.
They moved on 2026-10-07: [`PROBLEMS.md`](PROBLEMS.md) maps each former `P#`
and `T#` to its issue, and each `D#` became the ADR with the same number (`D8` is `adr/0008-*.md`). `bin/check_docs_index.py`
refuses new rows with those prefixes.

An issue body is written by the same agents as this directory, so the
disclaimer below applies to it too; open it with the one-line form used by the
migrated issues. When an issue needs long-form analysis, write it here as a
note and link it from the issue - the issue holds the status, the document
the detail.

## The ID prefixes

| | | Status values |
|---|---|---|
| `I#` | **Idea** — a proposal needing long-form analysis (a short one is just an `enhancement` issue); once actionable, its row names its issue | `unvalidated` · `actionable` · `absorbed` |
| `X#` | **Experiment** — one measurement, with its method and its unit of analysis | `holds` · `collapsed` · `untestable` · `disqualified` |
| `PL#` | **Plan** — a multi-stage body of work; its row names the issue tracking it | `live` · `superseded` |
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

Close its issue with a comment that records what was actually wrong. Where the
original guessed and the fix proved it wrong, say so - that correction is
usually the most useful sentence, and the next person to guess the same way is
the one it is for. Repoint anything in `src/` that cited it.

`archive/PROBLEMS-ARCHIVE.md` holds problems resolved before the move to
issues. It takes no new entries; a former `P#` closed since is a closed issue.

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

`INDEX.md` and any new standalone file open with this,
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
