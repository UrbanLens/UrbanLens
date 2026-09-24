# Storage quotas: the running-total proposal, and the decision not to build it

## D21 — Uploads are admitted under a per-profile database lock; the running total stays unbuilt

`id: D21` · `status: accepted` · `updated: 2026-09-24` · `supersedes: D8`

Taken 2026-09-24 by the agent fixing N29's G3-21/G5-19, G3-23/G3-30 and G2-22 (P154), under Jess's
standing instruction for that batch to fix verified problems at the root while there are no users.
**Not yet confirmed by Jess**; it reverses D8's "the lock stays as it is".

What it decides:

- **One critical section per profile**, `services/media/storage.reserve_upload(profile, size)`: a
  transaction-scoped Postgres advisory lock keyed on the profile, then the quota read as
  `SUM(file_size)` over the profile's counted rows, then the caller's own checks and insert, then
  commit. A second upload by the same profile waits at the lock and reads a total that already
  includes the first one's row. The wait is bounded (20 s for a request, 120 s for an import task);
  past it the upload is refused with 429 rather than admitted unchecked.
- **Everything that decides whether a row may be written goes inside it**: the quota, the
  duplicate-checksum lookups, the per-suggestion photo cap, and the external-media daily ceiling
  (`media_materialize`), which is a rolling window and so could never be a counter anyway.
- **Still no denormalised counter.** D8's reasoning about the counter stands: `file_size` changes in
  five places (I4, below), and a trigger-maintained total on one row would put a second, hotter lock
  in front of the sandbox worker, whose `process_image_upload` rewrites `file_size` and would queue
  behind whichever upload held that row. Reading the rows under the lock needs no release on failure,
  no delete hook and no reconciliation sweep: a refused, rolled-back, rejected or re-encoded upload
  is counted as whatever its row says, immediately.

Why D8 no longer holds: D8 accepted a lock that narrowed the race, but the lock did not narrow it.
`acquire_lock` was a single non-blocking `cache.add`, and on contention `per_profile_upload_lock`
logged and ran the body anyway, so two concurrent uploads were never serialized, and a visit log
held it past its 30 s TTL. The same gap let one file be stored twice (the checksum lookup ran
before the lock) and let parallel calls pass a spent external-media allowance. The race tests in
`tests/hypothesis/test_upload_reservation_races.py` fail against the old code on all three.

Cost: one profile's uploads are serialized, including each file write. The browser uploads files one
at a time (`album-items.ts`, `uploadFilesToAlbum`), so the contention left is another tab or a
background import, whose tasks hold the lock for one asset at a time. Not measured under load. What
would reopen it: a client that uploads one profile's files in parallel for throughput, at which point
reservations would need to be committed rows with an expiry rather than a lock held across the write.

---

## D8 — Storage quotas are enforced generally, not exactly

`id: D8` · `status: superseded` · `updated: 2026-09-24` · `superseded-by: D21`

**Decided 2026-09-06 by Jess, answering the questions at the foot of this document: no.** Exact
quota enforcement is not worth a denormalised counter.

The reasoning, in her words: *"It's not absolutely critical that we enforce quotas precisely.
Enforcing them generally still gets us to a place where users can upload and use assets for the
site, without completely unbounded storage being eaten up by a single user. General and imprecise
enforcement is therefore fine."*

The overshoot is bounded and self-limiting, and the behaviour it lands in is already the intended
one: **an over-quota profile keeps everything it has uploaded and is barred from uploading more
until it is back under.** Verified 2026-09-06 rather than assumed - nothing in `dashboard/` deletes
or purges anything on a quota failure; every one of the call sites refuses only the new upload (413
from the interactive paths, a skip in the four `tasks.py` fetch-and-store ones), and
`quota_error_for_upload`'s own message tells the user to free space themselves.

**What this decides:**

- The running-total column is **not being built**. The proposal below is kept as the record of what
  it would have cost and why, so the next session finds an answer instead of re-deriving the
  question.
- `per_profile_upload_lock` stays as it is - a fail-open narrowing of the race, applied at every
  call site since 2026-08-25. It is not a bound on a bulk fan-out, and it is not meant to be.
- The unknown-size admission described under "What this does *not* fix" is covered by the same
  reasoning and needs no separate entry: it is imprecision that self-corrects, since the true size
  is recorded once stored and the *next* check sees it. (That last inference is mine from the
  reasoning above, not something Jess said in as many words.)

**What would reopen it:** a plan to sell storage, or any other point at which "roughly enforced" is
no longer a defensible answer to a user who paid for a number of gigabytes. The proposal below is
the starting point if that day comes.

---

## I4 — The running-total column: what it is, and why the lock isn't enough

`id: I4` · `status: absorbed` · `updated: 2026-09-06`

Written 2026-09-06, because P28's sign-off asked for the "running-total column" proposal to be
re-explained before anyone implements it. Nothing here was built, and per D8 above nothing will be.
Kept as the record of the option that was considered and declined.

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

`per_profile_upload_lock` (removed 2026-09-24, see D21) was meant to narrow that gap and was
**deliberately fail-open**: a caller that could not take the lock logged a warning and proceeded.
In practice it narrowed nothing, since it never waited (D21). That is the right call for one interactive upload — a
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

## The decision that was asked for — and given

1. Is exact quota enforcement worth a denormalised counter at all, given that the current failure
   needs a bulk import to trigger and overshoots by a bounded amount? — **No** (D8, above).
2. If yes: mechanism A (trigger), B (model override), or C (service + reconciliation)? — Moot.
3. Should the unknown-size admission in the last section be folded in, or filed separately? —
   Neither; it is the same kind of imprecision D8 accepts, and it self-corrects.

The "no" was the answer this section said would be a good one. It is recorded in D8 above and in
P28's archive entry, so the next session finds the answer rather than re-deriving the question.
