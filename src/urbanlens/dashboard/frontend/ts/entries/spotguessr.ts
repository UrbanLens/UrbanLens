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
import { confirmAction, toast } from "../shared/dialogs";
import { createGameShell, playEntrance, type GameShell } from "../shared/game-shell";
import { openLiveSocket, type LiveSocketHandle } from "../shared/live-socket";
import { createMapLayers } from "../shared/map-layers";
import {
    avatarInitial,
    bonusSuffix,
    countUpValue,
    formatCountdown,
    formatRatingDelta,
    interpolateLatLng,
    summaryBestRoundSubtitle,
    summaryHeadline,
    type PanelName,
} from "../shared/spotguessr-format";

declare const L: typeof import("leaflet");
import type {} from "leaflet-draw";

declare global {
    interface Window {
        SPOTGUESSR_URLS: {
            start: string;
            pins: string;
            area_pin_count: string;
            settings: string;
            friends: string;
            lobby: string;
            invite: string;
            join: string;
            begin: string;
            end: string;
            round: string;
            guess: string;
            round_timeout: string;
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
    allow_arbitrary_external_photos: boolean;
    require_visited_all: boolean;
    date_guessing_enabled: boolean;
    use_aliases: boolean;
    geo_bounds_geojson: GeoJSON.Geometry | null;
    round_time_limit_seconds: number | null;
    label_id: number | null;
}

// [[south, west], [north, east]] - matches Leaflet's LatLngBoundsExpression shape directly.
type GeoBoundsBox = [[number, number], [number, number]];

interface RoundPayload {
    round_id: number;
    session_id: number;
    mode: string;
    sequence_index: number;
    revealed: boolean;
    geo_bounds?: GeoBoundsBox | null;
    // Server-computed (services.spotguessr.modes.shows_imagery) - which modes
    // show real photographic content worth reacting to, so the frontend never
    // hardcodes its own copy of that mode list.
    shows_imagery: boolean;
    // ISO timestamp the round's configured timer runs out, or null/absent for untimed play.
    expires_at?: string | null;
    image_url?: string;
    display_text?: string | null;
    street_view_image?: string | null;
    // The panorama's own resolved coordinates - see street_view_lat's docstring
    // on services.spotguessr.street_view.StreetViewPanorama for why this mode
    // gets an explicit exception to "never reveal the answer before a guess":
    // a real pan/zoom/walk-around panorama has to talk to Google directly, so
    // the browser needs to know where to look.
    street_view_lat?: number | null;
    street_view_lng?: number | null;
}

interface RevealPayload {
    round_id: number;
    revealed: boolean;
    distance_meters: number;
    points: number;
    date_points: number;
    bonus_points: number;
    bonus_tiers: string[];
    // This guesser's own Glicko-2 rating change from this round, once revealed.
    rating_delta?: number | null;
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
    bonus_points: number;
    rating_delta?: number | null;
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
    geo_bounds?: GeoBoundsBox | null;
    participants: ParticipantPayload[];
}

interface SummaryParticipant {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    total_points: number;
    rating_delta?: number;
    best_round_points?: number | null;
    best_round_distance_meters?: number | null;
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
// A true world view - both the area-restriction map and the guess map
// should start zoomed all the way out when nothing more specific applies,
// not centered on any one region.
const DEFAULT_CENTER: L.LatLngExpression = [20, 0];
const DEFAULT_ZOOM = 2;

const pageEl = document.querySelector<HTMLElement>(".spotguessr-page");
const myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");
const regionSearchUrl = pageEl?.dataset.regionSearchUrl ?? "";
const googleMapsApiKey = pageEl?.dataset.googleMapsApiKey ?? "";

// ---------------------------------------------------------------------------
// Session/UI state - every mutable cross-function value lives on this one
// object rather than as ~24 independent module-level `let`s. One source of
// truth that's trivial to inspect as a whole (e.g. `console.log(state)`) and
// impossible to half-reset by forgetting one of many scattered bindings -
// see _resetSessionState() below, which used to have to enumerate them all.
// ---------------------------------------------------------------------------

interface SpotGuessrState {
    sessionId: number | null;
    currentRoundId: number | null;
    currentMode: string;
    currentRoundShowsImagery: boolean;
    totalRounds: number;
    sessionScore: number;
    // What #sg-score-status currently shows, mid-count-up or not - the
    // animation target for the *next* count-up needs to start from here, not
    // from `sessionScore` itself (which is already the new total by the time
    // showReveal() updates it).
    displayedSessionScore: number;
    dateGuessingEnabled: boolean;
    isMultiplayer: boolean;
    hostProfileId: number | null;
    lastRevealedRoundId: number | null;
    ws: LiveSocketHandle | null;
    guessMap: L.Map | null;
    // Reused across rounds/sessions via setPano() rather than torn down and
    // recreated - same singleton-container convention as guessMap/areaMap
    // below.
    streetViewPanorama: google.maps.StreetViewPanorama | null;
    guessMarker: L.Marker | null;
    actualMarker: L.Marker | null;
    resultLine: L.Polyline | null;
    areaMap: L.Map | null;
    areaDrawnItems: L.FeatureGroup | null;
    // Set by applyLastConfig() when a saved geo_bounds exists - drawn once
    // ensureAreaMap() actually builds the map (deferred until the dialog is
    // visible, since a <dialog> without `open` has zero size before showModal()).
    restoredGeoBounds: GeoJSON.Geometry | null;
    pinOptions: PinOption[];
    friendOptions: FriendOption[];
    selectedInviteIds: Set<number>;
    scoreboard: SummaryParticipant[];
    // A round's configured timer (GameConfig.round_time_limit_seconds), if
    // any - see startRoundTimer()/clearRoundTimer().
    roundExpiresAtMs: number | null;
    roundTimerHandle: ReturnType<typeof setInterval> | null;
}

const state: SpotGuessrState = {
    sessionId: null,
    currentRoundId: null,
    currentMode: "photos",
    currentRoundShowsImagery: false,
    totalRounds: 0,
    sessionScore: 0,
    displayedSessionScore: 0,
    dateGuessingEnabled: false,
    isMultiplayer: false,
    hostProfileId: null,
    lastRevealedRoundId: null,
    ws: null,
    guessMap: null,
    streetViewPanorama: null,
    guessMarker: null,
    actualMarker: null,
    resultLine: null,
    areaMap: null,
    areaDrawnItems: null,
    restoredGeoBounds: null,
    pinOptions: [],
    friendOptions: [],
    selectedInviteIds: new Set<number>(),
    scoreboard: [],
    roundExpiresAtMs: null,
    roundTimerHandle: null,
};

// ---------------------------------------------------------------------------
// Top-level panel state machine - showPanel() is the single chokepoint every
// screen transition goes through, so no render path can forget to hide the
// others (the old code toggled all 5 `.hidden` flags by hand in every
// function that showed one of them).
// ---------------------------------------------------------------------------

const PANEL_IDS: Record<PanelName, string> = {
    settings: "sg-settings-panel",
    lobby: "sg-lobby-panel",
    game: "sg-game-panel",
    summary: "sg-summary-panel",
    empty: "sg-empty-state-panel",
};

// Assigned once at the bottom of this module, before any user interaction can
// trigger a panel change. Nullable only so the declaration can sit above the
// functions that use it.
let shell: GameShell | null = null;

function showPanel(name: PanelName): void {
    shell?.showPanel(name);
}

// One-shot scale/glow on an element whose number is about to be counted up, so
// the reward moment registers even when the value barely changes.
function flareCount(target: HTMLElement): void {
    if (shell?.reducedMotion()) return;
    target.classList.remove("is-counting");
    // Force a reflow so a repeat on the same element restarts the animation.
    void target.offsetWidth;
    target.classList.add("is-counting");
    target.addEventListener("animationend", () => target.classList.remove("is-counting"), { once: true });
}

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
    // url is always urlFor(urls.<name>, ...) - a same-origin, server-rendered path
    // template with only numeric ids substituted, never an arbitrary/external url.
    const response = await fetch(url, {  // lgtm[js/request-forgery]
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
    // The area map is always shown (not gated behind the "restrict to area"
    // toggle) - build it the first time the dialog is opened, since a
    // <dialog> without `open` has zero size before showModal() and Leaflet
    // needs real size to lay out tiles correctly.
    if (state.areaMap) {
        // Already built while the dialog was previously closed (0-size) -
        // nudge Leaflet now that it's actually visible.
        state.areaMap.invalidateSize();
    } else {
        ensureAreaMap();
        if (state.restoredGeoBounds) {
            setAreaGeometry(state.restoredGeoBounds);
            state.restoredGeoBounds = null;
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
    if (state.areaMap) return state.areaMap;
    state.areaMap = L.map("sg-area-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(state.areaMap);
    state.areaDrawnItems = new L.FeatureGroup();
    state.areaMap.addLayer(state.areaDrawnItems);
    const drawControl = new L.Control.Draw({
        draw: { polygon: {}, rectangle: false, circle: false, marker: false, polyline: false, circlemarker: false },
        edit: { featureGroup: state.areaDrawnItems },
    });
    state.areaMap.addControl(drawControl);
    state.areaMap.on(L.Draw.Event.CREATED, (event: L.LeafletEvent) => {
        const { layer } = event as unknown as { layer: L.Polygon };
        setAreaGeometry(layer.toGeoJSON().geometry);
    });
    return state.areaMap;
}

// Exactly one region is ever active - a new search selection or hand-drawn
// polygon always replaces whatever was there before, matching the old
// rectangle-only tool's single-shape behavior (no include/exclude sets).
// Setting a region always turns "restrict to area" on - drawing/searching a
// region is an unambiguous signal the player wants it applied; they can
// still uncheck the toggle afterward to keep the shape but not apply it, or
// use "Clear area" to drop it entirely.
function setAreaGeometry(geometry: GeoJSON.Geometry): void {
    const map = ensureAreaMap();
    state.areaDrawnItems?.clearLayers();
    const layer = L.geoJSON(geometry as GeoJSON.GeoJsonObject);
    layer.eachLayer((shapeLayer) => state.areaDrawnItems?.addLayer(shapeLayer));
    el<HTMLInputElement>("sg-area-geo-bounds").value = JSON.stringify(geometry);
    el<HTMLInputElement>("sg-restrict-area").checked = true;
    el<HTMLButtonElement>("sg-area-clear-btn").hidden = false;
    const bounds = state.areaDrawnItems?.getBounds();
    if (bounds?.isValid()) map.fitBounds(bounds, { padding: [40, 40] });
    void updateAreaPinCount();
}

function clearAreaGeometry(): void {
    state.areaDrawnItems?.clearLayers();
    el<HTMLInputElement>("sg-area-geo-bounds").value = "";
    el<HTMLInputElement>("sg-restrict-area").checked = false;
    el<HTMLButtonElement>("sg-area-clear-btn").hidden = true;
    el("sg-area-pin-count").hidden = true;
    state.areaMap?.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
}

async function updateAreaPinCount(): Promise<void> {
    const geoBounds = currentGeoBoundsGeoJson();
    const countEl = el("sg-area-pin-count");
    if (!geoBounds) {
        countEl.hidden = true;
        return;
    }
    let data: { count?: number };
    try {
        data = await getJson(`${urls.area_pin_count}?geo_bounds=${encodeURIComponent(geoBounds)}`);
    } catch {
        countEl.hidden = true;
        return;
    }
    const count = data.count ?? 0;
    countEl.textContent = count === 1 ? "1 of your pins is in this area." : `${count} of your pins are in this area.`;
    countEl.hidden = false;
}

function initAreaRestriction(): void {
    el("sg-area-clear-btn").addEventListener("click", clearAreaGeometry);
}

async function searchAreaRegion(): Promise<void> {
    const input = el<HTMLInputElement>("sg-area-search-input");
    const query = input.value.trim();
    const searchBtn = el<HTMLButtonElement>("sg-area-search-btn");
    const resultsEl = el("sg-area-search-results");
    const messageEl = el("sg-area-search-message");
    resultsEl.hidden = true;
    resultsEl.innerHTML = "";
    messageEl.hidden = false;
    messageEl.textContent = "Searching…";
    if (!query || !regionSearchUrl) {
        messageEl.hidden = true;
        return;
    }

    searchBtn.disabled = true;
    let data: { results?: RegionSearchResult[] };
    try {
        data = await getJson(`${regionSearchUrl}?q=${encodeURIComponent(query)}`);
    } catch {
        messageEl.textContent = "Could not search for that place right now.";
        messageEl.hidden = false;
        return;
    } finally {
        searchBtn.disabled = false;
    }

    const results = data.results ?? [];
    if (!results.length) {
        messageEl.textContent = "No area boundary found for that place - try a broader name, like a state or city, or draw the region manually.";
        messageEl.hidden = false;
        return;
    }
    messageEl.hidden = true;
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
    state.pinOptions = data.pins ?? [];
    const datalist = el<HTMLDataListElement>("sg-pin-options");
    datalist.innerHTML = "";
    for (const pin of state.pinOptions) {
        const option = document.createElement("option");
        option.value = pin.label;
        datalist.appendChild(option);
    }
}

function initPinSearch(): void {
    const input = el<HTMLInputElement>("sg-pin-search");
    input.addEventListener("change", () => {
        const match = state.pinOptions.find((pin) => pin.label === input.value);
        if (match) placeGuessMarker(L.latLng(match.latitude, match.longitude));
    });
}

// ---------------------------------------------------------------------------
// Friend invite picker
// ---------------------------------------------------------------------------

async function loadFriendOptions(): Promise<FriendOption[]> {
    if (state.friendOptions.length) return state.friendOptions;
    const data = await getJson(urls.friends);
    state.friendOptions = data.friends ?? [];
    return state.friendOptions;
}

// Fetched unconditionally at page load so the friend list is ready the
// moment the settings dialog opens - there's no separate opt-in toggle to
// gate it behind (any friend selected just starts a multiplayer lobby
// instead of a solo game).
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
    renderFriendCheckboxes(el("sg-friend-list"), state.friendOptions, new Set());
}

function initFriendListRetry(): void {
    el("sg-friend-list-retry").addEventListener("click", () => void fetchFriendsEagerly());
}

// `targetSet` defaults to state.selectedInviteIds (the initial invite flow's
// source of truth, read at game-start submit time) but can be swapped for a
// scoped-local Set - see pickFriendsToInvite() below, which reuses this same
// rendering logic for the "invite more" dialog without polluting
// state.selectedInviteIds (that set must only ever reflect the pending
// game-start invite list, not a completed, already-invited mid-game pick).
function renderFriendCheckboxes(container: HTMLElement, friends: FriendOption[], excludeIds: Set<number>, targetSet: Set<number> = state.selectedInviteIds): void {
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
        // "play with friends" off then on) - targetSet is the source of
        // truth read at submit time, so the checkboxes must reflect it
        // rather than always starting unchecked.
        checkbox.checked = targetSet.has(friend.profile_id);
        checkbox.addEventListener("change", () => {
            if (checkbox.checked) targetSet.add(friend.profile_id);
            else targetSet.delete(friend.profile_id);
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

// Builds a small checkbox-picker dialog on the fly and resolves with the
// chosen profile ids (empty if cancelled). There's no dedicated "invite
// more" dialog markup in the template (unlike the initial invite flow's
// sg-friend-list, which lives inside sg-settings-dialog) so this is
// constructed in JS, but it reuses renderFriendCheckboxes - the exact same
// checkbox rendering the initial invite flow uses - rather than duplicating
// it. Replaces the old window.prompt() exact-username-match flow, which
// silently no-op'd on any typo or case mismatch with zero feedback.
function pickFriendsToInvite(available: FriendOption[]): Promise<Set<number>> {
    return new Promise((resolve) => {
        const chosen = new Set<number>();
        const dialog = document.createElement("dialog");
        dialog.className = "ul-dialog ul-game-dialog spotguessr-invite-more-dialog";

        const header = document.createElement("div");
        header.className = "dialog-header";
        const heading = document.createElement("span");
        heading.textContent = "Invite more players";
        header.appendChild(heading);

        const body = document.createElement("div");
        body.className = "ul-dialog-body";
        const list = document.createElement("div");
        list.className = "spotguessr-invite-more-list";
        renderFriendCheckboxes(list, available, new Set(), chosen);
        body.appendChild(list);

        const actions = document.createElement("div");
        actions.className = "dialog-footer";
        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn--ghost";
        cancelBtn.textContent = "Cancel";
        const inviteBtn = document.createElement("button");
        inviteBtn.type = "button";
        inviteBtn.className = "btn btn--primary";
        inviteBtn.textContent = "Invite";
        actions.append(cancelBtn, inviteBtn);

        dialog.append(header, body, actions);
        // Into the shell, not document.body: outside it the dialog is neither
        // painted in true fullscreen nor reached by the page's [hidden] guard.
        if (shell) shell.mountOverlay(dialog);
        else document.body.appendChild(dialog);

        const cleanup = (result: Set<number>) => {
            dialog.close();
            dialog.remove();
            resolve(result);
        };
        cancelBtn.addEventListener("click", () => cleanup(new Set()));
        inviteBtn.addEventListener("click", () => cleanup(chosen));
        // Escape key / native "cancel" - treat like the Cancel button.
        dialog.addEventListener("cancel", () => cleanup(new Set()));

        dialog.showModal();
    });
}

async function handleInviteMore(): Promise<void> {
    if (state.sessionId === null) return;
    const friends = await loadFriendOptions();
    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, state.sessionId));
    const alreadyInvited = new Set(lobby.participants.map((participant) => participant.profile_id));
    const available = friends.filter((friend) => !alreadyInvited.has(friend.profile_id));
    if (!available.length) {
        toast.error("Everyone on your friends list is already in this game.");
        return;
    }

    const chosenIds = await pickFriendsToInvite(available);
    if (!chosenIds.size) return;

    for (const profileId of chosenIds) {
        const response = await postForm(urlFor(urls.invite, state.sessionId), { profile_id: String(profileId) });
        if (response.error) toast.error(response.error);
    }
    const refreshed: SessionPayload = await getJson(urlFor(urls.lobby, state.sessionId));
    renderLobbyParticipants(refreshed.participants);
}

// ---------------------------------------------------------------------------
// Guess map
// ---------------------------------------------------------------------------

function ensureGuessMap(): L.Map {
    if (state.guessMap) return state.guessMap;
    state.guessMap = L.map("sg-guess-map", { attributionControl: false }).setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    createMapLayers(state.guessMap, {
        root: document.getElementById("sg-guess-map-layers"),
        onAttribution: (text) => {
            const attributionEl = document.getElementById("sg-guess-map-attribution");
            if (attributionEl) attributionEl.textContent = text;
        },
    });
    state.guessMap.on("click", (event) => placeGuessMarker(event.latlng));
    return state.guessMap;
}

function placeGuessMarker(latlng: L.LatLng): void {
    const map = ensureGuessMap();
    if (state.guessMarker) {
        state.guessMarker.setLatLng(latlng);
    } else {
        state.guessMarker = L.marker(latlng, { draggable: true }).addTo(map);
    }
    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = false;
}

// When the session was configured with a geo_bounds restriction, the guess
// map opens zoomed to that area instead of the default world view - see
// docs/designs/spotguessr.md's eligibility rule 3.
function resetGuessMap(bounds?: GeoBoundsBox | null): void {
    const map = ensureGuessMap();
    if (state.guessMarker) {
        map.removeLayer(state.guessMarker);
        state.guessMarker = null;
    }
    if (state.actualMarker) {
        map.removeLayer(state.actualMarker);
        state.actualMarker = null;
    }
    if (state.resultLine) {
        map.removeLayer(state.resultLine);
        state.resultLine = null;
    }
    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = true;
    if (bounds) {
        map.fitBounds(bounds, { padding: [20, 20] });
    } else {
        map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    }
}

// ---------------------------------------------------------------------------
// Round timer (GameConfig.round_time_limit_seconds) - a purely local countdown
// display; the actual reveal is always server-authoritative (SpotGuessrRoundTimeoutView
// / the stall-sweep Celery task), never decided client-side.
// ---------------------------------------------------------------------------

function clearRoundTimer(): void {
    if (state.roundTimerHandle !== null) {
        clearInterval(state.roundTimerHandle);
        state.roundTimerHandle = null;
    }
    state.roundExpiresAtMs = null;
    el("sg-round-timer").hidden = true;
}

function startRoundTimer(expiresAtIso: string | null | undefined): void {
    clearRoundTimer();
    if (!expiresAtIso) return;

    state.roundExpiresAtMs = new Date(expiresAtIso).getTime();
    el("sg-round-timer").hidden = false;
    updateRoundTimerDisplay();
    state.roundTimerHandle = setInterval(updateRoundTimerDisplay, 1000);
}

function updateRoundTimerDisplay(): void {
    if (state.roundExpiresAtMs === null) return;
    const remainingSeconds = Math.ceil((state.roundExpiresAtMs - Date.now()) / 1000);
    const timerEl = el("sg-round-timer");
    timerEl.textContent = formatCountdown(remainingSeconds);
    timerEl.classList.toggle("spotguessr-round-timer--urgent", remainingSeconds <= 10);
    if (remainingSeconds <= 0) {
        clearRoundTimer();
        void reportRoundTimeout();
    }
}

// Tells the server this round's timer ran out - a fast path to the same
// force-reveal the stall-sweep Celery task would eventually apply anyway
// (see services.spotguessr.session.expire_round_timer). Multiplayer learns
// the outcome via the usual round.revealed/round.started broadcast; solo has
// no listener for those, so it re-fetches the (now-revealed) round directly.
async function reportRoundTimeout(): Promise<void> {
    if (state.sessionId === null || state.currentRoundId === null) return;
    await postForm(urlFor(urls.round_timeout, state.sessionId, state.currentRoundId), {});
    if (state.isMultiplayer) return;

    const data = await getJson(urlFor(urls.round, state.sessionId));
    if (data.no_eligible_locations) {
        showNoEligibleLocations();
    } else if (data.finished) {
        showSummary(data.summary);
    } else if (data.round) {
        renderRound(data.round, data.round.sequence_index + 1);
    }
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
    const isHost = state.hostProfileId === myProfileId;
    el<HTMLButtonElement>("sg-invite-more-btn").hidden = !isHost;
    el<HTMLButtonElement>("sg-join-lobby-btn").hidden = !(me && me.status === "invited");
    el<HTMLButtonElement>("sg-begin-btn").hidden = !isHost;
}

function renderLobby(session: SessionPayload): void {
    state.hostProfileId = session.host_profile_id;
    state.currentMode = session.mode;
    state.totalRounds = session.total_rounds;
    showPanel("lobby");
    renderLobbyParticipants(session.participants);
    connectSessionSocket();
}

async function refreshLobby(): Promise<void> {
    if (state.sessionId === null) return;
    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, state.sessionId));
    renderLobbyParticipants(lobby.participants);
}

// ---------------------------------------------------------------------------
// Street View panorama - interactive pan/zoom/walk-around for street_view
// mode rounds (google.maps.StreetViewPanorama), loaded lazily so players who
// never touch this mode never pay for the script. See street_view_lat's
// docstring on RoundPayload for why the pano's coordinates are safe to hand
// the client here despite the round payload never revealing the answer
// otherwise.
// ---------------------------------------------------------------------------

// Memoized so a second street_view round doesn't inject the <script> tag
// again - module load, not per-call, is the right lifetime for this promise.
let googleMapsLoadPromise: Promise<void> | null = null;

function loadGoogleMapsScript(): Promise<void> {
    if (googleMapsLoadPromise) return googleMapsLoadPromise;
    googleMapsLoadPromise = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(googleMapsApiKey)}&v=weekly`;
        script.async = true;
        script.onload = () => resolve();
        script.onerror = () => reject(new Error("Failed to load the Google Maps JS API."));
        document.head.appendChild(script);
    });
    return googleMapsLoadPromise;
}

// One-way switch for the round: once a player falls back to the static
// image there is nothing further to fall back to, so the button that got
// them here goes away too.
function showStreetViewFallback(): void {
    el("sg-street-view-pano").hidden = true;
    el<HTMLButtonElement>("sg-street-view-fallback-btn").hidden = true;
    el<HTMLImageElement>("sg-round-photo").hidden = false;
}

async function initStreetViewPanorama(lat: number, lng: number): Promise<void> {
    const container = el("sg-street-view-pano");
    const fallbackBtn = el<HTMLButtonElement>("sg-street-view-fallback-btn");
    container.hidden = false;
    fallbackBtn.hidden = false;
    el<HTMLImageElement>("sg-round-photo").hidden = true;

    if (!googleMapsApiKey) {
        showStreetViewFallback();
        return;
    }

    try {
        await loadGoogleMapsScript();
        // Resolved server-side too (services.spotguessr.street_view), but that
        // only confirms coverage existed when the round was built - re-resolving
        // here also gets us the pano id StreetViewPanorama needs, and gives a
        // definitive client-side OK/not-OK signal a bare `position:` option
        // wouldn't (silent failures here fall back to the static image).
        const service = new google.maps.StreetViewService();
        const response = await service.getPanorama({ location: { lat, lng }, radius: 75 });
        const pano = response.data.location?.pano;
        if (!pano) {
            showStreetViewFallback();
            return;
        }
        if (state.streetViewPanorama) {
            state.streetViewPanorama.setPano(pano);
        } else {
            state.streetViewPanorama = new google.maps.StreetViewPanorama(container, {
                pano,
                addressControl: false,
                fullscreenControl: false,
                motionTracking: false,
                showRoadLabels: false,
            });
        }
        // The container was `hidden` (display:none) until just above, so the
        // panorama needs a nudge to size itself correctly - same reason
        // guessMap.invalidateSize() gets one at the end of renderRound().
        setTimeout(() => {
            if (state.streetViewPanorama) google.maps.event.trigger(state.streetViewPanorama, "resize");
        }, 0);
    } catch {
        showStreetViewFallback();
    }
}

// ---------------------------------------------------------------------------
// Gameplay
// ---------------------------------------------------------------------------

function renderRound(round: RoundPayload, roundNumber: number): void {
    state.currentRoundId = round.round_id;
    state.currentMode = round.mode;
    state.currentRoundShowsImagery = round.shows_imagery;
    showPanel("game");
    const revealPanel = el("sg-reveal-panel");
    revealPanel.hidden = true;
    revealPanel.classList.remove("ul-game-reveal--good", "ul-game-reveal--bad");
    el("sg-round-status").textContent = `Round ${roundNumber} of ${state.totalRounds}`;
    shell?.setProgress(roundNumber, state.totalRounds);
    // Baseline for the new round - not a "reward" moment, so no count-up here.
    state.displayedSessionScore = state.sessionScore;
    el("sg-score-status").textContent = state.isMultiplayer ? "" : `Score: ${state.sessionScore}`;
    // Changing settings mid-game starts an entirely new session (see
    // sg-game-settings-btn's click handler) - safe to abandon a solo game in
    // progress, but would desync a shared multiplayer session's other
    // participants, so this is solo-only.
    el<HTMLButtonElement>("sg-game-settings-btn").hidden = state.isMultiplayer;
    // Ending early only makes sense for the host of a shared game - solo play
    // has "reload"/"play again" for the same purpose already.
    el<HTMLButtonElement>("sg-end-game-btn").hidden = !(state.isMultiplayer && state.hostProfileId === myProfileId);

    const photo = el<HTMLImageElement>("sg-round-photo");
    const nameHeading = el("sg-round-name");
    const pinSearchWrap = el("sg-pin-search-wrap");
    // The play panel stays mounted between rounds, so the clue and the photo
    // only animate if the entrance is re-applied - otherwise round 2..N is a
    // hard cut on the single most repeated moment in the game.
    const skipMotion = shell?.reducedMotion() ?? false;

    if (round.mode === "named_place") {
        photo.hidden = true;
        el("sg-street-view-pano").hidden = true;
        el<HTMLButtonElement>("sg-street-view-fallback-btn").hidden = true;
        nameHeading.hidden = false;
        nameHeading.textContent = round.display_text ?? "";
        playEntrance(nameHeading, skipMotion);
        pinSearchWrap.hidden = true; // Named Place mode is map-click only, per spec - no pin search.
    } else {
        nameHeading.hidden = true;
        pinSearchWrap.hidden = false;
        // street_view_image is a server-built data: URI (services.spotguessr.street_view);
        // image_url is a Django ImageField storage path. Neither is user-typed.
        photo.src = (round.mode === "street_view" ? round.street_view_image : round.image_url) ?? ""; // lgtm[js/xss,js/client-side-unvalidated-url-redirection]
        if (round.mode === "street_view" && round.street_view_lat != null && round.street_view_lng != null) {
            // initStreetViewPanorama sets container.hidden = false synchronously
            // (before its first await), so the entrance animation below sees
            // the un-hidden element even though the fetch it kicks off is async.
            void initStreetViewPanorama(round.street_view_lat, round.street_view_lng);
            playEntrance(el("sg-street-view-pano"), skipMotion);
        } else {
            photo.hidden = false;
            el("sg-street-view-pano").hidden = true;
            el<HTMLButtonElement>("sg-street-view-fallback-btn").hidden = true;
            playEntrance(photo, skipMotion);
        }
    }

    // The rail holds nothing but multiplayer scores and chat.
    shell?.setRailAvailable(state.isMultiplayer);

    el("sg-date-field").hidden = !state.dateGuessingEnabled;
    el("sg-photo-feedback").hidden = true;
    el("sg-photo-feedback-thanks").hidden = true;
    resetGuessMap(round.geo_bounds);
    startRoundTimer(round.expires_at);
    // The map container was hidden (display:none) while another panel was
    // showing, so Leaflet needs a nudge once it's visible again.
    setTimeout(() => state.guessMap?.invalidateSize(), 0);
}

// ---------------------------------------------------------------------------
// Animation helpers - thin requestAnimationFrame wrappers around the pure
// easing/interpolation math in shared/spotguessr-format.ts (kept there, not
// here, so that math is unit-testable without a DOM/rAF environment).
// ---------------------------------------------------------------------------

// Keyed per-element so re-triggering an animation on the same element (e.g.
// a fast player who guesses again before the previous reveal's count-up
// finished) cancels the stale one instead of both fighting over the text.
const activeCountUps = new WeakMap<HTMLElement, number>();

// Counts `el`'s text content from `from` to `to` over `durationMs`, easing
// out. `formatter` renders the current (possibly fractional, mid-animation)
// numeric value - defaults to a rounded integer, since points/ratings never
// show fractional digits mid-flight.
function animateCountUp(el: HTMLElement, from: number, to: number, durationMs = 700, formatter: (value: number) => string = (value) => String(Math.round(value))): void {
    const pending = activeCountUps.get(el);
    if (pending !== undefined) cancelAnimationFrame(pending);

    // requestAnimationFrame is exactly what the CSS reduced-motion blanket
    // cannot neutralise, so the guard has to live here.
    if (from === to || durationMs <= 0 || shell?.reducedMotion()) {
        el.textContent = formatter(to);
        activeCountUps.delete(el);
        return;
    }

    const start = performance.now();
    const tick = (now: number): void => {
        const progress = Math.min(1, (now - start) / durationMs);
        el.textContent = formatter(progress >= 1 ? to : countUpValue(from, to, progress));
        if (progress < 1) {
            activeCountUps.set(el, requestAnimationFrame(tick));
        } else {
            activeCountUps.delete(el);
        }
    };
    activeCountUps.set(el, requestAnimationFrame(tick));
}

// Draws `line` growing from `from` toward `to` over `durationMs`, rather
// than snapping into place instantly - the reveal's "here's how far off you
// were" line drawing itself in, GeoGuessr-style. Uses Leaflet's public
// setLatLngs() API (not internal SVG path plumbing), so it doesn't depend on
// Leaflet's rendering internals.
function animateLineDrawIn(map: L.Map, from: L.LatLng, to: L.LatLng, durationMs = 600): L.Polyline {
    const line = L.polyline([from, from], { color: shell?.cssVar("bad") || "#e74c3c" }).addTo(map);
    if (shell?.reducedMotion()) {
        line.setLatLngs([from, to]);
        return line;
    }
    const start = performance.now();
    const tick = (now: number): void => {
        const progress = Math.min(1, (now - start) / durationMs);
        const [lat, lng] = interpolateLatLng([from.lat, from.lng], [to.lat, to.lng], progress);
        line.setLatLngs([from, progress >= 1 ? to : L.latLng(lat, lng)]);
        if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    return line;
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
    ratingDelta?: number | null;
}

function renderScoreCard(data: ScoreCardData, rank?: number, animatePoints = false): HTMLElement {
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
            // data.avatarUrl is Profile.avatar.url (Django storage path), not user-typed.
            img.src = data.avatarUrl; // lgtm[js/xss,js/client-side-unvalidated-url-redirection]
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
    if (pointsEl) {
        if (animatePoints) {
            animateCountUp(pointsEl, 0, data.points, 700, (value) => `${Math.round(value)} pts`);
        } else {
            pointsEl.textContent = `${data.points} pts`;
        }
    }

    const ratingDeltaEl = card.querySelector<HTMLElement>(".spotguessr-score-card-rating-delta");
    if (ratingDeltaEl) {
        const formatted = formatRatingDelta(data.ratingDelta);
        ratingDeltaEl.textContent = formatted?.text ?? "";
        ratingDeltaEl.className = `spotguessr-score-card-rating-delta${formatted ? ` spotguessr-score-card-rating-delta--${formatted.direction}` : ""}`;
    }

    return card;
}

// Solo sessions show "Your score: X" instead of a username-labeled card -
// there's only ever one participant, and naming them is just noise. Still
// surfaces the same rating-delta/subtitle recap a multiplayer card would,
// when the entry carries them (see showSummary()).
function renderScoreCardList(container: HTMLElement, entries: ScoreCardData[], options: { solo: boolean; animatePoints?: boolean }): void {
    container.innerHTML = "";
    const sorted = [...entries].sort((a, b) => b.points - a.points);
    const [only] = sorted;
    if (options.solo && sorted.length === 1 && only) {
        const item = document.createElement("li");
        item.className = "spotguessr-score-card spotguessr-score-card--solo";
        const scoreLine = document.createElement("span");
        const scoreValue = document.createElement("span");
        scoreValue.className = "spotguessr-score-card-solo-value";
        scoreLine.append("Your score: ", scoreValue, " pts");
        item.appendChild(scoreLine);
        if (options.animatePoints) {
            animateCountUp(scoreValue, 0, only.points, 700);
        } else {
            scoreValue.textContent = String(only.points);
        }
        const formatted = formatRatingDelta(only.ratingDelta);
        if (formatted) {
            const ratingLine = document.createElement("span");
            ratingLine.className = `spotguessr-rating-delta spotguessr-rating-delta--${formatted.direction}`;
            ratingLine.textContent = formatted.text;
            item.appendChild(ratingLine);
        }
        if (only.subtitle) {
            const subtitleLine = document.createElement("span");
            subtitleLine.className = "spotguessr-score-card-subtitle";
            subtitleLine.textContent = only.subtitle;
            item.appendChild(subtitleLine);
        }
        container.appendChild(item);
        return;
    }
    sorted.forEach((entry, index) => container.appendChild(renderScoreCard(entry, index + 1, options.animatePoints)));
}

function renderResultsList(results: RoundRevealResult[]): void {
    const list = el("sg-reveal-results");
    if (!state.isMultiplayer) {
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
            points: result.points + result.date_points + result.bonus_points,
            subtitle: `${(result.distance_meters / 1000).toFixed(2)} km away`,
            ratingDelta: result.rating_delta,
        })),
        { solo: false, animatePoints: true },
    );
}

function updateScoreboardFromResults(results: RoundRevealResult[]): void {
    for (const result of results) {
        let entry = state.scoreboard.find((participant) => participant.profile_id === result.profile_id);
        if (!entry) {
            entry = { profile_id: result.profile_id, username: result.username, avatar_url: result.avatar_url, total_points: 0 };
            state.scoreboard.push(entry);
        }
        entry.total_points += result.points + result.date_points + result.bonus_points;
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
    if (!state.isMultiplayer || !state.scoreboard.length) {
        list.hidden = true;
        return;
    }
    list.hidden = false;
    renderScoreCardList(
        list,
        state.scoreboard.map((entry) => ({ profileId: entry.profile_id, username: entry.username, avatarUrl: entry.avatar_url, points: entry.total_points })),
        { solo: false },
    );
}

function showPhotoFeedbackIfApplicable(): void {
    // The photo itself (unlike the answer) has been visible since the round
    // started, whether or not everyone's guessed yet - so feedback on it is
    // always fair game once there's a reveal panel to put the buttons in.
    // shows_imagery comes straight from the round payload (services.spotguessr.modes) -
    // no separate hardcoded mode list to keep in sync here.
    el("sg-photo-feedback").hidden = !state.currentRoundShowsImagery;
    el("sg-photo-feedback-thanks").hidden = true;
}

function dropInMarker(marker: L.Marker): void {
    marker.getElement()?.classList.add("spotguessr-marker-drop");
}

// Shared by showReveal() and showBroadcastReveal() - both draw the actual-
// location marker, and (if a guess was placed) the line from that guess to
// it, identically. Used to be ~15 duplicated lines in each function.
function drawRevealMarkers(actualLatLng: L.LatLng): void {
    const map = ensureGuessMap();
    state.actualMarker = L.marker(actualLatLng).addTo(map);
    dropInMarker(state.actualMarker);
    // A flown camera reads as "here is where it actually was"; an instant jump
    // reads as a glitch. Reduced motion falls back to the old snap.
    const animate = !(shell?.reducedMotion() ?? false);

    if (state.guessMarker) {
        const guessLatLng = state.guessMarker.getLatLng();
        map.fitBounds(L.latLngBounds([guessLatLng, actualLatLng]), { padding: [40, 40], animate, duration: 0.9 });
        state.resultLine = animateLineDrawIn(map, guessLatLng, actualLatLng);
    } else {
        map.flyTo(actualLatLng, 14, { animate, duration: 0.9 });
    }
}

function updateRatingDeltaDisplay(delta: number | null | undefined): void {
    const badge = el("sg-reveal-rating-delta");
    const formatted = formatRatingDelta(delta);
    if (!formatted) {
        badge.hidden = true;
        badge.textContent = "";
        badge.className = "spotguessr-rating-delta";
        return;
    }
    badge.hidden = false;
    badge.textContent = formatted.text;
    badge.className = `spotguessr-rating-delta spotguessr-rating-delta--${formatted.direction}`;
}

// Colours the reveal sheet's border and flashes the stage. Scoring anything at
// all counts as "good" - a zero means the guess landed outside every scoring
// band, or the round timed out with no guess placed.
function setRevealVerdict(scored: boolean): void {
    const panel = el("sg-reveal-panel");
    panel.classList.toggle("ul-game-reveal--good", scored);
    panel.classList.toggle("ul-game-reveal--bad", !scored);
    shell?.flashStage(scored ? "good" : "bad");
}

function showReveal(reveal: RevealPayload): void {
    clearRoundTimer();
    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = true;
    const roundTotal = reveal.points + reveal.date_points + reveal.bonus_points;
    state.sessionScore += roundTotal;
    if (state.isMultiplayer) {
        el("sg-score-status").textContent = "";
    } else {
        animateCountUp(el("sg-score-status"), state.displayedSessionScore, state.sessionScore, 700, (value) => `Score: ${Math.round(value)}`);
        flareCount(el("sg-score-status"));
    }
    state.displayedSessionScore = state.sessionScore;
    showPhotoFeedbackIfApplicable();
    updateRatingDeltaDisplay(reveal.rating_delta);
    animateCountUp(el("sg-reveal-points-value"), 0, roundTotal);
    flareCount(el("sg-reveal-points"));
    setRevealVerdict(roundTotal > 0);

    const distanceKm = (reveal.distance_meters / 1000).toFixed(2);
    if (!reveal.revealed) {
        // Multiplayer: not everyone has guessed yet - the answer is withheld
        // so this player can't relay it via chat before their teammates
        // guess too. showBroadcastReveal() completes this once round.revealed
        // arrives (lastRevealedRoundId is left untouched so that handler
        // knows it still needs to draw the actual marker/line itself).
        el("sg-reveal-panel").hidden = false;
        el("sg-reveal-title").textContent = "Guess submitted!";
        // The big animated counter above already shows the point total - this
        // line is just the distance/bonus breakdown, not a repeat of it.
        let detail = `${distanceKm} km away. Waiting for other players…`;
        if (reveal.date_points) detail = `${distanceKm} km away (+${reveal.date_points} for the date guess). Waiting for other players…`;
        detail += bonusSuffix(reveal.bonus_points, reveal.bonus_tiers);
        el("sg-reveal-detail").textContent = detail;
        el("sg-reveal-results").hidden = true;
        el<HTMLButtonElement>("sg-next-round-btn").hidden = true;
        return;
    }

    state.lastRevealedRoundId = reveal.round_id;
    drawRevealMarkers(L.latLng(reveal.actual_latitude as number, reveal.actual_longitude as number));

    el("sg-reveal-panel").hidden = false;
    el("sg-reveal-title").textContent = reveal.location_name || "Revealed!";
    let detail = `${distanceKm} km away`;
    if (reveal.date_points) detail += ` (+${reveal.date_points} for the date guess)`;
    detail += bonusSuffix(reveal.bonus_points, reveal.bonus_tiers);
    el("sg-reveal-detail").textContent = detail;
    el("sg-reveal-results").hidden = true; // filled in by the round.revealed broadcast, for multiplayer
    el<HTMLButtonElement>("sg-next-round-btn").hidden = state.isMultiplayer; // multiplayer advances automatically
}

function showBroadcastReveal(data: RoundRevealBroadcast): void {
    clearRoundTimer();
    if (state.lastRevealedRoundId !== data.round_id) {
        state.lastRevealedRoundId = data.round_id;
        drawRevealMarkers(L.latLng(data.actual_latitude, data.actual_longitude));
        el("sg-reveal-panel").hidden = false;
        el("sg-reveal-title").textContent = data.location_name || "Revealed!";
        el("sg-reveal-detail").textContent = "";
        el<HTMLButtonElement>("sg-submit-guess-btn").disabled = true;
        el<HTMLButtonElement>("sg-next-round-btn").hidden = true;
        const myResult = data.results.find((result) => result.profile_id === myProfileId);
        updateRatingDeltaDisplay(myResult?.rating_delta);
        // Reached without showReveal() when this player never guessed (round
        // timed out), so the verdict still has to be set here.
        setRevealVerdict(myResult ? myResult.points + myResult.date_points + myResult.bonus_points > 0 : false);
    }
    updateScoreboardFromResults(data.results);
    renderResultsList(data.results);
}

function showSummary(summary: SummaryPayload): void {
    showPanel("summary");
    clearRoundTimer();

    const { heading, icon } = summaryHeadline(summary.participants, state.isMultiplayer, myProfileId);
    el("sg-summary-heading").textContent = heading;
    el("sg-summary-icon").textContent = icon;
    const roundWord = summary.total_rounds === 1 ? "round" : "rounds";
    el("sg-summary-subheading").textContent = `${summary.rounds_played} of ${summary.total_rounds} ${roundWord} played.`;

    renderScoreCardList(
        el("sg-summary-scores"),
        summary.participants.map((participant) => ({
            profileId: participant.profile_id,
            username: participant.username,
            avatarUrl: participant.avatar_url,
            points: participant.total_points,
            ratingDelta: participant.rating_delta,
            subtitle: summaryBestRoundSubtitle(participant),
        })),
        { solo: !state.isMultiplayer, animatePoints: true },
    );
    if (state.ws) {
        state.ws.close();
        state.ws = null;
    }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function startGame(mode: string): Promise<void> {
    state.currentMode = mode;
    state.dateGuessingEnabled = state.currentMode === "photos" && el<HTMLInputElement>("sg-date-guessing").checked;
    state.sessionScore = 0;
    state.displayedSessionScore = 0;
    state.scoreboard = [];
    state.lastRevealedRoundId = null;

    const geoBounds = el<HTMLInputElement>("sg-restrict-area").checked ? currentGeoBoundsGeoJson() : null;
    const roundTimeLimit = el<HTMLSelectElement>("sg-round-time-limit").value;
    const body = new URLSearchParams({
        mode: state.currentMode,
        difficulty: String(currentDifficulty()),
        total_rounds: el<HTMLInputElement>("sg-rounds").value,
        allow_arbitrary_external_photos: el<HTMLInputElement>("sg-allow-arbitrary-external-photos").checked ? "on" : "off",
        require_visited_all: el<HTMLInputElement>("sg-require-visited-all").checked ? "on" : "off",
        date_guessing_enabled: state.dateGuessingEnabled ? "on" : "off",
        use_aliases: el<HTMLInputElement>("sg-use-aliases").checked ? "on" : "off",
    });
    if (geoBounds) body.append("geo_bounds", geoBounds);
    if (roundTimeLimit) body.append("round_time_limit_seconds", roundTimeLimit);
    const labelId = el<HTMLSelectElement>("sg-label-filter").value;
    if (labelId) body.append("label_id", labelId);
    for (const profileId of state.selectedInviteIds) body.append("invite_profile_ids", String(profileId));

    const response = await postForm(urls.start, body);
    if (response.error) {
        toast.error(response.error);
        return;
    }
    if (response.error_code === "no_eligible_locations") {
        showNoEligibleLocations();
        return;
    }

    state.sessionId = response.session_id;
    state.totalRounds = Number(el<HTMLInputElement>("sg-rounds").value);

    if (response.lobby) {
        state.isMultiplayer = true;
        renderLobby(response.session as SessionPayload);
        return;
    }

    state.isMultiplayer = false;
    if (response.finished) {
        showSummary(response.summary);
        return;
    }
    await loadPinOptions();
    renderRound(response.round, response.round.sequence_index + 1);
}

async function submitGuess(): Promise<void> {
    if (!state.guessMarker || state.sessionId === null || state.currentRoundId === null) return;
    const latlng = state.guessMarker.getLatLng();
    const payload: Record<string, string> = { latitude: String(latlng.lat), longitude: String(latlng.lng) };
    if (state.dateGuessingEnabled) {
        const dateValue = el<HTMLInputElement>("sg-guessed-date").value;
        if (dateValue) payload.guessed_date = dateValue;
    }

    const reveal: RevealPayload = await postForm(urlFor(urls.guess, state.sessionId, state.currentRoundId), payload);
    if (reveal.error) {
        toast.error(reveal.error);
        return;
    }
    showReveal(reveal);
}

async function submitPhotoFeedback(kind: "thumbs_up" | "thumbs_down" | "reported"): Promise<void> {
    if (state.sessionId === null || state.currentRoundId === null) return;
    const response = await postForm(urlFor(urls.photo_feedback, state.sessionId, state.currentRoundId), { kind });
    if (response.error) {
        toast.error(response.error);
        return;
    }
    el("sg-photo-feedback-thanks").hidden = false;
}

async function goToNextRound(): Promise<void> {
    // Multiplayer sessions advance automatically via the round.started
    // broadcast - this button is hidden for them (see showReveal).
    if (state.sessionId === null) return;
    const data = await getJson(urlFor(urls.round, state.sessionId));
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
    if (state.sessionId === null) return;
    const response = await postForm(urlFor(urls.join, state.sessionId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
    await refreshLobby();
}

async function beginGame(): Promise<void> {
    if (state.sessionId === null) return;
    const response = await postForm(urlFor(urls.begin, state.sessionId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
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

// Shared by resetToSettings() and showNoEligibleLocations() - both tear down
// whatever session state exists before landing on a different panel.
function _resetSessionState(): void {
    state.sessionId = null;
    state.currentRoundId = null;
    state.sessionScore = 0;
    state.displayedSessionScore = 0;
    state.scoreboard = [];
    state.selectedInviteIds.clear();
    state.isMultiplayer = false;
    state.lastRevealedRoundId = null;
    clearRoundTimer();
    if (state.ws) {
        state.ws.close();
        state.ws = null;
    }
}

// Host-only manual escape hatch for a stalled/AFK multiplayer game - see
// controllers.spotguessr.SpotGuessrEndSessionView. Confirmed first since it's
// irreversible for every other participant, not just the host.
async function endGameNow(): Promise<void> {
    if (state.sessionId === null) return;
    const confirmed = await confirmAction({
        title: "End this game?",
        message: "This ends the game immediately for everyone, using the scores so far.",
        confirmLabel: "End game",
    });
    if (!confirmed) return;

    const response = await postForm(urlFor(urls.end, state.sessionId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
    showSummary(response.summary);
}

function resetToSettings(): void {
    _resetSessionState();
    showPanel("settings");
}

// Which currently-checked settings could plausibly be why nothing matched -
// shown on the empty state so the player isn't left guessing (a restrictive
// carried-over filter is a common, non-obvious cause - see
// controllers.spotguessr.SpotGuessrStartView's docstring).
function activeFilterLabels(): string[] {
    const labels: string[] = [];
    if (el<HTMLInputElement>("sg-require-visited-all").checked) labels.push("Only places I've visited");
    const labelFilter = el<HTMLSelectElement>("sg-label-filter");
    if (labelFilter.value) labels.push(`Restricted to the "${labelFilter.selectedOptions[0]?.textContent}" label`);
    if (el<HTMLInputElement>("sg-restrict-area").checked && currentGeoBoundsGeoJson()) labels.push("Restricted to a chosen area");
    return labels;
}

function clearActiveFilters(): void {
    el<HTMLInputElement>("sg-require-visited-all").checked = false;
    el<HTMLSelectElement>("sg-label-filter").value = "";
    clearAreaGeometry();
}

// Distinguishes "never got to play a single round because nothing eligible
// matched this mode/settings combination" from a real completed game -
// see controllers.spotguessr's error_code/no_eligible_locations shapes.
function showNoEligibleLocations(): void {
    const mode = state.currentMode;
    _resetSessionState();
    showPanel("empty");

    const filters = activeFilterLabels();
    const filtersList = el("sg-empty-state-active-filters");
    const clearBtn = el<HTMLButtonElement>("sg-empty-state-clear-filters-btn");
    filtersList.innerHTML = "";
    if (filters.length) {
        for (const label of filters) {
            const item = document.createElement("li");
            item.textContent = label;
            filtersList.appendChild(item);
        }
        filtersList.hidden = false;
        clearBtn.hidden = false;
        clearBtn.onclick = () => {
            clearActiveFilters();
            void startGame(mode);
        };
    } else {
        filtersList.hidden = true;
        clearBtn.hidden = true;
    }
}

function initEmptyState(): void {
    el("sg-empty-state-settings-btn").addEventListener("click", () => {
        resetToSettings();
        openSettingsDialog(state.currentMode);
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

    el<HTMLInputElement>("sg-allow-arbitrary-external-photos").checked = config.allow_arbitrary_external_photos;
    el<HTMLInputElement>("sg-date-guessing").checked = config.date_guessing_enabled;
    el<HTMLInputElement>("sg-use-aliases").checked = config.use_aliases;
    el<HTMLInputElement>("sg-require-visited-all").checked = config.require_visited_all;
    el<HTMLSelectElement>("sg-round-time-limit").value = config.round_time_limit_seconds ? String(config.round_time_limit_seconds) : "";
    // A label the profile deleted (or that never existed - a stale/tampered snapshot)
    // simply leaves no matching <option>, so the select silently falls back to "All spots"
    // rather than erroring.
    el<HTMLSelectElement>("sg-label-filter").value = config.label_id ? String(config.label_id) : "";

    if (config.geo_bounds_geojson) {
        el<HTMLInputElement>("sg-restrict-area").checked = true;
        // Populate the hidden input immediately (just a value assignment,
        // no map needed) so a "quick start" from a mode card - which never
        // opens this dialog at all - still sends the geo_bounds that
        // "restrict to area" implies. Drawing it onto the actual map is
        // deferred to openSettingsDialog(), which needs the dialog visible
        // first for Leaflet to lay out correctly.
        el<HTMLInputElement>("sg-area-geo-bounds").value = JSON.stringify(config.geo_bounds_geojson);
        el<HTMLButtonElement>("sg-area-clear-btn").hidden = false;
        state.restoredGeoBounds = config.geo_bounds_geojson;
    }
}

// ---------------------------------------------------------------------------
// Real-time (multiplayer only)
// ---------------------------------------------------------------------------

function connectSessionSocket(): void {
    if (state.ws || state.sessionId === null) return;
    // shared/live-socket.ts adds the heartbeat this socket needs to survive the
    // Cloudflare tunnel's idle cutoff, and the reconnect it never had.
    state.ws = openLiveSocket({
        path: `/ws/spotguessr/session/${state.sessionId}/`,
        onMessage: handleSocketMessage,
        // 4404 here means the host removed this player, or the entitlement went
        // away - nothing more is coming, so drop the handle rather than leave a
        // dead one blocking a later join.
        onPermanentClose: () => {
            state.ws = null;
        },
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
    if (state.sessionId === null) return;
    const data = await getJson(urlFor(urls.chat_history, state.sessionId));
    const log = el("sg-chat-log");
    log.innerHTML = "";
    for (const message of (data.messages ?? []) as ChatMessagePayload[]) appendChatMessage(message);
}

function initChat(): void {
    el("sg-chat-form").addEventListener("submit", (event) => {
        event.preventDefault();
        const input = el<HTMLInputElement>("sg-chat-input");
        const body = input.value.trim();
        // send() is false while the socket is reconnecting - leave what they
        // typed in the box rather than clearing it for a message that never left.
        if (!body || !state.ws?.send({ body })) return;
        input.value = "";
    });
}

// ---------------------------------------------------------------------------
// Deep link from an invite notification (?session=<id>)
// ---------------------------------------------------------------------------

async function loadInitialSession(): Promise<void> {
    const raw = pageEl?.dataset.initialSessionId;
    if (!raw) return;
    state.sessionId = Number(raw);

    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, state.sessionId));
    state.totalRounds = lobby.total_rounds;
    state.currentMode = lobby.mode;
    state.isMultiplayer = true;

    if (lobby.status === "lobby") {
        renderLobby(lobby);
        return;
    }
    if (lobby.status === "completed" || lobby.status === "abandoned") {
        const summary: SummaryPayload = await getJson(urlFor(urls.summary, state.sessionId));
        showSummary(summary);
        return;
    }
    // Already active - join the game in progress at its current round.
    connectSessionSocket();
    await loadPinOptions();
    const data = await getJson(urlFor(urls.round, state.sessionId));
    if (data.no_eligible_locations) {
        showNoEligibleLocations();
    } else if (data.finished) {
        showSummary(data.summary);
    } else {
        renderRound(data.round, data.round.sequence_index + 1);
    }
}

const shellEl = document.getElementById("sg-shell");
if (pageEl && shellEl) {
    shell = createGameShell({
        root: pageEl,
        shell: shellEl,
        panels: PANEL_IDS,
        // The summary counts as "in play" so the celebration keeps the game's
        // own chrome rather than dropping back into the site page.
        playingPanels: ["game", "summary"],
        onResize: () => {
            state.guessMap?.invalidateSize();
            state.areaMap?.invalidateSize();
            if (state.streetViewPanorama) google.maps.event.trigger(state.streetViewPanorama, "resize");
        },
    });
}

applyLastConfig();
initRoundsSlider();
initModeCards();
initRatingsToggle();
initAreaRestriction();
initAreaSearch();
initPinSearch();
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
el("sg-game-settings-btn").addEventListener("click", () => openSettingsDialog(state.currentMode));
el("sg-submit-guess-btn").addEventListener("click", () => void submitGuess());
el("sg-photo-thumbs-up-btn").addEventListener("click", () => void submitPhotoFeedback("thumbs_up"));
el("sg-photo-thumbs-down-btn").addEventListener("click", () => void submitPhotoFeedback("thumbs_down"));
el("sg-photo-report-btn").addEventListener("click", () => void submitPhotoFeedback("reported"));
el("sg-next-round-btn").addEventListener("click", () => void goToNextRound());
el("sg-play-again-btn").addEventListener("click", resetToSettings);
el("sg-join-lobby-btn").addEventListener("click", () => void joinLobby());
el("sg-begin-btn").addEventListener("click", () => void beginGame());
el("sg-invite-more-btn").addEventListener("click", () => void handleInviteMore());
el("sg-end-game-btn").addEventListener("click", () => void endGameNow());
el("sg-street-view-fallback-btn").addEventListener("click", showStreetViewFallback);
void loadInitialSession();
