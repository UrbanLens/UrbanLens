/**
 * Trivia - solo and multiplayer gameplay loop, lobby, and chat.
 *
 * Server-authoritative: this file never decides whether an answer is
 * correct or computes points itself - it only collects an answer, posts it,
 * and renders whatever `services.trivia.session` decided. Multiplayer state
 * sync (lobby updates, round advancement, chat) arrives over a WebSocket
 * (`consumers.TriviaSessionConsumer`); solo sessions never open one at all.
 * Mirrors spotguessr.ts's shape, minus the map/photo/lobby-drawing machinery
 * Trivia doesn't need.
 *
 * Chrome (panel swapping, focus mode, fullscreen, the players/chat drawer)
 * belongs to the shared game shell - see ts/shared/game-shell.ts.
 */
import { getCsrfToken } from "../shared/csrf";
import { confirmAction, toast } from "../shared/dialogs";
import { createGameShell, playEntrance, type GameShell } from "../shared/game-shell";

declare global {
    interface Window {
        TRIVIA_URLS: {
            start: string;
            friends: string;
            settings: string;
            lobby: string;
            invite: string;
            join: string;
            begin: string;
            end: string;
            leave: string;
            kick: string;
            round: string;
            answer: string;
            chat_history: string;
            summary: string;
            vote: string;
            session_id_sentinel: string;
            round_id_sentinel: string;
            question_id_sentinel: string;
        };
        TRIVIA_LAST_CONFIG: { difficulty?: number } | null;
    }
}

interface RoundPayload {
    round_id: number;
    session_id: number;
    sequence_index: number;
    prompt: string;
    question_id: number;
    revealed: boolean;
}

interface RevealResponse {
    round_id: number;
    question_id: number;
    is_correct: boolean;
    points: number;
    revealed: boolean;
    answer?: string;
    error?: string;
}

interface RoundResult {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    is_correct: boolean;
    points: number;
}

interface RoundRevealBroadcast {
    round_id: number;
    question_id: number;
    answer: string;
    results: RoundResult[];
}

interface ParticipantPayload {
    profile_id: number;
    username: string;
    avatar_url: string | null;
    status: "invited" | "joined";
    total_points: number;
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
    participants: { profile_id: number; username: string; avatar_url: string | null; total_points: number }[];
}

interface ChatMessagePayload {
    message_id: number;
    profile_id: number;
    username: string;
    body: string;
    created: string;
}

interface FriendOption {
    profile_id: number;
    username: string;
}

/** Shell panel name -> the element id the markup uses. */
const PANEL_IDS: Record<string, string> = {
    settings: "trivia-settings-panel",
    empty: "trivia-empty-state",
    lobby: "trivia-lobby-panel",
    round: "trivia-round-panel",
    summary: "trivia-summary-panel",
};

/** Reverse of PANEL_IDS, so callers can keep passing the element id they always did. */
const PANEL_NAME_BY_ID: Record<string, string> = {
    "trivia-settings-panel": "settings",
    "trivia-empty-state": "empty",
    "trivia-lobby-panel": "lobby",
    "trivia-round-panel": "round",
    "trivia-summary-panel": "summary",
};

const urls = window.TRIVIA_URLS;
const pageEl = document.querySelector<HTMLElement>(".trivia-page");
const myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");

let shell: GameShell | null = null;
let shellEl: HTMLElement | null = null;
let sessionId: number | null = null;
let currentRound: RoundPayload | null = null;
let isMultiplayer = false;
let hostProfileId: number | null = null;
let ws: WebSocket | null = null;
let friendOptions: FriendOption[] = [];
let totalRounds = 0;
let sessionPoints = 0;
// The round submitAnswer() has already credited points for via its own
// direct response, if any - showBroadcastReveal() still has to run for
// this round (it's the only source of every player's results table), but
// must not award this player's own points a second time.
let lastRevealedRoundId: number | null = null;

function urlFor(template: string, sessionIdValue?: number, roundIdValue?: number, questionIdValue?: number): string {
    let resolved = template;
    if (sessionIdValue !== undefined) resolved = resolved.replace(urls.session_id_sentinel, String(sessionIdValue));
    if (roundIdValue !== undefined) resolved = resolved.replace(urls.round_id_sentinel, String(roundIdValue));
    if (questionIdValue !== undefined) resolved = resolved.replace(urls.question_id_sentinel, String(questionIdValue));
    return resolved;
}

async function postForm(url: string, data: Record<string, string>): Promise<any> {
    // url is always urlFor(urls.<name>, ...) - a same-origin, server-rendered path
    // template with only numeric ids substituted, never an arbitrary/external url.
    const response = await fetch(url, {  // lgtm[js/request-forgery]
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(data),
    });
    return response.json();
}

async function getJson(url: string): Promise<any> {
    // Same-origin urlFor(...) path template - see postForm's note above.
    const response = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });  // lgtm[js/request-forgery]
    return response.json();
}

function el<T extends HTMLElement>(id: string): T {
    const found = document.getElementById(id);
    if (!found) throw new Error(`Missing #${id}`);
    return found as T;
}

function showPanel(id: string): void {
    const name = PANEL_NAME_BY_ID[id];
    if (!name) {
        console.error(`[trivia] no shell panel registered for #${id}`);
        return;
    }
    shell?.showPanel(name);
}

// Every network round-trip on this page is a button press, and none of them
// used to change anything on screen until the response landed.
async function withBusy<T>(button: HTMLButtonElement | null, work: () => Promise<T>): Promise<T> {
    if (button) {
        button.disabled = true;
        button.classList.add("is-loading");
    }
    try {
        return await work();
    } finally {
        if (button) {
            button.disabled = false;
            button.classList.remove("is-loading");
        }
    }
}

// ---------------------------------------------------------------------------
// Friend picker
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
        container.innerHTML = '<p class="trivia-panel-hint">No friends available to invite.</p>';
        return;
    }
    for (const friend of available) {
        const label = document.createElement("label");
        label.className = "trivia-friend-option";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = String(friend.profile_id);
        label.append(checkbox, document.createTextNode(friend.username));
        container.appendChild(label);
    }
}

async function initFriendPicker(): Promise<void> {
    const toggle = el<HTMLInputElement>("trivia-play-with-friends");
    const wrap = el("trivia-invite-wrap");
    const friends = await loadFriendOptions();
    toggle.addEventListener("change", () => {
        wrap.hidden = !toggle.checked;
        if (toggle.checked) renderFriendCheckboxes(el("trivia-friend-list"), friends, new Set());
    });
}

// Builds a small checkbox-picker dialog on the fly and resolves with the
// chosen profile ids (empty if cancelled). There's no dedicated "invite
// more" dialog markup in the template (unlike the initial invite flow's
// trivia-friend-list, which lives inside the settings panel) so this is
// constructed in JS, but it reuses renderFriendCheckboxes - the exact same
// checkbox rendering the initial invite flow uses - rather than duplicating
// it. Replaces the old window.prompt() exact-username-match flow, which
// silently no-op'd on any typo or case mismatch with zero feedback.
//
// It is mounted into the shell rather than document.body so it stays painted
// (and inside the page's [hidden] / custom-property scope) in true fullscreen.
function pickFriendsToInvite(available: FriendOption[]): Promise<number[]> {
    return new Promise((resolve) => {
        const dialog = document.createElement("dialog");
        dialog.className = "ul-dialog ul-game-dialog trivia-invite-more-dialog";

        const header = document.createElement("div");
        header.className = "dialog-header";
        const heading = document.createElement("h3");
        heading.textContent = "Invite more players";
        header.appendChild(heading);

        const list = document.createElement("div");
        list.className = "trivia-invite-more-list";
        renderFriendCheckboxes(list, available, new Set());

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

        dialog.append(header, list, actions);
        if (shell) {
            shell.mountOverlay(dialog);
        } else {
            document.body.appendChild(dialog);
        }

        const cleanup = (result: number[]) => {
            dialog.close();
            dialog.remove();
            resolve(result);
        };
        cancelBtn.addEventListener("click", () => cleanup([]));
        inviteBtn.addEventListener("click", () => {
            const checked = Array.from(list.querySelectorAll<HTMLInputElement>("input:checked")).map((input) => Number(input.value));
            cleanup(checked);
        });
        // Escape key / native "cancel" - treat like the Cancel button.
        dialog.addEventListener("cancel", () => cleanup([]));

        dialog.showModal();
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

    const chosenIds = await pickFriendsToInvite(available);
    if (!chosenIds.length) return;

    for (const profileId of chosenIds) {
        const response = await postForm(urlFor(urls.invite, sessionId), { profile_id: String(profileId) });
        if (response.error) toast.error(response.error);
    }
    await refreshLobby();
}

// ---------------------------------------------------------------------------
// Lobby
// ---------------------------------------------------------------------------

function renderLobbyParticipants(participants: ParticipantPayload[]): void {
    const list = el("trivia-lobby-participants");
    list.innerHTML = "";
    const isHost = hostProfileId === myProfileId;
    for (const participant of participants) {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = participant.is_host ? `${participant.username} (host)` : participant.username;
        const status = document.createElement("span");
        status.className = participant.status === "joined" ? "trivia-lobby-status trivia-lobby-status--joined" : "trivia-lobby-status";
        status.textContent = participant.status === "joined" ? "Joined" : "Invited";
        item.append(name, status);
        // Host can remove anyone but themselves - ending the whole game
        // instead is "End game", not a self-kick.
        if (isHost && participant.profile_id !== myProfileId) {
            const kickBtn = document.createElement("button");
            kickBtn.type = "button";
            kickBtn.className = "trivia-kick-btn";
            kickBtn.title = `Remove ${participant.username}`;
            kickBtn.setAttribute("aria-label", `Remove ${participant.username}`);
            kickBtn.textContent = "×";
            kickBtn.addEventListener("click", () => void kickParticipant(participant.profile_id, participant.username));
            item.appendChild(kickBtn);
        }
        list.appendChild(item);
    }

    const me = participants.find((participant) => participant.profile_id === myProfileId);
    el<HTMLButtonElement>("trivia-invite-more-btn").hidden = !isHost;
    el<HTMLButtonElement>("trivia-join-lobby-btn").hidden = !(me && me.status === "invited");
    el<HTMLButtonElement>("trivia-begin-btn").hidden = !isHost;
    el<HTMLButtonElement>("trivia-leave-lobby-btn").hidden = !me;
    el<HTMLButtonElement>("trivia-end-game-lobby-btn").hidden = !isHost;
}

function renderLobby(session: SessionPayload): void {
    sessionId = session.session_id;
    hostProfileId = session.host_profile_id;
    isMultiplayer = true;
    if (session.total_rounds) totalRounds = session.total_rounds;
    showPanel("trivia-lobby-panel");
    renderLobbyParticipants(session.participants);
    connectSessionSocket();
}

async function refreshLobby(): Promise<void> {
    if (sessionId === null) return;
    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, sessionId));
    renderLobbyParticipants(lobby.participants);
}

async function joinLobby(): Promise<void> {
    if (sessionId === null) return;
    const id = sessionId;
    const response = await withBusy(el<HTMLButtonElement>("trivia-join-lobby-btn"), () => postForm(urlFor(urls.join, id), {}));
    if (response.error) {
        toast.error(response.error);
        return;
    }
    await refreshLobby();
}

async function beginGame(): Promise<void> {
    if (sessionId === null) return;
    const id = sessionId;
    const payload = await withBusy(el<HTMLButtonElement>("trivia-begin-btn"), () => postForm(urlFor(urls.begin, id), {}));
    await handleStartOrRoundResponse(payload);
}

// ---------------------------------------------------------------------------
// Real-time (multiplayer only)
// ---------------------------------------------------------------------------

/** The players/chat drawer only has anything in it once a socket exists. */
function setRailAvailable(on: boolean): void {
    shell?.setRailAvailable(on);
}

function connectSessionSocket(): void {
    if (ws || sessionId === null) return;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    // Built entirely from the current page's own location - always same-origin.
    ws = new WebSocket(`${proto}${location.host}/ws/trivia/session/${sessionId}/`);  // lgtm[js/request-forgery]
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
    el("trivia-chat-panel").hidden = false;
    setRailAvailable(true);
    void loadChatHistory();
}

function handleSocketMessage(data: any): void {
    switch (data.type) {
        case "participant.joined":
            void refreshLobby();
            break;
        case "participant.left":
            if (data.new_host_profile_id) {
                hostProfileId = data.new_host_profile_id;
                updateRoundActionVisibility();
            }
            if (data.profile_id === myProfileId) {
                toast.warning(data.reason === "kicked" ? "You were removed from the game by the host." : "You left the game.");
                resetToSettings();
            } else {
                toast.info(data.reason === "kicked" ? "A player was removed from the game." : "A player left the game.");
                void refreshLobby();
            }
            break;
        case "session.started":
            renderRound(data.round);
            break;
        case "round.revealed":
            showBroadcastReveal(data as RoundRevealBroadcast);
            break;
        case "round.started":
            renderRound(data.round);
            break;
        case "session.completed":
            renderSummary(data as SummaryPayload);
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
    const log = el("trivia-chat-log");
    const line = document.createElement("div");
    const nameSpan = document.createElement("span");
    nameSpan.className = "trivia-chat-username";
    nameSpan.textContent = `${message.username}:`;
    line.append(nameSpan, document.createTextNode(message.body));
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

async function loadChatHistory(): Promise<void> {
    if (sessionId === null) return;
    const data = await getJson(urlFor(urls.chat_history, sessionId));
    const log = el("trivia-chat-log");
    log.innerHTML = "";
    for (const message of (data.messages ?? []) as ChatMessagePayload[]) appendChatMessage(message);
}

function initChat(): void {
    el("trivia-chat-form").addEventListener("submit", (event) => {
        event.preventDefault();
        const input = el<HTMLInputElement>("trivia-chat-input");
        const body = input.value.trim();
        if (!body || !ws) return;
        ws.send(JSON.stringify({ body }));
        input.value = "";
    });
}

// ---------------------------------------------------------------------------
// Scoreboards
// ---------------------------------------------------------------------------

interface ScoreRowData {
    rank: number | null;
    username: string;
    avatarUrl: string | null;
    points: number;
    /** null on the end-of-game list, where per-round correctness no longer applies. */
    correct: boolean | null;
    isSelf: boolean;
}

function avatarInitial(username: string): string {
    const first = username.trim().charAt(0);
    return first ? first.toUpperCase() : "?";
}

function buildScoreRow(data: ScoreRowData, index: number): HTMLLIElement {
    const row = document.createElement("li");
    row.className = data.isSelf ? "trivia-score-card trivia-score-card--self" : "trivia-score-card";
    if (data.rank !== null && data.rank <= 3) row.classList.add(`trivia-score-card--rank-${data.rank}`);
    // Drives the staggered row entrance in _game_shell.scss.
    row.style.setProperty("--ul-game-i", String(index));

    const rank = document.createElement("span");
    rank.className = "trivia-score-card-rank";
    rank.textContent = data.rank !== null ? `#${data.rank}` : "";

    const avatarWrap = document.createElement("span");
    avatarWrap.className = "trivia-score-card-avatar-wrap";
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

    const name = document.createElement("span");
    name.className = "trivia-score-card-name";
    name.textContent = data.username;

    const status = document.createElement("span");
    status.className = "trivia-score-card-status";
    if (data.correct !== null) {
        status.classList.add(data.correct ? "trivia-score-card-status--correct" : "trivia-score-card-status--wrong");
        status.classList.add("material-symbols-outlined");
        status.textContent = data.correct ? "check" : "close";
    }

    const points = document.createElement("span");
    points.className = "trivia-score-card-points";
    points.textContent = `${data.points} pts`;

    row.append(rank, avatarWrap, name, status, points);
    return row;
}

function renderRoundScores(results: RoundResult[]): void {
    const list = el<HTMLUListElement>("trivia-round-scores");
    list.innerHTML = "";
    // A solo round's "scoreboard" is one row repeating the line above it.
    if (results.length < 2) {
        list.hidden = true;
        return;
    }
    const ordered = results.slice().sort((a, b) => b.points - a.points);
    ordered.forEach((result, index) => {
        list.appendChild(
            buildScoreRow(
                {
                    rank: index + 1,
                    username: result.username,
                    avatarUrl: result.avatar_url,
                    points: result.points,
                    correct: result.is_correct,
                    isSelf: result.profile_id === myProfileId,
                },
                index,
            ),
        );
    });
    list.hidden = false;
}

// ---------------------------------------------------------------------------
// Gameplay
// ---------------------------------------------------------------------------

// Whether the current viewer can end the whole game (host, multiplayer
// only - solo play has "play again" for the same purpose). Recomputed on
// every round render and whenever a host transfer happens mid-game
// (see the "participant.left" socket handler), since hostProfileId can
// change without a new round starting.
function updateRoundActionVisibility(): void {
    el<HTMLButtonElement>("trivia-leave-round-btn").hidden = !isMultiplayer;
    el<HTMLButtonElement>("trivia-end-game-round-btn").hidden = !(isMultiplayer && hostProfileId === myProfileId);
}

function setSessionPoints(points: number): void {
    sessionPoints = points;
    const chip = el("trivia-score");
    chip.hidden = false;
    chip.textContent = `${sessionPoints} pts`;
    chip.classList.remove("is-counting");
    // Force a reflow so a repeated award replays the flare.
    void chip.offsetWidth;
    chip.classList.add("is-counting");
}

function resetVoteButtons(): void {
    for (const button of document.querySelectorAll<HTMLButtonElement>("[data-vote]")) {
        button.classList.remove("is-cast");
        button.disabled = false;
    }
}

/** `null` means "answer accepted, verdict not in yet" - the multiplayer wait state. */
function applyRevealVerdict(correct: boolean | null): void {
    const reveal = el("trivia-reveal");
    reveal.classList.remove("ul-game-reveal--good", "ul-game-reveal--bad");
    const icon = el("trivia-reveal-icon");
    icon.classList.remove("trivia-reveal-icon--good", "trivia-reveal-icon--bad");
    if (correct === null) {
        icon.textContent = "hourglass_top";
        return;
    }
    reveal.classList.add(correct ? "ul-game-reveal--good" : "ul-game-reveal--bad");
    icon.classList.add(correct ? "trivia-reveal-icon--good" : "trivia-reveal-icon--bad");
    icon.textContent = correct ? "check_circle" : "cancel";
    shell?.flashStage(correct ? "good" : "bad");
}

function renderRound(round: RoundPayload): void {
    currentRound = round;
    lastRevealedRoundId = null;
    sessionId = round.session_id;
    showPanel("trivia-round-panel");
    const index = round.sequence_index + 1;
    el<HTMLParagraphElement>("trivia-round-index").textContent = totalRounds > 0 ? `Question ${index} of ${totalRounds}` : `Question ${index}`;
    shell?.setProgress(index, totalRounds);

    const prompt = el<HTMLHeadingElement>("trivia-prompt");
    prompt.textContent = round.prompt;
    playEntrance(prompt, shell?.reducedMotion() ?? false);

    el<HTMLInputElement>("trivia-answer-input").value = "";
    el<HTMLFormElement>("trivia-answer-form").hidden = false;
    el("trivia-reveal").hidden = true;
    applyRevealVerdict(null);
    resetVoteButtons();
    el<HTMLUListElement>("trivia-round-scores").hidden = true;
    el<HTMLInputElement>("trivia-answer-input").focus();
    updateRoundActionVisibility();
}

function renderSummary(summary: SummaryPayload): void {
    const mine = summary.participants.find((participant) => participant.profile_id === myProfileId);
    const prefix = summary.status === "abandoned" ? "Game ended early - not enough players remained.\n\n" : "";
    const list = el<HTMLUListElement>("trivia-summary-scores");
    list.innerHTML = "";

    if (isMultiplayer && summary.participants.length) {
        const ranked = summary.participants.slice().sort((a, b) => b.total_points - a.total_points);
        ranked.forEach((participant, index) => {
            list.appendChild(
                buildScoreRow(
                    {
                        rank: index + 1,
                        username: participant.username,
                        avatarUrl: participant.avatar_url,
                        points: participant.total_points,
                        correct: null,
                        isSelf: participant.profile_id === myProfileId,
                    },
                    index,
                ),
            );
        });
        list.hidden = false;
        el<HTMLParagraphElement>("trivia-summary-score").textContent = `${prefix}${summary.rounds_played} of ${summary.total_rounds} rounds played.`;
    } else {
        list.hidden = true;
        el<HTMLParagraphElement>("trivia-summary-score").textContent = mine
            ? `${prefix}You scored ${mine.total_points} points across ${summary.rounds_played} rounds.`
            : `${prefix}Finished - ${summary.rounds_played} rounds played.`;
    }

    if (mine) setSessionPoints(mine.total_points);
    showPanel("trivia-summary-panel");
    if (ws) {
        ws.close();
        ws = null;
    }
}

// Bails out of whatever game/lobby state we were in, back to the settings
// panel - used both for "Leave" succeeding and for being told (over the
// socket) that we were removed by the host.
function resetToSettings(): void {
    sessionId = null;
    currentRound = null;
    isMultiplayer = false;
    hostProfileId = null;
    totalRounds = 0;
    sessionPoints = 0;
    if (ws) {
        ws.close();
        ws = null;
    }
    el("trivia-chat-panel").hidden = true;
    setRailAvailable(false);
    const chip = el("trivia-score");
    chip.hidden = true;
    chip.textContent = "";
    shell?.setProgress(0, 0);
    showPanel("trivia-settings-panel");
}

// Voluntarily leave a lobby or in-progress game - the rest of the group
// (if any remain) keeps playing without us.
async function leaveGame(): Promise<void> {
    if (sessionId === null) return;
    const confirmed = await confirmAction({
        title: "Leave this game?",
        message: "You'll stop playing, but the rest of the group can continue without you.",
        confirmLabel: "Leave",
    });
    if (!confirmed) return;

    const response = await postForm(urlFor(urls.leave, sessionId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
    resetToSettings();
}

// Host-only manual escape hatch for a stalled/AFK multiplayer game - see
// controllers.trivia.TriviaEndSessionView. Confirmed first since it's
// irreversible for every other participant, not just the host.
async function endGameNow(): Promise<void> {
    if (sessionId === null) return;
    const confirmed = await confirmAction({
        title: "End this game?",
        message: "This ends the game immediately for everyone, using the scores so far.",
        confirmLabel: "End game",
    });
    if (!confirmed) return;

    const response = await postForm(urlFor(urls.end, sessionId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
    renderSummary(response.summary as SummaryPayload);
}

// Host-only: remove another participant (still-invited or already-joined)
// from the lobby/game.
async function kickParticipant(profileId: number, username: string): Promise<void> {
    if (sessionId === null) return;
    const confirmed = await confirmAction({
        title: `Remove ${username}?`,
        message: `${username} will be removed from this game.`,
        confirmLabel: "Remove",
    });
    if (!confirmed) return;

    const response = await postForm(urlFor(urls.kick, sessionId), { profile_id: String(profileId) });
    if (response.error) {
        toast.error(response.error);
        return;
    }
    await refreshLobby();
}

async function handleStartOrRoundResponse(payload: any): Promise<void> {
    if (payload.error) {
        toast.error(payload.error);
        return;
    }
    if (payload.lobby) {
        renderLobby(payload.session as SessionPayload);
        return;
    }
    if (payload.error_code === "no_eligible_questions" || payload.no_eligible_questions) {
        showPanel("trivia-empty-state");
        return;
    }
    if (payload.session_id) sessionId = payload.session_id;
    if (payload.total_rounds) totalRounds = payload.total_rounds;
    if (payload.finished) {
        renderSummary(payload.summary ?? (await getJson(urlFor(urls.summary, sessionId ?? undefined))));
        return;
    }
    if (payload.round) renderRound(payload.round);
}

async function startGame(): Promise<void> {
    const difficulty = el<HTMLInputElement>("trivia-difficulty").value;
    const requestedRounds = el<HTMLInputElement>("trivia-total-rounds").value;
    const body: Record<string, string> = { difficulty, total_rounds: requestedRounds };

    const params = new URLSearchParams(body);
    if (el<HTMLInputElement>("trivia-play-with-friends").checked) {
        const checked = Array.from(document.querySelectorAll<HTMLInputElement>("#trivia-friend-list input:checked"));
        if (!checked.length) {
            toast.error("Pick at least one friend to invite, or turn off multiplayer.");
            return;
        }
        for (const checkbox of checked) params.append("invite_profile_ids", checkbox.value);
    }

    totalRounds = Number(requestedRounds) || 0;
    sessionPoints = 0;

    const payload = await withBusy(el<HTMLButtonElement>("trivia-start-btn"), async () => {
        const response = await fetch(urls.start, {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
            body: params,
        });
        return response.json();
    });
    await handleStartOrRoundResponse(payload);
}

async function submitAnswer(): Promise<void> {
    if (sessionId === null || currentRound === null) return;
    const form = el<HTMLFormElement>("trivia-answer-form");
    const answer = el<HTMLInputElement>("trivia-answer-input").value;
    if (!answer.trim()) return;

    const submitBtn = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    const id = sessionId;
    const roundId = currentRound.round_id;
    const payload: RevealResponse = await withBusy(submitBtn, () => postForm(urlFor(urls.answer, id, roundId), { answer }));
    if (payload.error) {
        toast.error(payload.error);
        return;
    }

    form.hidden = true;
    const reveal = el("trivia-reveal");
    reveal.hidden = false;
    el<HTMLParagraphElement>("trivia-reveal-result").textContent = payload.revealed
        ? payload.is_correct
            ? `Correct! +${payload.points} points`
            : `Not quite - the answer was "${payload.answer ?? "unknown"}".`
        : "Answer submitted - waiting for the rest of the group...";
    applyRevealVerdict(payload.revealed ? payload.is_correct : null);
    if (payload.revealed) {
        // The round.revealed broadcast for this same round is still coming
        // (it's the only source of every player's results table) - this
        // just keeps it from crediting these points again when it arrives.
        lastRevealedRoundId = roundId;
        if (payload.points) setSessionPoints(sessionPoints + payload.points);
    }
    el<HTMLButtonElement>("trivia-next-btn").hidden = !payload.revealed || isMultiplayer;
}

function showBroadcastReveal(data: RoundRevealBroadcast): void {
    if (!currentRound || currentRound.round_id !== data.round_id) return;
    el<HTMLFormElement>("trivia-answer-form").hidden = true;
    const reveal = el("trivia-reveal");
    reveal.hidden = false;
    const mine = data.results.find((result) => result.profile_id === myProfileId);
    el<HTMLParagraphElement>("trivia-reveal-result").textContent = mine
        ? mine.is_correct
            ? `Correct! +${mine.points} points. The answer was "${data.answer}".`
            : `Not quite - the answer was "${data.answer}".`
        : `The answer was "${data.answer}".`;
    applyRevealVerdict(mine ? mine.is_correct : null);
    // Skipped when this round already credited via submitAnswer()'s own
    // response (this player was the one who revealed it) - otherwise the
    // same points land twice, once from each path.
    if (mine?.is_correct && mine.points && lastRevealedRoundId !== data.round_id) setSessionPoints(sessionPoints + mine.points);
    renderRoundScores(data.results);
}

async function voteOnCurrentQuestion(button: HTMLButtonElement, kind: string): Promise<void> {
    if (currentRound === null) return;
    const questionId = currentRound.question_id;
    const payload = await withBusy(button, () => postForm(urlFor(urls.vote, undefined, undefined, questionId), { kind }));
    if (payload.error) {
        toast.error(payload.error);
        return;
    }
    // Up/down are mutually exclusive; "report" is an independent flag.
    if (kind === "upvote" || kind === "downvote") {
        for (const other of document.querySelectorAll<HTMLButtonElement>('[data-vote="upvote"], [data-vote="downvote"]')) {
            other.classList.remove("is-cast");
        }
    }
    button.classList.add("is-cast");
}

async function goToNextRound(): Promise<void> {
    if (sessionId === null) return;
    const id = sessionId;
    const payload = await withBusy(el<HTMLButtonElement>("trivia-next-btn"), () => getJson(urlFor(urls.round, id)));
    await handleStartOrRoundResponse(payload);
}

// ---------------------------------------------------------------------------
// Deep link from an invite notification (?session=<id>)
// ---------------------------------------------------------------------------

async function loadInitialSession(): Promise<void> {
    const raw = pageEl?.dataset.initialSessionId;
    if (!raw) return;
    sessionId = Number(raw);
    isMultiplayer = true;

    const lobby: SessionPayload = await getJson(urlFor(urls.lobby, sessionId));
    hostProfileId = lobby.host_profile_id;
    if (lobby.total_rounds) totalRounds = lobby.total_rounds;
    if (lobby.status === "lobby") {
        renderLobby(lobby);
        return;
    }
    if (lobby.status === "completed" || lobby.status === "abandoned") {
        const summary: SummaryPayload = await getJson(urlFor(urls.summary, sessionId));
        renderSummary(summary);
        return;
    }
    connectSessionSocket();
    const data = await getJson(urlFor(urls.round, sessionId));
    await handleStartOrRoundResponse(data);
}

function init(): void {
    shellEl = document.getElementById("trivia-shell");
    if (pageEl && shellEl) {
        shell = createGameShell({
            root: pageEl,
            shell: shellEl,
            panels: PANEL_IDS,
            playingPanels: ["round", "summary"],
        });
    }
    setRailAvailable(false);
    showPanel("trivia-settings-panel");

    el<HTMLFormElement>("trivia-start-form").addEventListener("submit", (event) => {
        event.preventDefault();
        void startGame();
    });
    el<HTMLFormElement>("trivia-answer-form").addEventListener("submit", (event) => {
        event.preventDefault();
        void submitAnswer();
    });
    el<HTMLButtonElement>("trivia-next-btn").addEventListener("click", () => void goToNextRound());
    el<HTMLButtonElement>("trivia-play-again-btn").addEventListener("click", () => resetToSettings());
    el<HTMLButtonElement>("trivia-empty-state-settings-btn").addEventListener("click", () => showPanel("trivia-settings-panel"));
    el<HTMLButtonElement>("trivia-join-lobby-btn").addEventListener("click", () => void joinLobby());
    el<HTMLButtonElement>("trivia-begin-btn").addEventListener("click", () => void beginGame());
    el<HTMLButtonElement>("trivia-invite-more-btn").addEventListener("click", () => void handleInviteMore());
    el<HTMLButtonElement>("trivia-leave-lobby-btn").addEventListener("click", () => void leaveGame());
    el<HTMLButtonElement>("trivia-end-game-lobby-btn").addEventListener("click", () => void endGameNow());
    el<HTMLButtonElement>("trivia-leave-round-btn").addEventListener("click", () => void leaveGame());
    el<HTMLButtonElement>("trivia-end-game-round-btn").addEventListener("click", () => void endGameNow());

    document.querySelectorAll<HTMLButtonElement>("[data-vote]").forEach((button) => {
        button.addEventListener("click", () => void voteOnCurrentQuestion(button, button.dataset.vote as string));
    });

    const ratingsToggle = document.getElementById("trivia-show-ratings-to-friends") as HTMLInputElement | null;
    ratingsToggle?.addEventListener("change", () => {
        void postForm(urls.settings, { show_ratings_to_friends: ratingsToggle.checked ? "on" : "off" });
    });

    if (window.TRIVIA_LAST_CONFIG?.difficulty !== undefined) {
        el<HTMLInputElement>("trivia-difficulty").value = String(window.TRIVIA_LAST_CONFIG.difficulty);
    }

    void initFriendPicker();
    initChat();
    void loadInitialSession();
}

document.addEventListener("DOMContentLoaded", init);
