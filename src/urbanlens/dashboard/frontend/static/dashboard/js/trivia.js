import {
  createGameShell,
  playEntrance
} from "./achievements-vedkz711.js";
import {
  confirmAction,
  getCsrfToken,
  toast
} from "./achievements-5jnnp4sj.js";
import"./achievements-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/entries/trivia.ts
var PANEL_IDS = {
  settings: "trivia-settings-panel",
  empty: "trivia-empty-state",
  lobby: "trivia-lobby-panel",
  round: "trivia-round-panel",
  summary: "trivia-summary-panel"
};
var PANEL_NAME_BY_ID = {
  "trivia-settings-panel": "settings",
  "trivia-empty-state": "empty",
  "trivia-lobby-panel": "lobby",
  "trivia-round-panel": "round",
  "trivia-summary-panel": "summary"
};
var urls = window.TRIVIA_URLS;
var pageEl = document.querySelector(".trivia-page");
var myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");
var shell = null;
var shellEl = null;
var sessionId = null;
var currentRound = null;
var isMultiplayer = false;
var hostProfileId = null;
var ws = null;
var friendOptions = [];
var totalRounds = 0;
var sessionPoints = 0;
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
  const name = PANEL_NAME_BY_ID[id];
  if (!name) {
    console.error(`[trivia] no shell panel registered for #${id}`);
    return;
  }
  shell?.showPanel(name);
}
async function withBusy(button, work) {
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
function pickFriendsToInvite(available) {
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
    renderFriendCheckboxes(list, available, new Set);
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
    const cleanup = (result) => {
      dialog.close();
      dialog.remove();
      resolve(result);
    };
    cancelBtn.addEventListener("click", () => cleanup([]));
    inviteBtn.addEventListener("click", () => {
      const checked = Array.from(list.querySelectorAll("input:checked")).map((input) => Number(input.value));
      cleanup(checked);
    });
    dialog.addEventListener("cancel", () => cleanup([]));
    dialog.showModal();
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
  const chosenIds = await pickFriendsToInvite(available);
  if (!chosenIds.length)
    return;
  for (const profileId of chosenIds) {
    const response = await postForm(urlFor(urls.invite, sessionId), { profile_id: String(profileId) });
    if (response.error)
      toast.error(response.error);
  }
  await refreshLobby();
}
function renderLobbyParticipants(participants) {
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
  el("trivia-invite-more-btn").hidden = !isHost;
  el("trivia-join-lobby-btn").hidden = !(me && me.status === "invited");
  el("trivia-begin-btn").hidden = !isHost;
  el("trivia-leave-lobby-btn").hidden = !me;
  el("trivia-end-game-lobby-btn").hidden = !isHost;
}
function renderLobby(session) {
  sessionId = session.session_id;
  hostProfileId = session.host_profile_id;
  isMultiplayer = true;
  if (session.total_rounds)
    totalRounds = session.total_rounds;
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
  const id = sessionId;
  const response = await withBusy(el("trivia-join-lobby-btn"), () => postForm(urlFor(urls.join, id), {}));
  if (response.error) {
    toast.error(response.error);
    return;
  }
  await refreshLobby();
}
async function beginGame() {
  if (sessionId === null)
    return;
  const id = sessionId;
  const payload = await withBusy(el("trivia-begin-btn"), () => postForm(urlFor(urls.begin, id), {}));
  await handleStartOrRoundResponse(payload);
}
function setRailAvailable(on) {
  shell?.setRailAvailable(on);
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
  setRailAvailable(true);
  loadChatHistory();
}
function handleSocketMessage(data) {
  switch (data.type) {
    case "participant.joined":
      refreshLobby();
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
        refreshLobby();
      }
      break;
    case "session.started":
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
function avatarInitial(username) {
  const first = username.trim().charAt(0);
  return first ? first.toUpperCase() : "?";
}
function buildScoreRow(data, index) {
  const row = document.createElement("li");
  row.className = data.isSelf ? "trivia-score-card trivia-score-card--self" : "trivia-score-card";
  if (data.rank !== null && data.rank <= 3)
    row.classList.add(`trivia-score-card--rank-${data.rank}`);
  row.style.setProperty("--ul-game-i", String(index));
  const rank = document.createElement("span");
  rank.className = "trivia-score-card-rank";
  rank.textContent = data.rank !== null ? `#${data.rank}` : "";
  const avatarWrap = document.createElement("span");
  avatarWrap.className = "trivia-score-card-avatar-wrap";
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
function renderRoundScores(results) {
  const list = el("trivia-round-scores");
  list.innerHTML = "";
  if (results.length < 2) {
    list.hidden = true;
    return;
  }
  const ordered = results.slice().sort((a, b) => b.points - a.points);
  ordered.forEach((result, index) => {
    list.appendChild(buildScoreRow({
      rank: index + 1,
      username: result.username,
      avatarUrl: result.avatar_url,
      points: result.points,
      correct: result.is_correct,
      isSelf: result.profile_id === myProfileId
    }, index));
  });
  list.hidden = false;
}
function updateRoundActionVisibility() {
  el("trivia-leave-round-btn").hidden = !isMultiplayer;
  el("trivia-end-game-round-btn").hidden = !(isMultiplayer && hostProfileId === myProfileId);
}
function setSessionPoints(points) {
  sessionPoints = points;
  const chip = el("trivia-score");
  chip.hidden = false;
  chip.textContent = `${sessionPoints} pts`;
  chip.classList.remove("is-counting");
  chip.offsetWidth;
  chip.classList.add("is-counting");
}
function resetVoteButtons() {
  for (const button of document.querySelectorAll("[data-vote]")) {
    button.classList.remove("is-cast");
    button.disabled = false;
  }
}
function applyRevealVerdict(correct) {
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
function renderRound(round) {
  currentRound = round;
  sessionId = round.session_id;
  showPanel("trivia-round-panel");
  const index = round.sequence_index + 1;
  el("trivia-round-index").textContent = totalRounds > 0 ? `Question ${index} of ${totalRounds}` : `Question ${index}`;
  shell?.setProgress(index, totalRounds);
  const prompt = el("trivia-prompt");
  prompt.textContent = round.prompt;
  playEntrance(prompt, shell?.reducedMotion() ?? false);
  el("trivia-answer-input").value = "";
  el("trivia-answer-form").hidden = false;
  el("trivia-reveal").hidden = true;
  applyRevealVerdict(null);
  resetVoteButtons();
  el("trivia-round-scores").hidden = true;
  el("trivia-answer-input").focus();
  updateRoundActionVisibility();
}
function renderSummary(summary) {
  const mine = summary.participants.find((participant) => participant.profile_id === myProfileId);
  const prefix = summary.status === "abandoned" ? `Game ended early - not enough players remained.

` : "";
  const list = el("trivia-summary-scores");
  list.innerHTML = "";
  if (isMultiplayer && summary.participants.length) {
    const ranked = summary.participants.slice().sort((a, b) => b.total_points - a.total_points);
    ranked.forEach((participant, index) => {
      list.appendChild(buildScoreRow({
        rank: index + 1,
        username: participant.username,
        avatarUrl: participant.avatar_url,
        points: participant.total_points,
        correct: null,
        isSelf: participant.profile_id === myProfileId
      }, index));
    });
    list.hidden = false;
    el("trivia-summary-score").textContent = `${prefix}${summary.rounds_played} of ${summary.total_rounds} rounds played.`;
  } else {
    list.hidden = true;
    el("trivia-summary-score").textContent = mine ? `${prefix}You scored ${mine.total_points} points across ${summary.rounds_played} rounds.` : `${prefix}Finished - ${summary.rounds_played} rounds played.`;
  }
  if (mine)
    setSessionPoints(mine.total_points);
  showPanel("trivia-summary-panel");
  if (ws) {
    ws.close();
    ws = null;
  }
}
function resetToSettings() {
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
async function leaveGame() {
  if (sessionId === null)
    return;
  const confirmed = await confirmAction({
    title: "Leave this game?",
    message: "You'll stop playing, but the rest of the group can continue without you.",
    confirmLabel: "Leave"
  });
  if (!confirmed)
    return;
  const response = await postForm(urlFor(urls.leave, sessionId), {});
  if (response.error) {
    toast.error(response.error);
    return;
  }
  resetToSettings();
}
async function endGameNow() {
  if (sessionId === null)
    return;
  const confirmed = await confirmAction({
    title: "End this game?",
    message: "This ends the game immediately for everyone, using the scores so far.",
    confirmLabel: "End game"
  });
  if (!confirmed)
    return;
  const response = await postForm(urlFor(urls.end, sessionId), {});
  if (response.error) {
    toast.error(response.error);
    return;
  }
  renderSummary(response.summary);
}
async function kickParticipant(profileId, username) {
  if (sessionId === null)
    return;
  const confirmed = await confirmAction({
    title: `Remove ${username}?`,
    message: `${username} will be removed from this game.`,
    confirmLabel: "Remove"
  });
  if (!confirmed)
    return;
  const response = await postForm(urlFor(urls.kick, sessionId), { profile_id: String(profileId) });
  if (response.error) {
    toast.error(response.error);
    return;
  }
  await refreshLobby();
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
  if (payload.total_rounds)
    totalRounds = payload.total_rounds;
  if (payload.finished) {
    renderSummary(payload.summary ?? await getJson(urlFor(urls.summary, sessionId ?? undefined)));
    return;
  }
  if (payload.round)
    renderRound(payload.round);
}
async function startGame() {
  const difficulty = el("trivia-difficulty").value;
  const requestedRounds = el("trivia-total-rounds").value;
  const body = { difficulty, total_rounds: requestedRounds };
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
  totalRounds = Number(requestedRounds) || 0;
  sessionPoints = 0;
  const payload = await withBusy(el("trivia-start-btn"), async () => {
    const response = await fetch(urls.start, {
      method: "POST",
      headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
      body: params
    });
    return response.json();
  });
  await handleStartOrRoundResponse(payload);
}
async function submitAnswer() {
  if (sessionId === null || currentRound === null)
    return;
  const form = el("trivia-answer-form");
  const answer = el("trivia-answer-input").value;
  if (!answer.trim())
    return;
  const submitBtn = form.querySelector('button[type="submit"]');
  const id = sessionId;
  const roundId = currentRound.round_id;
  const payload = await withBusy(submitBtn, () => postForm(urlFor(urls.answer, id, roundId), { answer }));
  if (payload.error) {
    toast.error(payload.error);
    return;
  }
  form.hidden = true;
  const reveal = el("trivia-reveal");
  reveal.hidden = false;
  el("trivia-reveal-result").textContent = payload.revealed ? payload.is_correct ? `Correct! +${payload.points} points` : `Not quite - the answer was "${payload.answer ?? "unknown"}".` : "Answer submitted - waiting for the rest of the group...";
  applyRevealVerdict(payload.revealed ? payload.is_correct : null);
  if (payload.revealed && payload.points)
    setSessionPoints(sessionPoints + payload.points);
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
  applyRevealVerdict(mine ? mine.is_correct : null);
  if (mine?.is_correct && mine.points)
    setSessionPoints(sessionPoints + mine.points);
  renderRoundScores(data.results);
}
async function voteOnCurrentQuestion(button, kind) {
  if (currentRound === null)
    return;
  const questionId = currentRound.question_id;
  const payload = await withBusy(button, () => postForm(urlFor(urls.vote, undefined, undefined, questionId), { kind }));
  if (payload.error) {
    toast.error(payload.error);
    return;
  }
  if (kind === "upvote" || kind === "downvote") {
    for (const other of document.querySelectorAll('[data-vote="upvote"], [data-vote="downvote"]')) {
      other.classList.remove("is-cast");
    }
  }
  button.classList.add("is-cast");
}
async function goToNextRound() {
  if (sessionId === null)
    return;
  const id = sessionId;
  const payload = await withBusy(el("trivia-next-btn"), () => getJson(urlFor(urls.round, id)));
  await handleStartOrRoundResponse(payload);
}
async function loadInitialSession() {
  const raw = pageEl?.dataset.initialSessionId;
  if (!raw)
    return;
  sessionId = Number(raw);
  isMultiplayer = true;
  const lobby = await getJson(urlFor(urls.lobby, sessionId));
  hostProfileId = lobby.host_profile_id;
  if (lobby.total_rounds)
    totalRounds = lobby.total_rounds;
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
  shellEl = document.getElementById("trivia-shell");
  if (pageEl && shellEl) {
    shell = createGameShell({
      root: pageEl,
      shell: shellEl,
      panels: PANEL_IDS,
      playingPanels: ["round", "summary"]
    });
  }
  setRailAvailable(false);
  showPanel("trivia-settings-panel");
  el("trivia-start-form").addEventListener("submit", (event) => {
    event.preventDefault();
    startGame();
  });
  el("trivia-answer-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitAnswer();
  });
  el("trivia-next-btn").addEventListener("click", () => void goToNextRound());
  el("trivia-play-again-btn").addEventListener("click", () => resetToSettings());
  el("trivia-empty-state-settings-btn").addEventListener("click", () => showPanel("trivia-settings-panel"));
  el("trivia-join-lobby-btn").addEventListener("click", () => void joinLobby());
  el("trivia-begin-btn").addEventListener("click", () => void beginGame());
  el("trivia-invite-more-btn").addEventListener("click", () => void handleInviteMore());
  el("trivia-leave-lobby-btn").addEventListener("click", () => void leaveGame());
  el("trivia-end-game-lobby-btn").addEventListener("click", () => void endGameNow());
  el("trivia-leave-round-btn").addEventListener("click", () => void leaveGame());
  el("trivia-end-game-round-btn").addEventListener("click", () => void endGameNow());
  document.querySelectorAll("[data-vote]").forEach((button) => {
    button.addEventListener("click", () => void voteOnCurrentQuestion(button, button.dataset.vote));
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
