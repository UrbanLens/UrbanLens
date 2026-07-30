/**
 * Consensus - wiki-data-completion game: gameplay, competitive lobby, and chat.
 *
 * Server-authoritative: this file never decides whether an answer is
 * correct, computes points, or resolves a vote itself - it only collects an
 * answer/skip/vote/photo, posts it, and renders whatever
 * `services.consensus.session` decided. Solo sessions apply an answer
 * immediately and have no reveal step worth showing (there's only one
 * player, so there's nothing to agree/disagree with) - after answering or
 * skipping, a solo session just fetches the next round directly via
 * `consensus.round` (no WebSocket). Competitive sessions open a WebSocket
 * (`consumers.ConsensusSessionConsumer`) for lobby updates, round
 * advancement, the reveal/vote sub-phase, and chat - mirrors
 * spotguessr.ts/trivia.ts's shape, trimmed to Consensus's simpler loop (no
 * ratings, no distance/date scoring, no photo-feedback thumbs).
 */
import { getCsrfToken } from "../shared/csrf";
import { confirmAction, toast } from "../shared/dialogs";

declare const L: typeof import("leaflet");

declare global {
    interface Window {
        CONSENSUS_URLS: {
            friends: string;
            start: string;
            lobby: string;
            invite: string;
            join: string;
            begin: string;
            end: string;
            round: string;
            answer: string;
            skip: string;
            vote: string;
            photo: string;
            chat_history: string;
            summary: string;
            session_id_sentinel: string;
            round_id_sentinel: string;
        };
        CONSENSUS_FIELD_KINDS: [string, string][];
    }
}

// Mirrors ConsensusFieldKind (models/consensus/model.py) - the registry key
// services.consensus.fields drives round generation/answer application from.
const FIELD_KIND = {
    WIKI_NAME: "wiki_name",
    WIKI_DESCRIPTION: "wiki_description",
    WIKI_INDOOR_OUTDOOR: "wiki_indoor_outdoor",
    WIKI_PIN_TYPE: "wiki_pin_type",
    WIKI_ALIAS: "wiki_alias",
    PHOTO_COORDINATES: "photo_coordinates",
} as const;

// Mirrors IndoorOutdoor (models/abstract/choices.py) - a closed vocabulary,
// rendered as a <select> rather than free text. No context variable carries
// this from the server today, so it's transcribed here; keep in sync if the
// model choices ever change.
const INDOOR_OUTDOOR_CHOICES: [string, string][] = [
    ["inside", "Inside"],
    ["outside", "Outside"],
    ["both", "Both"],
];

// Mirrors PinType (models/pin/model.py) - see INDOOR_OUTDOOR_CHOICES above
// for why this is transcribed rather than server-provided.
const PIN_TYPE_CHOICES: [string, string][] = [
    ["location", "Location"],
    ["parcel", "Property / Parcel"],
    ["building", "Building"],
    ["entrance", "Entrance"],
    ["poi", "Point of Interest"],
    ["danger", "Danger"],
    ["other", "Other"],
];

interface RoundPayload {
    round_id: number;
    session_id: number;
    sequence_index: number;
    field_kind: string;
    field_label: string;
    wiki_id: number;
    wiki_name: string;
    latitude: number | null;
    longitude: number | null;
    resolved: boolean;
    image_url?: string;
}

interface RevealAnswer {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    value: unknown;
    points_awarded: number;
}

interface VoteOption {
    answer_id: number;
    value: unknown;
    submitted_by: string;
}

interface RevealBroadcast {
    round_id: number;
    resolution: string;
    answers: RevealAnswer[];
    vote_options?: VoteOption[];
}

interface ParticipantPayload {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    status: "invited" | "joined";
    total_points_this_session: number;
    is_host: boolean;
}

interface SessionPayload {
    session_id: number;
    status: "lobby" | "active" | "completed" | "abandoned";
    total_rounds: number;
    host_profile_id: number;
    participants: ParticipantPayload[];
}

interface SummaryPayload {
    session_id: number;
    status: string;
    total_rounds: number;
    rounds_played: number;
    participants: ParticipantPayload[];
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

interface ScoreboardEntry {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    total_points_this_session: number;
}

const urls = window.CONSENSUS_URLS;
const DEFAULT_CENTER: L.LatLngExpression = [20, 0];
const DEFAULT_ZOOM = 2;

const pageEl = document.querySelector<HTMLElement>(".consensus-page");
const myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");

interface ConsensusState {
    sessionId: number | null;
    currentRoundId: number | null;
    currentFieldKind: string;
    totalRounds: number;
    isMultiplayer: boolean;
    hostProfileId: number | null;
    ws: WebSocket | null;
    roundMap: L.Map | null;
    contextMarker: L.Marker | null;
    answerMarker: L.Marker | null;
    friendOptions: FriendOption[];
    selectedInviteIds: Set<number>;
    participants: ParticipantPayload[];
    scoreboard: ScoreboardEntry[];
    answeredProfileIds: Set<number>;
    votedProfileIds: Set<number>;
}

const state: ConsensusState = {
    sessionId: null,
    currentRoundId: null,
    currentFieldKind: FIELD_KIND.WIKI_NAME,
    totalRounds: 0,
    isMultiplayer: false,
    hostProfileId: null,
    ws: null,
    roundMap: null,
    contextMarker: null,
    answerMarker: null,
    friendOptions: [],
    selectedInviteIds: new Set<number>(),
    participants: [],
    scoreboard: [],
    answeredProfileIds: new Set<number>(),
    votedProfileIds: new Set<number>(),
};

// ---------------------------------------------------------------------------
// Panel state machine
// ---------------------------------------------------------------------------

const PANEL_IDS = {
    settings: "cs-settings-panel",
    empty: "cs-empty-state-panel",
    lobby: "cs-lobby-panel",
    game: "cs-game-panel",
    summary: "cs-summary-panel",
} as const;

type PanelName = keyof typeof PANEL_IDS;

function showPanel(name: PanelName): void {
    for (const [key, id] of Object.entries(PANEL_IDS)) {
        el(id).hidden = key !== name;
    }
}

function el<T extends HTMLElement = HTMLElement>(id: string): T {
    const found = document.getElementById(id);
    if (!found) throw new Error(`Consensus: missing #${id}`);
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
// Friend invite picker (mirrors spotguessr.ts/trivia.ts)
// ---------------------------------------------------------------------------

async function loadFriendOptions(): Promise<FriendOption[]> {
    if (state.friendOptions.length) return state.friendOptions;
    const data = await getJson(urls.friends);
    state.friendOptions = data.friends ?? [];
    return state.friendOptions;
}

function renderFriendCheckboxes(container: HTMLElement, friends: FriendOption[], excludeIds: Set<number>, targetSet: Set<number> = state.selectedInviteIds): void {
    container.innerHTML = "";
    const available = friends.filter((friend) => !excludeIds.has(friend.profile_id));
    if (!available.length) {
        container.innerHTML = '<p class="consensus-panel-hint">No friends available to invite.</p>';
        return;
    }
    for (const friend of available) {
        const label = document.createElement("label");
        const wrap = document.createElement("span");
        wrap.className = "ul-checkbox-wrap";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = String(friend.profile_id);
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

async function fetchFriendsEagerly(): Promise<void> {
    const loadingEl = el("cs-friend-list-loading");
    const errorEl = el("cs-friend-list-error");
    const listEl = el("cs-friend-list");
    loadingEl.hidden = false;
    errorEl.hidden = true;
    listEl.hidden = true;
    try {
        await loadFriendOptions();
    } catch {
        loadingEl.hidden = true;
        errorEl.hidden = false;
        toast.error("Couldn't load your friends list.");
        return;
    }
    loadingEl.hidden = true;
    listEl.hidden = false;
    renderFriendCheckboxes(listEl, state.friendOptions, new Set());
}

// Builds a small checkbox-picker dialog on the fly and resolves with the
// chosen profile ids (empty if cancelled) - see spotguessr.ts's
// pickFriendsToInvite() for the original of this pattern.
function pickFriendsToInvite(available: FriendOption[]): Promise<Set<number>> {
    return new Promise((resolve) => {
        const chosen = new Set<number>();
        const dialog = document.createElement("dialog");
        dialog.className = "consensus-invite-more-dialog";
        dialog.style.cssText = "max-width:22rem;width:90vw;padding:1.25rem;border-radius:0.5rem;border:1px solid rgba(0,0,0,0.15);";

        const heading = document.createElement("h3");
        heading.textContent = "Invite more players";
        heading.style.marginTop = "0";

        const list = document.createElement("div");
        list.style.cssText = "display:flex;flex-direction:column;gap:0.5rem;max-height:16rem;overflow-y:auto;margin:0.75rem 0;";
        renderFriendCheckboxes(list, available, new Set(), chosen);

        const actions = document.createElement("div");
        actions.style.cssText = "display:flex;justify-content:flex-end;gap:0.5rem;";
        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.textContent = "Cancel";
        const inviteBtn = document.createElement("button");
        inviteBtn.type = "button";
        inviteBtn.textContent = "Invite";
        actions.append(cancelBtn, inviteBtn);

        dialog.append(heading, list, actions);
        document.body.appendChild(dialog);

        const cleanup = (result: Set<number>) => {
            dialog.close();
            dialog.remove();
            resolve(result);
        };
        cancelBtn.addEventListener("click", () => cleanup(new Set()));
        inviteBtn.addEventListener("click", () => cleanup(chosen));
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
    await refreshLobby();
}

// ---------------------------------------------------------------------------
// Lobby
// ---------------------------------------------------------------------------

function renderLobbyParticipants(participants: ParticipantPayload[]): void {
    state.participants = participants;
    const list = el("cs-lobby-participants");
    list.innerHTML = "";
    for (const participant of participants) {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = participant.is_host ? `${participant.username} (host)` : participant.username;
        const status = document.createElement("span");
        status.className = participant.status === "joined" ? "consensus-lobby-status consensus-lobby-status--joined" : "consensus-lobby-status";
        status.textContent = participant.status === "joined" ? "Joined" : "Invited";
        item.append(name, status);
        list.appendChild(item);
    }

    const me = participants.find((participant) => participant.profile_id === myProfileId);
    const isHost = state.hostProfileId === myProfileId;
    el<HTMLButtonElement>("cs-invite-more-btn").hidden = !isHost;
    el<HTMLButtonElement>("cs-join-lobby-btn").hidden = !(me && me.status === "invited");
    el<HTMLButtonElement>("cs-begin-btn").hidden = !isHost;
}

function renderLobby(session: SessionPayload): void {
    state.sessionId = session.session_id;
    state.hostProfileId = session.host_profile_id;
    state.totalRounds = session.total_rounds;
    state.isMultiplayer = true;
    showPanel("lobby");
    renderLobbyParticipants(session.participants);
    connectSessionSocket();
}

async function refreshLobby(): Promise<void> {
    if (state.sessionId === null) return;
    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, state.sessionId));
    renderLobbyParticipants(lobby.participants);
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
    if (response.no_eligible_wikis) {
        showNoEligibleWikis();
        return;
    }
    if (response.finished) {
        showSummary(response.summary);
        return;
    }
    if (response.round) renderRound(response.round, 1);
}

// ---------------------------------------------------------------------------
// Round map - a small context map centered on the wiki's location, and (for
// PHOTO_COORDINATES rounds only) click-to-place-a-marker for the answer.
// Mirrors spotguessr.ts's guess-map click pattern; not imported directly
// since spotguessr.ts doesn't export it as a reusable module.
// ---------------------------------------------------------------------------

function ensureRoundMap(): L.Map {
    if (state.roundMap) return state.roundMap;
    state.roundMap = L.map("cs-round-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(state.roundMap);
    state.roundMap.on("click", (event) => {
        if (state.currentFieldKind !== FIELD_KIND.PHOTO_COORDINATES) return;
        placeAnswerMarker(event.latlng);
    });
    return state.roundMap;
}

function placeAnswerMarker(latlng: L.LatLng): void {
    const map = ensureRoundMap();
    if (state.answerMarker) {
        state.answerMarker.setLatLng(latlng);
    } else {
        state.answerMarker = L.marker(latlng, { draggable: true }).addTo(map);
    }
    el<HTMLButtonElement>("cs-submit-answer-btn").disabled = false;
}

function resetRoundMap(latitude: number | null, longitude: number | null): void {
    const map = ensureRoundMap();
    if (state.answerMarker) {
        map.removeLayer(state.answerMarker);
        state.answerMarker = null;
    }
    if (state.contextMarker) {
        map.removeLayer(state.contextMarker);
        state.contextMarker = null;
    }
    if (latitude !== null && longitude !== null) {
        const latlng = L.latLng(latitude, longitude);
        state.contextMarker = L.marker(latlng, { opacity: 0.6 }).addTo(map);
        map.setView(latlng, 16);
    } else {
        map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    }
    // The map container may have been hidden (display:none) while another
    // panel was showing - Leaflet needs a nudge once it's visible again.
    setTimeout(() => map.invalidateSize(), 0);
}

// ---------------------------------------------------------------------------
// Round rendering - the answer widget shown depends on field_kind; every
// possible widget is pre-rendered in the template and toggled via
// [data-field-only], mirroring spotguessr.ts's [data-mode-only] pattern.
// ---------------------------------------------------------------------------

function updateAnswerAreaVisibility(fieldKind: string): void {
    document.querySelectorAll<HTMLElement>("[data-field-only]").forEach((field) => {
        field.hidden = true;
    });
    switch (fieldKind) {
        case FIELD_KIND.WIKI_DESCRIPTION:
            el("cs-answer-textarea-wrap").hidden = false;
            break;
        case FIELD_KIND.WIKI_INDOOR_OUTDOOR:
        case FIELD_KIND.WIKI_PIN_TYPE:
            el("cs-answer-select-wrap").hidden = false;
            break;
        case FIELD_KIND.PHOTO_COORDINATES:
            el("cs-answer-map-hint-wrap").hidden = false;
            break;
        default:
            el("cs-answer-text-wrap").hidden = false;
            break;
    }
}

function populateSelectOptions(fieldKind: string): void {
    const select = el<HTMLSelectElement>("cs-answer-select-input");
    select.innerHTML = "";
    const choices = fieldKind === FIELD_KIND.WIKI_INDOOR_OUTDOOR ? INDOOR_OUTDOOR_CHOICES : PIN_TYPE_CHOICES;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Choose one…";
    placeholder.disabled = true;
    placeholder.selected = true;
    select.appendChild(placeholder);
    for (const [value, label] of choices) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        select.appendChild(option);
    }
}

function currentAnswerValue(): string {
    switch (state.currentFieldKind) {
        case FIELD_KIND.WIKI_DESCRIPTION:
            return el<HTMLTextAreaElement>("cs-answer-textarea-input").value.trim();
        case FIELD_KIND.WIKI_INDOOR_OUTDOOR:
        case FIELD_KIND.WIKI_PIN_TYPE:
            return el<HTMLSelectElement>("cs-answer-select-input").value;
        default:
            return el<HTMLInputElement>("cs-answer-text-input").value.trim();
    }
}

function updateSubmitEnabled(): void {
    if (state.currentFieldKind === FIELD_KIND.PHOTO_COORDINATES) return; // handled by placeAnswerMarker instead
    el<HTMLButtonElement>("cs-submit-answer-btn").disabled = !currentAnswerValue();
}

function renderRound(round: RoundPayload, roundNumber: number): void {
    state.currentRoundId = round.round_id;
    state.currentFieldKind = round.field_kind;
    state.answeredProfileIds = new Set();
    state.votedProfileIds = new Set();

    showPanel("game");
    el("cs-reveal-panel").hidden = true;
    el("cs-vote-panel").hidden = true;
    el("cs-round-live-indicator").hidden = true;
    el("cs-round-status").textContent = `Round ${roundNumber} of ${state.totalRounds}`;
    el("cs-round-field-label").textContent = round.field_label;
    el("cs-round-wiki-name").textContent = round.wiki_name;
    // Ending early only makes sense for the host of a shared game - solo
    // play has "play again" for the same purpose already.
    el<HTMLButtonElement>("cs-end-game-btn").hidden = !(state.isMultiplayer && state.hostProfileId === myProfileId);

    const photo = el<HTMLImageElement>("cs-round-photo");
    if (round.field_kind === FIELD_KIND.PHOTO_COORDINATES && round.image_url) {
        // round.image_url is a Django ImageField storage path (Image.image.url), never
        // a user-typed value - see services.consensus.serializers.serialize_round.
        photo.src = round.image_url; // lgtm[js/xss,js/client-side-unvalidated-url-redirection]
        photo.hidden = false;
    } else {
        photo.hidden = true;
        photo.removeAttribute("src");
    }

    updateAnswerAreaVisibility(round.field_kind);
    if (round.field_kind === FIELD_KIND.WIKI_INDOOR_OUTDOOR || round.field_kind === FIELD_KIND.WIKI_PIN_TYPE) {
        populateSelectOptions(round.field_kind);
    } else if (round.field_kind === FIELD_KIND.WIKI_DESCRIPTION) {
        el<HTMLTextAreaElement>("cs-answer-textarea-input").value = "";
    } else if (round.field_kind !== FIELD_KIND.PHOTO_COORDINATES) {
        el<HTMLInputElement>("cs-answer-text-input").value = "";
    }

    resetRoundMap(round.latitude, round.longitude);

    el<HTMLButtonElement>("cs-submit-answer-btn").disabled = true;
    el<HTMLButtonElement>("cs-submit-answer-btn").hidden = false;
    el<HTMLButtonElement>("cs-skip-btn").hidden = false;
    el<HTMLButtonElement>("cs-skip-btn").disabled = false;
    el("cs-round-waiting-hint").hidden = true;
}

// ---------------------------------------------------------------------------
// Answer / skip / photo upload
// ---------------------------------------------------------------------------

async function afterAnswerOrSkip(): Promise<void> {
    el<HTMLButtonElement>("cs-submit-answer-btn").hidden = true;
    el<HTMLButtonElement>("cs-skip-btn").hidden = true;
    if (!state.isMultiplayer) {
        // Solo sessions never open a WebSocket and have nothing to agree/
        // disagree with - the answer already applied server-side, so just
        // advance straight to the next round (or the summary).
        await goToNextRound();
        return;
    }
    // Competitive: wait for the round.revealed/round.started broadcast.
    el("cs-round-waiting-hint").hidden = false;
}

async function submitAnswer(): Promise<void> {
    if (state.sessionId === null || state.currentRoundId === null) return;
    let payload: Record<string, string>;
    if (state.currentFieldKind === FIELD_KIND.PHOTO_COORDINATES) {
        if (!state.answerMarker) return;
        const latlng = state.answerMarker.getLatLng();
        payload = { latitude: String(latlng.lat), longitude: String(latlng.lng) };
    } else {
        const value = currentAnswerValue();
        if (!value) return;
        payload = { value };
    }

    const response = await postForm(urlFor(urls.answer, state.sessionId, state.currentRoundId), payload);
    if (response.error) {
        toast.error(response.error);
        return;
    }
    await afterAnswerOrSkip();
}

async function skipRound(): Promise<void> {
    if (state.sessionId === null || state.currentRoundId === null) return;
    const response = await postForm(urlFor(urls.skip, state.sessionId, state.currentRoundId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
    await afterAnswerOrSkip();
}

async function uploadPhoto(): Promise<void> {
    if (state.sessionId === null || state.currentRoundId === null) return;
    const input = el<HTMLInputElement>("cs-photo-upload-input");
    const file = input.files?.[0];
    if (!file) {
        toast.error("Choose a photo first.");
        return;
    }
    const formData = new FormData();
    formData.append("image", file);
    // Same-origin urlFor(...) path template - see postForm's note above.
    const response = await fetch(urlFor(urls.photo, state.sessionId, state.currentRoundId), {  // lgtm[js/request-forgery]
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken() },
        body: formData,
    });
    const data = await response.json();
    if (!response.ok || data.error) {
        toast.error(data.error ?? "Couldn't upload that photo.");
        return;
    }
    toast.success("Photo uploaded - thanks for helping out!");
    input.value = "";
}

async function goToNextRound(): Promise<void> {
    if (state.sessionId === null) return;
    const data = await getJson(urlFor(urls.round, state.sessionId));
    if (data.no_eligible_wikis) {
        showNoEligibleWikis();
        return;
    }
    if (data.finished) {
        showSummary(data.summary);
        return;
    }
    if (data.round) {
        renderRound(data.round, data.round.sequence_index + 1);
    }
}

// ---------------------------------------------------------------------------
// Reveal + vote (competitive only - arrives over the WebSocket)
// ---------------------------------------------------------------------------

function formatAnswerValue(value: unknown): string {
    if (value && typeof value === "object" && "latitude" in (value as Record<string, unknown>) && "longitude" in (value as Record<string, unknown>)) {
        const point = value as { latitude: number; longitude: number };
        return `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`;
    }
    return value === null || value === undefined || value === "" ? "(skipped)" : String(value);
}

// Deliberately identical text/markup whether resolution is "agreed" or a
// trust-check outcome ("check_passed"/"check_failed") - the client must
// never surface a check round as anything other than an ordinary one (see
// services.consensus.serializers's module docstring).
function revealTitle(resolution: string): string {
    switch (resolution) {
        case "agreed":
        case "check_passed":
        case "check_failed":
            return "Everyone agreed!";
        case "vote_open":
            return "You disagreed - time to vote";
        case "vote_resolved":
            return "The vote is in";
        case "tentative":
            return "No consensus yet - saved for later review";
        case "skipped":
            return "Round skipped";
        default:
            return "Round resolved";
    }
}

function renderResultsList(answers: RevealAnswer[]): void {
    const list = el("cs-reveal-results");
    list.innerHTML = "";
    for (const answer of answers) {
        const item = document.createElement("li");
        item.className = "consensus-score-card";

        const avatarWrap = document.createElement("span");
        avatarWrap.className = "consensus-score-card-avatar-wrap";
        if (answer.avatar_url) {
            const img = document.createElement("img");
            img.className = "friend-avatar-sm";
            // answer.avatar_url is Profile.avatar.url (Django storage path), not user-typed.
            img.src = answer.avatar_url; // lgtm[js/xss,js/client-side-unvalidated-url-redirection]
            img.alt = answer.username;
            avatarWrap.appendChild(img);
        } else {
            const placeholder = document.createElement("span");
            placeholder.className = "friend-avatar-sm friend-avatar-placeholder";
            placeholder.textContent = answer.username.slice(0, 1).toUpperCase();
            avatarWrap.appendChild(placeholder);
        }

        const info = document.createElement("span");
        info.className = "consensus-score-card-info";
        const name = document.createElement("span");
        name.className = "consensus-score-card-name";
        name.textContent = answer.username;
        const subtitle = document.createElement("span");
        subtitle.className = "consensus-score-card-subtitle";
        subtitle.textContent = formatAnswerValue(answer.value);
        info.append(name, subtitle);

        const points = document.createElement("span");
        points.className = "consensus-score-card-points";
        points.textContent = `+${answer.points_awarded} pts`;

        item.append(avatarWrap, info, points);
        list.appendChild(item);
    }
}

function renderVoteOptions(options: VoteOption[]): void {
    const container = el("cs-vote-options");
    container.hidden = false;
    container.innerHTML = "";
    el("cs-vote-waiting").hidden = true;
    for (const option of options) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "consensus-vote-option";
        const value = document.createElement("span");
        value.className = "consensus-vote-option-value";
        value.textContent = formatAnswerValue(option.value);
        const submittedBy = document.createElement("span");
        submittedBy.className = "consensus-vote-option-submitted-by";
        submittedBy.textContent = `submitted by ${option.submitted_by}`;
        button.append(value, submittedBy);
        button.addEventListener("click", () => void submitVote(option.answer_id, container));
        container.appendChild(button);
    }
}

async function submitVote(answerId: number, container: HTMLElement): Promise<void> {
    if (state.sessionId === null || state.currentRoundId === null) return;
    container.querySelectorAll("button").forEach((button) => {
        (button as HTMLButtonElement).disabled = true;
    });
    const response = await postForm(urlFor(urls.vote, state.sessionId, state.currentRoundId), { answer_id: String(answerId) });
    if (response.error) {
        toast.error(response.error);
        container.querySelectorAll("button").forEach((button) => {
            (button as HTMLButtonElement).disabled = false;
        });
        return;
    }
    container.hidden = true;
    el("cs-vote-waiting").hidden = false;
}

function updateScoreboardFromReveal(answers: RevealAnswer[]): void {
    if (!state.isMultiplayer) return;
    for (const answer of answers) {
        let entry = state.scoreboard.find((participant) => participant.profile_id === answer.profile_id);
        if (!entry) {
            entry = { profile_id: answer.profile_id, username: answer.username, avatar_url: answer.avatar_url, total_points_this_session: 0 };
            state.scoreboard.push(entry);
        }
        entry.total_points_this_session += answer.points_awarded;
    }
    renderScoreboard();
}

function renderScoreboard(): void {
    const list = el("cs-scoreboard");
    if (!state.isMultiplayer || !state.scoreboard.length) {
        list.hidden = true;
        return;
    }
    list.hidden = false;
    list.innerHTML = "";
    const sorted = [...state.scoreboard].sort((a, b) => b.total_points_this_session - a.total_points_this_session);
    sorted.forEach((entry, index) => {
        const item = document.createElement("li");
        item.className = "consensus-score-card";
        item.dataset.profileId = String(entry.profile_id);
        const rank = document.createElement("span");
        rank.className = "consensus-score-card-rank";
        rank.textContent = `#${index + 1}`;
        const name = document.createElement("span");
        name.className = "consensus-score-card-name";
        name.textContent = entry.username;
        const points = document.createElement("span");
        points.className = "consensus-score-card-points";
        points.textContent = `${entry.total_points_this_session} pts`;
        item.append(rank, name, points);
        list.appendChild(item);
    });
}

function renderReveal(data: RevealBroadcast): void {
    el<HTMLButtonElement>("cs-submit-answer-btn").hidden = true;
    el<HTMLButtonElement>("cs-skip-btn").hidden = true;
    el("cs-round-waiting-hint").hidden = true;
    el("cs-round-live-indicator").hidden = true;

    el("cs-reveal-panel").hidden = false;
    el("cs-reveal-title").textContent = revealTitle(data.resolution);
    renderResultsList(data.answers);
    updateScoreboardFromReveal(data.answers);

    const votePanel = el("cs-vote-panel");
    if (data.resolution === "vote_open" && data.vote_options?.length) {
        votePanel.hidden = false;
        renderVoteOptions(data.vote_options);
    } else {
        votePanel.hidden = true;
    }
}

// ---------------------------------------------------------------------------
// Live "someone answered/voted" indicator - purely cosmetic, driven by the
// answer.submitted/vote.submitted broadcasts (which carry only a profile_id).
// ---------------------------------------------------------------------------

function markAnswered(profileId: number): void {
    state.answeredProfileIds.add(profileId);
    const total = state.participants.filter((participant) => participant.status === "joined").length || state.participants.length;
    updateLiveIndicator(`${state.answeredProfileIds.size} of ${total || "?"} answered`);
}

function markVoted(profileId: number): void {
    state.votedProfileIds.add(profileId);
    const total = state.participants.filter((participant) => participant.status === "joined").length || state.participants.length;
    updateLiveIndicator(`${state.votedProfileIds.size} of ${total || "?"} voted`);
}

function updateLiveIndicator(text: string): void {
    const indicator = el("cs-round-live-indicator");
    indicator.textContent = text;
    indicator.hidden = false;
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

function showSummary(summary: SummaryPayload): void {
    showPanel("summary");
    const roundWord = summary.total_rounds === 1 ? "round" : "rounds";
    el("cs-summary-heading").textContent = summary.status === "abandoned" ? "Game ended early" : "Game over!";
    el("cs-summary-subheading").textContent = `${summary.rounds_played} of ${summary.total_rounds} ${roundWord} played.`;

    const list = el("cs-summary-scores");
    list.innerHTML = "";
    const sorted = [...summary.participants].sort((a, b) => b.total_points_this_session - a.total_points_this_session);

    if (!state.isMultiplayer && sorted.length === 1) {
        const [only] = sorted;
        const item = document.createElement("li");
        item.className = "consensus-score-card consensus-score-card--solo";
        item.textContent = `Your score: ${only?.total_points_this_session ?? 0} pts`;
        list.appendChild(item);
    } else {
        sorted.forEach((participant, index) => {
            const item = document.createElement("li");
            item.className = "consensus-score-card";
            const rank = document.createElement("span");
            rank.className = "consensus-score-card-rank";
            rank.textContent = `#${index + 1}`;
            const name = document.createElement("span");
            name.className = "consensus-score-card-name";
            name.textContent = participant.is_host ? `${participant.username} (host)` : participant.username;
            const points = document.createElement("span");
            points.className = "consensus-score-card-points";
            points.textContent = `${participant.total_points_this_session} pts`;
            item.append(rank, name, points);
            list.appendChild(item);
        });
    }

    if (state.ws) {
        state.ws.close();
        state.ws = null;
    }
}

// ---------------------------------------------------------------------------
// Real-time (multiplayer only)
// ---------------------------------------------------------------------------

function connectSessionSocket(): void {
    if (state.ws || state.sessionId === null) return;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    state.ws = new WebSocket(`${proto}${location.host}/ws/consensus/session/${state.sessionId}/`);
    state.ws.addEventListener("message", (event) => {
        try {
            handleSocketMessage(JSON.parse(event.data));
        } catch {
            // Ignore unparseable frames - nothing actionable to do with one.
        }
    });
    state.ws.addEventListener("close", () => {
        state.ws = null;
    });
    el("cs-chat-panel").hidden = false;
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
        case "answer.submitted":
            markAnswered(data.profile_id);
            break;
        case "vote.submitted":
            markVoted(data.profile_id);
            break;
        case "round.revealed":
            renderReveal(data as RevealBroadcast);
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
    const log = el("cs-chat-log");
    const line = document.createElement("div");
    const nameSpan = document.createElement("span");
    nameSpan.className = "consensus-chat-username";
    nameSpan.textContent = `${message.username}:`;
    line.append(nameSpan, document.createTextNode(message.body));
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

async function loadChatHistory(): Promise<void> {
    if (state.sessionId === null) return;
    const data = await getJson(urlFor(urls.chat_history, state.sessionId));
    const log = el("cs-chat-log");
    log.innerHTML = "";
    for (const message of (data.messages ?? []) as ChatMessagePayload[]) appendChatMessage(message);
}

function initChat(): void {
    el("cs-chat-form").addEventListener("submit", (event) => {
        event.preventDefault();
        const input = el<HTMLInputElement>("cs-chat-input");
        const body = input.value.trim();
        if (!body || !state.ws) return;
        state.ws.send(JSON.stringify({ body }));
        input.value = "";
    });
}

// ---------------------------------------------------------------------------
// Start / reset
// ---------------------------------------------------------------------------

function _resetSessionState(): void {
    state.sessionId = null;
    state.currentRoundId = null;
    state.isMultiplayer = false;
    state.hostProfileId = null;
    state.scoreboard = [];
    state.participants = [];
    state.selectedInviteIds.clear();
    if (state.ws) {
        state.ws.close();
        state.ws = null;
    }
}

function resetToSettings(): void {
    _resetSessionState();
    showPanel("settings");
}

function showNoEligibleWikis(): void {
    _resetSessionState();
    showPanel("empty");
}

async function startGame(): Promise<void> {
    state.scoreboard = [];
    const body = new URLSearchParams({ total_rounds: el<HTMLInputElement>("cs-rounds").value });
    for (const profileId of state.selectedInviteIds) body.append("invite_profile_ids", String(profileId));

    const response = await postForm(urls.start, body);
    if (response.error) {
        toast.error(response.error);
        return;
    }
    if (response.error_code === "no_eligible_wikis") {
        showNoEligibleWikis();
        return;
    }

    state.sessionId = response.session_id;
    state.totalRounds = Number(el<HTMLInputElement>("cs-rounds").value);

    if (response.lobby) {
        renderLobby(response.session as SessionPayload);
        return;
    }

    state.isMultiplayer = false;
    if (response.round) {
        renderRound(response.round, response.round.sequence_index + 1);
    }
}

// Host-only manual escape hatch for a stalled/AFK competitive session -
// mirrors spotguessr.ts's endGameNow().
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

// ---------------------------------------------------------------------------
// Deep link from an invite notification (?session=<id>)
// ---------------------------------------------------------------------------

async function loadInitialSession(): Promise<void> {
    const raw = pageEl?.dataset.initialSessionId;
    if (!raw) return;
    state.sessionId = Number(raw);
    state.isMultiplayer = true;

    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, state.sessionId));
    state.totalRounds = lobby.total_rounds;
    state.hostProfileId = lobby.host_profile_id;

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
    const data = await getJson(urlFor(urls.round, state.sessionId));
    if (data.no_eligible_wikis) {
        showNoEligibleWikis();
    } else if (data.finished) {
        showSummary(data.summary);
    } else if (data.round) {
        renderRound(data.round, data.round.sequence_index + 1);
    }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function init(): void {
    el("cs-start-form").addEventListener("submit", (event) => {
        event.preventDefault();
        void startGame();
    });
    el("cs-submit-answer-btn").addEventListener("click", () => void submitAnswer());
    el("cs-skip-btn").addEventListener("click", () => void skipRound());
    el("cs-photo-upload-btn").addEventListener("click", () => void uploadPhoto());
    el("cs-join-lobby-btn").addEventListener("click", () => void joinLobby());
    el("cs-begin-btn").addEventListener("click", () => void beginGame());
    el("cs-invite-more-btn").addEventListener("click", () => void handleInviteMore());
    el("cs-end-game-btn").addEventListener("click", () => void endGameNow());
    el("cs-play-again-btn").addEventListener("click", resetToSettings);
    el("cs-empty-state-settings-btn").addEventListener("click", resetToSettings);
    el("cs-friend-list-retry").addEventListener("click", () => void fetchFriendsEagerly());
    el<HTMLInputElement>("cs-answer-text-input").addEventListener("input", updateSubmitEnabled);
    el<HTMLTextAreaElement>("cs-answer-textarea-input").addEventListener("input", updateSubmitEnabled);
    el<HTMLSelectElement>("cs-answer-select-input").addEventListener("change", updateSubmitEnabled);

    initChat();
    void fetchFriendsEagerly();
    void loadInitialSession();
}

document.addEventListener("DOMContentLoaded", init);
