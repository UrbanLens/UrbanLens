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
    }
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

function initDifficultySlider(): void {
    const slider = el<HTMLInputElement>("sg-difficulty");
    const label = el("sg-difficulty-label");
    const describe = (value: number): string => (value < 33 ? "Easy" : value < 66 ? "Medium" : "Hard");
    slider.addEventListener("input", () => {
        label.textContent = describe(Number(slider.value));
    });
}

function updateModeVisibility(): void {
    const mode = el<HTMLSelectElement>("sg-mode").value;
    document.querySelectorAll<HTMLElement>("[data-mode-only]").forEach((field) => {
        field.hidden = field.dataset.modeOnly !== mode;
    });
}

function initModeSelect(): void {
    el<HTMLSelectElement>("sg-mode").addEventListener("change", updateModeVisibility);
    updateModeVisibility();
}

function initRatingsToggle(): void {
    const checkbox = el<HTMLInputElement>("sg-show-ratings-to-friends");
    checkbox.addEventListener("change", () => {
        void postForm(urls.settings, { show_ratings_to_friends: checkbox.checked ? "on" : "off" });
    });
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
        areaMap = L.map("sg-area-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(areaMap);
        areaDrawnItems = new L.FeatureGroup();
        areaMap.addLayer(areaDrawnItems);
        const drawControl = new L.Control.Draw({
            draw: { rectangle: {}, polygon: false, circle: false, marker: false, polyline: false, circlemarker: false },
            edit: { featureGroup: areaDrawnItems },
        });
        areaMap.addControl(drawControl);
        areaMap.on(L.Draw.Event.CREATED, (event: L.LeafletEvent) => {
            const { layer } = event as unknown as { layer: L.Layer };
            areaDrawnItems?.clearLayers();
            areaDrawnItems?.addLayer(layer);
        });
    });
}

function currentGeoBoundsGeoJson(): string | null {
    if (!areaDrawnItems) return null;
    const [layer] = areaDrawnItems.getLayers();
    if (!layer || !("toGeoJSON" in layer)) return null;
    const feature = (layer as L.Polygon).toGeoJSON();
    return JSON.stringify(feature.geometry);
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

function renderFriendCheckboxes(container: HTMLElement, friends: FriendOption[], excludeIds: Set<number>): void {
    container.innerHTML = "";
    const available = friends.filter((friend) => !excludeIds.has(friend.profile_id));
    if (!available.length) {
        container.innerHTML = '<p class="spotguessr-panel-hint">No friends available to invite.</p>';
        return;
    }
    for (const friend of available) {
        const label = document.createElement("label");
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
        label.appendChild(checkbox);
        label.appendChild(document.createTextNode(friend.username));
        container.appendChild(label);
    }
}

function initFriendPicker(): void {
    const toggle = el<HTMLInputElement>("sg-play-with-friends");
    const wrap = el("sg-invite-wrap");
    toggle.addEventListener("change", async () => {
        wrap.hidden = !toggle.checked;
        if (!toggle.checked) return;
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
        window.alert("Everyone on your friends list is already in this game.");
        return;
    }
    const chosenName = window.prompt(`Invite who? (${available.map((friend) => friend.username).join(", ")})`);
    if (!chosenName) return;
    const chosen = available.find((friend) => friend.username === chosenName);
    if (!chosen) return;

    const response = await postForm(urlFor(urls.invite, sessionId), { profile_id: String(chosen.profile_id) });
    if (response.error) {
        window.alert(response.error);
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

function renderResultsList(results: RoundRevealResult[]): void {
    const list = el("sg-reveal-results");
    if (!isMultiplayer) {
        list.hidden = true;
        return;
    }
    list.hidden = false;
    list.innerHTML = "";
    for (const result of [...results].sort((a, b) => b.points - a.points)) {
        const item = document.createElement("li");
        const km = (result.distance_meters / 1000).toFixed(2);
        const points = result.points + result.date_points;
        item.textContent = `${result.username}: ${points} pts (${km} km away)`;
        list.appendChild(item);
    }
}

function updateScoreboardFromResults(results: RoundRevealResult[]): void {
    for (const result of results) {
        let entry = scoreboard.find((participant) => participant.profile_id === result.profile_id);
        if (!entry) {
            entry = { profile_id: result.profile_id, username: result.username, total_points: 0 };
            scoreboard.push(entry);
        }
        entry.total_points += result.points + result.date_points;
    }
    renderScoreboard();
}

function renderScoreboard(): void {
    const list = el("sg-scoreboard");
    if (!isMultiplayer || !scoreboard.length) {
        list.hidden = true;
        return;
    }
    list.hidden = false;
    list.innerHTML = "";
    for (const entry of [...scoreboard].sort((a, b) => b.total_points - a.total_points)) {
        const item = document.createElement("li");
        item.textContent = `${entry.username}: ${entry.total_points}`;
        list.appendChild(item);
    }
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
    const list = el("sg-summary-scores");
    list.innerHTML = "";
    for (const participant of [...summary.participants].sort((a, b) => b.total_points - a.total_points)) {
        const item = document.createElement("li");
        item.textContent = `${participant.username}: ${participant.total_points}`;
        list.appendChild(item);
    }
    if (ws) {
        ws.close();
        ws = null;
    }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function startGame(event: Event): Promise<void> {
    event.preventDefault();
    currentMode = el<HTMLSelectElement>("sg-mode").value;
    dateGuessingEnabled = currentMode === "photos" && el<HTMLInputElement>("sg-date-guessing").checked;
    sessionScore = 0;
    scoreboard = [];
    lastRevealedRoundId = null;

    const geoBounds = el<HTMLInputElement>("sg-restrict-area").checked ? currentGeoBoundsGeoJson() : null;
    const body = new URLSearchParams({
        mode: currentMode,
        difficulty: String(Number(el<HTMLInputElement>("sg-difficulty").value) / 100),
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
        window.alert(response.error);
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
        window.alert(reveal.error);
        return;
    }
    showReveal(reveal);
}

async function submitPhotoFeedback(kind: "thumbs_up" | "thumbs_down" | "reported"): Promise<void> {
    if (sessionId === null || currentRoundId === null) return;
    const response = await postForm(urlFor(urls.photo_feedback, sessionId, currentRoundId), { kind });
    if (response.error) {
        window.alert(response.error);
        return;
    }
    el("sg-photo-feedback-thanks").hidden = false;
}

async function goToNextRound(): Promise<void> {
    // Multiplayer sessions advance automatically via the round.started
    // broadcast - this button is hidden for them (see showReveal).
    if (sessionId === null) return;
    const data = await getJson(urlFor(urls.round, sessionId));
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
        window.alert(response.error);
        return;
    }
    await refreshLobby();
}

async function beginGame(): Promise<void> {
    if (sessionId === null) return;
    const response = await postForm(urlFor(urls.begin, sessionId), {});
    if (response.error) {
        window.alert(response.error);
        return;
    }
    el("sg-lobby-panel").hidden = true;
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
    el("sg-settings-panel").hidden = false;
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
    if (data.finished) {
        showSummary(data.summary);
    } else {
        renderRound(data.round, data.round.sequence_index + 1);
    }
}

initDifficultySlider();
initModeSelect();
initRatingsToggle();
initAreaRestriction();
initPinSearch();
initFriendPicker();
initChat();
el("sg-start-form").addEventListener("submit", (event) => void startGame(event));
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
