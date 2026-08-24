# Versioned content: forward revisions, enforced writes, bounded storage

**Status: DESIGN — 2026-08-24.** Grew out of `reputation-and-gating.md` R18 and outgrew it.
Concealment is the first consumer, not the reason: the same substrate is what merge
(R15), offline sync, undo, and "view this at an earlier revision" all need.

## The shape

Three materialised things and one log.

| | What it is | Cost to read |
|---|---|---|
| **Real HEAD** | the live row, exactly as today | free — a plain row read |
| **Automatic HEAD** | the row as it would be if only automatic writers had ever touched it | free — a plain row read |
| **Revision 1** | the origin state | free — always stored whole (see storage) |
| **The log** | append-only, forward-only, one entry per write | cold path only |

**No read path ever reconstructs anything.** Reconstruction exists for history browsing and
merge, both cold, and is bounded (below). This is the whole answer to "no performance hit for
typical users".

The automatic HEAD is what makes this better than recording provenance per field. The state a
concealed viewer needs is not "the wiki at time T" — that would freeze enrichment and go stale,
which is what sank the earlier proposal. It is *"the row as if only automatic writers had
touched it"*, which is **a filter over the log rather than a point in it**, and therefore keeps
up as enrichment continues.

## 1. Enforcement: make the right path the only path

The prerequisite is that every write is logged. Today three known writers bypass `save()`
entirely — `tasks.py:124` (bulk `.update()`), `wiki_creation.py:177` (bare `save()`, no edit
row), `controllers/markup.py:76` (omits `"updated"` from `update_fields`) — and all three are
already bugs.

**Do not build a funnel that callers must remember to use.** Intercept instead, so there is no
"proper path" to forget:

1. **`VersionedModel.save()`** records a revision. Covers every ORM instance write.
2. **`VersionedQuerySet.update()` and `.bulk_update()` record one too.** This is the important
   one: `.update()` bypasses `save()` *and* signals, which is precisely why the current
   bypasses are invisible. The queryset is ours, so it is interceptable. Django gives no signal
   here at all — an override is the only place to stand.
3. **Source is inferred, not annotated.** A write inside an authenticated request defaults to
   `user`; a write inside a Celery task defaults to `automatic`. Both via a context variable
   set once at the request/task boundary. Explicit annotation is then needed only where the
   default is wrong (enrichment kicked off inline from a request, say), instead of at every
   one of hundreds of call sites.
4. **Escape hatches are explicit and loud.** `with unversioned(reason=...)` for migrations and
   backfills, which logs at WARNING and is greppable.

What that leaves uncovered, and the answer for each:

- **Raw SQL and migrations** — a structural CI check (the repo has three already) plus the
  `unversioned()` hatch. Migrations legitimately need it.
- **`bulk_create`** — covered by intercepting it too; a create is revision 1.
- **A future model that forgets to inherit `VersionedModel`** — the CI check asserts every
  model declaring `versioned_fields` inherits the mixin, and vice versa.

**Degradation, deliberately asymmetric.** In `DEBUG`/`TESTING`, an unlogged write **raises** —
loud, in the developer's face, in CI. In production it **records the revision anyway** from the
post-write state (degraded provenance, marked as such) and logs an error. A privacy feature
must not lose a log entry silently, and must not take down a user's request either.

## 2. Generalisation: one substrate, per-model tables

Generic from the start, per Jess. Django-idiomatic and consistent with this codebase's
`models/abstract/` tier hierarchy:

```
models/abstract/versioned.py
    VersionedModel          # mixin: save() interception, automatic-HEAD pointer, revisions
    VersionedQuerySet       # update()/bulk_update()/bulk_create() interception
    AbstractRevision        # abstract base: payload, source, actor, parent, sequence, kind
```

Each versioned model declares its own concrete revision table by subclassing
`AbstractRevision` — `WikiRevision`, `PinRevision`, `FloorplanRevision`, and so on.

**Per-model tables, not one `contenttypes` table.** A single generic table would need a
`GenericForeignKey`, which costs a join on every history read, produces one enormous index, and
prevents a real FK constraint. Concrete subclasses give each model a properly-typed FK, its own
indexes, and independent retention policy, at the cost of boilerplate an abstract base already
removes.

**Which fields are versioned is declared, not inferred:** `versioned_fields` on the model. The
log covers **scalar fields**. Related rows — photos, comments, aliases, links, floorplans, child
wikis — are rows rather than fields, already carry usable provenance (`Image.source`,
`WikiAlias.source`, `created_by`), and keep row-level filtering.

**`ArticleRevision` is a special case to migrate, not to duplicate.** It already stores complete
text per revision and already has `restored_from`. It should become an `AbstractRevision`
subclass so articles join the same machinery rather than keeping a parallel one.

## 3. Storage: forward deltas with periodic checkpoints

Full snapshots per edit are the right *starting* shape and the wrong *ending* one. The standard
answer, and the one that fits the read pattern here:

- **Deltas by default** — changed fields only. Most edits touch one or two of ~15, so an entry
  is small.
- **A checkpoint every `N` revisions**, storing the whole field set. `N` tunable, starting
  around 50.
- **Revision 1 is always a checkpoint.** It is the origin state and is read often enough to
  matter.
- Reconstructing revision K = nearest preceding checkpoint, then forward-apply. **Bounded by
  `N`, never by the wiki's age.**

The hot reads never touch this: current version is the real HEAD, concealed version is the
automatic HEAD, origin is a checkpoint. All three are plain row reads.

**Store only the post-state, never the prior value.** Forward replay does not need `from` — the
prior state is reconstructible by replaying to K-1. This halves entry size, and it also closes
a real leak: the tells audit found that `WikiEdit.changes` storing the community's *prior* value
inside the viewer's own edit row survives a perfect read gate (type a character into the
"empty" description, read the hidden one out of your own history, revert). Forward-only storage
removes that by construction rather than by remembering to redact it — which is worth more than
the space saving.

**Retention** is per-model policy: collapse runs of deltas older than a window into a fresh
checkpoint and drop the deltas between, always keeping revision 1. Undo and merge need recent
history; nobody needs the 400th intermediate state of a wiki from three years ago.

## Sequencing

1. `abstract/versioned.py` and one concrete subclass, behind a `versioned_fields` declaration —
   no behaviour change, nothing reads it yet.
2. Interception, with the asymmetric degradation, plus the CI check. Fix the three known
   bypasses. **This is independently valuable and fixes existing bugs.**
3. The automatic HEAD projection, and concealment reading it.
4. Checkpointing and retention — needed only once real edit volume exists; deltas-with-N=1 is
   just "full snapshots" and is a correct starting configuration.
5. Migrate `ArticleRevision` onto the base; extend to pins and floorplans.

## Open

- Does the automatic HEAD live as a second row in the same table (a self-FK, `is_projection`),
  a `JSONField` on the model, or a parallel table? Second row keeps one code path for reads
  and is probably right, but it means every existing query must exclude projections — which is
  exactly the "one call site at a time" failure this codebase has been bitten by twice.
- Whether `source` needs more than `user` / `automatic` / `system` — a merge resolution, for
  instance, is arguably a fourth.
