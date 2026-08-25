# Versioned content: forward revisions, enforced writes, bounded storage

**Status: DESIGN — 2026-08-24.** Grew out of `reputation-and-gating.md` R18 and outgrew it.
Concealment is the first consumer, not the reason: the same substrate is what merge
(R15), offline sync, undo, and "view this at an earlier revision" all need.

## The shape

**Revised 2026-08-24 after the friend requirement (below), which invalidated the earlier
two-materialised-HEADs design.**

### What a concealed viewer must see

Not "automatic content". Three things unioned:

1. **automatic** content — provider and enrichment writes;
2. **their own** contributions — the own-content rule holds everywhere;
3. **their friends'** contributions — because friends talk offline. "Just check the UrbanLens
   wiki, I put a load of stuff up there" has to work, or concealment breaks the product for
   the people it is not aimed at.

That third clause is what forces the design. The visible set is **per viewer** — everyone has
a different friend list — so it cannot be materialised as a fixed projection. There is no
"automatic HEAD" row to read.

### The resolution: field-granular revisions, no replay

Store one row per **(target, field, write)** rather than one per edit. Then every view of the
data is the same single query — *the latest qualifying write per field*:

```sql
SELECT DISTINCT ON (field_name) field_name, value
FROM   <model>_field_revisions
WHERE  target_id = %s
  AND  (source = 'automatic' OR actor_id = ANY(%s))   -- viewer + friends
ORDER  BY field_name, sequence DESC
```

One indexed query, Postgres `DISTINCT ON`, no replay, no checkpoints, no reconstruction. And
it is correct about ordering for free, which a delta-overlay design is not: if a stranger edits
`name` after a friend did, the stranger's row is filtered out and the friend's is still the
newest *qualifying* row. If enrichment then writes `name` again, its row is newer than both and
wins. Per-field last-writer-wins, over whatever subset the viewer is entitled to.

The same query shape answers every other question the substrate exists for:

| Question | Change to the query |
|---|---|
| current state | *nothing reads this* — the live row is HEAD |
| concealed view | `WHERE source='automatic' OR actor = ANY(viewer+friends)` |
| state at revision K | `AND sequence <= K` |
| as if only automatic | `WHERE source='automatic'` |
| who last set this field | drop `DISTINCT ON`, read `actor` |

**The live row stays HEAD.** Normal viewers read a plain row exactly as today and touch none of
this. That is Jess's performance constraint, satisfied by not changing the hot path at all.

### Why not the earlier design

Forward deltas plus periodic whole-row checkpoints do not survive the friend clause: a
checkpoint is a *cumulative* snapshot including everyone's edits, so it cannot be the starting
point for a filtered replay. Filtered replay would have to start at revision 1 every time.
Field-granular rows make the question a `max(sequence) per field` aggregate instead, which the
database answers directly.

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

## 3. Storage

Field-granular rows are already the delta — a row exists only for a field that was actually
written, so an edit touching two of fifteen fields costs two rows. There is no whole-row
snapshot to economise on and no checkpoint machinery to build.

**Value column: text, not JSON.** Per Jess, JSON columns are avoided unless strongly preferred
— they fight searching, indexing and encryption. A single text column with the field name
alongside is indexable, and deserialisation goes through Django's own
`model._meta.get_field(name).to_python()` rather than a bare `setattr`. That last detail
matters: the existing revert path assigns **stringified** values back to typed fields, which is
the bug that made backward replay lossy in the first place. Going through `to_python()` is what
stops this design inheriting it.

**Store only the post-state.** Forward-only needs no `from` value: the prior state is the
previous row for that field. This halves the rows' width and closes a real leak — the tells
audit found `WikiEdit.changes` keeps the community's *prior* value inside the viewer's own
edit history, which survives a perfect read gate (type a character into the "empty"
description, read the hidden one out of your own history, revert). Forward-only removes it by
construction rather than by remembering to redact.

**Retention** is per-model policy, and safe by construction: the newest row per field must be
kept, everything older is history. Collapsing old history is a delete, not a rewrite.

**No projection row, no JSON column, no parallel table.** The open question from R18 is closed
by the above: with the filtered read being one indexed query, there is nothing to materialise.
This also avoids the hazard flagged when it was proposed — a projection row in the same table
means every existing query must remember to exclude projections, which is exactly the
"one call site at a time" failure this codebase has been bitten by twice. If profiling later
shows the concealed query is hot, a cache can be added behind the same accessor without moving
the source of truth.

## Sequencing

1. `abstract/versioned.py` and one concrete subclass, behind a `versioned_fields` declaration —
   no behaviour change, nothing reads it yet.
2. Interception, with the asymmetric degradation, plus the CI check. Fix the three known
   bypasses. **This is independently valuable and fixes existing bugs.**
3. The automatic HEAD projection, and concealment reading it.
4. Checkpointing and retention — needed only once real edit volume exists; deltas-with-N=1 is
   just "full snapshots" and is a correct starting configuration.
5. Migrate `ArticleRevision` onto the base; extend to pins and floorplans.

## Settled

- **Where the concealed projection lives** — nowhere. It is a query, not a row. See §3.
- **Whether `source` needs a merge value** — no. Jess: a merge is a *reason*, not a source;
  both user and automatic merge resolution are possible, and the provenance of the things being
  merged may itself need tracking. `source` stays `user` / `automatic` / `system`, and a
  separate `reason` field is the right shape when merge work starts. Not now.
