import {
  getCsrfToken,
  toast
} from "./article-wysiwyg-5jnnp4sj.js";
import"./article-wysiwyg-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/entries/trivia.ts
var urls = window.TRIVIA_URLS;
var pageEl = document.querySelector(".trivia-page");
var myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");
var sessionId = null;
var currentRound = null;
var isMultiplayer = false;
var hostProfileId = null;
var ws = null;
var friendOptions = [];
function urlFor(template, sessionIdValue, roundIdValue, questionIdValue) {
  let resolved = template;
  if (sessionIdValue !== undefined)
    resolved = resolved.replace(urls.session_id_sentinel, String(sessionIdValue));
  if (roundIdValue !== undefined)
    resolved = resolved.replace(urls.round_id_sentinel, String(roundIdValue));
  if (questionIdValue !== undefined)
    resolved = resolved.replace(urls.question_id_sentinel, String(questionIdValue));
  return resolved;
}
async function postForm(url, data) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(data)
  });
  return response.json();
}
async function getJson(url) {
  const response = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
  return response.json();
}
function el(id) {
  const found = document.getElementById(id);
  if (!found)
    throw new Error(`Missing #${id}`);
  return found;
}
function showPanel(id) {
  for (const panelId of ["trivia-settings-panel", "trivia-empty-state", "trivia-lobby-panel", "trivia-round-panel", "trivia-summary-panel"]) {
    el(panelId).hidden = panelId !== id;
  }
}
async function loadFriendOptions() {
  if (friendOptions.length)
    return friendOptions;
  const data = await getJson(urls.friends);
  friendOptions = data.friends ?? [];
  return friendOptions;
}
function renderFriendCheckboxes(container, friends, excludeIds) {
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
async function initFriendPicker() {
  const toggle = el("trivia-play-with-friends");
  const wrap = el("trivia-invite-wrap");
  const friends = await loadFriendOptions();
  toggle.addEventListener("change", () => {
    wrap.hidden = !toggle.checked;
    if (toggle.checked)
      renderFriendCheckboxes(el("trivia-friend-list"), friends, new Set);
  });
}
async function handleInviteMore() {
  if (sessionId === null)
    return;
  const friends = await loadFriendOptions();
  const lobby = await getJson(urlFor(urls.lobby, sessionId));
  const alreadyInvited = new Set(lobby.participants.map((participant) => participant.profile_id));
  const available = friends.filter((friend) => !alreadyInvited.has(friend.profile_id));
  if (!available.length) {
    toast.error("Everyone on your friends list is already in this game.");
    return;
  }
  const chosenName = window.prompt(`Invite who? (${available.map((friend) => friend.username).join(", ")})`);
  if (!chosenName)
    return;
  const chosen = available.find((friend) => friend.username === chosenName);
  if (!chosen)
    return;
  const response = await postForm(urlFor(urls.invite, sessionId), { profile_id: String(chosen.profile_id) });
  if (response.error) {
    toast.error(response.error);
    return;
  }
  await refreshLobby();
}
function renderLobbyParticipants(participants) {
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
  el("trivia-invite-more-btn").hidden = !isHost;
  el("trivia-join-lobby-btn").hidden = !(me && me.status === "invited");
  el("trivia-begin-btn").hidden = !isHost;
}
function renderLobby(session) {
  sessionId = session.session_id;
  hostProfileId = session.host_profile_id;
  isMultiplayer = true;
  showPanel("trivia-lobby-panel");
  renderLobbyParticipants(session.participants);
  connectSessionSocket();
}
async function refreshLobby() {
  if (sessionId === null)
    return;
  const lobby = await getJson(urlFor(urls.lobby, sessionId));
  renderLobbyParticipants(lobby.participants);
}
async function joinLobby() {
  if (sessionId === null)
    return;
  const response = await postForm(urlFor(urls.join, sessionId), {});
  if (response.error) {
    toast.error(response.error);
    return;
  }
  await refreshLobby();
}
async function beginGame() {
  if (sessionId === null)
    return;
  const payload = await postForm(urlFor(urls.begin, sessionId), {});
  await handleStartOrRoundResponse(payload);
}
function connectSessionSocket() {
  if (ws || sessionId === null)
    return;
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  ws = new WebSocket(`${proto}${location.host}/ws/trivia/session/${sessionId}/`);
  ws.addEventListener("message", (event) => {
    try {
      handleSocketMessage(JSON.parse(event.data));
    } catch {}
  });
  ws.addEventListener("close", () => {
    ws = null;
  });
  el("trivia-chat-panel").hidden = false;
  loadChatHistory();
}
function handleSocketMessage(data) {
  switch (data.type) {
    case "participant.joined":
      refreshLobby();
      break;
    case "session.started":
      showPanel("trivia-round-panel");
      renderRound(data.round);
      break;
    case "round.revealed":
      showBroadcastReveal(data);
      break;
    case "round.started":
      renderRound(data.round);
      break;
    case "session.completed":
      renderSummary(data);
      break;
    case "chat.message":
      appendChatMessage(data.message);
      break;
    default:
      break;
  }
}
function appendChatMessage(message) {
  const log = el("trivia-chat-log");
  const line = document.createElement("div");
  const nameSpan = document.createElement("span");
  nameSpan.className = "trivia-chat-username";
  nameSpan.textContent = `${message.username}:`;
  line.append(nameSpan, document.createTextNode(message.body));
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}
async function loadChatHistory() {
  if (sessionId === null)
    return;
  const data = await getJson(urlFor(urls.chat_history, sessionId));
  const log = el("trivia-chat-log");
  log.innerHTML = "";
  for (const message of data.messages ?? [])
    appendChatMessage(message);
}
function initChat() {
  el("trivia-chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = el("trivia-chat-input");
    const body = input.value.trim();
    if (!body || !ws)
      return;
    ws.send(JSON.stringify({ body }));
    input.value = "";
  });
}
function renderRound(round) {
  currentRound = round;
  sessionId = round.session_id;
  showPanel("trivia-round-panel");
  el("trivia-round-index").textContent = `Question ${round.sequence_index + 1}`;
  el("trivia-prompt").textContent = round.prompt;
  el("trivia-answer-input").value = "";
  el("trivia-answer-form").hidden = false;
  el("trivia-reveal").hidden = true;
  el("trivia-answer-input").focus();
}
function renderSummary(summary) {
  const mine = summary.participants.find((participant) => participant.profile_id === myProfileId);
  const lines = isMultiplayer ? summary.participants.slice().sort((a, b) => b.total_points - a.total_points).map((participant, index) => `${index + 1}. ${participant.username} - ${participant.total_points} pts`).join(`
`) : mine ? `You scored ${mine.total_points} points across ${summary.rounds_played} rounds.` : `Finished - ${summary.rounds_played} rounds played.`;
  el("trivia-summary-score").textContent = lines;
  showPanel("trivia-summary-panel");
  if (ws) {
    ws.close();
    ws = null;
  }
}
async function handleStartOrRoundResponse(payload) {
  if (payload.error) {
    toast.error(payload.error);
    return;
  }
  if (payload.lobby) {
    renderLobby(payload.session);
    return;
  }
  if (payload.error_code === "no_eligible_questions" || payload.no_eligible_questions) {
    showPanel("trivia-empty-state");
    return;
  }
  if (payload.session_id)
    sessionId = payload.session_id;
  if (payload.finished) {
    renderSummary(payload.summary ?? await getJson(urlFor(urls.summary, sessionId ?? undefined)));
    return;
  }
  if (payload.round)
    renderRound(payload.round);
}
async function startGame() {
  const difficulty = el("trivia-difficulty").value;
  const totalRounds = el("trivia-total-rounds").value;
  const body = { difficulty, total_rounds: totalRounds };
  const params = new URLSearchParams(body);
  if (el("trivia-play-with-friends").checked) {
    const checked = Array.from(document.querySelectorAll("#trivia-friend-list input:checked"));
    if (!checked.length) {
      toast.error("Pick at least one friend to invite, or turn off multiplayer.");
      return;
    }
    for (const checkbox of checked)
      params.append("invite_profile_ids", checkbox.value);
  }
  const response = await fetch(urls.start, {
    method: "POST",
    headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
    body: params
  });
  await handleStartOrRoundResponse(await response.json());
}
async function submitAnswer() {
  if (sessionId === null || currentRound === null)
    return;
  const answer = el("trivia-answer-input").value;
  if (!answer.trim())
    return;
  const payload = await postForm(urlFor(urls.answer, sessionId, currentRound.round_id), { answer });
  if (payload.error) {
    toast.error(payload.error);
    return;
  }
  el("trivia-answer-form").hidden = true;
  const reveal = el("trivia-reveal");
  reveal.hidden = false;
  el("trivia-reveal-result").textContent = payload.revealed ? payload.is_correct ? `Correct! +${payload.points} points` : `Not quite - the answer was "${payload.answer ?? "unknown"}".` : "Answer submitted - waiting for the rest of the group...";
  el("trivia-next-btn").hidden = !payload.revealed || isMultiplayer;
}
function showBroadcastReveal(data) {
  if (!currentRound || currentRound.round_id !== data.round_id)
    return;
  el("trivia-answer-form").hidden = true;
  const reveal = el("trivia-reveal");
  reveal.hidden = false;
  const mine = data.results.find((result) => result.profile_id === myProfileId);
  el("trivia-reveal-result").textContent = mine ? mine.is_correct ? `Correct! +${mine.points} points. The answer was "${data.answer}".` : `Not quite - the answer was "${data.answer}".` : `The answer was "${data.answer}".`;
}
async function voteOnCurrentQuestion(kind) {
  if (currentRound === null)
    return;
  const payload = await postForm(urlFor(urls.vote, undefined, undefined, currentRound.question_id), { kind });
  if (payload.error)
    toast.error(payload.error);
}
async function goToNextRound() {
  if (sessionId === null)
    return;
  const payload = await getJson(urlFor(urls.round, sessionId));
  await handleStartOrRoundResponse(payload);
}
async function loadInitialSession() {
  const raw = pageEl?.dataset.initialSessionId;
  if (!raw)
    return;
  sessionId = Number(raw);
  isMultiplayer = true;
  const lobby = await getJson(urlFor(urls.lobby, sessionId));
  if (lobby.status === "lobby") {
    renderLobby(lobby);
    return;
  }
  if (lobby.status === "completed" || lobby.status === "abandoned") {
    const summary = await getJson(urlFor(urls.summary, sessionId));
    renderSummary(summary);
    return;
  }
  connectSessionSocket();
  const data = await getJson(urlFor(urls.round, sessionId));
  await handleStartOrRoundResponse(data);
}
function init() {
  el("trivia-start-form").addEventListener("submit", (event) => {
    event.preventDefault();
    startGame();
  });
  el("trivia-answer-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitAnswer();
  });
  el("trivia-next-btn").addEventListener("click", () => void goToNextRound());
  el("trivia-play-again-btn").addEventListener("click", () => showPanel("trivia-settings-panel"));
  el("trivia-join-lobby-btn").addEventListener("click", () => void joinLobby());
  el("trivia-begin-btn").addEventListener("click", () => void beginGame());
  el("trivia-invite-more-btn").addEventListener("click", () => void handleInviteMore());
  document.querySelectorAll("[data-vote]").forEach((button) => {
    button.addEventListener("click", () => void voteOnCurrentQuestion(button.dataset.vote));
  });
  const ratingsToggle = document.getElementById("trivia-show-ratings-to-friends");
  ratingsToggle?.addEventListener("change", () => {
    postForm(urls.settings, { show_ratings_to_friends: ratingsToggle.checked ? "on" : "off" });
  });
  if (window.TRIVIA_LAST_CONFIG?.difficulty !== undefined) {
    el("trivia-difficulty").value = String(window.TRIVIA_LAST_CONFIG.difficulty);
  }
  initFriendPicker();
  initChat();
  loadInitialSession();
}
document.addEventListener("DOMContentLoaded", init);
