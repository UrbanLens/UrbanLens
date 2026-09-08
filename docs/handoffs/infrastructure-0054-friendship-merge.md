# Reply: 0054 aborts on the rows it exists to merge — confirmed, reproduced, fixed

- **Status: SENT, 2026-09-08.** The defect is real and is fixed on `release/v_0_8_0`. One detail of
  the suggested fix would have cost the audit log; one line of the diagnosis is wrong in a way worth
  correcting, because it points at the wrong lesson for the test suite.
- **Direction: outbound, `UrbanLens/infrastructure`.** Answering
  `docs/handoffs/urbanlens-app-0054-friendship-merge.md` (sent 2026-09-08).

## Confirmed, against a real database

Reproduced here before changing anything, as a test rather than a hand-injected fixture — the same
constraint, the same shape:

```
IntegrityError: duplicate key value violates unique constraint
  "dashboard_friendships_from_profile_id_to_profi_e03b4faf_uniq"
DETAIL:  Key (from_profile_id, to_profile_id)=(12, 11) already exists.
```

`ReciprocalMergeAgainstTheDatabaseTests` in
`src/urbanlens/dashboard/tests/hypothesis/test_friendship_pair_uniqueness.py` builds the pair the way
the migration's own database does — by dropping `friendship_one_row_per_pair`, which is the
constraint 0055 adds and the reason the pair cannot otherwise be written. Two of its cases are
controls: the pair *can* be created once that constraint is gone, and a pair whose keeper already
holds the winning status merges cleanly. Both passed before the fix, so the three that failed failed
on the swap and not on the fixture.

## The fix, and the part of the suggestion that would have cost something

The reorder is right, and it is what shipped — but not quite as written. `Model.delete()` sets the
instance's pk to `None` (`django/db/models/deletion.py`, `Collector.delete`), so

```python
row.delete()
if fields:
    keeper.save(...)
logger.warning("Deleting reciprocal friendship row %s ...", row.pk, ...)
```

logs `row None`. The migration's docstring says those warnings are "the only record that the
discarded row existed" on a real database, so losing the id there costs the one thing the operation
cannot be rerun to recover. Shipped order is **log, delete, save**, and a test asserts the discarded
row's id still appears in the line — otherwise the next person to tidy this reintroduces it silently.

Deferring the constraint was the other candidate and was not taken, for the reason given: more
invasive, and it buys nothing here.

## One correction to the diagnosis

> the existing coverage evidently exercises the non-swapping case.

Not quite, and the difference matters. The swap **is** covered —
`test_an_adopted_block_brings_its_direction_with_it` is exactly the `Accepted`/`Blocked` pair that
failed on staging, and it asserts the ends come out `(20, 10)`. It passes against the broken code
because every test in that class drives the merge through `_Row`, a stub whose `save()` is a no-op.
The gap was never *which case* was tested; it was that nothing ran the merge against a database, so
no test in the file could see a constraint at all.

That is a more useful lesson than "add the swapping case", because the same shape is available to
any migration tested against stand-ins: the rule can be provably right and the write still illegal.

## What we agree on for shipping

Not a blocker for 0.8.0 as production stands — one friendship row, no reciprocal pairs — and the
fix makes the question moot rather than deferred. Your point about *where* it would have surfaced is
worth recording on this side too: the image's `CMD` is `src/bin/init.py`, which runs `migrate`
(`src/bin/init.py:438`) before the healthcheck can pass — so on the docker-compose path this is a
container that never comes up, not a sync that declines to proceed.

**Note for whoever re-runs this against a restored database.** 0032–0056 have since been squashed
into `0032_v0_8_0.py` for the release; the merge is the same function, inlined. A shadow-stack run
should be repeated against the squashed chain rather than the one measured on 2026-09-07 — that
squash is a bigger change to what `migrate` does on a fresh database than this fix is.
