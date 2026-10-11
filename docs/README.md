# How this documentation works

**With rare exceptions, everything in this directory was written by a Claude agent, not by Jess.**
It records what one automated session measured or believed on a given date. It
was not independently reviewed. Treat it as evidence, not authority. When a
document and the code disagree, the code wins.

## Where things live

| What | Where |
|---|---|
| **Work to do** - a defect, a task, anything that will be closed | A [GitHub issue](https://github.com/UrbanLens/UrbanLens/issues) |
| **A decision** - we chose X over Y because Z | An ADR in [`adr/`](adr/README.md) |
| **What exists** | [`FEATURES.md`](FEATURES.md) |
| **How a subsystem works** | The reference documents in this directory, such as [`MEDIA_PIPELINE.md`](MEDIA_PIPELINE.md), [`DATA_ENCRYPTION.md`](DATA_ENCRYPTION.md), [`PRIVACY_MODEL.md`](PRIVACY_MODEL.md) and [`EXTERNAL_API.md`](EXTERNAL_API.md) |
| **How to test and check a change** | [`TOOLING.md`](TOOLING.md), [`CONTRACT_TESTS.md`](CONTRACT_TESTS.md), [`INTEGRATION_TESTS.md`](INTEGRATION_TESTS.md) |

Some identifiers in these documents (`P#`, `N#`, `X#`, `R#`, `PL#`) name records
kept with the project's working notes, which are not published here.

## House style

- **A title is a claim, not a category.** "Path casing is not normalised on
  write, so joins silently drop rows" - not "Casing issue".
- **Never stack a correction under an old claim - rewrite the claim.**
- **Say what you did not measure.** "Not re-measured this session" is a
  complete and useful sentence. Silence reads as verification.

Cite `file:line` or the exact command, so the next reader can re-run it rather
than believe you.

## The disclaimer block

New standalone documents open with this, immediately after the title:

```markdown
> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.
```
