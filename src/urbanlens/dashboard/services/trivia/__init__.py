# isort: skip_file
# The ordering below is NOT alphabetical - it's dependency ordering, and
# ruff/isort's automatic sorting would silently break it (see the comment on
# the `session` import further down, and services/spotguessr/__init__.py's
# identical warning, and "Package __init__ import ordering" in
# docs/NOTES.md for the failure mode this avoids).
# Do not let `ruff --fix` (or an editor's organize-imports action) re-sort
# this file.
from urbanlens.dashboard.services.trivia.voting import EXPLICIT_KINDS, backfill_no_reaction, effective_score, record_vote
from urbanlens.dashboard.services.trivia.deterministic import generate_deterministic_questions
from urbanlens.dashboard.services.trivia.eligibility import eligible_questions, has_eligible_questions
from urbanlens.dashboard.services.trivia.selection import pick_next_question, target_rating_for_difficulty
from urbanlens.dashboard.services.trivia.ratings import apply_round_ratings
from urbanlens.dashboard.services.trivia.realtime import broadcast, session_group_name
from urbanlens.dashboard.services.trivia.serializers import (
    serialize_chat_message,
    serialize_participant,
    serialize_reveal,
    serialize_round,
    serialize_round_reveal,
    serialize_session,
)
from urbanlens.dashboard.services.trivia.chat import recent_messages, send_chat_message
from urbanlens.dashboard.services.trivia.social import visible_friend_ratings
from urbanlens.dashboard.services.trivia.classifier import ClassifierVerdict, classify_trivia_question
from urbanlens.dashboard.services.trivia.submission import classify_and_update, submit_user_question
from urbanlens.dashboard.services.trivia.generation import generate_questions_for_wiki, sweep_wikis_for_generation
from urbanlens.dashboard.services.trivia.answer_check import is_answer_equivalent
from urbanlens.dashboard.services.trivia.wiki_incorporation import incorporate_question_into_wiki, sweep_questions_for_wiki_incorporation

# session must be imported last - it imports several of its own sibling
# submodules (eligibility, selection, voting, realtime, serializers) from
# this very package at module scope. Every name session.py needs has to
# already be fully imported (and so already cached/set as an attribute
# above) before this import runs - importing session any earlier can
# intermittently raise ImportError ("partially initialized module")
# depending on which process happens to trigger this package's import
# first - see "Package __init__ import ordering" in docs/NOTES.md.
from urbanlens.dashboard.services.trivia.session import (
    TriviaConfig,
    TriviaError,
    begin_session,
    complete_session,
    get_or_create_round,
    invite_to_session,
    join_session,
    session_summary,
    start_multiplayer_session,
    start_solo_session,
    submit_answer,
)
