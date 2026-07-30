# isort: skip_file
# The ordering below is NOT alphabetical - it's dependency ordering, and
# ruff/isort's automatic sorting would silently break it (see the comment
# on the `session` import further down for why, and
# services/spotguessr/__init__.py for the precedent). Do not let
# `ruff --fix` (or an editor's organize-imports action) re-sort this file.
from urbanlens.dashboard.services.consensus.chat import CHAT_HISTORY_LIMIT, recent_messages, send_chat_message
from urbanlens.dashboard.services.consensus.eligibility import eligible_wikis, eligible_wikis_for_all, has_eligible_wikis, has_eligible_wikis_for_all
from urbanlens.dashboard.services.consensus.fields import ConsensusFieldStrategy, RoundContent, get_strategy
from urbanlens.dashboard.services.consensus.points import award_points, award_points_for_manual_edit, level_for_points, points_required_for_level
from urbanlens.dashboard.services.consensus.realtime import broadcast, session_group_name
from urbanlens.dashboard.services.consensus.selection import RoundSelection, pick_next_round_content
from urbanlens.dashboard.services.consensus.serializers import (
    serialize_answer,
    serialize_chat_message,
    serialize_consensus_profile,
    serialize_participant,
    serialize_round,
    serialize_round_reveal,
    serialize_session,
)
from urbanlens.dashboard.services.consensus.tentative import record_tentative_answers
from urbanlens.dashboard.services.consensus.trust import record_check_result, should_inject_check, trust_score, trust_score_for_profile
from urbanlens.dashboard.services.consensus.voting import ConsensusVotingError, VoteTally, cluster_answers, open_vote, record_vote, tally_votes

# session must be imported last - it imports several of its own sibling
# submodules (eligibility, fields, points, realtime, selection, serializers,
# tentative, trust, voting) from this very package at module scope. Every
# name session.py needs has to already be fully imported (and so cached as
# an attribute above) before this import runs - see
# services/spotguessr/__init__.py's identical comment for why importing
# session any earlier intermittently raises ImportError.
from urbanlens.dashboard.services.consensus.session import (
    ConsensusError,
    begin_session,
    complete_session,
    end_session_now,
    force_reveal_round,
    force_resolve_vote,
    get_or_create_round,
    invite_to_session,
    join_session,
    resolve_vote,
    session_summary,
    skip_round,
    start_competitive_session,
    start_solo_session,
    submit_answer,
    submit_vote,
)
