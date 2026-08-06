import {
  createMapLayers
} from "./photo-location-scan-rarq1vf2.js";
import {
  createGameShell,
  playEntrance
} from "./photo-location-scan-vedkz711.js";
import {
  confirmAction,
  getCsrfToken,
  toast
} from "./photo-location-scan-5jnnp4sj.js";
import"./photo-location-scan-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/entries/consensus.ts
var FIELD_KIND = {
  WIKI_NAME: "wiki_name",
  WIKI_DESCRIPTION: "wiki_description",
  WIKI_INDOOR_OUTDOOR: "wiki_indoor_outdoor",
  WIKI_PIN_TYPE: "wiki_pin_type",
  WIKI_ALIAS: "wiki_alias",
  PHOTO_COORDINATES: "photo_coordinates"
};
var INDOOR_OUTDOOR_CHOICES = [
  ["inside", "Inside"],
  ["outside", "Outside"],
  ["both", "Both"]
];
var PIN_TYPE_CHOICES = [
  ["location", "Location"],
  ["parcel", "Property / Parcel"],
  ["building", "Building"],
  ["entrance", "Entrance"],
  ["poi", "Point of Interest"],
  ["danger", "Danger"],
  ["other", "Other"]
];
var urls = window.CONSENSUS_URLS;
var DEFAULT_CENTER = [20, 0];
var DEFAULT_ZOOM = 2;
var pageEl = document.querySelector(".consensus-page");
var myProfileId = Number(pageEl?.dataset.myProfileId ?? "0");
var state = {
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
  selectedInviteIds: new Set,
  participants: [],
  scoreboard: [],
  answeredProfileIds: new Set,
  votedProfileIds: new Set
};
var PANEL_IDS = {
  settings: "cs-settings-panel",
  empty: "cs-empty-state-panel",
  lobby: "cs-lobby-panel",
  game: "cs-game-panel",
  summary: "cs-summary-panel"
};
var gameShell = null;
function showPanel(name) {
  if (gameShell) {
    gameShell.showPanel(name);
    return;
  }
  for (const [key, id] of Object.entries(PANEL_IDS)) {
    el(id).hidden = key !== name;
  }
}
function reducedMotion() {
  return gameShell?.reducedMotion() ?? false;
}
function el(id) {
  const found = document.getElementById(id);
  if (!found)
    throw new Error(`Consensus: missing #${id}`);
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
async function withBusy(button, action) {
  if (button) {
    button.classList.add("is-loading");
    button.disabled = true;
  }
  try {
    return await action();
  } finally {
    if (button) {
      button.classList.remove("is-loading");
      button.disabled = false;
    }
  }
}
function optionalEl(id) {
  return document.getElementById(id);
}
var LEVEL_SCALE_K = 100;
var MAX_LEVEL = 500;
function pointsRequiredForLevel(level) {
  if (level < 1)
    return 0;
  return Math.round(LEVEL_SCALE_K * level * Math.log(level + 1));
}
function levelForPoints(points) {
  let level = 1;
  while (level < MAX_LEVEL && points >= pointsRequiredForLevel(level))
    level += 1;
  return level;
}
var progression = { basePoints: 0, sessionPoints: 0 };
function renderProgression() {
  const points = progression.basePoints + progression.sessionPoints;
  const level = levelForPoints(points);
  const floor = pointsRequiredForLevel(level - 1);
  const ceiling = pointsRequiredForLevel(level);
  const ratio = Math.min(1, Math.max(0, (points - floor) / Math.max(1, ceiling - floor)));
  const levelEl = optionalEl("cs-level-value");
  if (levelEl)
    levelEl.textContent = String(level);
  const pointsEl = optionalEl("cs-points-value");
  if (pointsEl)
    pointsEl.textContent = String(points);
  const noteEl = optionalEl("cs-level-note");
  if (noteEl)
    noteEl.textContent = `${Math.max(0, ceiling - points)} pts to level ${level + 1}`;
  const fillEl = optionalEl("cs-level-meter-fill");
  if (fillEl)
    fillEl.style.setProperty("--consensus-level-progress", String(ratio));
}
function setSessionPoints(total) {
  if (total === progression.sessionPoints)
    return;
  progression.sessionPoints = total;
  renderProgression();
  const pointsEl = optionalEl("cs-points-value");
  if (!pointsEl || reducedMotion())
    return;
  pointsEl.classList.remove("is-counting");
  pointsEl.offsetWidth;
  pointsEl.classList.add("is-counting");
}
function bankSessionPoints() {
  if (progression.sessionPoints) {
    progression.basePoints += progression.sessionPoints;
    progression.sessionPoints = 0;
  }
  renderProgression();
}
function initProgression() {
  const wrap = optionalEl("cs-progression");
  progression.basePoints = Number(wrap?.dataset.points ?? "0") || 0;
  progression.sessionPoints = 0;
  renderProgression();
}
async function loadFriendOptions() {
  if (state.friendOptions.length)
    return state.friendOptions;
  const data = await getJson(urls.friends);
  state.friendOptions = data.friends ?? [];
  return state.friendOptions;
}
function renderFriendCheckboxes(container, friends, excludeIds, targetSet = state.selectedInviteIds) {
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
async function fetchFriendsEagerly() {
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
  renderFriendCheckboxes(listEl, state.friendOptions, new Set);
}
function pickFriendsToInvite(available) {
  return new Promise((resolve) => {
    const chosen = new Set;
    const dialog = document.createElement("dialog");
    dialog.className = "ul-dialog ul-game-dialog consensus-invite-more-dialog";
    const header = document.createElement("div");
    header.className = "dialog-header";
    const heading = document.createElement("h3");
    heading.textContent = "Invite more players";
    header.appendChild(heading);
    const list = document.createElement("div");
    list.className = "consensus-friend-list";
    renderFriendCheckboxes(list, available, new Set, chosen);
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
    if (gameShell)
      gameShell.mountOverlay(dialog);
    else
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
  await refreshLobby();
}
function renderLobbyParticipants(participants) {
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
  el("cs-invite-more-btn").hidden = !isHost;
  el("cs-join-lobby-btn").hidden = !(me && me.status === "invited");
  el("cs-begin-btn").hidden = !isHost;
}
function renderLobby(session) {
  state.sessionId = session.session_id;
  state.hostProfileId = session.host_profile_id;
  state.totalRounds = session.total_rounds;
  state.isMultiplayer = true;
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
  if (response.no_eligible_wikis) {
    showNoEligibleWikis();
    return;
  }
  if (response.finished) {
    showSummary(response.summary);
    return;
  }
  if (response.round)
    renderRound(response.round, 1);
}
function ensureRoundMap() {
  if (state.roundMap)
    return state.roundMap;
  const map = L.map("cs-round-map", { attributionControl: false }).setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  createMapLayers(map, {
    root: document.getElementById("cs-round-map-layers"),
    onAttribution: (text) => {
      const attributionEl = optionalEl("cs-round-map-attribution");
      if (attributionEl)
        attributionEl.textContent = text;
    }
  });
  map.on("click", (event) => {
    if (state.currentFieldKind !== FIELD_KIND.PHOTO_COORDINATES)
      return;
    placeAnswerMarker(event.latlng);
  });
  state.roundMap = map;
  return map;
}
function clearRoundMarkers() {
  const map = state.roundMap;
  if (!map)
    return;
  if (state.answerMarker) {
    map.removeLayer(state.answerMarker);
    state.answerMarker = null;
  }
  if (state.contextMarker) {
    map.removeLayer(state.contextMarker);
    state.contextMarker = null;
  }
}
function placeAnswerMarker(latlng) {
  const map = ensureRoundMap();
  if (state.answerMarker) {
    state.answerMarker.setLatLng(latlng);
  } else {
    state.answerMarker = L.marker(latlng, { draggable: true }).addTo(map);
  }
  el("cs-submit-answer-btn").disabled = false;
}
function resetRoundMap(latitude, longitude) {
  const map = ensureRoundMap();
  clearRoundMarkers();
  if (latitude !== null && longitude !== null) {
    const latlng = L.latLng(latitude, longitude);
    state.contextMarker = L.marker(latlng, { opacity: 0.6 }).addTo(map);
    map.setView(latlng, 16);
  } else {
    map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  }
  setTimeout(() => map.invalidateSize(), 0);
}
function setStageLayout(showPhoto, showMap) {
  const media = optionalEl("cs-stage-media");
  if (media)
    media.hidden = !showPhoto;
  const mapWrap = optionalEl("cs-round-mapwrap");
  if (mapWrap)
    mapWrap.hidden = !showMap;
  const stage = document.querySelector("[data-game-stage]");
  if (stage)
    stage.classList.toggle("ul-game-stage--single", !showPhoto);
}
function updateAnswerAreaVisibility(fieldKind) {
  document.querySelectorAll("[data-field-only]").forEach((field) => {
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
function populateSelectOptions(fieldKind) {
  const select = el("cs-answer-select-input");
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
function currentAnswerValue() {
  switch (state.currentFieldKind) {
    case FIELD_KIND.WIKI_DESCRIPTION:
      return el("cs-answer-textarea-input").value.trim();
    case FIELD_KIND.WIKI_INDOOR_OUTDOOR:
    case FIELD_KIND.WIKI_PIN_TYPE:
      return el("cs-answer-select-input").value;
    default:
      return el("cs-answer-text-input").value.trim();
  }
}
function updateSubmitEnabled() {
  if (state.currentFieldKind === FIELD_KIND.PHOTO_COORDINATES)
    return;
  el("cs-submit-answer-btn").disabled = !currentAnswerValue();
}
function renderRound(round, roundNumber) {
  state.currentRoundId = round.round_id;
  state.currentFieldKind = round.field_kind;
  state.answeredProfileIds = new Set;
  state.votedProfileIds = new Set;
  showPanel("game");
  el("cs-reveal-panel").hidden = true;
  el("cs-vote-panel").hidden = true;
  el("cs-round-live-indicator").hidden = true;
  el("cs-round-status").textContent = `Round ${roundNumber} of ${state.totalRounds}`;
  el("cs-round-field-label").textContent = round.field_label;
  const skipMotion = gameShell?.reducedMotion() ?? false;
  const wikiName = el("cs-round-wiki-name");
  wikiName.textContent = round.wiki_name;
  playEntrance(wikiName, skipMotion);
  el("cs-end-game-btn").hidden = !(state.isMultiplayer && state.hostProfileId === myProfileId);
  gameShell?.setProgress(roundNumber, state.totalRounds);
  gameShell?.setRailAvailable(state.isMultiplayer);
  const isPhotoRound = round.field_kind === FIELD_KIND.PHOTO_COORDINATES;
  const photo = el("cs-round-photo");
  const showPhoto = isPhotoRound && Boolean(round.image_url);
  if (showPhoto) {
    photo.src = round.image_url;
    photo.hidden = false;
    playEntrance(photo, skipMotion);
  } else {
    photo.hidden = true;
    photo.removeAttribute("src");
  }
  setStageLayout(showPhoto, isPhotoRound);
  updateAnswerAreaVisibility(round.field_kind);
  if (round.field_kind === FIELD_KIND.WIKI_INDOOR_OUTDOOR || round.field_kind === FIELD_KIND.WIKI_PIN_TYPE) {
    populateSelectOptions(round.field_kind);
  } else if (round.field_kind === FIELD_KIND.WIKI_DESCRIPTION) {
    el("cs-answer-textarea-input").value = "";
  } else if (round.field_kind !== FIELD_KIND.PHOTO_COORDINATES) {
    el("cs-answer-text-input").value = "";
  }
  if (isPhotoRound) {
    resetRoundMap(round.latitude, round.longitude);
  } else {
    clearRoundMarkers();
  }
  el("cs-submit-answer-btn").disabled = true;
  el("cs-submit-answer-btn").hidden = false;
  el("cs-skip-btn").hidden = false;
  el("cs-skip-btn").disabled = false;
  el("cs-round-waiting-hint").hidden = true;
}
async function afterAnswerOrSkip() {
  el("cs-submit-answer-btn").hidden = true;
  el("cs-skip-btn").hidden = true;
  if (!state.isMultiplayer) {
    await goToNextRound();
    return;
  }
  el("cs-round-waiting-hint").hidden = false;
}
async function submitAnswer() {
  if (state.sessionId === null || state.currentRoundId === null)
    return;
  let payload;
  if (state.currentFieldKind === FIELD_KIND.PHOTO_COORDINATES) {
    if (!state.answerMarker)
      return;
    const latlng = state.answerMarker.getLatLng();
    payload = { latitude: String(latlng.lat), longitude: String(latlng.lng) };
  } else {
    const value = currentAnswerValue();
    if (!value)
      return;
    payload = { value };
  }
  const button = el("cs-submit-answer-btn");
  button.classList.add("is-loading");
  button.disabled = true;
  let response;
  try {
    response = await postForm(urlFor(urls.answer, state.sessionId, state.currentRoundId), payload);
  } finally {
    button.classList.remove("is-loading");
  }
  if (response.error) {
    button.disabled = false;
    toast.error(response.error);
    return;
  }
  await afterAnswerOrSkip();
}
async function skipRound() {
  if (state.sessionId === null || state.currentRoundId === null)
    return;
  const button = el("cs-skip-btn");
  button.classList.add("is-loading");
  button.disabled = true;
  let response;
  try {
    response = await postForm(urlFor(urls.skip, state.sessionId, state.currentRoundId), {});
  } finally {
    button.classList.remove("is-loading");
  }
  if (response.error) {
    button.disabled = false;
    toast.error(response.error);
    return;
  }
  await afterAnswerOrSkip();
}
async function uploadPhoto() {
  if (state.sessionId === null || state.currentRoundId === null)
    return;
  const input = el("cs-photo-upload-input");
  const file = input.files?.[0];
  if (!file) {
    toast.error("Choose a photo first.");
    return;
  }
  const formData = new FormData;
  formData.append("image", file);
  const response = await fetch(urlFor(urls.photo, state.sessionId, state.currentRoundId), {
    method: "POST",
    headers: { "X-CSRFToken": getCsrfToken() },
    body: formData
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    toast.error(data.error ?? "Couldn't upload that photo.");
    return;
  }
  toast.success("Photo uploaded - thanks for helping out!");
  input.value = "";
  showChosenPhotoName();
}
function showChosenPhotoName() {
  const label = optionalEl("cs-photo-upload-name");
  if (!label)
    return;
  const file = el("cs-photo-upload-input").files?.[0];
  label.textContent = file ? file.name : "Choose a photo…";
}
async function goToNextRound() {
  if (state.sessionId === null)
    return;
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
function formatAnswerValue(value) {
  if (value && typeof value === "object" && "latitude" in value && "longitude" in value) {
    const point = value;
    return `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`;
  }
  return value === null || value === undefined || value === "" ? "(skipped)" : String(value);
}
function revealTitle(resolution) {
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
function renderResultsList(answers) {
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
      img.src = answer.avatar_url;
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
function renderVoteOptions(options) {
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
async function submitVote(answerId, container) {
  if (state.sessionId === null || state.currentRoundId === null)
    return;
  container.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });
  const response = await postForm(urlFor(urls.vote, state.sessionId, state.currentRoundId), { answer_id: String(answerId) });
  if (response.error) {
    toast.error(response.error);
    container.querySelectorAll("button").forEach((button) => {
      button.disabled = false;
    });
    return;
  }
  container.hidden = true;
  el("cs-vote-waiting").hidden = false;
}
function updateScoreboardFromReveal(answers) {
  if (!state.isMultiplayer)
    return;
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
function renderScoreboard() {
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
function renderReveal(data) {
  el("cs-submit-answer-btn").hidden = true;
  el("cs-skip-btn").hidden = true;
  el("cs-round-waiting-hint").hidden = true;
  el("cs-round-live-indicator").hidden = true;
  el("cs-reveal-panel").hidden = false;
  el("cs-reveal-title").textContent = revealTitle(data.resolution);
  renderResultsList(data.answers);
  updateScoreboardFromReveal(data.answers);
  const mine = data.answers.find((answer) => answer.profile_id === myProfileId);
  if (mine)
    setSessionPoints(progression.sessionPoints + mine.points_awarded);
  const votePanel = el("cs-vote-panel");
  if (data.resolution === "vote_open" && data.vote_options?.length) {
    votePanel.hidden = false;
    renderVoteOptions(data.vote_options);
  } else {
    votePanel.hidden = true;
  }
}
function markAnswered(profileId) {
  state.answeredProfileIds.add(profileId);
  const total = state.participants.filter((participant) => participant.status === "joined").length || state.participants.length;
  updateLiveIndicator(`${state.answeredProfileIds.size} of ${total || "?"} answered`);
}
function markVoted(profileId) {
  state.votedProfileIds.add(profileId);
  const total = state.participants.filter((participant) => participant.status === "joined").length || state.participants.length;
  updateLiveIndicator(`${state.votedProfileIds.size} of ${total || "?"} voted`);
}
function updateLiveIndicator(text) {
  const indicator = el("cs-round-live-indicator");
  indicator.textContent = text;
  indicator.hidden = false;
}
function showSummary(summary) {
  showPanel("summary");
  const roundWord = summary.total_rounds === 1 ? "round" : "rounds";
  el("cs-summary-heading").textContent = summary.status === "abandoned" ? "Game ended early" : "Game over!";
  el("cs-summary-subheading").textContent = `${summary.rounds_played} of ${summary.total_rounds} ${roundWord} played.`;
  const mine = summary.participants.find((participant) => participant.profile_id === myProfileId);
  if (mine)
    setSessionPoints(mine.total_points_this_session);
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
function connectSessionSocket() {
  if (state.ws || state.sessionId === null)
    return;
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  state.ws = new WebSocket(`${proto}${location.host}/ws/consensus/session/${state.sessionId}/`);
  state.ws.addEventListener("message", (event) => {
    try {
      handleSocketMessage(JSON.parse(event.data));
    } catch {}
  });
  state.ws.addEventListener("close", () => {
    state.ws = null;
  });
  el("cs-chat-panel").hidden = false;
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
    case "answer.submitted":
      markAnswered(data.profile_id);
      break;
    case "vote.submitted":
      markVoted(data.profile_id);
      break;
    case "round.revealed":
      renderReveal(data);
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
  const log = el("cs-chat-log");
  const line = document.createElement("div");
  const nameSpan = document.createElement("span");
  nameSpan.className = "consensus-chat-username";
  nameSpan.textContent = `${message.username}:`;
  line.append(nameSpan, document.createTextNode(message.body));
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}
async function loadChatHistory() {
  if (state.sessionId === null)
    return;
  const data = await getJson(urlFor(urls.chat_history, state.sessionId));
  const log = el("cs-chat-log");
  log.innerHTML = "";
  for (const message of data.messages ?? [])
    appendChatMessage(message);
}
function initChat() {
  el("cs-chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = el("cs-chat-input");
    const body = input.value.trim();
    if (!body || !state.ws)
      return;
    state.ws.send(JSON.stringify({ body }));
    input.value = "";
  });
}
function _resetSessionState() {
  bankSessionPoints();
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
function resetToSettings() {
  _resetSessionState();
  showPanel("settings");
}
function showNoEligibleWikis() {
  _resetSessionState();
  showPanel("empty");
}
async function startGame() {
  state.scoreboard = [];
  const body = new URLSearchParams({ total_rounds: el("cs-rounds").value });
  for (const profileId of state.selectedInviteIds)
    body.append("invite_profile_ids", String(profileId));
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
  state.totalRounds = Number(el("cs-rounds").value);
  if (response.lobby) {
    renderLobby(response.session);
    return;
  }
  state.isMultiplayer = false;
  if (response.round) {
    renderRound(response.round, response.round.sequence_index + 1);
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
  const response = await withBusy(el("cs-end-game-btn"), () => postForm(urlFor(urls.end, state.sessionId), {}));
  if (response.error) {
    toast.error(response.error);
    return;
  }
  showSummary(response.summary);
}
async function loadInitialSession() {
  const raw = pageEl?.dataset.initialSessionId;
  if (!raw)
    return;
  state.sessionId = Number(raw);
  state.isMultiplayer = true;
  const lobby = await getJson(urlFor(urls.lobby, state.sessionId));
  state.totalRounds = lobby.total_rounds;
  state.hostProfileId = lobby.host_profile_id;
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
  const data = await getJson(urlFor(urls.round, state.sessionId));
  if (data.no_eligible_wikis) {
    showNoEligibleWikis();
  } else if (data.finished) {
    showSummary(data.summary);
  } else if (data.round) {
    renderRound(data.round, data.round.sequence_index + 1);
  }
}
function initGameShell() {
  const shellEl = optionalEl("cs-shell");
  if (!pageEl || !shellEl)
    return;
  gameShell = createGameShell({
    root: pageEl,
    shell: shellEl,
    panels: PANEL_IDS,
    playingPanels: ["game", "summary"],
    onResize: () => state.roundMap?.invalidateSize()
  });
  gameShell.showPanel("settings");
}
function init() {
  initGameShell();
  initProgression();
  const startForm = el("cs-start-form");
  const startBtn = startForm.querySelector('button[type="submit"]');
  startForm.addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy(startBtn, startGame);
  });
  el("cs-submit-answer-btn").addEventListener("click", () => void submitAnswer());
  el("cs-skip-btn").addEventListener("click", () => void skipRound());
  el("cs-photo-upload-btn").addEventListener("click", () => void withBusy(el("cs-photo-upload-btn"), uploadPhoto));
  el("cs-photo-upload-input").addEventListener("change", showChosenPhotoName);
  el("cs-join-lobby-btn").addEventListener("click", () => void withBusy(el("cs-join-lobby-btn"), joinLobby));
  el("cs-begin-btn").addEventListener("click", () => void withBusy(el("cs-begin-btn"), beginGame));
  el("cs-invite-more-btn").addEventListener("click", () => void withBusy(el("cs-invite-more-btn"), handleInviteMore));
  el("cs-end-game-btn").addEventListener("click", () => void endGameNow());
  el("cs-play-again-btn").addEventListener("click", resetToSettings);
  el("cs-empty-state-settings-btn").addEventListener("click", resetToSettings);
  el("cs-friend-list-retry").addEventListener("click", () => void fetchFriendsEagerly());
  el("cs-answer-text-input").addEventListener("input", updateSubmitEnabled);
  el("cs-answer-textarea-input").addEventListener("input", updateSubmitEnabled);
  el("cs-answer-select-input").addEventListener("change", updateSubmitEnabled);
  initChat();
  fetchFriendsEagerly();
  loadInitialSession();
}
document.addEventListener("DOMContentLoaded", init);
