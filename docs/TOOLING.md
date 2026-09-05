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

The sync covers `src/`, `bin/`, `sample_data/`, and the deployment files at the
repo root (`docker-compose.yml`, `docker-entrypoint.sh`, `gunicorn.conf.py`,
`pyproject.toml`, `uv.lock`, `.gitignore`, `.env*-sample`). The last group is
there because several suites assert on topology rather than on Python -
`test_ai_isolation`, `test_sandbox_isolation`, `test_metrics_endpoint` - and
resolve those files by path off the repo root. They are baked into the image, so
before this they were read at whatever the image was last built with **while the
checksum line still printed `tree matches`**: on 2026-09-03 that failed all 42
`ComposeTopologyTests` against a compose file predating ai-inference, which
reads as a broken branch rather than as an unsynced file.

Note the parity check still only covers `src/`. A new test reading some *other*
repo-root path will hit the same trap; add it to `sync_tree` when you write it.

```bash
bin/run_tests.sh src/urbanlens/dashboard/tests/hypothesis/test_billing_banking.py -q
bin/run_tests.sh --fast <paths>      # reuse a persistent database
bin/run_tests.sh --fresh-db <paths>  # rebuild it (do this after any migration)
bin/run_tests.sh --verify-only       # just compare host and container
bin/run_tests.sh --allow-drift ...   # run despite drift, on purpose
bin/run_tests.sh --parallel[=N] ...  # N xdist workers (default: auto)
bin/run_tests.sh --shuffle ...       # randomise order (pytest-randomly)
```

`--parallel` cuts the opposite way to `--fast`: pytest-django gives each xdist
worker its own database, so N workers means N database builds before any test
runs. It pays off on a large selection, especially combined with `--fast` (each
worker then reuses its own), and loses badly on a single file. It is not the
default because it multiplies concurrent load on Postgres, which is what has
been observed to take the local instance down — the symptom is mass "ERROR at
setup" across files that have nothing to do with each other.

`--shuffle` enables pytest-randomly, which is installed but switched off in
`addopts`. Shuffling found no order dependence when probed across three seeds,
but only over a subset, so it stays opt-in until a full shuffled run has been
green. Under it, pytest prints the seed; reproduce with that before assuming the
plugin is at fault.

**Use `--fast`.** Building a test database costs about 190 seconds; the tests
themselves usually cost single-digit seconds. The consensus field-scope file
takes 188s cold and 3.05s against a database that already exists. `--reuse-db`
does not apply new migrations, hence `--fresh-db` when models move.

**Killing a run mid-migration poisons the reusable database**, and the symptom
names neither cause. The interrupted migration leaves its schema change applied
but unrecorded, and the dead process leaves a Postgres session holding the
database open, so the next `--fresh-db` cannot drop it and quietly reuses it
instead. Every test then errors in fixture setup with pytest's internal
`assert not self._finalizers`, which is what a failed `django_db_setup` looks
like from the outside — the real error, `column ... already exists`, is only
visible above the fold, and a `| tail` hides it. Diagnosed 2026-08-24 after the
same failure had already been written off once as a flaky transient.
`--fresh-db` now terminates sessions and drops the database itself, so it means
what it says; if a run still fails this way, the database survived some other
way and dropping it by hand is the fix.

`--allow-drift` exists for verifying a fix by breaking it: editing the
container's copy on purpose and expecting failures. Without it the parity guard
refuses the run, which is otherwise exactly what you want — a file restored on
the host but not re-copied is how the audit's only red consolidation happened.

### `bin/sync_app.sh`

The same copy-into-a-container sequence as `run_tests.sh`, pointed at a running
**app** container instead of the test runner — they share `bin/lib/container_sync.sh`
so the two cannot drift apart.

```bash
bin/sync_app.sh               # copy src/ + bin/, chown, prune deletions, verify
bin/sync_app.sh --frontend    # also rebuild SCSS/TS and run collectstatic
bin/sync_app.sh --restart     # also restart the container afterwards
UL_APP_CONTAINER=... bin/sync_app.sh     # a slot other than development_main
```

Use it instead of a hand-typed `docker cp`. The hand-typed form omits the chown,
and `docker cp` preserves *source* ownership while the container runs as
`appuser` — so the copy takes away the app's ability to write what it was just
given. That has taken the container down twice, both times without saying so:
once on the logs directory (Django's logging config raises before `runserver`
binds a port) and once on `dashboard/frontend/static/dashboard/js`, where the
entrypoint's `bun run build` could not remove its own output directory and the
container crash-looped.

**`docker exec` defaults to root, and that is what makes this hard to see.**
Root can write everything, so every diagnostic, every `pytest` run, and every
manual `bun run build` succeeds while the served site is down — reading as
"permissions are fine", which is the exact opposite of the truth for the process
that matters. Reproduce as the account that actually runs: `docker exec -u appuser`.

The sync deliberately skips `urbanlens/frontend/static`, `urbanlens/media` and
`backups`, which the app container mounts as volumes *inside* the tree being
copied. Writing into the first is actively harmful: it replaces the
`staticfiles.json` nginx is serving with whatever the host last collected, and
under `ManifestStaticFilesStorage` an asset missing from the manifest is a
render-time error, not a stale file. Measured on 2026-09-04, a plain copy
swapped a 16,769-byte container manifest for a 14,166-byte host one. None of the
skipped paths hold Python or templates, so the parity check is unaffected.

`--frontend` is a separate flag because copying built assets in does not reach
what nginx serves. `collectstatic` populates a *volume* mounted at
`/app/src/urbanlens/frontend/static`; the package directory the sync writes to
is a different path. Without it the site keeps serving the bundle that was live
at last boot, and a browser check of a fresh TS change silently verifies the old
one.

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

### `bin/run_nuclei_scan.sh`

Template-driven vulnerability scan of a deployed instance - CVEs, exposed
panels and files, default credentials, misconfigured headers - via
[Nuclei](https://docs.projectdiscovery.io/opensource/nuclei/ci-cd). Manual
only, refuses production the same way `run_integration_tests.sh` does, and
excludes DoS-tagged templates unconditionally.

```bash
bin/run_nuclei_scan.sh --url https://s1.dev.urbanlens.org
bin/run_nuclei_scan.sh --url ... --docker              # needs only Docker
bin/run_nuclei_scan.sh --url ... --fail-on-findings    # gate a run on it
bin/run_nuclei_scan.sh --url ... --accounts-file /tmp/e2e.json --all-tiers
```

Preflights the target with `curl` before scanning, and never combines
`-update-templates` with a scan in one Nuclei invocation - that combination
updates the template catalogue, exits 0, and silently scans nothing, which is
how the first live run against staging reported "0 findings" that turned out
to be a broken invocation rather than a hardened deployment. `--accounts-file`
takes the same manifest `provision_integration_env` writes for
`run_integration_tests.sh`; `--all-tiers` scans four times - unauthenticated,
restricted-scope API key, full-scope API key, and a real signed-in session -
since the API key and a session reach genuinely disjoint route surfaces
rather than overlapping ones. The session tier signs in for real through
`tests/integration/setup/auth.setup.ts` rather than crafting a cookie by
hand. Full story, including two live deployment bugs and a credential-cleanup
bug this found along the way, in `docs/INTEGRATION_TESTS.md`.

Runs by default alongside `.github/workflows/integration.yml` (skip with
`run_nuclei: false` on dispatch) and is separately dispatchable as
`.github/workflows/nuclei.yml`, which runs all four tiers automatically
whenever the `staging` environment's `UL_E2E_ACCOUNTS_JSON` secret is set.
Each tier's findings upload as their own SARIF category to GitHub Code
Scanning. Full documentation is `docs/INTEGRATION_TESTS.md`.

### `bin/run_sqlmap_scan.sh`

[sqlmap](https://github.com/sqlmapproject/sqlmap) against a deployed
instance's own published OpenAPI schema, plus a crawl of the HTML/HTMX
dashboard under a real session. Nuclei detects known patterns; this actively
exploits an injection if one exists, which is why it is stricter than every
other tool here: it only runs against an **allowlist** of disposable
dev-container hosts (`UL_SQLMAP_ALLOWED_HOSTS`) rather than a denylist of
production ones, `staging.urbanlens.org` included, and a fixed set of flags
that go past confirming an injection into OS/filesystem access are refused
unconditionally, with no opt-in inside this wrapper.

```bash
bin/run_sqlmap_scan.sh --url https://s1.dev.urbanlens.org
bin/run_sqlmap_scan.sh --url ... --accounts-file /tmp/e2e.json --all-tiers
bin/run_sqlmap_scan.sh --url ... --fail-on-findings
```

sqlmap itself is not a project dependency - `bin/install_sqlmap.py` installs a
version- and hash-pinned copy (`bin/sqlmap-requirements.txt`, verified via
`pip install --require-hashes`) into its own throwaway `.sqlmap/venv`, since
sqlmap publishes no checksums of its own and this is not something every
contributor running `ruff`/`pytest` should have installed. Full documentation,
including why sqlmap's own `--openapi` flag replaced a hand-built target
generator, is `docs/INTEGRATION_TESTS.md`.

Dispatchable on its own as `.github/workflows/sqlmap.yml`; unlike Nuclei it is
**not** bundled into `integration.yml`'s dispatch, so running the integration
suite never fires this as a side effect.

### `bin/run_contract_tests.sh`

Property-based conformance testing of the external API against its own published
OpenAPI document, via schemathesis. Two modes: in-process (generate the schema
from the urlconf, drive Django's WSGI callable — no deployment needed) and live
(`--url`, fetch the schema from a deployment and call it over HTTP).

```bash
bin/run_contract_tests.sh                      # in-process, safe methods
bin/run_contract_tests.sh --methods all        # include writes
bin/run_contract_tests.sh --url https://s1.dev.urbanlens.org
```

It exists because the schema is a *deployed artefact* with generated clients
depending on it, and nothing else compared a response to it. The Python suite
checks endpoints against hand-written expectations — written by the same author
as the serializer, so both can agree while disagreeing with the schema — and the
Playwright `api` project checks the document generates, not that responses match
it.

**Its first run found two things.** `passkey_wrap_create` and
`passkey_wrap_destroy` are each claimed by two operations, which drf-spectacular
resolves by appending `_2` to whichever it reaches second — so adding a route can
rename a method downstream code calls. And no authenticated operation documents
a 401, though every one of them returns it.

Full documentation, including the three fixture traps it cost to get working, is
`docs/CONTRACT_TESTS.md`.

### `bin/report_diff_coverage.sh`

Coverage of the lines a branch changed, rather than a whole-tree percentage that
barely moves on any one commit.

```bash
bin/report_diff_coverage.sh -- <the tests covering your change>
bin/report_diff_coverage.sh --reuse            # against an existing coverage.xml
bin/report_diff_coverage.sh --fail-under 80
```

Measuring means running under coverage, which is slow, so pass the relevant
tests after `--`. A partial report is honest here in a way it would not be for
whole-tree coverage, because the lines reported on are the ones just written.

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

Fifteen checkers guard properties that are invisible from a working copy, which
is exactly why they need checking — the machine that made the mistake is the one
that cannot see it. Every one of them is `bin/check_*.py`, so the list here and
the directory can be compared with `ls`.

| Check | Catches |
| --- | --- |
| `bin/check_imports_tracked.py` | An import resolving to a file git is not tracking |
| `bin/check_migration_graph.py` | A migration depending on one a fresh checkout won't have |
| `bin/check_doc_line_refs.py` | A documentation citation pointing past end-of-file |
| `bin/check_docs_refs.py` | Code citing a `docs/` path that does not exist, or one only its author can read |
| `bin/check_docs_index.py` | `docs/INDEX.md` drifting from the entries it allocates ids for |
| `bin/check_outage_not_cached.py` | A `fetch` that caches a swallowed failure as though it were an answer |
| `bin/check_notification_choke_point.py` | A notification written around the mute preference |
| `bin/check_versioned_writes.py` | A model half-adopting field versioning, so bulk writes go unrecorded |
| `bin/check_signal_reachable.py` | A `post_save` subscription waiting on a field only a queryset `update()` sets |
| `bin/check_concealed_writes.py` | A wiki resolved for reading being saved, persisting one viewer's redacted view |
| `bin/check_pin_not_published_to_wiki.py` | A private pin's fields reaching a community wiki |
| `bin/check_template_comments.py` | A Django `{#` comment that is not closed on the same line, so the tokens render as text |
| `bin/check_line_endings.py` | A tracked text file stored with CRLF |
| `bin/check_typescript_coverage.py` | A `.ts` file in no tsconfig project, or a project `bun run typecheck` never runs |
| `bin/check_css_variables.py` | A `var()` naming a custom property nothing defines, so the rule renders its fallback on every theme |

`check_outage_not_cached.py` and `check_notification_choke_point.py` exist because a
*defect class* recurred, not because one bug did.
`check_outage_not_cached.py` came from an outage being stored as "nothing here"
and outliving the outage; `check_notification_choke_point.py` came from a mute
preference that two UI surfaces wrote and nothing read, which was possible
because ~30 places created a `NotificationLog` and honouring a preference meant
remembering it thirty times. Both replace "remember to" with "cannot forget":
notifications go through `NotificationLog.objects.notify()`, and a deliberate
bypass is marked `notify-bypass-ok: <why>` on the line above so the exemption
sits where the decision is.

`check_signal_reachable.py` came from three instances of one defect. Rules in
the reputation ledger subscribed to `post_save` on `FriendInvitation`, `Wiki`
and `WikiEdit`, whose real transitions all happen through
`QuerySet.update()` — which emits no signal, *deliberately*, because those
transitions are atomic compare-and-sets. Every one looked correct in review and
none could ever fire. The check matches a subscription's watched fields against
`.update()` calls on the same model; a deliberate case is marked
`signal-update-ok: <ModelName> <why>`. Its limits are in its docstring, and a
pass means "no detected gap" rather than "the subscription fires".

`check_concealed_writes.py` guards the seam the concealment rework created.
`resolve_visible_wiki` is the one gate all 99 wiki-scoped call sites pass
through, and since concealment moved to resolve time it may return a
*projection*: a real `Wiki` carrying only the field values one viewer may see.
Reading one is the point; saving one writes that viewer's redacted view over
what the community wrote. Nine write paths sat downstream of that gate across
four modules, and every one looked correct, because a projection is a `Wiki` and
mutating one is ordinary Django — nothing at the call site says which kind of
row it holds. They now launder through `concealment.writable_wiki`, and a
deliberate case is marked `concealed-write-ok: <why>`.

`check_template_comments.py` exists because `{# #}` is single-line, and Django
does not treat an opener that never meets `#}` on that line as a comment — the
tokens are emitted as text.

`check_versioned_writes.py` exists for the same reason as those two: provenance
has to be recorded at write time, and the wiki's *existing* edit history is
already bypassed by three writers — a bulk `update()`, a bare `save()`, and one
that omits `updated` from `update_fields`. None is visible to a `post_save`
receiver, which is why recording is interception (`VersionedModel.save()`,
`VersionedQuerySet.update()`/`bulk_update()`) rather than a funnel. The check
catches the half-adopted cases that look fine in review: `versioned_fields`
declared without the mixin, the mixin without a `VersionedQuerySet` (instance
saves recorded, every bulk write silently not — the worse half), a missing
`revision_model`, and a field name a rename left behind.

`check_typescript_coverage.py` checks two halves of one claim, because either
alone is worthless. A file in no project is never read by `tsc`, and the
pre-commit `tsc` hook still fires when you edit it — so it passes while telling
you nothing. A project nothing *runs* is the same failure one level up:
`tests/integration/` had its own `tsconfig.json`, covering 84 files, and no
command in the repository invoked it. Files that cannot join a project are
listed in the script's `_UNCOVERED` map with the reason, so the exception is a
line someone chose rather than an absence nobody can see.

`check_doc_line_refs.py --report-drift` additionally lists citations whose line
exists but no longer holds what the prose claims. That half is *not* enforced:
several name symbols that no longer exist, where the repair is rewriting the
sentence rather than the number, and a CI job should not be making that call.

Note its one blind spot: it cannot tell a specimen from a claim, so prose that
*quotes* a broken citation as an example will be flagged.

### `bin/build_docs.py`

Builds the Sphinx site, and fails if it produced no API reference.

Exit status is not the check. `sphinx-build` reports "build succeeded" for a
configuration that reads no source at all, which is what this repository shipped
until 2026-09-05: `docs/conf.py` and `docs/index.rst` existed, no `automodule`
directive was ever written, nothing ran `sphinx-apidoc`, and the output was three
pages. Meanwhile `CLAUDE.md` justified its Google-docstring standard with the
claim that Sphinx consumes them. The script asserts a floor on the number of
generated API pages instead.

`autoapi` rather than `autodoc`, so the build parses the source instead of
importing it: `autodoc` would need `django.setup()`, a settings module and a
system GDAL/GEOS install, which would confine the docs to the app container and
CI. `myst_parser` is what lets the toctree reference the Markdown that the rest
of this directory is written in.

It is slow — double-digit minutes even with `-j auto`, since `autoapi` generates
and then reads a page per module. That is why CI runs it as its own job rather
than as a step in `python-quality`. `docs/_build/` and the generated `docs/api/`
are both gitignored.

```bash
uv run python bin/build_docs.py            # what CI runs
uv run python bin/build_docs.py --strict   # warnings become errors
```

`bun run docs` is the same command, and works only where the venv is already on
`PATH` - Sphinx is a `dev` dependency, so a bare `python3` has neither it nor
`myst_parser`.

### `bin/run_codeql.py`

CodeQL is already in CI (`.github/workflows/security.yml`) on every PR. That is
after the branch exists. This is the same analysis on a working copy, so a
finding shows up before a PR does.

```bash
python bin/run_codeql.py --install     # once per machine; ~700MB download
python bin/run_codeql.py               # exhaustive: security-and-quality + local threat model
python bin/run_codeql.py --languages python
python bin/run_codeql.py --quiet       # per-rule counts only
python bin/run_codeql.py --verbose     # include note-level findings
python bin/run_codeql.py --all-queries # every query in each language pack, including experimental
python bin/run_codeql.py --gate        # the suites GitHub runs; reused when the tree is unchanged
python bin/run_codeql.py --rebuild     # recreate databases even if a previous extract finished
```

The default manual run is broader than CI on purpose. GitHub uses the
`code-scanning` suites. A local run uses
`security-and-quality` and also treats file/env/CLI as source. `--all-queries`
goes further still: every query in the Python, JavaScript/TypeScript, and GitHub
Actions packs, including experimental ones that the suites exclude for noise.

Default output is a per-rule count plus each error/warning. Notes (unused
imports, cyclic imports, and similar) are counted but not printed unless
`--verbose`. SARIF under `.codeql/results/` still has everything.

A failed JavaScript or Actions extract leaves a database with `finalised:
false`. The wrapper does not reuse that; it rebuilds. Those extractors need
**Node.js** on PATH - bun is not a substitute.

CodeQL does **not** run from a git hook. It was wired as a pre-push hook and
had to be removed: the analysis is minutes long, `--gate` exits non-zero on the
repo's known-and-triaged findings, and a GUI git client shows none of a hook's
output - so `git push` from VS Code simply failed with no explanation. CI still
runs CodeQL on every PR (`.github/workflows/security.yml`).

Run it on demand instead - the hook is still defined, at the `manual` stage:

```bash
bun run codeql:gate                                  # the suites CI runs
pre-commit run --hook-stage manual --all-files codeql # same thing, via pre-commit
```

`UL_SKIP_CODEQL=1` short-circuits the wrapper wherever it is invoked.

Install uses the official CodeQL Action *bundle*.
The standalone CLI zip does not ship query packs.

### `noUnusedLocals` (`tsconfig.json`)

On because nothing else catches dead TypeScript. `tsc` did not mind, ruff does
not read TypeScript at all, and a full green suite says nothing about code that
is never reached — a helper is written, the caller it was written for changes
shape, and twenty lines stay behind looking load-bearing.

That is not hypothetical: a metres-input helper sat unused in the floorplan
editor for a day while three near-copies of it were written around it, because
its intended caller had become a date input. Turning the flag on found nine
across the whole frontend, including a type import left by a signature change
and a value assigned only so the call that produced it looked used.

Note the flag's own blind spot: it sees locals, not exports. A module-level
`export function` nothing imports is invisible to it, and to everything else.

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

### `django_perf_rec.record()` — query fingerprints

Records the exact sequence of statements an endpoint runs to a `.perf.yml`
beside its test, so a change in query *shape* arrives as a reviewable diff.

Complementary to `QueryScalingMixin`, and the split matters. The scaling harness
measures a **slope** — same endpoint at two data sizes, fail if queries grow with
rows — which is right for a list and deliberately blind to the intercept, so an
endpoint that always cost thirty queries keeps passing. A record measures a
**fingerprint**, which catches what a slope cannot: a detail view that gains one
query per related object (three rows today, no visible slope), a
`select_related` dropped in a refactor, a cache read that became a database read.

The cost is that a legitimate query change is a diff somebody has to approve.
Re-record with `PERF_REC={"MODE": "overwrite"}` once you have confirmed it was
intended. `settings/test.py` sets `MODE` to `none` under CI, so a *missing*
record fails there rather than silently recording whatever the code does today —
including the regression under review.

Applied in `dashboard/tests/hypothesis/test_query_records.py` to the external
API's most-fetched endpoints, `whoami/` among them as a floor: if that record
grows, the cost went into authentication or middleware, and every other endpoint
grew by the same amount without any of their records explaining why.

## Evaluated, not adopted

- **`nplusone`** — the obvious runtime N+1 detector, and rejected on two counts:
  it has not shipped a release since 2019, and `django-auto-prefetch` (already a
  dependency) suppresses exactly the access pattern it watches for, so it would
  be quietest where this codebase's N+1s actually came from — model *properties*
  that fall back to a query. `django-perf-rec` was adopted instead, above.
- **`django-linear-migrations`** — would subsume part of
  `check_migration_graph.py` and additionally prevent branching migration
  graphs. Worth adopting if migrations ever branch across parallel work.
- **`django-test-migrations`** — asserts migrations apply *and* roll back.
  Complements the static graph check with runtime behaviour.
- **VCR.py / `responses` cassettes** — would replace the ~60 hand-written
  `mock.patch` sites for outbound HTTP. Rejected for now: most of those sites
  sit under Hypothesis property tests, where each generated input is a different
  request and a recorded cassette either misses or has to be re-recorded, and
  recording against the real gateways needs live credentials for REData,
  Overpass and Stripe. (`respx` does not apply at all — it is httpx-only, and
  this project is on `requests`.)
- **`testcontainers-python`** — an ephemeral PostGIS per run. `bin/run_tests.sh`
  already runs pytest inside the project's own compose stack against real
  PostGIS, so this would replace a working setup rather than add a capability.
- **Load testing (Locust / k6)** — a genuine gap: `QueryScalingMixin` proves
  query counts do not grow per row, which says nothing about connection-pool
  exhaustion or gevent worker behaviour under concurrency. Wants its own scoped
  effort against a deployment, not a bolt-on here.
