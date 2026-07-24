from urbanlens.dashboard.services.spotguessr.distance import geodesic_distance_meters, location_boundary_polygon
from urbanlens.dashboard.services.spotguessr.eligibility import eligible_locations
from urbanlens.dashboard.services.spotguessr.glicko2 import DEFAULT_TAU, Opponent, Rating, rate
from urbanlens.dashboard.services.spotguessr.photos import candidate_image_for_location
from urbanlens.dashboard.services.spotguessr.ratings import apply_round_ratings
from urbanlens.dashboard.services.spotguessr.scoring import (
    RoundTarget,
    distance_for_guess,
    points_for_date_guess,
    points_for_distance,
    resolve_target,
)
from urbanlens.dashboard.services.spotguessr.selection import pick_next_location, target_rating_for_difficulty
from urbanlens.dashboard.services.spotguessr.social import friend_profiles, visible_friend_ratings
from urbanlens.dashboard.services.spotguessr.session import (
    GameConfig,
    SpotGuessrError,
    complete_session,
    get_or_create_round,
    session_summary,
    start_solo_session,
    submit_guess,
)
