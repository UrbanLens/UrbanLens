import {
  createMapLayers
} from "./article-wysiwyg-f04nz5p5.js";
import {
  getCsrfToken,
  toast
} from "./article-wysiwyg-5jnnp4sj.js";
import"./article-wysiwyg-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/entries/spotguessr.ts
var urls = window.SPOTGUESSR_URLS;
var DEFAULT_CENTER = [20, 0];
var DEFAULT_ZOOM = 2;
var pageEl = document.querySelector(".spotguessr-page");
var myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");
var regionSearchUrl = pageEl?.dataset.regionSearchUrl ?? "";
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
var restoredGeoBounds = null;
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
  if (areaMap) {
    areaMap.invalidateSize();
  } else {
    ensureAreaMap();
    if (restoredGeoBounds) {
      setAreaGeometry(restoredGeoBounds);
      restoredGeoBounds = null;
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
  if (areaMap)
    return areaMap;
  areaMap = L.map("sg-area-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(areaMap);
  areaDrawnItems = new L.FeatureGroup;
  areaMap.addLayer(areaDrawnItems);
  const drawControl = new L.Control.Draw({
    draw: { polygon: {}, rectangle: false, circle: false, marker: false, polyline: false, circlemarker: false },
    edit: { featureGroup: areaDrawnItems }
  });
  areaMap.addControl(drawControl);
  areaMap.on(L.Draw.Event.CREATED, (event) => {
    const { layer } = event;
    setAreaGeometry(layer.toGeoJSON().geometry);
  });
  return areaMap;
}
function setAreaGeometry(geometry) {
  const map = ensureAreaMap();
  areaDrawnItems?.clearLayers();
  const layer = L.geoJSON(geometry);
  layer.eachLayer((shapeLayer) => areaDrawnItems?.addLayer(shapeLayer));
  el("sg-area-geo-bounds").value = JSON.stringify(geometry);
  el("sg-restrict-area").checked = true;
  el("sg-area-clear-btn").hidden = false;
  const bounds = areaDrawnItems?.getBounds();
  if (bounds?.isValid())
    map.fitBounds(bounds, { padding: [40, 40] });
  updateAreaPinCount();
}
function clearAreaGeometry() {
  areaDrawnItems?.clearLayers();
  el("sg-area-geo-bounds").value = "";
  el("sg-restrict-area").checked = false;
  el("sg-area-clear-btn").hidden = true;
  el("sg-area-pin-count").hidden = true;
  areaMap?.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
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
  renderFriendCheckboxes(el("sg-friend-list"), friendOptions, new Set);
}
function initFriendListRetry() {
  el("sg-friend-list-retry").addEventListener("click", () => void fetchFriendsEagerly());
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
    const wrap = document.createElement("span");
    wrap.className = "ul-checkbox-wrap";
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
    const box = document.createElement("span");
    box.className = "ul-checkbox";
    wrap.append(checkbox, box);
    const nameSpan = document.createElement("span");
    nameSpan.textContent = friend.username;
    label.append(wrap, nameSpan);
    container.appendChild(label);
  }
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
  const refreshed = await getJson(urlFor(urls.lobby, sessionId));
  renderLobbyParticipants(refreshed.participants);
}
function ensureGuessMap() {
  if (guessMap)
    return guessMap;
  guessMap = L.map("sg-guess-map", { attributionControl: false }).setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  createMapLayers(guessMap, {
    root: document.getElementById("sg-guess-map-layers"),
    onAttribution: (text) => {
      const attributionEl = document.getElementById("sg-guess-map-attribution");
      if (attributionEl)
        attributionEl.textContent = text;
    }
  });
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
function resetGuessMap(bounds) {
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
  if (bounds) {
    map.fitBounds(bounds, { padding: [20, 20] });
  } else {
    map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
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
  el("sg-game-settings-btn").hidden = isMultiplayer;
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
  resetGuessMap(round.geo_bounds);
  setTimeout(() => guessMap?.invalidateSize(), 0);
}
function avatarInitial(username) {
  return username.charAt(0).toUpperCase() || "?";
}
function renderScoreCard(data, rank) {
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
  if (pointsEl)
    pointsEl.textContent = `${data.points} pts`;
  return card;
}
function renderScoreCardList(container, entries, options) {
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
function renderResultsList(results) {
  const list = el("sg-reveal-results");
  if (!isMultiplayer) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  renderScoreCardList(list, results.map((result) => ({
    profileId: result.profile_id,
    username: result.username,
    avatarUrl: result.avatar_url,
    points: result.points + result.date_points + result.bonus_points,
    subtitle: `${(result.distance_meters / 1000).toFixed(2)} km away`
  })), { solo: false });
}
function updateScoreboardFromResults(results) {
  for (const result of results) {
    let entry = scoreboard.find((participant) => participant.profile_id === result.profile_id);
    if (!entry) {
      entry = { profile_id: result.profile_id, username: result.username, avatar_url: result.avatar_url, total_points: 0 };
      scoreboard.push(entry);
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
  if (!isMultiplayer || !scoreboard.length) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  renderScoreCardList(list, scoreboard.map((entry) => ({ profileId: entry.profile_id, username: entry.username, avatarUrl: entry.avatar_url, points: entry.total_points })), { solo: false });
}
function showPhotoFeedbackIfApplicable() {
  el("sg-photo-feedback").hidden = !["photos", "street_view"].includes(currentMode);
  el("sg-photo-feedback-thanks").hidden = true;
}
function dropInMarker(marker) {
  marker.getElement()?.classList.add("spotguessr-marker-drop");
}
function bonusSuffix(bonusPoints, bonusTiers) {
  return bonusPoints ? ` (+${bonusPoints} bonus: ${bonusTiers.join(", ")})` : "";
}
function showReveal(reveal) {
  el("sg-submit-guess-btn").disabled = true;
  sessionScore += reveal.points + reveal.date_points + reveal.bonus_points;
  el("sg-score-status").textContent = isMultiplayer ? "" : `Score: ${sessionScore}`;
  showPhotoFeedbackIfApplicable();
  const distanceKm = (reveal.distance_meters / 1000).toFixed(2);
  if (!reveal.revealed) {
    el("sg-reveal-panel").hidden = false;
    el("sg-reveal-title").textContent = "Guess submitted!";
    let detail2 = `${reveal.points} points – ${distanceKm} km away. Waiting for other players…`;
    if (reveal.date_points)
      detail2 = `${reveal.points} points (+${reveal.date_points} for the date guess) – ${distanceKm} km away. Waiting for other players…`;
    detail2 += bonusSuffix(reveal.bonus_points, reveal.bonus_tiers);
    el("sg-reveal-detail").textContent = detail2;
    el("sg-reveal-results").hidden = true;
    el("sg-next-round-btn").hidden = true;
    return;
  }
  lastRevealedRoundId = reveal.round_id;
  const map = ensureGuessMap();
  const actualLatLng = L.latLng(reveal.actual_latitude, reveal.actual_longitude);
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
  if (reveal.date_points)
    detail += ` (+${reveal.date_points} for the date guess)`;
  detail += bonusSuffix(reveal.bonus_points, reveal.bonus_tiers);
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
  renderScoreCardList(el("sg-summary-scores"), summary.participants.map((participant) => ({
    profileId: participant.profile_id,
    username: participant.username,
    avatarUrl: participant.avatar_url,
    points: participant.total_points
  })), { solo: !isMultiplayer });
  if (ws) {
    ws.close();
    ws = null;
  }
}
async function startGame(mode) {
  currentMode = mode;
  dateGuessingEnabled = currentMode === "photos" && el("sg-date-guessing").checked;
  sessionScore = 0;
  scoreboard = [];
  lastRevealedRoundId = null;
  const geoBounds = el("sg-restrict-area").checked ? currentGeoBoundsGeoJson() : null;
  const body = new URLSearchParams({
    mode: currentMode,
    difficulty: String(currentDifficulty()),
    total_rounds: el("sg-rounds").value,
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
    toast.error(response.error);
    return;
  }
  if (response.error_code === "no_eligible_locations") {
    showNoEligibleLocations();
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
    toast.error(reveal.error);
    return;
  }
  showReveal(reveal);
}
async function submitPhotoFeedback(kind) {
  if (sessionId === null || currentRoundId === null)
    return;
  const response = await postForm(urlFor(urls.photo_feedback, sessionId, currentRoundId), { kind });
  if (response.error) {
    toast.error(response.error);
    return;
  }
  el("sg-photo-feedback-thanks").hidden = false;
}
async function goToNextRound() {
  if (sessionId === null)
    return;
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
  el("sg-empty-state-panel").hidden = true;
  el("sg-settings-panel").hidden = false;
}
function activeFilterLabels() {
  const labels = [];
  if (el("sg-require-visited-all").checked)
    labels.push("Only places I've visited");
  if (el("sg-restrict-area").checked && currentGeoBoundsGeoJson())
    labels.push("Restricted to a chosen area");
  return labels;
}
function clearActiveFilters() {
  el("sg-require-visited-all").checked = false;
  clearAreaGeometry();
}
function showNoEligibleLocations() {
  const mode = currentMode;
  resetToSettings();
  el("sg-settings-panel").hidden = true;
  el("sg-empty-state-panel").hidden = false;
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
      el("sg-empty-state-panel").hidden = true;
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
    openSettingsDialog(currentMode);
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
  if (config.geo_bounds_geojson) {
    el("sg-restrict-area").checked = true;
    el("sg-area-geo-bounds").value = JSON.stringify(config.geo_bounds_geojson);
    el("sg-area-clear-btn").hidden = false;
    restoredGeoBounds = config.geo_bounds_geojson;
  }
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
el("sg-game-settings-btn").addEventListener("click", () => openSettingsDialog(currentMode));
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
