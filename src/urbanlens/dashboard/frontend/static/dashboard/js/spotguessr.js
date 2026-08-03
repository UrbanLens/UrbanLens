import {
  createMapLayers
} from "./achievements-rarq1vf2.js";
import {
  confirmAction,
  getCsrfToken,
  toast
} from "./achievements-5jnnp4sj.js";
import"./achievements-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/shared/spotguessr-format.ts
var PANEL_NAMES = ["settings", "lobby", "game", "summary", "empty"];
function panelVisibility(active) {
  const visibility = {};
  for (const name of PANEL_NAMES)
    visibility[name] = name === active;
  return visibility;
}
function bonusSuffix(bonusPoints, bonusTiers) {
  return bonusPoints ? ` (+${bonusPoints} bonus: ${bonusTiers.join(", ")})` : "";
}
function avatarInitial(username) {
  return username.charAt(0).toUpperCase() || "?";
}
function formatRatingDelta(delta) {
  if (delta === null || delta === undefined)
    return null;
  const rounded = Math.round(delta * 10) / 10;
  if (rounded === 0)
    return { text: "±0 rating", direction: "flat" };
  const direction = rounded > 0 ? "up" : "down";
  const arrow = direction === "up" ? "▲" : "▼";
  const sign = direction === "up" ? "+" : "";
  return { text: `${arrow} ${sign}${rounded} rating`, direction };
}
function formatCountdown(remainingSeconds) {
  const clamped = Math.max(0, remainingSeconds);
  const minutes = Math.floor(clamped / 60);
  const seconds = clamped % 60;
  return minutes > 0 ? `${minutes}:${String(seconds).padStart(2, "0")}` : `${seconds}s`;
}
function easeOutQuad(progress) {
  const clamped = Math.min(1, Math.max(0, progress));
  return 1 - (1 - clamped) * (1 - clamped);
}
function countUpValue(from, to, progress) {
  return from + (to - from) * easeOutQuad(progress);
}
function interpolateLatLng(from, to, progress) {
  const eased = easeOutQuad(progress);
  return [from[0] + (to[0] - from[0]) * eased, from[1] + (to[1] - from[1]) * eased];
}
function summaryBestRoundSubtitle(participant) {
  if (participant.best_round_points === null || participant.best_round_points === undefined)
    return;
  const distance = participant.best_round_distance_meters;
  const distanceSuffix = distance !== null && distance !== undefined ? ` (${(distance / 1000).toFixed(2)} km)` : "";
  return `Best round: ${participant.best_round_points} pts${distanceSuffix}`;
}
function summaryHeadline(participants, multiplayer, viewerProfileId) {
  if (!multiplayer)
    return { heading: "Nice work!", icon: "explore" };
  const sorted = [...participants].sort((a, b) => b.total_points - a.total_points);
  const [leader, runnerUp] = sorted;
  if (!leader || runnerUp && runnerUp.total_points === leader.total_points) {
    return { heading: "It's a tie!", icon: "handshake" };
  }
  return {
    heading: leader.profile_id === viewerProfileId ? "You win! \uD83C\uDF89" : `${leader.username} wins!`,
    icon: "emoji_events"
  };
}

// src/urbanlens/dashboard/frontend/ts/entries/spotguessr.ts
var urls = window.SPOTGUESSR_URLS;
var DEFAULT_CENTER = [20, 0];
var DEFAULT_ZOOM = 2;
var pageEl = document.querySelector(".spotguessr-page");
var myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");
var regionSearchUrl = pageEl?.dataset.regionSearchUrl ?? "";
var state = {
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
  guessMarker: null,
  actualMarker: null,
  resultLine: null,
  areaMap: null,
  areaDrawnItems: null,
  restoredGeoBounds: null,
  pinOptions: [],
  friendOptions: [],
  selectedInviteIds: new Set,
  scoreboard: [],
  roundExpiresAtMs: null,
  roundTimerHandle: null
};
var PANEL_IDS = {
  settings: "sg-settings-panel",
  lobby: "sg-lobby-panel",
  game: "sg-game-panel",
  summary: "sg-summary-panel",
  empty: "sg-empty-state-panel"
};
function showPanel(name) {
  const visibility = panelVisibility(name);
  for (const [key, id] of Object.entries(PANEL_IDS)) {
    el(id).hidden = !visibility[key];
  }
}
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
var DIFFICULTY_VALUES = { easy: 0.2, medium: 0.5, hard: 0.8 };
function currentDifficulty() {
  const checked = document.querySelector('input[name="sg-difficulty-choice"]:checked');
  return DIFFICULTY_VALUES[checked?.value ?? "medium"];
}
function initRoundsSlider() {
  const slider = el("sg-rounds");
  const label = el("sg-rounds-label");
  slider.addEventListener("input", () => {
    label.textContent = slider.value;
  });
}
function currentSettingsMode() {
  return el("sg-settings-mode").value || "photos";
}
function updateModeVisibility() {
  const mode = currentSettingsMode();
  document.querySelectorAll("[data-mode-only]").forEach((field) => {
    field.hidden = field.dataset.modeOnly !== mode;
  });
}
function openSettingsDialog(mode) {
  el("sg-settings-mode").value = mode;
  updateModeVisibility();
  el("sg-settings-dialog").showModal();
  if (state.areaMap) {
    state.areaMap.invalidateSize();
  } else {
    ensureAreaMap();
    if (state.restoredGeoBounds) {
      setAreaGeometry(state.restoredGeoBounds);
      state.restoredGeoBounds = null;
    }
  }
}
function initModeCards() {
  document.querySelectorAll(".spotguessr-mode-card-body").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.mode;
      if (!mode)
        return;
      el("sg-settings-mode").value = mode;
      startGame(mode);
    });
  });
  document.querySelectorAll(".spotguessr-card-gear").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.mode;
      if (mode)
        openSettingsDialog(mode);
    });
  });
}
function initRatingsToggle() {
  const checkbox = el("sg-show-ratings-to-friends");
  checkbox.addEventListener("change", () => {
    postForm(urls.settings, { show_ratings_to_friends: checkbox.checked ? "on" : "off" });
  });
}
function ensureAreaMap() {
  if (state.areaMap)
    return state.areaMap;
  state.areaMap = L.map("sg-area-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(state.areaMap);
  state.areaDrawnItems = new L.FeatureGroup;
  state.areaMap.addLayer(state.areaDrawnItems);
  const drawControl = new L.Control.Draw({
    draw: { polygon: {}, rectangle: false, circle: false, marker: false, polyline: false, circlemarker: false },
    edit: { featureGroup: state.areaDrawnItems }
  });
  state.areaMap.addControl(drawControl);
  state.areaMap.on(L.Draw.Event.CREATED, (event) => {
    const { layer } = event;
    setAreaGeometry(layer.toGeoJSON().geometry);
  });
  return state.areaMap;
}
function setAreaGeometry(geometry) {
  const map = ensureAreaMap();
  state.areaDrawnItems?.clearLayers();
  const layer = L.geoJSON(geometry);
  layer.eachLayer((shapeLayer) => state.areaDrawnItems?.addLayer(shapeLayer));
  el("sg-area-geo-bounds").value = JSON.stringify(geometry);
  el("sg-restrict-area").checked = true;
  el("sg-area-clear-btn").hidden = false;
  const bounds = state.areaDrawnItems?.getBounds();
  if (bounds?.isValid())
    map.fitBounds(bounds, { padding: [40, 40] });
  updateAreaPinCount();
}
function clearAreaGeometry() {
  state.areaDrawnItems?.clearLayers();
  el("sg-area-geo-bounds").value = "";
  el("sg-restrict-area").checked = false;
  el("sg-area-clear-btn").hidden = true;
  el("sg-area-pin-count").hidden = true;
  state.areaMap?.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
}
async function updateAreaPinCount() {
  const geoBounds = currentGeoBoundsGeoJson();
  const countEl = el("sg-area-pin-count");
  if (!geoBounds) {
    countEl.hidden = true;
    return;
  }
  let data;
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
function initAreaRestriction() {
  el("sg-area-clear-btn").addEventListener("click", clearAreaGeometry);
}
async function searchAreaRegion() {
  const input = el("sg-area-search-input");
  const query = input.value.trim();
  const searchBtn = el("sg-area-search-btn");
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
  let data;
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
function initAreaSearch() {
  el("sg-area-search-btn").addEventListener("click", () => void searchAreaRegion());
  el("sg-area-search-input").addEventListener("keydown", (event) => {
    if (event.key !== "Enter")
      return;
    event.preventDefault();
    searchAreaRegion();
  });
}
function currentGeoBoundsGeoJson() {
  return el("sg-area-geo-bounds").value || null;
}
async function loadPinOptions() {
  const data = await getJson(urls.pins);
  state.pinOptions = data.pins ?? [];
  const datalist = el("sg-pin-options");
  datalist.innerHTML = "";
  for (const pin of state.pinOptions) {
    const option = document.createElement("option");
    option.value = pin.label;
    datalist.appendChild(option);
  }
}
function initPinSearch() {
  const input = el("sg-pin-search");
  input.addEventListener("change", () => {
    const match = state.pinOptions.find((pin) => pin.label === input.value);
    if (match)
      placeGuessMarker(L.latLng(match.latitude, match.longitude));
  });
}
async function loadFriendOptions() {
  if (state.friendOptions.length)
    return state.friendOptions;
  const data = await getJson(urls.friends);
  state.friendOptions = data.friends ?? [];
  return state.friendOptions;
}
async function fetchFriendsEagerly() {
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
  renderFriendCheckboxes(el("sg-friend-list"), state.friendOptions, new Set);
}
function initFriendListRetry() {
  el("sg-friend-list-retry").addEventListener("click", () => void fetchFriendsEagerly());
}
function renderFriendCheckboxes(container, friends, excludeIds, targetSet = state.selectedInviteIds) {
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
    checkbox.checked = targetSet.has(friend.profile_id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked)
        targetSet.add(friend.profile_id);
      else
        targetSet.delete(friend.profile_id);
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
function pickFriendsToInvite(available) {
  return new Promise((resolve) => {
    const chosen = new Set;
    const dialog = document.createElement("dialog");
    dialog.className = "spotguessr-invite-more-dialog";
    dialog.style.cssText = "max-width:22rem;width:90vw;padding:1.25rem;border-radius:0.5rem;border:1px solid rgba(0,0,0,0.15);";
    const heading = document.createElement("h3");
    heading.textContent = "Invite more players";
    heading.style.marginTop = "0";
    const list = document.createElement("div");
    list.style.cssText = "display:flex;flex-direction:column;gap:0.5rem;max-height:16rem;overflow-y:auto;margin:0.75rem 0;";
    renderFriendCheckboxes(list, available, new Set, chosen);
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
    const cleanup = (result) => {
      dialog.close();
      dialog.remove();
      resolve(result);
    };
    cancelBtn.addEventListener("click", () => cleanup(new Set));
    inviteBtn.addEventListener("click", () => cleanup(chosen));
    dialog.addEventListener("cancel", () => cleanup(new Set));
    dialog.showModal();
  });
}
async function handleInviteMore() {
  if (state.sessionId === null)
    return;
  const friends = await loadFriendOptions();
  const lobby = await getJson(urlFor(urls.lobby, state.sessionId));
  const alreadyInvited = new Set(lobby.participants.map((participant) => participant.profile_id));
  const available = friends.filter((friend) => !alreadyInvited.has(friend.profile_id));
  if (!available.length) {
    toast.error("Everyone on your friends list is already in this game.");
    return;
  }
  const chosenIds = await pickFriendsToInvite(available);
  if (!chosenIds.size)
    return;
  for (const profileId of chosenIds) {
    const response = await postForm(urlFor(urls.invite, state.sessionId), { profile_id: String(profileId) });
    if (response.error)
      toast.error(response.error);
  }
  const refreshed = await getJson(urlFor(urls.lobby, state.sessionId));
  renderLobbyParticipants(refreshed.participants);
}
function ensureGuessMap() {
  if (state.guessMap)
    return state.guessMap;
  state.guessMap = L.map("sg-guess-map", { attributionControl: false }).setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  createMapLayers(state.guessMap, {
    root: document.getElementById("sg-guess-map-layers"),
    onAttribution: (text) => {
      const attributionEl = document.getElementById("sg-guess-map-attribution");
      if (attributionEl)
        attributionEl.textContent = text;
    }
  });
  state.guessMap.on("click", (event) => placeGuessMarker(event.latlng));
  return state.guessMap;
}
function placeGuessMarker(latlng) {
  const map = ensureGuessMap();
  if (state.guessMarker) {
    state.guessMarker.setLatLng(latlng);
  } else {
    state.guessMarker = L.marker(latlng, { draggable: true }).addTo(map);
  }
  el("sg-submit-guess-btn").disabled = false;
}
function resetGuessMap(bounds) {
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
  el("sg-submit-guess-btn").disabled = true;
  if (bounds) {
    map.fitBounds(bounds, { padding: [20, 20] });
  } else {
    map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  }
}
function clearRoundTimer() {
  if (state.roundTimerHandle !== null) {
    clearInterval(state.roundTimerHandle);
    state.roundTimerHandle = null;
  }
  state.roundExpiresAtMs = null;
  el("sg-round-timer").hidden = true;
}
function startRoundTimer(expiresAtIso) {
  clearRoundTimer();
  if (!expiresAtIso)
    return;
  state.roundExpiresAtMs = new Date(expiresAtIso).getTime();
  el("sg-round-timer").hidden = false;
  updateRoundTimerDisplay();
  state.roundTimerHandle = setInterval(updateRoundTimerDisplay, 1000);
}
function updateRoundTimerDisplay() {
  if (state.roundExpiresAtMs === null)
    return;
  const remainingSeconds = Math.ceil((state.roundExpiresAtMs - Date.now()) / 1000);
  const timerEl = el("sg-round-timer");
  timerEl.textContent = formatCountdown(remainingSeconds);
  timerEl.classList.toggle("spotguessr-round-timer--urgent", remainingSeconds <= 10);
  if (remainingSeconds <= 0) {
    clearRoundTimer();
    reportRoundTimeout();
  }
}
async function reportRoundTimeout() {
  if (state.sessionId === null || state.currentRoundId === null)
    return;
  await postForm(urlFor(urls.round_timeout, state.sessionId, state.currentRoundId), {});
  if (state.isMultiplayer)
    return;
  const data = await getJson(urlFor(urls.round, state.sessionId));
  if (data.no_eligible_locations) {
    showNoEligibleLocations();
  } else if (data.finished) {
    showSummary(data.summary);
  } else if (data.round) {
    renderRound(data.round, data.round.sequence_index + 1);
  }
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
  const isHost = state.hostProfileId === myProfileId;
  el("sg-invite-more-btn").hidden = !isHost;
  el("sg-join-lobby-btn").hidden = !(me && me.status === "invited");
  el("sg-begin-btn").hidden = !isHost;
}
function renderLobby(session) {
  state.hostProfileId = session.host_profile_id;
  state.currentMode = session.mode;
  state.totalRounds = session.total_rounds;
  showPanel("lobby");
  renderLobbyParticipants(session.participants);
  connectSessionSocket();
}
async function refreshLobby() {
  if (state.sessionId === null)
    return;
  const lobby = await getJson(urlFor(urls.lobby, state.sessionId));
  renderLobbyParticipants(lobby.participants);
}
function renderRound(round, roundNumber) {
  state.currentRoundId = round.round_id;
  state.currentMode = round.mode;
  state.currentRoundShowsImagery = round.shows_imagery;
  showPanel("game");
  el("sg-reveal-panel").hidden = true;
  el("sg-round-status").textContent = `Round ${roundNumber} of ${state.totalRounds}`;
  state.displayedSessionScore = state.sessionScore;
  el("sg-score-status").textContent = state.isMultiplayer ? "" : `Score: ${state.sessionScore}`;
  el("sg-game-settings-btn").hidden = state.isMultiplayer;
  el("sg-end-game-btn").hidden = !(state.isMultiplayer && state.hostProfileId === myProfileId);
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
  el("sg-date-field").hidden = !state.dateGuessingEnabled;
  el("sg-photo-feedback").hidden = true;
  el("sg-photo-feedback-thanks").hidden = true;
  resetGuessMap(round.geo_bounds);
  startRoundTimer(round.expires_at);
  setTimeout(() => state.guessMap?.invalidateSize(), 0);
}
var activeCountUps = new WeakMap;
function animateCountUp(el2, from, to, durationMs = 700, formatter = (value) => String(Math.round(value))) {
  const pending = activeCountUps.get(el2);
  if (pending !== undefined)
    cancelAnimationFrame(pending);
  if (from === to || durationMs <= 0) {
    el2.textContent = formatter(to);
    activeCountUps.delete(el2);
    return;
  }
  const start = performance.now();
  const tick = (now) => {
    const progress = Math.min(1, (now - start) / durationMs);
    el2.textContent = formatter(progress >= 1 ? to : countUpValue(from, to, progress));
    if (progress < 1) {
      activeCountUps.set(el2, requestAnimationFrame(tick));
    } else {
      activeCountUps.delete(el2);
    }
  };
  activeCountUps.set(el2, requestAnimationFrame(tick));
}
function animateLineDrawIn(map, from, to, durationMs = 600) {
  const line = L.polyline([from, from], { color: "#e74c3c" }).addTo(map);
  const start = performance.now();
  const tick = (now) => {
    const progress = Math.min(1, (now - start) / durationMs);
    const [lat, lng] = interpolateLatLng([from.lat, from.lng], [to.lat, to.lng], progress);
    line.setLatLngs([from, progress >= 1 ? to : L.latLng(lat, lng)]);
    if (progress < 1)
      requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return line;
}
function renderScoreCard(data, rank, animatePoints = false) {
  const template = el("sg-score-card-template");
  const node = template.content.firstElementChild?.cloneNode(true);
  if (!node)
    throw new Error("SpotGuessr: score card template is empty");
  const card = node;
  card.dataset.profileId = String(data.profileId);
  const rankEl = card.querySelector(".spotguessr-score-card-rank");
  if (rankEl)
    rankEl.textContent = rank ? `#${rank}` : "";
  if (rank && rank <= 3)
    card.classList.add(`spotguessr-score-card--rank-${rank}`);
  const avatarWrap = card.querySelector(".spotguessr-score-card-avatar-wrap");
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
  const nameEl = card.querySelector(".spotguessr-score-card-name");
  if (nameEl)
    nameEl.textContent = data.username;
  const subtitleEl = card.querySelector(".spotguessr-score-card-subtitle");
  if (subtitleEl)
    subtitleEl.textContent = data.subtitle ?? "";
  const pointsEl = card.querySelector(".spotguessr-score-card-points");
  if (pointsEl) {
    if (animatePoints) {
      animateCountUp(pointsEl, 0, data.points, 700, (value) => `${Math.round(value)} pts`);
    } else {
      pointsEl.textContent = `${data.points} pts`;
    }
  }
  const ratingDeltaEl = card.querySelector(".spotguessr-score-card-rating-delta");
  if (ratingDeltaEl) {
    const formatted = formatRatingDelta(data.ratingDelta);
    ratingDeltaEl.textContent = formatted?.text ?? "";
    ratingDeltaEl.className = `spotguessr-score-card-rating-delta${formatted ? ` spotguessr-score-card-rating-delta--${formatted.direction}` : ""}`;
  }
  return card;
}
function renderScoreCardList(container, entries, options) {
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
function renderResultsList(results) {
  const list = el("sg-reveal-results");
  if (!state.isMultiplayer) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  renderScoreCardList(list, results.map((result) => ({
    profileId: result.profile_id,
    username: result.username,
    avatarUrl: result.avatar_url,
    points: result.points + result.date_points + result.bonus_points,
    subtitle: `${(result.distance_meters / 1000).toFixed(2)} km away`,
    ratingDelta: result.rating_delta
  })), { solo: false, animatePoints: true });
}
function updateScoreboardFromResults(results) {
  for (const result of results) {
    let entry = state.scoreboard.find((participant) => participant.profile_id === result.profile_id);
    if (!entry) {
      entry = { profile_id: result.profile_id, username: result.username, avatar_url: result.avatar_url, total_points: 0 };
      state.scoreboard.push(entry);
    }
    entry.total_points += result.points + result.date_points + result.bonus_points;
  }
  renderScoreboard();
  const list = el("sg-scoreboard");
  for (const result of results) {
    const card = list.querySelector(`[data-profile-id="${result.profile_id}"]`);
    if (!card)
      continue;
    card.classList.add("spotguessr-score-card--pulse");
    card.addEventListener("animationend", () => card.classList.remove("spotguessr-score-card--pulse"), { once: true });
  }
}
function renderScoreboard() {
  const list = el("sg-scoreboard");
  if (!state.isMultiplayer || !state.scoreboard.length) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  renderScoreCardList(list, state.scoreboard.map((entry) => ({ profileId: entry.profile_id, username: entry.username, avatarUrl: entry.avatar_url, points: entry.total_points })), { solo: false });
}
function showPhotoFeedbackIfApplicable() {
  el("sg-photo-feedback").hidden = !state.currentRoundShowsImagery;
  el("sg-photo-feedback-thanks").hidden = true;
}
function dropInMarker(marker) {
  marker.getElement()?.classList.add("spotguessr-marker-drop");
}
function drawRevealMarkers(actualLatLng) {
  const map = ensureGuessMap();
  state.actualMarker = L.marker(actualLatLng).addTo(map);
  dropInMarker(state.actualMarker);
  if (state.guessMarker) {
    const guessLatLng = state.guessMarker.getLatLng();
    map.fitBounds(L.latLngBounds([guessLatLng, actualLatLng]), { padding: [40, 40] });
    state.resultLine = animateLineDrawIn(map, guessLatLng, actualLatLng);
  } else {
    map.setView(actualLatLng, 14);
  }
}
function updateRatingDeltaDisplay(delta) {
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
function showReveal(reveal) {
  clearRoundTimer();
  el("sg-submit-guess-btn").disabled = true;
  const roundTotal = reveal.points + reveal.date_points + reveal.bonus_points;
  state.sessionScore += roundTotal;
  if (state.isMultiplayer) {
    el("sg-score-status").textContent = "";
  } else {
    animateCountUp(el("sg-score-status"), state.displayedSessionScore, state.sessionScore, 700, (value) => `Score: ${Math.round(value)}`);
  }
  state.displayedSessionScore = state.sessionScore;
  showPhotoFeedbackIfApplicable();
  updateRatingDeltaDisplay(reveal.rating_delta);
  animateCountUp(el("sg-reveal-points-value"), 0, roundTotal);
  const distanceKm = (reveal.distance_meters / 1000).toFixed(2);
  if (!reveal.revealed) {
    el("sg-reveal-panel").hidden = false;
    el("sg-reveal-title").textContent = "Guess submitted!";
    let detail2 = `${distanceKm} km away. Waiting for other players…`;
    if (reveal.date_points)
      detail2 = `${distanceKm} km away (+${reveal.date_points} for the date guess). Waiting for other players…`;
    detail2 += bonusSuffix(reveal.bonus_points, reveal.bonus_tiers);
    el("sg-reveal-detail").textContent = detail2;
    el("sg-reveal-results").hidden = true;
    el("sg-next-round-btn").hidden = true;
    return;
  }
  state.lastRevealedRoundId = reveal.round_id;
  drawRevealMarkers(L.latLng(reveal.actual_latitude, reveal.actual_longitude));
  el("sg-reveal-panel").hidden = false;
  el("sg-reveal-title").textContent = reveal.location_name || "Revealed!";
  let detail = `${distanceKm} km away`;
  if (reveal.date_points)
    detail += ` (+${reveal.date_points} for the date guess)`;
  detail += bonusSuffix(reveal.bonus_points, reveal.bonus_tiers);
  el("sg-reveal-detail").textContent = detail;
  el("sg-reveal-results").hidden = true;
  el("sg-next-round-btn").hidden = state.isMultiplayer;
}
function showBroadcastReveal(data) {
  clearRoundTimer();
  if (state.lastRevealedRoundId !== data.round_id) {
    state.lastRevealedRoundId = data.round_id;
    drawRevealMarkers(L.latLng(data.actual_latitude, data.actual_longitude));
    el("sg-reveal-panel").hidden = false;
    el("sg-reveal-title").textContent = data.location_name || "Revealed!";
    el("sg-reveal-detail").textContent = "";
    el("sg-submit-guess-btn").disabled = true;
    el("sg-next-round-btn").hidden = true;
    const myResult = data.results.find((result) => result.profile_id === myProfileId);
    updateRatingDeltaDisplay(myResult?.rating_delta);
  }
  updateScoreboardFromResults(data.results);
  renderResultsList(data.results);
}
function showSummary(summary) {
  showPanel("summary");
  clearRoundTimer();
  const { heading, icon } = summaryHeadline(summary.participants, state.isMultiplayer, myProfileId);
  el("sg-summary-heading").textContent = heading;
  el("sg-summary-icon").textContent = icon;
  const roundWord = summary.total_rounds === 1 ? "round" : "rounds";
  el("sg-summary-subheading").textContent = `${summary.rounds_played} of ${summary.total_rounds} ${roundWord} played.`;
  renderScoreCardList(el("sg-summary-scores"), summary.participants.map((participant) => ({
    profileId: participant.profile_id,
    username: participant.username,
    avatarUrl: participant.avatar_url,
    points: participant.total_points,
    ratingDelta: participant.rating_delta,
    subtitle: summaryBestRoundSubtitle(participant)
  })), { solo: !state.isMultiplayer, animatePoints: true });
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
}
async function startGame(mode) {
  state.currentMode = mode;
  state.dateGuessingEnabled = state.currentMode === "photos" && el("sg-date-guessing").checked;
  state.sessionScore = 0;
  state.displayedSessionScore = 0;
  state.scoreboard = [];
  state.lastRevealedRoundId = null;
  const geoBounds = el("sg-restrict-area").checked ? currentGeoBoundsGeoJson() : null;
  const roundTimeLimit = el("sg-round-time-limit").value;
  const body = new URLSearchParams({
    mode: state.currentMode,
    difficulty: String(currentDifficulty()),
    total_rounds: el("sg-rounds").value,
    allow_arbitrary_external_photos: el("sg-allow-arbitrary-external-photos").checked ? "on" : "off",
    require_visited_all: el("sg-require-visited-all").checked ? "on" : "off",
    date_guessing_enabled: state.dateGuessingEnabled ? "on" : "off",
    use_aliases: el("sg-use-aliases").checked ? "on" : "off"
  });
  if (geoBounds)
    body.append("geo_bounds", geoBounds);
  if (roundTimeLimit)
    body.append("round_time_limit_seconds", roundTimeLimit);
  const labelId = el("sg-label-filter").value;
  if (labelId)
    body.append("label_id", labelId);
  for (const profileId of state.selectedInviteIds)
    body.append("invite_profile_ids", String(profileId));
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
  state.totalRounds = Number(el("sg-rounds").value);
  if (response.lobby) {
    state.isMultiplayer = true;
    renderLobby(response.session);
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
async function submitGuess() {
  if (!state.guessMarker || state.sessionId === null || state.currentRoundId === null)
    return;
  const latlng = state.guessMarker.getLatLng();
  const payload = { latitude: String(latlng.lat), longitude: String(latlng.lng) };
  if (state.dateGuessingEnabled) {
    const dateValue = el("sg-guessed-date").value;
    if (dateValue)
      payload.guessed_date = dateValue;
  }
  const reveal = await postForm(urlFor(urls.guess, state.sessionId, state.currentRoundId), payload);
  if (reveal.error) {
    toast.error(reveal.error);
    return;
  }
  showReveal(reveal);
}
async function submitPhotoFeedback(kind) {
  if (state.sessionId === null || state.currentRoundId === null)
    return;
  const response = await postForm(urlFor(urls.photo_feedback, state.sessionId, state.currentRoundId), { kind });
  if (response.error) {
    toast.error(response.error);
    return;
  }
  el("sg-photo-feedback-thanks").hidden = false;
}
async function goToNextRound() {
  if (state.sessionId === null)
    return;
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
async function joinLobby() {
  if (state.sessionId === null)
    return;
  const response = await postForm(urlFor(urls.join, state.sessionId), {});
  if (response.error) {
    toast.error(response.error);
    return;
  }
  await refreshLobby();
}
async function beginGame() {
  if (state.sessionId === null)
    return;
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
function _resetSessionState() {
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
async function endGameNow() {
  if (state.sessionId === null)
    return;
  const confirmed = await confirmAction({
    title: "End this game?",
    message: "This ends the game immediately for everyone, using the scores so far.",
    confirmLabel: "End game"
  });
  if (!confirmed)
    return;
  const response = await postForm(urlFor(urls.end, state.sessionId), {});
  if (response.error) {
    toast.error(response.error);
    return;
  }
  showSummary(response.summary);
}
function resetToSettings() {
  _resetSessionState();
  showPanel("settings");
}
function activeFilterLabels() {
  const labels = [];
  if (el("sg-require-visited-all").checked)
    labels.push("Only places I've visited");
  const labelFilter = el("sg-label-filter");
  if (labelFilter.value)
    labels.push(`Restricted to the "${labelFilter.selectedOptions[0]?.textContent}" label`);
  if (el("sg-restrict-area").checked && currentGeoBoundsGeoJson())
    labels.push("Restricted to a chosen area");
  return labels;
}
function clearActiveFilters() {
  el("sg-require-visited-all").checked = false;
  el("sg-label-filter").value = "";
  clearAreaGeometry();
}
function showNoEligibleLocations() {
  const mode = state.currentMode;
  _resetSessionState();
  showPanel("empty");
  const filters = activeFilterLabels();
  const filtersList = el("sg-empty-state-active-filters");
  const clearBtn = el("sg-empty-state-clear-filters-btn");
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
      startGame(mode);
    };
  } else {
    filtersList.hidden = true;
    clearBtn.hidden = true;
  }
}
function initEmptyState() {
  el("sg-empty-state-settings-btn").addEventListener("click", () => {
    resetToSettings();
    openSettingsDialog(state.currentMode);
  });
}
function applyLastConfig() {
  const config = window.SPOTGUESSR_LAST_CONFIG;
  if (!config)
    return;
  let nearestDifficulty = "medium";
  let smallestGap = Infinity;
  for (const key of Object.keys(DIFFICULTY_VALUES)) {
    const gap = Math.abs(DIFFICULTY_VALUES[key] - config.difficulty);
    if (gap < smallestGap) {
      smallestGap = gap;
      nearestDifficulty = key;
    }
  }
  el(`sg-difficulty-${nearestDifficulty}`).checked = true;
  el("sg-allow-arbitrary-external-photos").checked = config.allow_arbitrary_external_photos;
  el("sg-date-guessing").checked = config.date_guessing_enabled;
  el("sg-use-aliases").checked = config.use_aliases;
  el("sg-require-visited-all").checked = config.require_visited_all;
  el("sg-round-time-limit").value = config.round_time_limit_seconds ? String(config.round_time_limit_seconds) : "";
  el("sg-label-filter").value = config.label_id ? String(config.label_id) : "";
  if (config.geo_bounds_geojson) {
    el("sg-restrict-area").checked = true;
    el("sg-area-geo-bounds").value = JSON.stringify(config.geo_bounds_geojson);
    el("sg-area-clear-btn").hidden = false;
    state.restoredGeoBounds = config.geo_bounds_geojson;
  }
}
function connectSessionSocket() {
  if (state.ws || state.sessionId === null)
    return;
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  state.ws = new WebSocket(`${proto}${location.host}/ws/spotguessr/session/${state.sessionId}/`);
  state.ws.addEventListener("message", (event) => {
    try {
      handleSocketMessage(JSON.parse(event.data));
    } catch {}
  });
  state.ws.addEventListener("close", () => {
    state.ws = null;
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
  if (state.sessionId === null)
    return;
  const data = await getJson(urlFor(urls.chat_history, state.sessionId));
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
    if (!body || !state.ws)
      return;
    state.ws.send(JSON.stringify({ body }));
    input.value = "";
  });
}
async function loadInitialSession() {
  const raw = pageEl?.dataset.initialSessionId;
  if (!raw)
    return;
  state.sessionId = Number(raw);
  const lobby = await getJson(urlFor(urls.lobby, state.sessionId));
  state.totalRounds = lobby.total_rounds;
  state.currentMode = lobby.mode;
  state.isMultiplayer = true;
  if (lobby.status === "lobby") {
    renderLobby(lobby);
    return;
  }
  if (lobby.status === "completed" || lobby.status === "abandoned") {
    const summary = await getJson(urlFor(urls.summary, state.sessionId));
    showSummary(summary);
    return;
  }
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
fetchFriendsEagerly();
el("sg-start-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const mode = currentSettingsMode();
  el("sg-settings-dialog").close();
  startGame(mode);
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
loadInitialSession();
