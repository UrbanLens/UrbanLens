# Diagnostic and CI tooling

What exists, what each thing catches, and when it is worth running. Every tool
here was built because a specific defect got through without it — the evidence is
recorded next to each so nobody has to re-derive whether it earns its runtime.

Companion to `docs/reports/2026-08-11-codebase-audit.md`, whose **Coverage index**
lists which areas have already been swept. Check that before choosing something to
investigate; two sweeps were re-run from scratch during the audit because the
answer was buried in 1,700 lines of prose.

## Running tests

### `bin/run_tests.sh`

Runs pytest inside the test container with the sync this repo requires: copy the
tree in, chown it to `appuser`, prune `.py` files the host has deleted, then
verify host and container agree **by checksum** before running anything.

```bash
bin/run_tests.sh src/urbanlens/dashboard/tests/hypothesis/test_billing_banking.py -q
bin/run_tests.sh --fast <paths>      # reuse a persistent database
bin/run_tests.sh --fresh-db <paths>  # rebuild it (do this after any migration)
bin/run_tests.sh --verify-only       # just compare host and container
bin/run_tests.sh --allow-drift ...   # run despite drift, on purpose
```

**Use `--fast`.** Building a test database costs about 190 seconds; the tests
themselves usually cost single-digit seconds. The consensus field-scope file
takes 188s cold and 3.05s against a database that already exists. `--reuse-db`
does not apply new migrations, hence `--fresh-db` when models move.

`--allow-drift` exists for verifying a fix by breaking it: editing the
container's copy on purpose and expecting failures. Without it the parity guard
refuses the run, which is otherwise exactly what you want — a file restored on
the host but not re-copied is how the audit's only red consolidation happened.

### `bin/run_integration_tests.sh`

Drives a **deployed** instance over HTTP - real database, real Valkey, real
Celery workers, real WebSocket container, real proxy. Manual only; the config
refuses to start against production.

```bash
# On the target, once:
python src/urbanlens/manage.py provision_integration_env --out /tmp/e2e.json

bin/run_integration_tests.sh --url https://s1.dev.urbanlens.org
bin/run_integration_tests.sh --url ... --project smoke   # the 5-second version
bin/run_integration_tests.sh --url ... --docker          # needs only Docker
```

It exists because the 11,000-test pytest suite structurally cannot answer "do
the pieces work together when they are separate processes". Everything it
checks is invisible from inside one process: a bundle that did not build, a
proxy that will not upgrade a WebSocket, a worker container consuming a
different broker, a header a proxy rewrote, an asset manifest never
regenerated. Six selectable projects - `smoke`, `services`, `api`, `ui`,
`a11y`, and an opt-in `visual`.

Full documentation, including how to write a test and what it deliberately does
not cover, is `docs/INTEGRATION_TESTS.md`.

### `bin/run_mutation_tests.sh`

Mutation testing over the modules in `[tool.mutmut]` — money, privacy, and the
community-editable wiki. Roughly one mutant per second.

```bash
bin/run_mutation_tests.sh              # run every configured mutant
bin/run_mutation_tests.sh --results    # list survivors
bin/run_mutation_tests.sh --show NAME  # show one mutant's diff
```

This is the systematic form of "break the code and check the test fails". Doing
that by hand caught four vacuous tests during the audit, but only ever covers
code you are actively editing.

**It found a real gap on its first run:** a mutant replaced the billing ledger's
`select_for_update()` with `None` — keeping the refresh, dropping the lock — and
every test passed, because they drove two in-process snapshots and a lock is
invisible on one connection. `test_billing_ledger_lock.py` now covers it.

A survivor is not automatically a missing test; some sit in code the configured
selection does not exercise, which the report marks separately as `no tests`.
The ones that matter are survivors in code you believed was covered.

## Finding where to look

### `bin/report_model_writers.py`

Ranks models by how many distinct modules write them, and lists the bare
`save()` calls against each. A bare `save()` writes every column from a
possibly-stale instance: harmless on a single-writer row, a lost update on a
contested one.

This question found four of the five lost updates in the audit, and found a
fifth site the manual sweep had missed (`consensus/fields.py`). Two rankings
that **did not** work are recorded in the tool so they are not retried: by call
count (found nothing) and by I/O proximity (13 false positives out of 13).

### `bin/report_defect_history.py`

Two git-history queries:

- **Fix density** — the share of a file's commits that are fixes. Where bugs
  have been found is where bugs are.
- **The incomplete-fix query** — fixes whose own message implies more instances
  exist ("like its sibling already did", "the same fix"). Each names a spot
  where the author knew the pattern had more than one instance.

Following one such commit led to both money bugs in the billing ledger. On
2026-08-20 the same query paid out twice more, from one line of its output:
commit `1634837e` ("masks identity **like its sibling does**") had a third
instance in `services/visits/visits.py`, and reading around it confirmed two
other raw usernames were correct by design — which is the other half of the
value, since "checked and correct" is worth recording too.

Its companion `report_model_writers.py` found the `Friendship` lost update the
same day. Neither report finds bugs on its own; both narrow *where to read*, and
the reading is what finds them.

**The fix-density half was swept on 2026-08-20** — five readers over the top of
that list, each finding then handed to an adversarial verifier told to refute it.
10 findings, 9 survived, 4 fixed (see the 2026-08-20 hunt entry in
`docs/PROBLEMS.md`). Two lessons about the method rather than the findings:

- **The verify pass earns its cost in both directions.** It killed one finding
  outright (a "privacy leak" whose facts were already public by design) and
  corrected another's severity by doing arithmetic the reporter had not — the
  2FA counter degrades the lockout, it does not disable it.
- **Verify the findings yourself before acting.** Two of the four fixed differed
  from their report: the trip-activity 500 affected *every* caller rather than
  just the edit dialog, and the hidden-activity leak had two further instances
  the report did not name. A finding is a place to look, not a diagnosis.

## Structural checks (CI)

Five checkers guard properties that are invisible from a working copy, which is
exactly why they need checking — the machine that made the mistake is the one
that cannot see it.

| Check | Catches |
| --- | --- |
| `bin/check_imports_tracked.py` | An import resolving to a file git is not tracking |
| `bin/check_migration_graph.py` | A migration depending on one a fresh checkout won't have |
| `bin/check_doc_line_refs.py` | A documentation citation pointing past end-of-file |
| `bin/check_outage_not_cached.py` | A `fetch` that caches a swallowed failure as though it were an answer |
| `bin/check_notification_choke_point.py` | A notification written around the mute preference |

The last two exist because a *defect class* recurred, not because one bug did.
`check_outage_not_cached.py` came from an outage being stored as "nothing here"
and outliving the outage; `check_notification_choke_point.py` came from a mute
preference that two UI surfaces wrote and nothing read, which was possible
because ~30 places created a `NotificationLog` and honouring a preference meant
remembering it thirty times. Both replace "remember to" with "cannot forget":
notifications go through `NotificationLog.objects.notify()`, and a deliberate
bypass is marked `notify-bypass-ok: <why>` on the line above so the exemption
sits where the decision is.

`check_doc_line_refs.py --report-drift` additionally lists citations whose line
exists but no longer holds what the prose claims. That half is *not* enforced:
several name symbols that no longer exist, where the repair is rewriting the
sentence rather than the number, and a CI job should not be making that call.

Note its one blind spot: it cannot tell a specimen from a claim, so prose that
*quotes* a broken citation as an example will be flagged.

## Test helpers

### `QueryScalingMixin` (`core/tests/query_scaling.py`)

Asserts an endpoint's query count does not grow with its row count, and:

- **requires the response to actually grow**, so a seed that doesn't exercise
  the endpoint fails loudly instead of passing quietly. A survey called the
  conversation list flat while seeding pins and labels; seeding conversations
  showed ~11 queries per row. The 200-byte floor is measured, not guessed —
  identical requests vary by 11 bytes, ten real rows added 12,033;
- **reports which statements multiplied** on failure, so the cause is in the
  message rather than a separate diagnostic run.

### `run_concurrently` (`core/tests/concurrency.py`)

Runs callables on real threads released from a barrier. Necessary because a lock
is not observable on one connection — the snapshot technique proves a refresh,
not a lock.

Its docstring carries the trap that made one race test vacuous: seeding a
brand-new parent row makes both threads take `get_or_create`'s *insert* path,
where the unique index serialises them by accident.

### `assert_agrees` (`core/tests/agreement.py`)

Holds a fast reimplementation to the answers of the function it replaced, across
generated inputs rather than hand-written expectations — the author who writes
the bug also writes the expectations. Use it whenever adding a batch or cached
path over an existing predicate. It caught a real defect in the identity-batching
fix within minutes.

## Evaluated, not adopted

- **`pytest-randomly`** — the suite always runs in one order, so order
  dependence would be invisible. Probed with three shuffled seeds over the
  scaling, consensus and helper suites: **no order dependence found**. Safe to
  adopt; a full-suite shuffled run (~95 min) would be needed for confidence
  across all 11,000 tests.
- **`django-linear-migrations`** — would subsume part of
  `check_migration_graph.py` and additionally prevent branching migration
  graphs. Worth adopting if migrations ever branch across parallel work.
- **`django-test-migrations`** — asserts migrations apply *and* roll back.
  Complements the static graph check with runtime behaviour.
- **`diff-cover`** — coverage on changed lines, which targets review at what is
  new rather than reporting a whole-project percentage.
- **Runtime N+1 detectors** — attractive given three N+1s in one audit, but the
  scaling harness already covers the measured endpoints, and maintenance status
  should be checked before taking the dependency.
