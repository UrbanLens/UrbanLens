# Test Quality Audit — Progress Tracker

Standing task (started 2026-08-29, run as an open-ended `/loop`): review every unit test file
under `src/urbanlens/{core,dashboard}/tests/` for **test quality**, not just "does it pass."
Specifically, for each file:

1. Does it cover both the happy path AND negative/error cases (invalid input, permission-denied,
   wrong-owner, boundary conditions, duplicates, empty/None, expected exceptions)?
2. Would each assertion actually FAIL against a plausible buggy implementation (wrong branch,
   off-by-one, missing check, swallowed exception) — or does it just assert non-null / no-exception
   / "a mock was called" without checking the real effect?
3. Are assumptions baked into the test (ordering, global state, mock behavior) actually guaranteed
   by the implementation's contract?

Genuine gaps get fixed (tests added/strengthened) in place. This is **not** a rewrite pass — files
that already have real positive+negative coverage are left alone and marked `solid`.

The full frozen file list (832 files, numbered, snapshotted at start so batch ranges stay stable
even if other concurrent agents add/remove test files mid-audit) is in
`docs/notes/ai/test-quality-audit-files.txt`.

## Method (revised after batch 1 — see log)

- Batches of 20 files (from the manifest, in order), run via the `Workflow` tool.
- Within a batch, sub-agents run in groups of 4 concurrent (not all 20 at once) — local Postgres
  has been observed to crash under heavy concurrent test-DB load (see memory:
  `local-postgres-crashes-under-concurrent-load`). Each sub-agent uses a unique
  `UL_TEST_DB_NAME` and `--reuse-db`.
- **Do NOT mutate implementation code to "prove" a test is meaningful**, even temporarily. Jess
  rejected this explicitly — see memory `feedback-negative-before-positive-same-test`. It caused
  a real regression in `core/version.py` (a stray unrevert) AND is suspected of hanging a whole
  batch (see batch 1 log below). Instead: for gated/conditional behavior, assert the negative case
  first, transition state for real, then assert the positive case, in the same test:
  ```python
  def test_user_is_denied_without_permission(self):
      user = baker.make("User")
      with self.assertRaises(AuthenticationException):
          action_that_requires_a_subscription(user)
      add_subscription_to_user(user)
      result = action_that_requires_a_subscription(user)
      assert result == some_value
  ```
  A negative-only test still passes against an implementation that always denies; this pattern
  catches that. **Only apply this where a real gate/condition exists.** Batch 2 caught an agent
  bolting `assertFalse(User.objects.filter(pk=x).exists())` onto the front of a test for a plain,
  unconditional `hard_delete_profile()` call — trivially false since nothing had deleted anything
  yet. There's no "denied" pre-state to assert when the function being tested has no gate.
- After a batch's Workflow returns: wait ~2 minutes (deliberate pause so the next review is done
  with fresh eyes, per the requester's explicit process), then actually assess the batch's diff
  and findings, run a consolidated `pytest` across the batch's files as a final check, commit, and
  launch the next batch.
- Findings that are real *product* bugs (not test-quality issues) get a one-line note in
  `docs/PROBLEMS.md` rather than a full fix, unless trivial and obviously safe.

## Status

- Total files: 832 | Batch size: 20 | Total batches: ~42
- **PAUSED 2026-08-29 ~16:20** to resume on a different machine. Batches 1-3 (24 files, manifest
  lines 1-24) are done, verified, committed, and pushed (`release/v_0_8_0` is up to date with
  origin as of the pause). Batch 4 (lines 45-64) had only just started - see below.
- **Resuming on a new machine**: this file and `test-quality-audit-files.txt` are in
  `docs/notes/ai/`, which is **gitignored** (see its own `.gitignore` entry) - they will NOT be
  on a fresh clone/pull. Copy both files over manually, or re-paste this file's content into the
  new session, before picking the work back up - otherwise the new session has no record of what's
  done and will either redo work or lose the frozen manifest's stable line numbering.
- A Workflow probe batch (`wf_4afcb30e-ed3`, files 45-48: `test_backfilled_place_geometry.py`,
  `test_background_enrichment.py`, `test_backup_code_single_use.py`, `test_backup_services.py`)
  was stopped mid-run for the pause - **none of its work survived**. One result came back before
  stopping: `test_backup_code_single_use.py`'s agent designed 2 good new tests (cross-user
  scoping, exact-row consumption) but couldn't verify them - local Postgres was down for its whole
  run ("FATAL: the database system is starting up", the documented concurrent-load crash) - and by
  the time of the pause, `git status` on that file shows it clean (no diff from HEAD): whatever it
  wrote either never got flushed or was reverted before the stop. **Files 45-48 are untouched and
  need a fresh start on resume**, not a resume-from-cache - do not assume any partial credit here.
- Completed files: 24 (batches 1-3)

## Batch Log

| Batch | Manifest lines | Status | Notes |
|------:|----------------|--------|-------|
| 1 | 1–4 (of intended 1–20) | done, partial | Workflow run `wf_8763d2b5-d0a` completed lines 1–4 (`test_cache_keys.py`, `test_https.py`, `test_testing_network.py`, `test_version.py` — all `gaps_fixed`, real negative-case coverage added, mutation-verified by the agents per the *old* prompt). One agent left `core/version.py` with an un-reverted mutation (stripped `--noinput` from a migrate subprocess call) — caught via `git diff` review and reverted before commit. The run then stalled: no journal/transcript activity for 30+ min on the remaining 16 files (lines 5–20), consistent with a hung sub-agent (plausibly the version.py one, mid mutation-check). Abandoned; consolidated pytest on the 4 completed files passed clean (71 passed, 12 subtests, 132.8s). Already committed upstream by another concurrent agent's broad commit (`fe264d11`) before I could commit it myself — content verified sound regardless. Lines 5–20 carried forward into batch 2. Prompt corrected for batch 2+ (see Method above). |
| 3 | 25–44 | done | Workflow run `wf_9c8413e3-4fb` hit the account's **monthly spend limit** partway through (9/20 agents completed cleanly, 11 failed with "You've hit your monthly spend limit"; see claude.ai/settings/usage to raise it). Of the 11 failures, 2 files (`test_api_spend_summary.py`, `test_article_expansion.py`) turned out to already have real, complete-looking edits from elsewhere in this shared working tree (per Jess: don't investigate further, already handled) - left as-is. The remaining **9 files with no content at all were reviewed and fixed manually by me directly (no subagents)**, since further Workflow batches will likely keep hitting the same cap: `test_article_conflict_locking.py` (added 2 tests exercising the concealed-viewer conflict-check branch, currently dead code since `concealment_active` is hardcoded False - mocked to prove it works ahead of the reputation-ledger feature landing), `test_articles.py` (added both-hosts-provided ValueError case, retrofit two 404-only ownership/visibility tests to prove the positive case on the same subject first), `test_audit_inverted_friendship_blocks.py` (added exact date-cutoff boundary test), `test_auto_nest_buildings.py` (strengthened a count-only assertion to check identity), `test_auto_removals.py` (reviewed, already solid, no changes), `test_auto_tag_redata_source.py` + `test_auto_tag_service.py` + `test_auto_tagging_gate.py` (retrofit several negative-only gate tests to flip back and prove the positive on the same subject, per Jess's "apply this nearly always" correction), `test_avatar_colors.py` (added an over-palette-size boundary test). All fixes verified via direct `pytest` runs (see below), no Workflow involved for this remainder. All 8 changed files verified (141 tests passing) and committed (`5740cc82`). |
| 4 | 45–64 | starting | Probing with a small (4-file) Workflow batch first to check whether the spend limit has cleared before committing to a full 20-file run or falling back to manual review again. The underlying Claude Code process restarted mid-probe (unrelated to the spend limit - a session continuity break, not a billing issue); both the already-abandoned batch-1 workflow and this probe came back as `status: stopped` with a `resumeFromRunId` on the next wakeup. Resumed the probe via `Workflow({scriptPath, resumeFromRunId})` - **if this recurs, that's the recovery move**: relaunch with the same scriptPath + resumeFromRunId rather than starting a fresh run, so completed agent() calls replay from cache instead of re-billing. **Then paused entirely (stopped, not just idled) at Jess's request to resume on a different machine** - the probe workflow and its verification pytest were both explicitly killed via TaskStop rather than left running. See the Status section above for exactly what state files 45-48 are in (none of it survived - start fresh). |
| 2 | 5–24 | done | Workflow run `wf_08a4d492-5d3` — all 20 agents completed, 0 errors, no hang this time. 3 `solid` (no changes: `test_account.py`, `test_add_pin_dialog_closefn.py`, `test_addressable.py`), 16 `gaps_fixed` with genuine, well-reasoned negative-case/boundary additions (several correctly applying the negative-then-positive-same-subject pattern), 1 `gaps_found_not_fixed` (`test_alias_views.py` — a real product bug: `PinAliasView.post` didn't sanitize before its emptiness check, so an emoji/markup-only name raised an uncaught `ValueError` → 500; agent correctly left the new regression test failing rather than loosening it). **Caught one unauthorized implementation edit** to `settings/base.py` (DRF `DEFAULT_RENDERER_CLASSES`) during the git-status sanity check — turned out NOT to be from this audit at all (none of the 20 agent reports mentioned it); it was another concurrent agent's legitimate WIP security fix, which I nearly destroyed by reverting it before realizing the mistake and restoring it from the diff I'd already captured. See memory `feedback-verify-attribution-before-reverting-shared-tree-diffs`. Fixed the one real bug myself (`PinAliasView.post`, mirroring the already-correct wiki-side view) since it was small, well-understood, and directly guarded by the new regression test — all 35 alias tests now pass. Six other genuine findings (network-guard bypass via `connect_ex`, `make_cache_key` colon-collision, missing overlap lock on the hard-delete sweep, over-eager achievement backfill re-queue, dangling-comma address formatting, duplicated AI-assistant quota logic) logged to `docs/PROBLEMS.md` under "Test-quality audit follow-ups (2026-08-29)" rather than fixed, per scope. **Consolidated verification found 2 more failures** 
