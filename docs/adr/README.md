# Architecture decision records

Decisions live here, one file each, numbered sequentially: `NNNN-<slug>.md`. The next number is `0028`.

ADR-NNNN for N ≤ 27 is the former decision record `D`N in [`docs/INDEX.md`](../INDEX.md), so `D8` is [ADR-0008](0008-storage-quotas-enforced-generally.md).

Each file starts with frontmatter holding `status` (`accepted`, or `superseded by ADR-NNNN`) and `date`, then a short title and one to three sentences of context, decision and reason. `## Considered options` and `## Consequences` are added only when a rejected alternative or a downstream effect would otherwise be re-proposed or tripped over.

An ADR is the short record. The long-form design doc stays under [`docs/designs/`](../designs/) and is linked from the ADR's `Detail:` line.
