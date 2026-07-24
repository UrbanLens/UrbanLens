/**
 * SpotGuessr (UL-391..UL-393) - gameplay, multiplayer lobby, and chat.
 *
 * Server-authoritative: this file never computes a score or reveals an
 * answer itself - it only collects a guess (map click or pin search),
 * posts it, and renders whatever `services.spotguessr.session` decided.
 * Multiplayer state sync (lobby updates, round advancement, chat) arrives
 * over a WebSocket (`consumers.GameSessionConsumer`); solo sessions never
 * open one at all.
 */
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";

declare const L: typeof import("leaflet");
import type {} from "leaflet-draw";

declare global {
    interface Window {
        SPOTGUESSR_URLS: {
            start: string;
            pins: string;
            settings: string;
            friends: string;
            lobby: string;
            invite: string;
            join: string;
            begin: string;
            round: string;
            guess: string;
            photo_feedback: string;
            chat_history: string;
            summary: string;
            session_id_sentinel: string;
            round_id_sentinel: string;
        };
        SPOTGUESSR_LAST_CONFIG: LastConfig | null;
    }
}

// Mirrors services.spotguessr.session.GameConfig.to_dict() - the profile's
// last-used settings, restored on page load (applyLastConfig). total_rounds
// isn't part of this - it's a GameSession field, never persisted to
// SpotGuessrPreference, so the rounds slider always starts at the default.
interface LastConfig {
    difficulty: number;
    external_media_only: boolean;
    allow_arbitrary_external_photos: boolean;
    require_visited_all: boolean;
    date_guessing_enabled: boolean;
    use_aliases: boolean;
    geo_bounds_geojson: GeoJSON.Geometry | null;
}

interface RoundPayload {
    round_id: number;
    session_id: number;
    mode: string;
    sequence_index: number;
    revealed: boolean;
    image_url?: string;
    display_text?: string | null;
    street_view_image?: string | null;
}

interface RevealPayload {
    round_id: number;
    revealed: boolean;
    distance_meters: number;
    points: number;
    date_points: number;
    // Only present when `revealed` is true - see showReveal().
    actual_latitude?: number;
    actual_longitude?: number;
    location_name?: string;
    error?: string;
}

interface RoundRevealResult {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    distance_meters: number;
    points: number;
    date_points: number;
}

interface RoundRevealBroadcast {
    round_id: number;
    actual_latitude: number;
    actual_longitude: number;
    location_name: string;
    results: RoundRevealResult[];
}

interface ParticipantPayload {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    status: string;
    total_points: number;
    is_host: boolean;
}

interface SessionPayload {
    session_id: number;
    mode: string;
    status: string;
    total_rounds: number;
    host_profile_id: number;
    participants: ParticipantPayload[];
}

interface SummaryParticipant {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    total_points: number;
}

interface SummaryPayload {
    session_id: number;
    rounds_played: number;
    total_rounds: number;
    participants: SummaryParticipant[];
}

interface PinOption {
    label: string;
    latitude: number;
    longitude: number;
}

interface FriendOption {
    profile_id: number;
    username: string;
}

interface ChatMessagePayload {
    message_id: number;
    profile_id: number;
    username: string;
    body: string;
    created: string;
}

const urls = window.SPOTGUESSR_URLS;
const DEFAULT_CENTER: L.LatLngExpression = [39.5, -98.35];
const DEFAULT_ZOOM = 4;

const pageEl = document.querySelector<HTMLElement>(".spotguessr-page");
const myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");
const regionSearchUrl = pageEl?.dataset.regionSearchUrl ?? "";

let sessionId: number | null = null;
let currentRoundId: number | null = null;
let currentMode = "photos";
let totalRounds = 0;
let sessionScore = 0;
let dateGuessingEnabled = false;
let isMultiplayer = false;
let hostProfileId: number | null = null;
let lastRevealedRoundId: number | null = null;
let ws: WebSocket | null = null;

let guessMap: L.Map | null = null;
let guessMarker: L.Marker | null = null;
let actualMarker: L.Marker | null = null;
let resultLine: L.Polyline | null = null;

let areaMap: L.Map | null = null;
let areaDrawnItems: L.FeatureGroup | null = null;
// Set by applyLastConfig() when a saved geo_bounds exists - drawn once
// ensureAreaMap() actually builds the map (deferred until the dialog is
// visible, since a <dialog> without `open` has zero size before showModal()).
let restoredGeoBounds: GeoJSON.Geometry | null = null;

let pinOptions: PinOption[] = [];
let friendOptions: FriendOption[] = [];
const selectedInviteIds = new Set<number>();
let scoreboard: SummaryParticipant[] = [];

function el<T extends HTMLElement = HTMLElement>(id: string): T {
    const found = document.getElementById(id);
    if (!found) throw new Error(`SpotGuessr: missing #${id}`);
    return found as T;
}

function urlFor(template: string, sessionIdValue?: number, roundIdValue?: number): string {
    let resolved = template;
    if (sessionIdValue !== undefined) resolved = resolved.replace(urls.session_id_sentinel, String(sessionIdValue));
    if (roundIdValue !== undefined) resolved = resolved.replace(urls.round_id_sentinel, String(roundIdValue));
    return resolved;
}

async function postForm(url: string, data: Record<string, string> | URLSearchParams): Promise<any> {
    const body = data instanceof URLSearchParams ? data : new URLSearchParams(data);
    const response = await fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
        body,
    });
    return response.json();
}

async function getJson(url: string): Promise<any> {
    const response = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
    return response.json();
}

// ---------------------------------------------------------------------------
// Settings panel
// ---------------------------------------------------------------------------

type Difficulty = "easy" | "medium" | "hard";

// The backend accepts any float 0.0-1.0 (see docs/designs/spotguessr.md's
// Gaussian-kernel difficulty mapping) - these 3 buttons just quantize the
// player's choice down to one of 3 representative values.
const DIFFICULTY_VALUES: Record<Difficulty, number> = { easy: 0.2, medium: 0.5, hard: 0.8 };

function currentDifficulty(): number {
    const checked = document.querySelector<HTMLInputElement>('input[name="sg-difficulty-choice"]:checked');
    return DIFFICULTY_VALUES[(checked?.value as Difficulty) ?? "medium"];
}

function initRoundsSlider(): void {
    const slider = el<HTMLInputElement>("sg-rounds");
    const label = el("sg-rounds-label");
    slider.addEventListener("input", () => {
        label.textContent = slider.value;
    });
}

function currentSettingsMode(): string {
    return el<HTMLInputElement>("sg-settings-mode").value || "photos";
}

function updateModeVisibility(): void {
    const mode = currentSettingsMode();
    document.querySelectorAll<HTMLElement>("[data-mode-only]").forEach((field) => {
        field.hidden = field.dataset.modeOnly !== mode;
    });
}

// The dialog is shared across all 3 mode cards - a gear icon click scopes it
// to that card's mode (updateModeVisibility shows/hides [data-mode-only]
// fields accordingly); there's no in-dialog mode switcher.
function openSettingsDialog(mode: string): void {
    el<HTMLInputElement>("sg-settings-mode").value = mode;
    updateModeVisibility();
    el<HTMLDialogElement>("sg-settings-dialog").showModal();
    if (areaMap) {
        // Already built while the dialog was previously closed (0-size) -
        // nudge Leaflet now that it's actually visible.
        areaMap.invalidateSize();
    } else if (el<HTMLInputElement>("sg-restrict-area").checked) {
        // First time the dialog's ever been opened with restrict-area
        // already on (applyLastConfig pre-checked it) - build the map now
        // that it has real size, and draw the restored region once.
        ensureAreaMap();
        if (restoredGeoBounds) {
            setAreaGeometry(restoredGeoBounds);
            restoredGeoBounds = null;
        }
    }
}

function initModeCards(): void {
    document.querySelectorAll<HTMLButtonElement>(".spotguessr-mode-card-body").forEach((button) => {
        button.addEventListener("click", () => {
            const mode = button.dataset.mode;
            if (!mode) return;
            el<HTMLInputElement>("sg-settings-mode").value = mode;
            void startGame(mode);
        });
    });
    document.querySelectorAll<HTMLButtonElement>(".spotguessr-card-gear").forEach((button) => {
        button.addEventListener("click", () => {
            const mode = button.dataset.mode;
            if (mode) openSettingsDialog(mode);
        });
    });
}

function initRatingsToggle(): void {
    const checkbox = el<HTMLInputElement>("sg-show-ratings-to-friends");
    checkbox.addEventListener("change", () => {
        void postForm(urls.settings, { show_ratings_to_friends: checkbox.checked ? "on" : "off" });
    });
}

interface RegionSearchResult {
    display_name: string;
    geojson: GeoJSON.Geometry;
}

function ensureAreaMap(): L.Map {
    if (areaMap) return areaMap;
    areaMap = L.map("sg-area-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(areaMap);
    areaDrawnItems = new L.FeatureGroup();
    areaMap.addLayer(areaDrawnItems);
    const drawControl = new L.Control.Draw({
        draw: { polygon: {}, rectangle: false, circle: false, marker: false, polyline: false, circlemarker: false },
        edit: { featureGroup: areaDrawnItems },
    });
    areaMap.addControl(drawControl);
    areaMap.on(L.Draw.Event.CREATED, (event: L.LeafletEvent) => {
        const { layer } = event as unknown as { layer: L.Polygon };
        setAreaGeometry(layer.toGeoJSON().geometry);
    });
    return areaMap;
}

// Exactly one region is ever active - a new search selection or hand-drawn
// polygon always replaces whatever was there before, matching the old
// rectangle-only tool's single-shape behavior (no include/exclude sets).
function setAreaGeometry(geometry: GeoJSON.Geometry): void {
    const map = ensureAreaMap();
    areaDrawnItems?.clearLayers();
    const layer = L.geoJSON(geometry as GeoJSON.GeoJsonObject);
    layer.eachLayer((shapeLayer) => areaDrawnItems?.addLayer(shapeLayer));
    el<HTMLInputElement>("sg-area-geo-bounds").value = JSON.stringify(geometry);
    const bounds = areaDrawnItems?.getBounds();
    if (bounds?.isValid()) map.fitBounds(bounds, { padding: [40, 40] });
}

function initAreaRestriction(): void {
    const toggle = el<HTMLInputElement>("sg-restrict-area");
    const wrap = el("sg-area-map-wrap");
    toggle.addEventListener("change", () => {
        wrap.hidden = !toggle.checked;
        if (!toggle.checked) return;
        if (areaMap) {
            areaMap.invalidateSize();
            return;
        }
        ensureAreaMap();
    });
}

async function searchAreaRegion(): Promise<void> {
    const input = el<HTMLInputElement>("sg-area-search-input");
    const query = input.value.trim();
    const resultsEl = el("sg-area-search-results");
    const messageEl = el("sg-area-search-message");
    resultsEl.hidden = true;
    resultsEl.innerHTML = "";
    messageEl.hidden = true;
    if (!query || !regionSearchUrl) return;

    let data: { results?: RegionSearchResult[] };
    try {
        data = await getJson(`${regionSearchUrl}?q=${encodeURIComponent(query)}`);
    } catch {
        messageEl.textContent = "Could not search for that place right now.";
        messageEl.hidden = false;
        return;
    }

    const results = data.results ?? [];
    if (!results.length) {
        messageEl.textContent = "No area boundary found for that place - try a broader name, like a state or city, or draw the region manually.";
        messageEl.hidden = false;
        return;
    }
    const [onlyResult] = results;
    if (results.length === 1 && onlyResult) {
        setAreaGeometry(onlyResult.geojson);
        return;
    }
    for (const result of results) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "spotguessr-area-search-result";
        button.textContent = result.display_name;
        button.addEventListener("click", () => {
            setAreaGeometry(result.geojson);
            resultsEl.hidden = true;
        });
        resultsEl.appendChild(button);
    }
    resultsEl.hidden = false;
}

function initAreaSearch(): void {
    el("sg-area-search-btn").addEventListener("click", () => void searchAreaRegion());
    el<HTMLInputElement>("sg-area-search-input").addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        void searchAreaRegion();
    });
}

function currentGeoBoundsGeoJson(): string | null {
    return el<HTMLInputElement>("sg-area-geo-bounds").value || null;
}

async function loadPinOptions(): Promise<void> {
    const data = await getJson(urls.pins);
    pinOptions = data.pins ?? [];
    const datalist = el<HTMLDataListElement>("sg-pin-options");
    datalist.innerHTML = "";
    for (const pin of pinOptions) {
        const option = document.createElement("option");
        option.value = pin.label;
        datalist.appendChild(option);
    }
}

function initPinSearch(): void {
    const input = el<HTMLInputElement>("sg-pin-search");
    input.addEventListener("change", () => {
        const match = pinOptions.find((pin) => pin.label === input.value);
        if (match) placeGuessMarker(L.latLng(match.latitude, match.longitude));
    });
}

// ---------------------------------------------------------------------------
// Friend invite picker
// ---------------------------------------------------------------------------

async function loadFriendOptions(): Promise<FriendOption[]> {
    if (friendOptions.length) return friendOptions;
    const data = await getJson(urls.friends);
    friendOptions = data.friends ?? [];
    return friendOptions;
}

// Fetched unconditionally at page load (not gated behind the "Play with
// friends" toggle) so the list is ready the moment someone opens that
// section, instead of the old permanent "Loading friends..." whenever the
// toggle was never successfully clicked.
async function fetchFriendsEagerly(): Promise<void> {
    const loadingEl = el("sg-friend-list-loading");
    const errorEl = el("sg-friend-list-error");
    loadingEl.hidden = false;
    errorEl.hidden = true;
    try {
        await loadFriendOptions();
    } catch {
        loadingEl.hidden = true;
        errorEl.hidden = false;
        toast.error("Couldn't load your friends list.");
        return;
    }
    loadingEl.hidden = true;
    renderFriendCheckboxes(el("sg-friend-list"), friendOptions, new Set());
}

function initFriendListRetry(): void {
    el("sg-friend-list-retry").addEventListener("click", () => void fetchFriendsEagerly());
}

function renderFriendCheckboxes(container: HTMLElement, friends: FriendOption[], excludeIds: Set<number>): void {
    container.innerHTML = "";
    const available = friends.filter((friend) => !excludeIds.has(friend.profile_id));
    if (!available.length) {
        container.innerHTML = '<p class="spotguessr-panel-hint">No friends available to invite.</p>';
        return;
    }
    for (const friend of available) {
        const label = document.createElement("label");
        const wrap = document.createElement("span");
        wrap.className = "ul-checkbox-wrap";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = String(friend.profile_id);
        // Preserve prior selections across re-renders (e.g. toggling
        // "play with friends" off then on) - selectedInviteIds is the
        // source of truth read at submit time, so the checkboxes must
        // reflect it rather than always starting unchecked.
        checkbox.checked = selectedInviteIds.has(friend.profile_id);
        checkbox.addEventListener("change", () => {
            if (checkbox.checked) selectedInviteIds.add(friend.profile_id);
            else selectedInviteIds.delete(friend.profile_id);
        });
        const box = document.createElement("span");
        box.className = "ul-checkbox";
        wrap.append(checkbox, box);
        const nameSpan = document.createElement("span");
        nameSpan.textContent = friend.username;
        label.append(wrap, nameSpan);
        container.appendChild(label);
    }
}

function initFriendPicker(): void {
    const toggle = el<HTMLInputElement>("sg-play-with-friends");
    const wrap = el("sg-invite-wrap");
    toggle.addEventListener("change", async () => {
        wrap.hidden = !toggle.checked;
        if (!toggle.checked) return;
        // Friends are already fetched eagerly at page load (see
        // fetchFriendsEagerly) - this await is just a safety net for the
        // rare case the toggle is checked before that fetch resolves.
        const friends = await loadFriendOptions();
        renderFriendCheckboxes(el("sg-friend-list"), friends, new Set());
    });
}

async function handleInviteMore(): Promise<void> {
    if (sessionId === null) return;
    const friends = await loadFriendOptions();
    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, sessionId));
    const alreadyInvited = new Set(lobby.participants.map((participant) => participant.profile_id));
    const available = friends.filter((friend) => !alreadyInvited.has(friend.profile_id));
    if (!available.length) {
        toast.error("Everyone on your friends list is already in this game.");
        return;
    }
    const chosenName = window.prompt(`Invite who? (${available.map((friend) => friend.username).join(", ")})`);
    if (!chosenName) return;
    const chosen = available.find((friend) => friend.username === chosenName);
    if (!chosen) return;

    const response = await postForm(urlFor(urls.invite, sessionId), { profile_id: String(chosen.profile_id) });
    if (response.error) {
        toast.error(response.error);
        return;
    }
    const refreshed: SessionPayload = await getJson(urlFor(urls.lobby, sessionId));
    renderLobbyParticipants(refreshed.participants);
}

// ---------------------------------------------------------------------------
// Guess map
// ---------------------------------------------------------------------------

function ensureGuessMap(): L.Map {
    if (guessMap) return guessMap;
    guessMap = L.map("sg-guess-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(guessMap);
    guessMap.on("click", (event) => placeGuessMarker(event.latlng));
    return guessMap;
}

function placeGuessMarker(latlng: L.LatLng): void {
    const map = ensureGuessMap();
    if (guessMarker) {
        guessMarker.setLatLng(latlng);
    } else {
        guessMarker = L.marker(latlng, { draggable: true }).addTo(map);
    }
    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = false;
}

function resetGuessMap(): void {
    const map = ensureGuessMap();
    if (guessMarker) {
        map.removeLayer(guessMarker);
        guessMarker = null;
    }
    if (actualMarker) {
        map.removeLayer(actualMarker);
        actualMarker = null;
    }
    if (resultLine) {
        map.removeLayer(resultLine);
        resultLine = null;
    }
    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = true;
    map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
}

// ---------------------------------------------------------------------------
// Lobby
// ---------------------------------------------------------------------------

function renderLobbyParticipants(participants: ParticipantPayload[]): void {
    const list = el("sg-lobby-participants");
    list.innerHTML = "";
    for (const participant of participants) {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = participant.is_host ? `${participant.username} (host)` : participant.username;
        const status = document.createElement("span");
        status.className = participant.status === "joined" ? "spotguessr-lobby-status spotguessr-lobby-status--joined" : "spotguessr-lobby-status";
        status.textContent = participant.status === "joined" ? "Joined" : "Invited";
        item.append(name, status);
        list.appendChild(item);
    }

    const me = participants.find((participant) => participant.profile_id === myProfileId);
    const isHost = hostProfileId === myProfileId;
    el<HTMLButtonElement>("sg-invite-more-btn").hidden = !isHost;
    el<HTMLButtonElement>("sg-join-lobby-btn").hidden = !(me && me.status === "invited");
    el<HTMLButtonElement>("sg-begin-btn").hidden = !isHost;
}

function renderLobby(session: SessionPayload): void {
    hostProfileId = session.host_profile_id;
    currentMode = session.mode;
    totalRounds = session.total_rounds;
    el("sg-settings-panel").hidden = true;
    el("sg-lobby-panel").hidden = false;
    el("sg-game-panel").hidden = true;
    el("sg-summary-panel").hidden = true;
    renderLobbyParticipants(session.participants);
    connectSessionSocket();
}

async function refreshLobby(): Promise<void> {
    if (sessionId === null) return;
    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, sessionId));
    renderLobbyParticipants(lobby.participants);
}

// ---------------------------------------------------------------------------
// Gameplay
// ---------------------------------------------------------------------------

function renderRound(round: RoundPayload, roundNumber: number): void {
    currentRoundId = round.round_id;
    currentMode = round.mode;
    el("sg-settings-panel").hidden = true;
    el("sg-lobby-panel").hidden = true;
    el("sg-summary-panel").hidden = true;
    el("sg-game-panel").hidden = false;
    el("sg-reveal-panel").hidden = true;
    el("sg-round-status").textContent = `Round ${roundNumber} of ${totalRounds}`;
    el("sg-score-status").textContent = isMultiplayer ? "" : `Score: ${sessionScore}`;

    const photo = el<HTMLImageElement>("sg-round-photo");
    const nameHeading = el("sg-round-name");
    const pinSearchWrap = el("sg-pin-search-wrap");

    if (round.mode === "named_place") {
        photo.hidden = true;
        nameHeading.hidden = false;
        nameHeading.textContent = round.display_text ?? "";
        pinSearchWrap.hidden = true; // Named Place mode is map-click only, per spec - no pin search.
    } else {
        nameHeading.hidden = true;
        pinSearchWrap.hidden = false;
        photo.hidden = false;
        photo.src = (round.mode === "street_view" ? round.street_view_image : round.image_url) ?? "";
    }

    el("sg-date-field").hidden = !dateGuessingEnabled;
    el("sg-photo-feedback").hidden = true;
    el("sg-photo-feedback-thanks").hidden = true;
    resetGuessMap();
    // The map container was hidden (display:none) while another panel was
    // showing, so Leaflet needs a nudge once it's visible again.
    setTimeout(() => guessMap?.invalidateSize(), 0);
}

// ---------------------------------------------------------------------------
// Score card - shared by the reveal results list, the live scoreboard, and
// the summary panel (renderResultsList/renderScoreboard/showSummary below).
// ---------------------------------------------------------------------------

interface ScoreCardData {
    profileId: number;
    username: string;
    avatarUrl: string | null;
    points: number;
    subtitle?: string;
}

function avatarInitial(username: string): string {
    return username.charAt(0).toUpperCase() || "?";
}

function renderScoreCard(data: ScoreCardData, rank?: number): HTMLElement {
    const template = el<HTMLTemplateElement>("sg-score-card-template");
    const node = template.content.firstElementChild?.cloneNode(true);
    if (!node) throw new Error("SpotGuessr: score card template is empty");
    const card = node as HTMLElement;
    card.dataset.profileId = String(data.profileId);

    const rankEl = card.querySelector<HTMLElement>(".spotguessr-score-card-rank");
    if (rankEl) rankEl.textContent = rank ? `#${rank}` : "";
    if (rank && rank <= 3) card.classList.add(`spotguessr-score-card--rank-${rank}`);

    const avatarWrap = card.querySelector<HTMLElement>(".spotguessr-score-card-avatar-wrap");
    if (avatarWrap) {
        if (data.avatarUrl) {
            const img = document.createElement("img");
            img.className = "friend-avatar-sm";
            img.src = data.avatarUrl;
            img.alt = data.username;
            avatarWrap.appendChild(img);
        } else {
            const placeholder = document.createElement("span");
            placeholder.className = "friend-avatar-sm friend-avatar-placeholder";
            placeholder.textContent = avatarInitial(data.username);
            avatarWrap.appendChild(placeholder);
        }
    }

    const nameEl = card.querySelector<HTMLElement>(".spotguessr-score-card-name");
    if (nameEl) nameEl.textContent = data.username;
    const subtitleEl = card.querySelector<HTMLElement>(".spotguessr-score-card-subtitle");
    if (subtitleEl) subtitleEl.textContent = data.subtitle ?? "";
    const pointsEl = card.querySelector<HTMLElement>(".spotguessr-score-card-points");
    if (pointsEl) pointsEl.textContent = `${data.points} pts`;

    return card;
}

// Solo sessions show "Your score: X" instead of a username-labeled card -
// there's only ever one participant, and naming them is just noise.
function renderScoreCardList(container: HTMLElement, entries: ScoreCardData[], options: { solo: boolean }): void {
    container.innerHTML = "";
    const sorted = [...entries].sort((a, b) => b.points - a.points);
    const [only] = sorted;
    if (options.solo && sorted.length === 1 && only) {
        const item = document.createElement("li");
        item.className = "spotguessr-score-card spotguessr-score-card--solo";
        item.textContent = `Your score: ${only.points} pts`;
        container.appendChild(item);
        return;
    }
    sorted.forEach((entry, index) => container.appendChild(renderScoreCard(entry, index + 1)));
}

function renderResultsList(results: RoundRevealResult[]): void {
    const list = el("sg-reveal-results");
    if (!isMultiplayer) {
        list.hidden = true;
        return;
    }
    list.hidden = false;
    renderScoreCardList(
        list,
        results.map((result) => ({
            profileId: result.profile_id,
            username: result.username,
            avatarUrl: result.avatar_url,
            points: result.points + result.date_points,
            subtitle: `${(result.distance_meters / 1000).toFixed(2)} km away`,
        })),
        { solo: false },
    );
}

function updateScoreboardFromResults(results: RoundRevealResult[]): void {
    for (const result of results) {
        let entry = scoreboard.find((participant) => participant.profile_id === result.profile_id);
        if (!entry) {
            entry = { profile_id: result.profile_id, username: result.username, avatar_url: result.avatar_url, total_points: 0 };
            scoreboard.push(entry);
        }
        entry.total_points += result.points + result.date_points;
    }
    renderScoreboard();
    // Pulse whichever cards actually changed this round - renderScoreboard()
    // rebuilds the whole list, so this has to run after, keyed off the
    // profile_id dataset attribute renderScoreCard() stamps on each card.
    const list = el("sg-scoreboard");
    for (const result of results) {
        const card = list.querySelector<HTMLElement>(`[data-profile-id="${result.profile_id}"]`);
        if (!card) continue;
        card.classList.add("spotguessr-score-card--pulse");
        card.addEventListener("animationend", () => card.classList.remove("spotguessr-score-card--pulse"), { once: true });
    }
}

function renderScoreboard(): void {
    const list = el("sg-scoreboard");
    if (!isMultiplayer || !scoreboard.length) {
        list.hidden = true;
        return;
    }
    list.hidden = false;
    renderScoreCardList(
        list,
        scoreboard.map((entry) => ({ profileId: entry.profile_id, username: entry.username, avatarUrl: entry.avatar_url, points: entry.total_points })),
        { solo: false },
    );
}

function showPhotoFeedbackIfApplicable(): void {
    // The photo itself (unlike the answer) has been visible since the round
    // started, whether or not everyone's guessed yet - so feedback on it is
    // always fair game once there's a reveal panel to put the buttons in.
    // Street View also shows imagery, but only Photos-mode rounds have a
    // `round.image` for GamePhotoFeedback to attach to server-side.
    el("sg-photo-feedback").hidden = currentMode !== "photos";
    el("sg-photo-feedback-thanks").hidden = true;
}

function dropInMarker(marker: L.Marker): void {
    marker.getElement()?.classList.add("spotguessr-marker-drop");
}

function showReveal(reveal: RevealPayload): void {
    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = true;
    sessionScore += reveal.points + reveal.date_points;
    el("sg-score-status").textContent = isMultiplayer ? "" : `Score: ${sessionScore}`;
    showPhotoFeedbackIfApplicable();

    const distanceKm = (reveal.distance_meters / 1000).toFixed(2);
    if (!reveal.revealed) {
        // Multiplayer: not everyone has guessed yet - the answer is withheld
        // so this player can't relay it via chat before their teammates
        // guess too. showBroadcastReveal() completes this once round.revealed
        // arrives (lastRevealedRoundId is left untouched so that handler
        // knows it still needs to draw the actual marker/line itself).
        el("sg-reveal-panel").hidden = false;
        el("sg-reveal-title").textContent = "Guess submitted!";
        let detail = `${reveal.points} points – ${distanceKm} km away. Waiting for other players…`;
        if (reveal.date_points) detail = `${reveal.points} points (+${reveal.date_points} for the date guess) – ${distanceKm} km away. Waiting for other players…`;
        el("sg-reveal-detail").textContent = detail;
        el("sg-reveal-results").hidden = true;
        el<HTMLButtonElement>("sg-next-round-btn").hidden = true;
        return;
    }

    lastRevealedRoundId = reveal.round_id;
    const map = ensureGuessMap();
    const actualLatLng = L.latLng(reveal.actual_latitude as number, reveal.actual_longitude as number);
    actualMarker = L.marker(actualLatLng).addTo(map);
    dropInMarker(actualMarker);

    if (guessMarker) {
        const guessLatLng = guessMarker.getLatLng();
        resultLine = L.polyline([guessLatLng, actualLatLng], { color: "#e74c3c" }).addTo(map);
        map.fitBounds(L.latLngBounds([guessLatLng, actualLatLng]), { padding: [40, 40] });
    } else {
        map.setView(actualLatLng, 14);
    }

    el("sg-reveal-panel").hidden = false;
    el("sg-reveal-title").textContent = reveal.location_name || "Revealed!";
    let detail = `${reveal.points} points – ${distanceKm} km away`;
    if (reveal.date_points) detail += ` (+${reveal.date_points} for the date guess)`;
    el("sg-reveal-detail").textContent = detail;
    el("sg-reveal-results").hidden = true; // filled in by the round.revealed broadcast, for multiplayer
    el<HTMLButtonElement>("sg-next-round-btn").hidden = isMultiplayer; // multiplayer advances automatically
}

function showBroadcastReveal(data: RoundRevealBroadcast): void {
    if (lastRevealedRoundId !== data.round_id) {
        lastRevealedRoundId = data.round_id;
        const map = ensureGuessMap();
        const actualLatLng = L.latLng(data.actual_latitude, data.actual_longitude);
        actualMarker = L.marker(actualLatLng).addTo(map);
        dropInMarker(actualMarker);
        if (guessMarker) {
            const guessLatLng = guessMarker.getLatLng();
            resultLine = L.polyline([guessLatLng, actualLatLng], { color: "#e74c3c" }).addTo(map);
            map.fitBounds(L.latLngBounds([guessLatLng, actualLatLng]), { padding: [40, 40] });
        } else {
            map.setView(actualLatLng, 14);
        }
        el("sg-reveal-panel").hidden = false;
        el("sg-reveal-title").textContent = data.location_name || "Revealed!";
        el("sg-reveal-detail").textContent = "";
        el<HTMLButtonElement>("sg-submit-guess-btn").disabled = true;
        el<HTMLButtonElement>("sg-next-round-btn").hidden = true;
    }
    updateScoreboardFromResults(data.results);
    renderResultsList(data.results);
}

function showSummary(summary: SummaryPayload): void {
    el("sg-game-panel").hidden = true;
    el("sg-lobby-panel").hidden = true;
    el("sg-summary-panel").hidden = false;
    renderScoreCardList(
        el("sg-summary-scores"),
        summary.participants.map((participant) => ({
            profileId: participant.profile_id,
            username: participant.username,
            avatarUrl: participant.avatar_url,
            points: participant.total_points,
        })),
        { solo: !isMultiplayer },
    );
    if (ws) {
        ws.close();
        ws = null;
    }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function startGame(mode: string): Promise<void> {
    currentMode = mode;
    dateGuessingEnabled = currentMode === "photos" && el<HTMLInputElement>("sg-date-guessing").checked;
    sessionScore = 0;
    scoreboard = [];
    lastRevealedRoundId = null;

    const geoBounds = el<HTMLInputElement>("sg-restrict-area").checked ? currentGeoBoundsGeoJson() : null;
    const body = new URLSearchParams({
        mode: currentMode,
        difficulty: String(currentDifficulty()),
        total_rounds: el<HTMLInputElement>("sg-rounds").value,
        external_media_only: el<HTMLInputElement>("sg-external-media-only").checked ? "on" : "off",
        allow_arbitrary_external_photos: el<HTMLInputElement>("sg-allow-arbitrary-external-photos").checked ? "on" : "off",
        require_visited_all: el<HTMLInputElement>("sg-require-visited-all").checked ? "on" : "off",
        date_guessing_enabled: dateGuessingEnabled ? "on" : "off",
        use_aliases: el<HTMLInputElement>("sg-use-aliases").checked ? "on" : "off",
    });
    if (geoBounds) body.append("geo_bounds", geoBounds);
    for (const profileId of selectedInviteIds) body.append("invite_profile_ids", String(profileId));

    const response = await postForm(urls.start, body);
    if (response.error) {
        toast.error(response.error);
        return;
    }
    if (response.error_code === "no_eligible_locations") {
        showNoEligibleLocations();
        return;
    }

    sessionId = response.session_id;
    totalRounds = Number(el<HTMLInputElement>("sg-rounds").value);

    if (response.lobby) {
        isMultiplayer = true;
        renderLobby(response.session as SessionPayload);
        return;
    }

    isMultiplayer = false;
    if (response.finished) {
        showSummary(response.summary);
        return;
    }
    await loadPinOptions();
    renderRound(response.round, response.round.sequence_index + 1);
}

async function submitGuess(): Promise<void> {
    if (!guessMarker || sessionId === null || currentRoundId === null) return;
    const latlng = guessMarker.getLatLng();
    const payload: Record<string, string> = { latitude: String(latlng.lat), longitude: String(latlng.lng) };
    if (dateGuessingEnabled) {
        const dateValue = el<HTMLInputElement>("sg-guessed-date").value;
        if (dateValue) payload.guessed_date = dateValue;
    }

    const reveal: RevealPayload = await postForm(urlFor(urls.guess, sessionId, currentRoundId), payload);
    if (reveal.error) {
        toast.error(reveal.error);
        return;
    }
    showReveal(reveal);
}

async function submitPhotoFeedback(kind: "thumbs_up" | "thumbs_down" | "reported"): Promise<void> {
    if (sessionId === null || currentRoundId === null) return;
    const response = await postForm(urlFor(urls.photo_feedback, sessionId, currentRoundId), { kind });
    if (response.error) {
        toast.error(response.error);
        return;
    }
    el("sg-photo-feedback-thanks").hidden = false;
}

async function goToNextRound(): Promise<void> {
    // Multiplayer sessions advance automatically via the round.started
    // broadcast - this button is hidden for them (see showReveal).
    if (sessionId === null) return;
    const data = await getJson(urlFor(urls.round, sessionId));
    if (data.no_eligible_locations) {
        showNoEligibleLocations();
        return;
    }
    if (data.finished) {
        showSummary(data.summary);
        return;
    }
    renderRound(data.round, data.round.sequence_index + 1);
}

async function joinLobby(): Promise<void> {
    if (sessionId === null) return;
    const response = await postForm(urlFor(urls.join, sessionId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
    await refreshLobby();
}

async function beginGame(): Promise<void> {
    if (sessionId === null) return;
    const response = await postForm(urlFor(urls.begin, sessionId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
    el("sg-lobby-panel").hidden = true;
    if (response.no_eligible_locations) {
        showNoEligibleLocations();
        return;
    }
    if (response.finished) {
        showSummary(response.summary);
        return;
    }
    await loadPinOptions();
    renderRound(response.round, 1);
}

function resetToSettings(): void {
    sessionId = null;
    currentRoundId = null;
    sessionScore = 0;
    scoreboard = [];
    selectedInviteIds.clear();
    isMultiplayer = false;
    lastRevealedRoundId = null;
    if (ws) {
        ws.close();
        ws = null;
    }
    el("sg-summary-panel").hidden = true;
    el("sg-lobby-panel").hidden = true;
    el("sg-game-panel").hidden = true;
    el("sg-empty-state-panel").hidden = true;
    el("sg-settings-panel").hidden = false;
}

// Distinguishes "never got to play a single round because nothing eligible
// matched this mode/settings combination" from a real completed game -
// see controllers.spotguessr's error_code/no_eligible_locations shapes.
function showNoEligibleLocations(): void {
    resetToSettings();
    el("sg-settings-panel").hidden = true;
    el("sg-empty-state-panel").hidden = false;
}

function initEmptyState(): void {
    el("sg-empty-state-settings-btn").addEventListener("click", () => {
        resetToSettings();
        openSettingsDialog(currentMode);
    });
}

// Pre-fills the (still-closed) settings dialog from the profile's last-used
// settings, saved on every previous game start (SpotGuessrStartView) but
// never read back until now. Runs once at page load, before any dialog is
// opened - geo_bounds is only staged (restoredGeoBounds) since actually
// drawing it requires the dialog to be visible first (see openSettingsDialog).
function applyLastConfig(): void {
    const config = window.SPOTGUESSR_LAST_CONFIG;
    if (!config) return;

    let nearestDifficulty: Difficulty = "medium";
    let smallestGap = Infinity;
    for (const key of Object.keys(DIFFICULTY_VALUES) as Difficulty[]) {
        const gap = Math.abs(DIFFICULTY_VALUES[key] - config.difficulty);
        if (gap < smallestGap) {
            smallestGap = gap;
            nearestDifficulty = key;
        }
    }
    el<HTMLInputElement>(`sg-difficulty-${nearestDifficulty}`).checked = true;

    el<HTMLInputElement>("sg-external-media-only").checked = config.external_media_only;
    el<HTMLInputElement>("sg-allow-arbitrary-external-photos").checked = config.allow_arbitrary_external_photos;
    el<HTMLInputElement>("sg-date-guessing").checked = config.date_guessing_enabled;
    el<HTMLInputElement>("sg-use-aliases").checked = config.use_aliases;
    el<HTMLInputElement>("sg-require-visited-all").checked = config.require_visited_all;

    if (config.geo_bounds_geojson) {
        el<HTMLInputElement>("sg-restrict-area").checked = true;
        el("sg-area-map-wrap").hidden = false;
        restoredGeoBounds = config.geo_bounds_geojson;
    }
}

// ---------------------------------------------------------------------------
// Real-time (multiplayer only)
// ---------------------------------------------------------------------------

function connectSessionSocket(): void {
    if (ws || sessionId === null) return;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(`${proto}${location.host}/ws/spotguessr/session/${sessionId}/`);
    ws.addEventListener("message", (event) => {
        try {
            handleSocketMessage(JSON.parse(event.data));
        } catch {
            // Ignore unparseable frames - nothing actionable to do with one.
        }
    });
    ws.addEventListener("close", () => {
        ws = null;
    });
    el("sg-chat-panel").hidden = false;
    void loadChatHistory();
}

function handleSocketMessage(data: any): void {
    switch (data.type) {
        case "participant.joined":
            void refreshLobby();
            break;
        case "session.started":
            el("sg-lobby-panel").hidden = true;
            renderRound(data.round, 1);
            break;
        case "round.revealed":
            showBroadcastReveal(data as RoundRevealBroadcast);
            break;
        case "round.started":
            renderRound(data.round, data.round.sequence_index + 1);
            break;
        case "session.completed":
            showSummary(data as SummaryPayload);
            break;
        case "chat.message":
            appendChatMessage(data.message as ChatMessagePayload);
            break;
        default:
            break;
    }
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

function appendChatMessage(message: ChatMessagePayload): void {
    const log = el("sg-chat-log");
    const line = document.createElement("div");
    const nameSpan = document.createElement("span");
    nameSpan.className = "spotguessr-chat-username";
    nameSpan.textContent = `${message.username}:`;
    line.append(nameSpan, document.createTextNode(message.body));
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

async function loadChatHistory(): Promise<void> {
    if (sessionId === null) return;
    const data = await getJson(urlFor(urls.chat_history, sessionId));
    const log = el("sg-chat-log");
    log.innerHTML = "";
    for (const message of (data.messages ?? []) as ChatMessagePayload[]) appendChatMessage(message);
}

function initChat(): void {
    el("sg-chat-form").addEventListener("submit", (event) => {
        event.preventDefault();
        const input = el<HTMLInputElement>("sg-chat-input");
        const body = input.value.trim();
        if (!body || !ws) return;
        ws.send(JSON.stringify({ body }));
        input.value = "";
    });
}

// ---------------------------------------------------------------------------
// Deep link from an invite notification (?session=<id>)
// ---------------------------------------------------------------------------

async function loadInitialSession(): Promise<void> {
    const raw = pageEl?.dataset.initialSessionId;
    if (!raw) return;
    sessionId = Number(raw);

    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, sessionId));
    totalRounds = lobby.total_rounds;
    currentMode = lobby.mode;
    isMultiplayer = true;

    if (lobby.status === "lobby") {
        renderLobby(lobby);
        return;
    }
    if (lobby.status === "completed" || lobby.status === "abandoned") {
        const summary: SummaryPayload = await getJson(urlFor(urls.summary, sessionId));
        showSummary(summary);
        return;
    }
    // Already active - join the game in progress at its current round.
    connectSessionSocket();
    await loadPinOptions();
    const data = await getJson(urlFor(urls.round, sessionId));
    if (data.no_eligible_locations) {
        showNoEligibleLocations();
    } else if (data.finished) {
        showSummary(data.summary);
    } else {
        renderRound(data.round, data.round.sequence_index + 1);
    }
}

applyLastConfig();
initRoundsSlider();
initModeCards();
initRatingsToggle();
initAreaRestriction();
initAreaSearch();
initPinSearch();
initFriendPicker();
initFriendListRetry();
initEmptyState();
initChat();
void fetchFriendsEagerly();
el("sg-start-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const mode = currentSettingsMode();
    el<HTMLDialogElement>("sg-settings-dialog").close();
    void startGame(mode);
});
el("sg-submit-guess-btn").addEventListener("click", () => void submitGuess());
el("sg-photo-thumbs-up-btn").addEventListener("click", () => void submitPhotoFeedback("thumbs_up"));
el("sg-photo-thumbs-down-btn").addEventListener("click", () => void submitPhotoFeedback("thumbs_down"));
el("sg-photo-report-btn").addEventListener("click", () => void submitPhotoFeedback("reported"));
el("sg-next-round-btn").addEventListener("click", () => void goToNextRound());
el("sg-play-again-btn").addEventListener("click", resetToSettings);
el("sg-join-lobby-btn").addEventListener("click", () => void joinLobby());
el("sg-begin-btn").addEventListener("click", () => void beginGame());
el("sg-invite-more-btn").addEventListener("click", () => void handleInviteMore());
void loadInitialSession();
