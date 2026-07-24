import {
  getCsrfToken
} from "./article-wysiwyg-y9qpab7g.js";
import"./article-wysiwyg-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/entries/spotguessr.ts
var urls = window.SPOTGUESSR_URLS;
var DEFAULT_CENTER = [39.5, -98.35];
var DEFAULT_ZOOM = 4;
var pageEl = document.querySelector(".spotguessr-page");
var myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");
var sessionId = null;
var currentRoundId = null;
var currentMode = "photos";
var totalRounds = 0;
var sessionScore = 0;
var dateGuessingEnabled = false;
var isMultiplayer = false;
var hostProfileId = null;
var lastRevealedRoundId = null;
var ws = null;
var guessMap = null;
var guessMarker = null;
var actualMarker = null;
var resultLine = null;
var areaMap = null;
var areaDrawnItems = null;
var pinOptions = [];
var friendOptions = [];
var selectedInviteIds = new Set;
var scoreboard = [];
function el(id) {
  const found = document.getElementById(id);
  if (!found)
    throw new Error(`SpotGuessr: missing #${id}`);
  return found;
}
function urlFor(template, sessionIdValue, roundIdValue) {
  let resolved = template;
  if (sessionIdValue !== undefined)
    resolved = resolved.replace(urls.session_id_sentinel, String(sessionIdValue));
  if (roundIdValue !== undefined)
    resolved = resolved.replace(urls.round_id_sentinel, String(roundIdValue));
  return resolved;
}
async function postForm(url, data) {
  const body = data instanceof URLSearchParams ? data : new URLSearchParams(data);
  const response = await fetch(url, {
    method: "POST",
    headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
    body
  });
  return response.json();
}
async function getJson(url) {
  const response = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
  return response.json();
}
function initDifficultySlider() {
  const slider = el("sg-difficulty");
  const label = el("sg-difficulty-label");
  const describe = (value) => value < 33 ? "Easy" : value < 66 ? "Medium" : "Hard";
  slider.addEventListener("input", () => {
    label.textContent = describe(Number(slider.value));
  });
}
function updateModeVisibility() {
  const mode = el("sg-mode").value;
  document.querySelectorAll("[data-mode-only]").forEach((field) => {
    field.hidden = field.dataset.modeOnly !== mode;
  });
}
function initModeSelect() {
  el("sg-mode").addEventListener("change", updateModeVisibility);
  updateModeVisibility();
}
function initRatingsToggle() {
  const checkbox = el("sg-show-ratings-to-friends");
  checkbox.addEventListener("change", () => {
    postForm(urls.settings, { show_ratings_to_friends: checkbox.checked ? "on" : "off" });
  });
}
function initAreaRestriction() {
  const toggle = el("sg-restrict-area");
  const wrap = el("sg-area-map-wrap");
  toggle.addEventListener("change", () => {
    wrap.hidden = !toggle.checked;
    if (!toggle.checked)
      return;
    if (areaMap) {
      areaMap.invalidateSize();
      return;
    }
    areaMap = L.map("sg-area-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(areaMap);
    areaDrawnItems = new L.FeatureGroup;
    areaMap.addLayer(areaDrawnItems);
    const drawControl = new L.Control.Draw({
      draw: { rectangle: {}, polygon: false, circle: false, marker: false, polyline: false, circlemarker: false },
      edit: { featureGroup: areaDrawnItems }
    });
    areaMap.addControl(drawControl);
    areaMap.on(L.Draw.Event.CREATED, (event) => {
      const { layer } = event;
      areaDrawnItems?.clearLayers();
      areaDrawnItems?.addLayer(layer);
    });
  });
}
function currentGeoBoundsGeoJson() {
  if (!areaDrawnItems)
    return null;
  const [layer] = areaDrawnItems.getLayers();
  if (!layer || !("toGeoJSON" in layer))
    return null;
  const feature = layer.toGeoJSON();
  return JSON.stringify(feature.geometry);
}
async function loadPinOptions() {
  const data = await getJson(urls.pins);
  pinOptions = data.pins ?? [];
  const datalist = el("sg-pin-options");
  datalist.innerHTML = "";
  for (const pin of pinOptions) {
    const option = document.createElement("option");
    option.value = pin.label;
    datalist.appendChild(option);
  }
}
function initPinSearch() {
  const input = el("sg-pin-search");
  input.addEventListener("change", () => {
    const match = pinOptions.find((pin) => pin.label === input.value);
    if (match)
      placeGuessMarker(L.latLng(match.latitude, match.longitude));
  });
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
    container.innerHTML = '<p class="spotguessr-panel-hint">No friends available to invite.</p>';
    return;
  }
  for (const friend of available) {
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = String(friend.profile_id);
    checkbox.checked = selectedInviteIds.has(friend.profile_id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked)
        selectedInviteIds.add(friend.profile_id);
      else
        selectedInviteIds.delete(friend.profile_id);
    });
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(friend.username));
    container.appendChild(label);
  }
}
function initFriendPicker() {
  const toggle = el("sg-play-with-friends");
  const wrap = el("sg-invite-wrap");
  toggle.addEventListener("change", async () => {
    wrap.hidden = !toggle.checked;
    if (!toggle.checked)
      return;
    const friends = await loadFriendOptions();
    renderFriendCheckboxes(el("sg-friend-list"), friends, new Set);
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
    window.alert("Everyone on your friends list is already in this game.");
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
    window.alert(response.error);
    return;
  }
  const refreshed = await getJson(urlFor(urls.lobby, sessionId));
  renderLobbyParticipants(refreshed.participants);
}
function ensureGuessMap() {
  if (guessMap)
    return guessMap;
  guessMap = L.map("sg-guess-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(guessMap);
  guessMap.on("click", (event) => placeGuessMarker(event.latlng));
  return guessMap;
}
function placeGuessMarker(latlng) {
  const map = ensureGuessMap();
  if (guessMarker) {
    guessMarker.setLatLng(latlng);
  } else {
    guessMarker = L.marker(latlng, { draggable: true }).addTo(map);
  }
  el("sg-submit-guess-btn").disabled = false;
}
function resetGuessMap() {
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
  el("sg-submit-guess-btn").disabled = true;
  map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
}
function renderLobbyParticipants(participants) {
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
  el("sg-invite-more-btn").hidden = !isHost;
  el("sg-join-lobby-btn").hidden = !(me && me.status === "invited");
  el("sg-begin-btn").hidden = !isHost;
}
function renderLobby(session) {
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
async function refreshLobby() {
  if (sessionId === null)
    return;
  const lobby = await getJson(urlFor(urls.lobby, sessionId));
  renderLobbyParticipants(lobby.participants);
}
function renderRound(round, roundNumber) {
  currentRoundId = round.round_id;
  currentMode = round.mode;
  el("sg-settings-panel").hidden = true;
  el("sg-lobby-panel").hidden = true;
  el("sg-summary-panel").hidden = true;
  el("sg-game-panel").hidden = false;
  el("sg-reveal-panel").hidden = true;
  el("sg-round-status").textContent = `Round ${roundNumber} of ${totalRounds}`;
  el("sg-score-status").textContent = isMultiplayer ? "" : `Score: ${sessionScore}`;
  const photo = el("sg-round-photo");
  const nameHeading = el("sg-round-name");
  const pinSearchWrap = el("sg-pin-search-wrap");
  if (round.mode === "named_place") {
    photo.hidden = true;
    nameHeading.hidden = false;
    nameHeading.textContent = round.display_text ?? "";
    pinSearchWrap.hidden = true;
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
  setTimeout(() => guessMap?.invalidateSize(), 0);
}
function renderResultsList(results) {
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
function updateScoreboardFromResults(results) {
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
function renderScoreboard() {
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
function showPhotoFeedbackIfApplicable() {
  el("sg-photo-feedback").hidden = currentMode !== "photos";
  el("sg-photo-feedback-thanks").hidden = true;
}
function showReveal(reveal) {
  el("sg-submit-guess-btn").disabled = true;
  sessionScore += reveal.points + reveal.date_points;
  el("sg-score-status").textContent = isMultiplayer ? "" : `Score: ${sessionScore}`;
  showPhotoFeedbackIfApplicable();
  const distanceKm = (reveal.distance_meters / 1000).toFixed(2);
  if (!reveal.revealed) {
    el("sg-reveal-panel").hidden = false;
    el("sg-reveal-title").textContent = "Guess submitted!";
    let detail2 = `${reveal.points} points – ${distanceKm} km away. Waiting for other players…`;
    if (reveal.date_points)
      detail2 = `${reveal.points} points (+${reveal.date_points} for the date guess) – ${distanceKm} km away. Waiting for other players…`;
    el("sg-reveal-detail").textContent = detail2;
    el("sg-reveal-results").hidden = true;
    el("sg-next-round-btn").hidden = true;
    return;
  }
  lastRevealedRoundId = reveal.round_id;
  const map = ensureGuessMap();
  const actualLatLng = L.latLng(reveal.actual_latitude, reveal.actual_longitude);
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
  if (reveal.date_points)
    detail += ` (+${reveal.date_points} for the date guess)`;
  el("sg-reveal-detail").textContent = detail;
  el("sg-reveal-results").hidden = true;
  el("sg-next-round-btn").hidden = isMultiplayer;
}
function showBroadcastReveal(data) {
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
    el("sg-submit-guess-btn").disabled = true;
    el("sg-next-round-btn").hidden = true;
  }
  updateScoreboardFromResults(data.results);
  renderResultsList(data.results);
}
function showSummary(summary) {
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
async function startGame(event) {
  event.preventDefault();
  currentMode = el("sg-mode").value;
  dateGuessingEnabled = currentMode === "photos" && el("sg-date-guessing").checked;
  sessionScore = 0;
  scoreboard = [];
  lastRevealedRoundId = null;
  const geoBounds = el("sg-restrict-area").checked ? currentGeoBoundsGeoJson() : null;
  const body = new URLSearchParams({
    mode: currentMode,
    difficulty: String(Number(el("sg-difficulty").value) / 100),
    total_rounds: el("sg-rounds").value,
    external_media_only: el("sg-external-media-only").checked ? "on" : "off",
    allow_arbitrary_external_photos: el("sg-allow-arbitrary-external-photos").checked ? "on" : "off",
    require_visited_all: el("sg-require-visited-all").checked ? "on" : "off",
    date_guessing_enabled: dateGuessingEnabled ? "on" : "off",
    use_aliases: el("sg-use-aliases").checked ? "on" : "off"
  });
  if (geoBounds)
    body.append("geo_bounds", geoBounds);
  for (const profileId of selectedInviteIds)
    body.append("invite_profile_ids", String(profileId));
  const response = await postForm(urls.start, body);
  if (response.error) {
    window.alert(response.error);
    return;
  }
  sessionId = response.session_id;
  totalRounds = Number(el("sg-rounds").value);
  if (response.lobby) {
    isMultiplayer = true;
    renderLobby(response.session);
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
async function submitGuess() {
  if (!guessMarker || sessionId === null || currentRoundId === null)
    return;
  const latlng = guessMarker.getLatLng();
  const payload = { latitude: String(latlng.lat), longitude: String(latlng.lng) };
  if (dateGuessingEnabled) {
    const dateValue = el("sg-guessed-date").value;
    if (dateValue)
      payload.guessed_date = dateValue;
  }
  const reveal = await postForm(urlFor(urls.guess, sessionId, currentRoundId), payload);
  if (reveal.error) {
    window.alert(reveal.error);
    return;
  }
  showReveal(reveal);
}
async function submitPhotoFeedback(kind) {
  if (sessionId === null || currentRoundId === null)
    return;
  const response = await postForm(urlFor(urls.photo_feedback, sessionId, currentRoundId), { kind });
  if (response.error) {
    window.alert(response.error);
    return;
  }
  el("sg-photo-feedback-thanks").hidden = false;
}
async function goToNextRound() {
  if (sessionId === null)
    return;
  const data = await getJson(urlFor(urls.round, sessionId));
  if (data.finished) {
    showSummary(data.summary);
    return;
  }
  renderRound(data.round, data.round.sequence_index + 1);
}
async function joinLobby() {
  if (sessionId === null)
    return;
  const response = await postForm(urlFor(urls.join, sessionId), {});
  if (response.error) {
    window.alert(response.error);
    return;
  }
  await refreshLobby();
}
async function beginGame() {
  if (sessionId === null)
    return;
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
function resetToSettings() {
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
function connectSessionSocket() {
  if (ws || sessionId === null)
    return;
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  ws = new WebSocket(`${proto}${location.host}/ws/spotguessr/session/${sessionId}/`);
  ws.addEventListener("message", (event) => {
    try {
      handleSocketMessage(JSON.parse(event.data));
    } catch {}
  });
  ws.addEventListener("close", () => {
    ws = null;
  });
  el("sg-chat-panel").hidden = false;
  loadChatHistory();
}
function handleSocketMessage(data) {
  switch (data.type) {
    case "participant.joined":
      refreshLobby();
      break;
    case "session.started":
      el("sg-lobby-panel").hidden = true;
      renderRound(data.round, 1);
      break;
    case "round.revealed":
      showBroadcastReveal(data);
      break;
    case "round.started":
      renderRound(data.round, data.round.sequence_index + 1);
      break;
    case "session.completed":
      showSummary(data);
      break;
    case "chat.message":
      appendChatMessage(data.message);
      break;
    default:
      break;
  }
}
function appendChatMessage(message) {
  const log = el("sg-chat-log");
  const line = document.createElement("div");
  const nameSpan = document.createElement("span");
  nameSpan.className = "spotguessr-chat-username";
  nameSpan.textContent = `${message.username}:`;
  line.append(nameSpan, document.createTextNode(message.body));
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}
async function loadChatHistory() {
  if (sessionId === null)
    return;
  const data = await getJson(urlFor(urls.chat_history, sessionId));
  const log = el("sg-chat-log");
  log.innerHTML = "";
  for (const message of data.messages ?? [])
    appendChatMessage(message);
}
function initChat() {
  el("sg-chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = el("sg-chat-input");
    const body = input.value.trim();
    if (!body || !ws)
      return;
    ws.send(JSON.stringify({ body }));
    input.value = "";
  });
}
async function loadInitialSession() {
  const raw = pageEl?.dataset.initialSessionId;
  if (!raw)
    return;
  sessionId = Number(raw);
  const lobby = await getJson(urlFor(urls.lobby, sessionId));
  totalRounds = lobby.total_rounds;
  currentMode = lobby.mode;
  isMultiplayer = true;
  if (lobby.status === "lobby") {
    renderLobby(lobby);
    return;
  }
  if (lobby.status === "completed" || lobby.status === "abandoned") {
    const summary = await getJson(urlFor(urls.summary, sessionId));
    showSummary(summary);
    return;
  }
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
loadInitialSession();
