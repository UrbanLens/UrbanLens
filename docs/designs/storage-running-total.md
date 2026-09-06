# I4 — The storage running-total column: what it is, and why the lock isn't enough

`id: I4` · `status: actionable` · `updated: 2026-09-06`

Written 2026-09-06, because P28's sign-off asked for the "running-total column" proposal to be
re-explained before anyone implements it. Nothing here is built. This is the explanation, the
options, and a recommendation to accept or reject.

## What is wrong today, in one paragraph

A quota check is two steps that are not joined together:

```python
used = get_storage_used_bytes(profile)          # SELECT SUM(file_size) ... WHERE profile=X
if used + upload_size <= quota:                 # decide
    Image.objects.create(...)                   # then, separately, INSERT
```

Nothing stops a second upload running its `SUM` in the gap between this one's `SUM` and its
`INSERT`. Both read the same total, both decide there is room, both insert. With N uploads in
flight the profile can exceed its quota by up to N files.

`per_profile_upload_lock` narrows that gap and is **deliberately fail-open**: a caller that cannot
take the lock logs a warning and proceeds. That is the right call for one interactive upload — a
missed lock should not hang someone's photo — but it means the lock does not bound a bulk import,
which fans out one task per image and therefore produces exactly the contention that makes the lock
unavailable. Every background path now wraps the check in it (2026-08-25); that closed an
inconsistency between call sites without changing what the lock can guarantee.

## Why a column fixes what a lock cannot

The fix is not really "a column". It is **moving the number onto a single row**, because a database
can do two things with one row that it cannot do with a `SUM` over many:

1. **Increment it atomically.** `UPDATE profile SET storage_used_bytes = storage_used_bytes + :n
   WHERE id = :x` cannot lose a concurrent increment. No lock, no coordination, no fail-open path —
   the database serialises the two writes itself.
2. **Lock it.** `SELECT ... FOR UPDATE` on that one row makes check-then-insert genuinely atomic:
   take the row, read the total, decide, insert the image, add the bytes, commit. A second uploader
   waits at the `SELECT` and then reads a total that already includes the first one's file.

A `SUM` over a thousand image rows can be neither of those things. That is the whole argument. The
cache lock is an approximation of (2) built outside the database, and it is approximate precisely
where it matters most.

## The part that makes this a design decision rather than a refactor

**The total has to be maintained in five places, not one.** In this codebase `file_size` is not
written once at insert and left alone:

| # | When the number changes | Where |
|---|---|---|
| 1 | An image is created | ~10 call sites, `Image.objects.create(..., file_size=...)` |
| 2 | An image is deleted | anywhere a row is removed, including cascades |
| 3 | The size is **backfilled after admission** | `tasks.py:1270` — an upload is admitted before its stored size is known, and `process_image_upload` records it afterwards |
| 4 | The file is **re-encoded** | the same task rewrites `.jpg` → `.webp` and downscales, so the size changes *again* after it was first recorded |
| 5 | The exemption flips | `services/media/quota_rewards.py` moves a row between counted and exempt in both directions with `queryset.update()`, without the file changing at all |

An implementation that hooks only (1) and (2) — the obvious one — is wrong here. It would drift on
the first re-encode, which is every photo. This is the reason the proposal has stayed a proposal:
the interesting question is not the column, it is which mechanism catches all five.

## Three mechanisms, and what each gets wrong

**A. A database trigger** on the images table, `AFTER INSERT/UPDATE/DELETE`, computing the delta
from `OLD`/`NEW`.

- Catches all five by construction, including `queryset.update()`, `bulk_create`, data migrations,
  the Django admin, and a psql session.
- Invisible to anyone reading the Python. This tree also already has a migration trap around
  triggers — see the pending-trigger-events note — so the migration that adds it needs care.

**B. Override `Image.save()` and `Image.delete()`** to apply an `F()` delta in the same transaction.

- Reads naturally in Python and is easy to test.
- **Misses (5) entirely**, because `quota_rewards` uses `queryset.update()`, which never calls
  `save()`. It also misses any `bulk_create`. Those are exactly the paths that change the number
  without a human thinking about storage, so this is the option that looks cleanest and breaks
  quietest.

**C. One explicit service function every writer calls, plus a periodic reconciliation** job that
re-derives the true `SUM` and corrects any drift.

- Honest that drift will happen, and self-healing.
- Two mechanisms to keep in step, and the reconciliation is the thing that actually guarantees
  correctness — which raises the question of why the first mechanism is trusted at all.

**Recommendation: A, with C's reconciliation as a cheap safety net** (a management command, run on a
schedule or by hand). The drift modes here are (3) and (4), which happen inside a background task,
and (5), which is a `queryset.update()` — and B is the option that misses precisely those. The
reconciliation command is worth having regardless, because it is also how you verify the migration
worked.

## Backfill

One data migration:

```sql
UPDATE dashboard_profiles p
   SET storage_used_bytes = COALESCE(
       (SELECT SUM(i.file_size) FROM dashboard_images i
         WHERE i.profile_id = p.id AND i.quota_exempt_reason = ''), 0);
```

Cheap and one-time. Worth knowing: rows with a `NULL` `file_size` are skipped by the current check
too, so the backfilled total equals what the quota already counts. **Nobody's quota position moves
on day one** — which is the property that makes this safe to ship separately from any behaviour
change.

## What this does *not* fix

An upload of unknown size is admitted as zero (`quota_error_for_upload` treats `None` as `0`) and
charged once stored. A running total makes the *accounting* exact; it does not make that admission
decision exact. Whether an unknown-size upload should be admitted at all is a separate question, and
this design does not answer it.

## On the performance argument

P28 says the column "would also remove the repeated `SUM(file_size)` scan that
`get_storage_used_bytes` runs on each upload". True, but **not currently worth anything**: there is
an index covering it (`idxdb_image_profile_quota` on `profile, quota_exempt_reason`), and at this
install's scale the scan is trivial. This should be accepted or rejected on correctness. If it is
sold as a performance fix, it will be measured and found not to matter.

## The decision being asked for

1. Is exact quota enforcement worth a denormalised counter at all, given that the current failure
   needs a bulk import to trigger and overshoots by a bounded amount?
2. If yes: mechanism A (trigger), B (model override), or C (service + reconciliation)?
3. Should the unknown-size admission in the last section be folded in, or filed separately?

A "no" to (1) is a perfectly good answer, and it should then be written into P28 so the next
session stops proposing it.
