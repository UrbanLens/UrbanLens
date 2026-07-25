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
 */
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";

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

interface RoundRevealBroadcast {
    round_id: number;
    question_id: number;
    answer: string;
    results: { profile_id: number; username: string; avatar_url: string | null; is_correct: boolean; points: number }[];
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

const urls = window.TRIVIA_URLS;
const pageEl = document.querySelector<HTMLElement>(".trivia-page");
const myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");

let sessionId: number | null = null;
let currentRound: RoundPayload | null = null;
let isMultiplayer = false;
let hostProfileId: number | null = null;
let ws: WebSocket | null = null;
let friendOptions: FriendOption[] = [];

function urlFor(template: string, sessionIdValue?: number, roundIdValue?: number, questionIdValue?: number): string {
    let resolved = template;
    if (sessionIdValue !== undefined) resolved = resolved.replace(urls.session_id_sentinel, String(sessionIdValue));
    if (roundIdValue !== undefined) resolved = resolved.replace(urls.round_id_sentinel, String(roundIdValue));
    if (questionIdValue !== undefined) resolved = resolved.replace(urls.question_id_sentinel, String(questionIdValue));
    return resolved;
}

async function postForm(url: string, data: Record<string, string>): Promise<any> {
    const response = await fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(data),
    });
    return response.json();
}

async function getJson(url: string): Promise<any> {
    const response = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
    return response.json();
}

function el<T extends HTMLElement>(id: string): T {
    const found = document.getElementById(id);
    if (!found) throw new Error(`Missing #${id}`);
    return found as T;
}

function showPanel(id: string): void {
    for (const panelId of ["trivia-settings-panel", "trivia-empty-state", "trivia-lobby-panel", "trivia-round-panel", "trivia-summary-panel"]) {
        el(panelId).hidden = panelId !== id;
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
function pickFriendsToInvite(available: FriendOption[]): Promise<number[]> {
    return new Promise((resolve) => {
        const dialog = document.createElement("dialog");
        dialog.className = "trivia-invite-more-dialog";
        dialog.style.cssText = "max-width:22rem;width:90vw;padding:1.25rem;border-radius:0.5rem;border:1px solid rgba(0,0,0,0.15);";

        const heading = document.createElement("h3");
        heading.textContent = "Invite more players";
        heading.style.marginTop = "0";

        const list = document.createElement("div");
        list.style.cssText = "display:flex;flex-direction:column;gap:0.5rem;max-height:16rem;overflow-y:auto;margin:0.75rem 0;";
        renderFriendCheckboxes(list, available, new Set());

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
    for (const participant of participants) {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = participant.is_host ? `${participant.username} (host)` : participant.username;
        const status = document.createElement("span");
        status.className = participant.status === "joined" ? "trivia-lobby-status trivia-lobby-status--joined" : "trivia-lobby-status";
        status.textContent = participant.status === "joined" ? "Joined" : "Invited";
        item.append(name, status);
        list.appendChild(item);
    }

    const me = participants.find((participant) => participant.profile_id === myProfileId);
    const isHost = hostProfileId === myProfileId;
    el<HTMLButtonElement>("trivia-invite-more-btn").hidden = !isHost;
    el<HTMLButtonElement>("trivia-join-lobby-btn").hidden = !(me && me.status === "invited");
    el<HTMLButtonElement>("trivia-begin-btn").hidden = !isHost;
}

function renderLobby(session: SessionPayload): void {
    sessionId = session.session_id;
    hostProfileId = session.host_profile_id;
    isMultiplayer = true;
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
    const response = await postForm(urlFor(urls.join, sessionId), {});
    if (response.error) {
        toast.error(response.error);
        return;
    }
    await refreshLobby();
}

async function beginGame(): Promise<void> {
    if (sessionId === null) return;
    const payload = await postForm(urlFor(urls.begin, sessionId), {});
    await handleStartOrRoundResponse(payload);
}

// ---------------------------------------------------------------------------
// Real-time (multiplayer only)
// ---------------------------------------------------------------------------

function connectSessionSocket(): void {
    if (ws || sessionId === null) return;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(`${proto}${location.host}/ws/trivia/session/${sessionId}/`);
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
    void loadChatHistory();
}

function handleSocketMessage(data: any): void {
    switch (data.type) {
        case "participant.joined":
            void refreshLobby();
            break;
        case "session.started":
            showPanel("trivia-round-panel");
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
// Gameplay
// ---------------------------------------------------------------------------

function renderRound(round: RoundPayload): void {
    currentRound = round;
    sessionId = round.session_id;
    showPanel("trivia-round-panel");
    el<HTMLParagraphElement>("trivia-round-index").textContent = `Question ${round.sequence_index + 1}`;
    el<HTMLHeadingElement>("trivia-prompt").textContent = round.prompt;
    el<HTMLInputElement>("trivia-answer-input").value = "";
    el<HTMLFormElement>("trivia-answer-form").hidden = false;
    el("trivia-reveal").hidden = true;
    el<HTMLInputElement>("trivia-answer-input").focus();
}

function renderSummary(summary: SummaryPayload): void {
    const mine = summary.participants.find((participant) => participant.profile_id === myProfileId);
    const lines = isMultiplayer
        ? summary.participants
              .slice()
              .sort((a, b) => b.total_points - a.total_points)
              .map((participant, index) => `${index + 1}. ${participant.username} - ${participant.total_points} pts`)
              .join("\n")
        : mine
          ? `You scored ${mine.total_points} points across ${summary.rounds_played} rounds.`
          : `Finished - ${summary.rounds_played} rounds played.`;
    el<HTMLParagraphElement>("trivia-summary-score").textContent = lines;
    showPanel("trivia-summary-panel");
    if (ws) {
        ws.close();
        ws = null;
    }
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
    if (payload.finished) {
        renderSummary(payload.summary ?? (await getJson(urlFor(urls.summary, sessionId ?? undefined))));
        return;
    }
    if (payload.round) renderRound(payload.round);
}

async function startGame(): Promise<void> {
    const difficulty = el<HTMLInputElement>("trivia-difficulty").value;
    const totalRounds = el<HTMLInputElement>("trivia-total-rounds").value;
    const body: Record<string, string> = { difficulty, total_rounds: totalRounds };

    const params = new URLSearchParams(body);
    if (el<HTMLInputElement>("trivia-play-with-friends").checked) {
        const checked = Array.from(document.querySelectorAll<HTMLInputElement>("#trivia-friend-list input:checked"));
        if (!checked.length) {
            toast.error("Pick at least one friend to invite, or turn off multiplayer.");
            return;
        }
        for (const checkbox of checked) params.append("invite_profile_ids", checkbox.value);
    }

    const response = await fetch(urls.start, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
        body: params,
    });
    await handleStartOrRoundResponse(await response.json());
}

async function submitAnswer(): Promise<void> {
    if (sessionId === null || currentRound === null) return;
    const answer = el<HTMLInputElement>("trivia-answer-input").value;
    if (!answer.trim()) return;

    const payload: RevealResponse = await postForm(urlFor(urls.answer, sessionId, currentRound.round_id), { answer });
    if (payload.error) {
        toast.error(payload.error);
        return;
    }

    el<HTMLFormElement>("trivia-answer-form").hidden = true;
    const reveal = el("trivia-reveal");
    reveal.hidden = false;
    el<HTMLParagraphElement>("trivia-reveal-result").textContent = payload.revealed
        ? payload.is_correct
            ? `Correct! +${payload.points} points`
            : `Not quite - the answer was "${payload.answer ?? "unknown"}".`
        : "Answer submitted - waiting for the rest of the group...";
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
}

async function voteOnCurrentQuestion(kind: string): Promise<void> {
    if (currentRound === null) return;
    const payload = await postForm(urlFor(urls.vote, undefined, undefined, currentRound.question_id), { kind });
    if (payload.error) toast.error(payload.error);
}

async function goToNextRound(): Promise<void> {
    if (sessionId === null) return;
    const payload = await getJson(urlFor(urls.round, sessionId));
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
    el<HTMLFormElement>("trivia-start-form").addEventListener("submit", (event) => {
        event.preventDefault();
        void startGame();
    });
    el<HTMLFormElement>("trivia-answer-form").addEventListener("submit", (event) => {
        event.preventDefault();
        void submitAnswer();
    });
    el<HTMLButtonElement>("trivia-next-btn").addEventListener("click", () => void goToNextRound());
    el<HTMLButtonElement>("trivia-play-again-btn").addEventListener("click", () => showPanel("trivia-settings-panel"));
    el<HTMLButtonElement>("trivia-join-lobby-btn").addEventListener("click", () => void joinLobby());
    el<HTMLButtonElement>("trivia-begin-btn").addEventListener("click", () => void beginGame());
    el<HTMLButtonElement>("trivia-invite-more-btn").addEventListener("click", () => void handleInviteMore());

    document.querySelectorAll<HTMLButtonElement>("[data-vote]").forEach((button) => {
        button.addEventListener("click", () => void voteOnCurrentQuestion(button.dataset.vote as string));
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
