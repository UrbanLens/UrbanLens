# Mobile API — SpotGuessr

Quick reference for integrating the SpotGuessr geo-guessing game into the mobile app. Full
conventions (auth, scopes, error envelope, rate limits) are in
[`docs/EXTERNAL_API.md`](../EXTERNAL_API.md) — this doc only covers what's specific to SpotGuessr.

**Base path**: `/dashboard/api/external/v1/games/spotguessr/`
**Auth**: `Authorization: Bearer <token>` (OAuth2 PKCE recommended — a fresh PAT does not carry
`games:*` by default, see EXTERNAL_API.md § Scopes).
**Scopes**: `games:read` / `games:write`, plus `social:read` for friend ratings and `media:read`
for round photos.

**Solo play only.** Multiplayer (lobby/invite/join/live-chat) is web-only and not exposed here —
starting a session via this API always creates a solo session. A session id that turns out to be
multiplayer returns `409 {"error_code": "multiplayer_unsupported"}` from the detail/round endpoints.

## Flow

1. `GET /` — start-screen data (modes, limits, own rating, resumable session).
2. Optionally `GET /eligible-count/` or `/eligible-pins/` to check the player has pins in the target area before spending session-start budget.
3. `POST /sessions/` — start a session, returns round 1 already generated.
4. `POST /sessions/{id}/rounds/{round_id}/guess/` — submit a guess, get scored.
5. `GET /sessions/{id}/round/` — fetch the next round (or completion) after each guess.
6. `GET /sessions/{id}/summary/` — final scoreboard once finished.

## Endpoints

| Method | Path | Scopes | Purpose |
|---|---|---|---|
| GET | `/` | `games:read`(+`social:read`) | Start-screen: modes, round-count/time limits, `last_config`, `own_rating`, `active_session_id`, `friend_ratings` |
| PATCH | `/preferences/` | `games:write` | Body `{show_ratings_to_friends: bool}` — only editable preference |
| GET | `/eligible-count/?geo_bounds={geojson}` | `games:read` | Count of caller's pins inside a candidate area (required query) |
| GET | `/eligible-pins/?geo_bounds={geojson}` | `games:read` | Paginated `{label, latitude, longitude}` candidates; `geo_bounds` optional (omit = all pins) |
| GET | `/sessions/` | `games:read` | Caller's session history |
| POST | `/sessions/` | `games:write` | Start a solo session + generate round 1. **40/hour throttle.** |
| GET | `/sessions/{id}/` | `games:read` | Resume-state row |
| GET | `/sessions/{id}/round/` | `games:read` (forced write throttle tier) | Current/next round, or session summary if finished. Not idempotent. |
| POST | `/sessions/{id}/rounds/{rid}/guess/` | `games:write` | Submit a guess, get scored |
| GET | `/sessions/{id}/summary/` | `games:read` | Final scoreboard |
| POST | `/sessions/{id}/rounds/{rid}/expire/` | `games:write` | Client-driven "timer hit zero"; server re-validates independently |
| POST | `/sessions/{id}/rounds/{rid}/feedback/` | `games:write` | `{kind: thumbs_up\|thumbs_down\|reported}` on a Photos-mode round's photo |
| GET | `/sessions/{id}/rounds/{rid}/image/` | `games:read` + `media:read` | Round photo bytes, EXIF stripped. Own throttle bucket (media, not JSON). |

**WebSocket**: `ws/spotguessr/session/{id}/` — real-time round/reveal sync for a session the caller
participates in. Same `games:*` scope gating as the HTTP surface (`games:read` to connect,
`games:write` to send). Optional — polling `GET .../round/` is sufficient for solo play.

## Session create (`POST /sessions/`)

Body (all optional except none are required — omit for defaults):

```
mode: "photos" | "street_view" | "named_place"   (default photos)
total_rounds: int                                 (server clamps to allowed range)
difficulty: float 0.0–1.0
allow_arbitrary_external_photos: bool             (default false)
require_visited_all: bool                         (default false)
date_guessing_enabled: bool                       (default false)
use_aliases: bool                                 (default true)
round_time_limit_seconds: int | null              (one of the choices from GET /)
geo_bounds: GeoJSON object | null                 (parsed object, not a string)
label_id: int | null                              (restrict to pins under this label)
```

Returns `201` `{session_id, mode, status, total_rounds, finished, round}`, or `409
{"error_code": "no_eligible_locations"}` if nothing matches.

## Round payload

```
round_id, session_id, mode, sequence_index, revealed,
geo_bounds, shows_imagery, expires_at,
image_url,          # photos mode only — fetch via the /image/ endpoint, needs media:read
display_text,       # named_place mode
street_view_image   # street_view mode
```

Answer fields never appear in an unrevealed round — this is an explicit server-side whitelist,
not a client-side convention.

## Guess response (`POST .../guess/`)

```
round_id, distance_meters, points, date_points, bonus_points, bonus_tiers[],
revealed, rating_delta,
actual_latitude, actual_longitude, location_name, image_caption   # only once revealed
```

`guessed_date` in the request is only scored if the session has `date_guessing_enabled` and the
round's photo has a known capture date; otherwise ignored.

## Notes

- Session ids are plain integers (no uuid) — a non-participant or nonexistent session both 404.
- `image_caption` is EXIF-derived and routinely names the location; it's withheld from the round
  payload and only sent after reveal.
- Ratings are Glicko-2 on a display (Elo-familiar) scale; `rating_delta` is per-round, not
  cumulative — sum it yourself or read `summary`/`GET /` for the settled value.
