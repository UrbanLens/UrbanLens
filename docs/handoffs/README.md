# Handoffs

Correspondence with another repository or another team. Each file records what
was asked, what was measured, and what was decided on one particular day, and
is **frozen at its send date**: when its status changes the header line changes
and the body does not. A record edited to agree with the present is a second
copy of the present with a misleading date on it.

That makes this directory the one place under `docs/` exempt from the
present-tense rule in [`../README.md`](../README.md). Everywhere else a stale
sentence is a bug and gets rewritten; here it is the content.

`grep -n 'Status:' docs/handoffs/*.md` is the whole picture.

| Note | Direction | Status |
|---|---|---|
| [`infrastructure-media-and-static.md`](infrastructure-media-and-static.md) — reply on `/static/`, object-storage media, and the 100 MB upload cap | outbound, `UrbanLens/infrastructure` | SENT 2026-09-06 |
| [`infrastructure-0054-friendship-merge.md`](infrastructure-0054-friendship-merge.md) — reply confirming 0054's `IntegrityError` on a reciprocal pair, and what the suggested fix would have cost | outbound, `UrbanLens/infrastructure` | SENT 2026-09-08 |

## The convention

Every file opens with its `# Title`, then two bullets and nothing else before
the body:

```markdown
- **Status: OPEN as of YYYY-MM-DD.** / **Status: SENT, YYYY-MM-DD.** ...
- **Direction: inbound / outbound.** Who sent it to whom.
```

The shape is the `infrastructure` repo's, deliberately — these are two halves
of one thread, and a reader following it should not have to learn two filing
systems. A reply is named after the ask it answers, so `ls` shows the thread.

Decisions that came out of a handoff live under [`../designs/`](../designs/)
with a `D#` id, not here: this directory is what was said, and a decision has to
stay editable when it is superseded.
