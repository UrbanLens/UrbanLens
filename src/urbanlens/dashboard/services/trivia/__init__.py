# isort: skip_file
# The ordering below is NOT alphabetical - it's dependency ordering, and
# ruff/isort's automatic sorting would silently break it (see the comment on
# the `session` import further down, and services/spotguessr/__init__.py's
# identical warning / docs/PROBLEMS.md for the failure mode this avoids).
# Do not let `ruff --fix` (or an editor's organize-imports action) re-sort
# this file.
from urbanlens.dashboard.services.trivia.voting import EXPLICIT_KINDS, backfill_no_reaction, effective_score, record_vote
from urbanlens.dashboard.services.trivia.deterministic import generate_deterministic_questions
from urbanlens.dashboard.services.trivia.eligibility import eligible_questions, has_eligible_questions
from urbanlens.dashboard.services.trivia.selection import pick_next_question, target_rating_for_difficulty
from urbanlens.dashboard.services.trivia.ratings import apply_round_ratings

# session must be imported last - it imports several of its own sibling
# submodules (eligibility, selection, voting) from this very package at
# module scope. Every name session.py needs has to already be fully
# imported (and so already cached/set as an attribute above) before this
# import runs - importing session any earlier can intermittently raise
# ImportError ("partially initialized module") depending on which process
# happens to trigger this package's import first - see
# services/spotguessr/__init__.py's identical precedent and docs/PROBLEMS.md.
from urbanlens.dashboard.services.trivia.session import (
    TriviaConfig,
    TriviaError,
    complete_session,
    get_or_create_round,
    session_summary,
    start_solo_session,
    submit_answer,
)
