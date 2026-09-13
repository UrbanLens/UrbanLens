# Code assessment noted during comment-trim pass — 2026-09-11

> Comment-trim workers are comment-only by rule. The items below are
> code-level observations for another agent to review. Nothing here has been
> acted on except where marked "already applied".

## Already applied (behavior-preserving, flagging for awareness)

1. `src/urbanlens/dashboard/tests/hypothesis/test_saved_filter_color_opacity.py`
   — the module defined `_FILTER_NAME = "Ruins"` twice, with a duplicated `#:` comment
   block. The duplicate assignment and one copy of the comment were removed.
   Revert the code line if a strictly comment-only diff is required; the remaining
   `#:` comment could also be shortened to one line.

## Needs another agent (not touched)

2. `src/urbanlens/dashboard/services/spotguessr/session.py` and
   `src/urbanlens/dashboard/services/trivia/session.py` — an automated trimmer hit an
   unterminated-literal edge on these files and rolled back, so they were left
   untrimmed. They likely still contain wordy blocks; trim by hand.
3. The stripping commits `f68b3f3f`, `4fad3406`, `821e7a24`, `5b9fced0`,
   `7849157a`, `0a19100c` carry the caveat that weaker-model output "requires
   additional scrutiny" and may need partial revert where a comment was load-bearing.
   Suggested spot-checks before merge:
   - `src/urbanlens/dashboard/models/fields.py` (encryption `fail_soft` guidance),
   - `src/urbanlens/dashboard/models/abstract/versioned.py` (snapshot/lock ordering),
   - `src/urbanlens/dashboard/consumers.py` (revoked-broadcast defense-in-depth),
   - `src/urbanlens/dashboard/services/media/origin.py` (PSL two-label floor),
   - `src/urbanlens/UrbanLens/settings/base.py` (CSP / worker / throttle notes).
   - `src/urbanlens/dashboard/tests/hypothesis/test_pin_detail_fanout_budget.py`
     (`MAX_LOAD_TRIGGERED_REQUESTS = 48` — value unchanged, history comment removed).
4. `docs/INDEX.md` was not updated: this file is a handoff note, not an indexed
   record, so no new id was allocated. If any item above becomes a tracked problem,
   allocate the next free `P` id there per `docs/README.md`.

## Likely runtime bug (ruff F821, pre-existing, not introduced by trim pass)

5. `src/urbanlens/dashboard/models/consensus/queryset.py:85-87` —
   `ConsensusRoundQuerySet.for_round(self, round_)` filters on a bare undefined name:
   `return self.filter(session=session).order_by("sequence_index")`. Any call raises
   `NameError`. Siblings (`ConsensusAnswer/Vote/RoundPhoto.for_round`) filter on
   `round=round_`, so the intent here is ambiguous: either the parameter should be a
   session (`def for_session(self, session)`, matching `ConsensusSessionChatMessage.for_session`
   two classes down) or the filter should be `session=round_.session`. Needs an owner
   who knows the caller to pick the fix and add a regression test.
