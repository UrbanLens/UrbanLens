# isort: skip_file
# The ordering below is NOT alphabetical - it's dependency ordering, and
# ruff/isort's automatic sorting would silently break it (see the comment
# on the `session` import further down for why). Do not let `ruff --fix`
# (or an editor's organize-imports action) re-sort this file.
from urbanlens.dashboard.services.spotguessr.chat import CHAT_HISTORY_LIMIT, recent_messages, send_chat_message
from urbanlens.dashboard.services.spotguessr.distance import geodesic_distance_meters, location_boundary_polygon
from urbanlens.dashboard.services.spotguessr.eligibility import eligible_locations
from urbanlens.dashboard.services.spotguessr.glicko2 import DEFAULT_TAU, Opponent, Rating, rate
from urbanlens.dashboard.services.spotguessr.named_place import candidate_name_for_location
from urbanlens.dashboard.services.spotguessr.photo_coordinates import record_guess
from urbanlens.dashboard.services.spotguessr.photos import candidate_image_for_location
from urbanlens.dashboard.services.spotguessr.ratings import apply_round_ratings
from urbanlens.dashboard.services.spotguessr.realtime import broadcast, session_group_name
from urbanlens.dashboard.services.spotguessr.relevance import EXPLICIT_KINDS, backfill_no_reaction, record_feedback
from urbanlens.dashboard.services.spotguessr.scoring import (
    RoundTarget,
    distance_for_guess,
    points_for_date_guess,
    points_for_distance,
    resolve_target,
    street_view_target,
)
from urbanlens.dashboard.services.spotguessr.selection import pick_next_location, target_rating_for_difficulty
from urbanlens.dashboard.services.spotguessr.serializers import (
    serialize_chat_message,
    serialize_participant,
    serialize_reveal,
    serialize_round,
    serialize_round_reveal,
    serialize_session,
)
from urbanlens.dashboard.services.spotguessr.social import friend_profiles, visible_friend_ratings
from urbanlens.dashboard.services.spotguessr.street_view import candidate_street_view_for_location

# session must be imported last - it imports several of its own sibling
# submodules (eligibility, named_place, photo_coordinates, photos, realtime,
# relevance, scoring, selection, serializers, street_view) from this very
# package at module scope. Every name session.py needs has to already be
# fully imported (and so already cached/set as an attribute above) before
# this import runs - importing session any earlier intermittently raises
# ImportError ("partially initialized module") depending on which process
# happens to trigger this package's import first (celery workers hit it,
# a plain `manage.py check` didn't) - see docs/PROBLEMS.md.
from urbanlens.dashboard.services.spotguessr.session import (
    GameConfig,
    SpotGuessrError,
    begin_session,
    complete_session,
    get_or_create_round,
    invite_to_session,
    join_session,
    session_summary,
    start_multiplayer_session,
    start_solo_session,
    submit_guess,
)
