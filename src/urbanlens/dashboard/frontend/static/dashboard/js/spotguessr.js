import {
  getCsrfToken
} from "./article-wysiwyg-y9qpab7g.js";
import"./article-wysiwyg-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/entries/spotguessr.ts
var urls = window.SPOTGUESSR_URLS;
var DEFAULT_CENTER = [39.5, -98.35];
var DEFAULT_ZOOM = 4;
var sessionId = null;
var currentRoundId = null;
var totalRounds = 0;
var sessionScore = 0;
var dateGuessingEnabled = false;
var guessMap = null;
var guessMarker = null;
var actualMarker = null;
var resultLine = null;
var areaMap = null;
var areaDrawnItems = null;
var pinOptions = [];
function el(id) {
  const found = document.getElementById(id);
  if (!found)
    throw new Error(`SpotGuessr: missing #${id}`);
  return found;
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
function initDifficultySlider() {
  const slider = el("sg-difficulty");
  const label = el("sg-difficulty-label");
  const describe = (value) => value < 33 ? "Easy" : value < 66 ? "Medium" : "Hard";
  slider.addEventListener("input", () => {
    label.textContent = describe(Number(slider.value));
  });
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
function renderRound(round, roundNumber) {
  currentRoundId = round.round_id;
  el("sg-settings-panel").hidden = true;
  el("sg-summary-panel").hidden = true;
  el("sg-game-panel").hidden = false;
  el("sg-reveal-panel").hidden = true;
  el("sg-round-status").textContent = `Round ${roundNumber} of ${totalRounds}`;
  el("sg-score-status").textContent = `Score: ${sessionScore}`;
  el("sg-round-photo").src = round.image_url ?? "";
  el("sg-date-field").hidden = !dateGuessingEnabled;
  resetGuessMap();
  setTimeout(() => guessMap?.invalidateSize(), 0);
}
function showReveal(reveal) {
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
  el("sg-submit-guess-btn").disabled = true;
  el("sg-reveal-panel").hidden = false;
  el("sg-reveal-title").textContent = reveal.location_name || "Revealed!";
  const distanceKm = (reveal.distance_meters / 1000).toFixed(2);
  let detail = `${reveal.points} points – ${distanceKm} km away`;
  if (reveal.date_points)
    detail += ` (+${reveal.date_points} for the date guess)`;
  el("sg-reveal-detail").textContent = detail;
  sessionScore += reveal.points + reveal.date_points;
  el("sg-score-status").textContent = `Score: ${sessionScore}`;
}
function showSummary(summary) {
  el("sg-game-panel").hidden = true;
  el("sg-summary-panel").hidden = false;
  const [mine] = summary.participants;
  el("sg-summary-score").textContent = mine ? `You scored ${mine.total_points} points over ${summary.rounds_played} round${summary.rounds_played === 1 ? "" : "s"}.` : "";
}
async function startGame(event) {
  event.preventDefault();
  dateGuessingEnabled = el("sg-date-guessing").checked;
  sessionScore = 0;
  const geoBounds = el("sg-restrict-area").checked ? currentGeoBoundsGeoJson() : null;
  const payload = {
    difficulty: String(Number(el("sg-difficulty").value) / 100),
    total_rounds: el("sg-rounds").value,
    external_media_only: el("sg-external-media-only").checked ? "on" : "off",
    require_visited_all: el("sg-require-visited-all").checked ? "on" : "off",
    date_guessing_enabled: dateGuessingEnabled ? "on" : "off"
  };
  if (geoBounds)
    payload.geo_bounds = geoBounds;
  const response = await postForm(urls.start, payload);
  if (response.error) {
    window.alert(response.error);
    return;
  }
  sessionId = response.session_id;
  totalRounds = Number(el("sg-rounds").value);
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
  const reveal = await postForm(`/spotguessr/session/${sessionId}/round/${currentRoundId}/guess/`, payload);
  if (reveal.error) {
    window.alert(reveal.error);
    return;
  }
  showReveal(reveal);
}
async function goToNextRound() {
  if (sessionId === null)
    return;
  const data = await getJson(`/spotguessr/session/${sessionId}/round/`);
  if (data.finished) {
    showSummary(data.summary);
    return;
  }
  renderRound(data.round, data.round.sequence_index + 1);
}
function resetToSettings() {
  sessionId = null;
  currentRoundId = null;
  sessionScore = 0;
  el("sg-summary-panel").hidden = true;
  el("sg-game-panel").hidden = true;
  el("sg-settings-panel").hidden = false;
}
initDifficultySlider();
initRatingsToggle();
initAreaRestriction();
initPinSearch();
el("sg-start-form").addEventListener("submit", (event) => void startGame(event));
el("sg-submit-guess-btn").addEventListener("click", () => void submitGuess());
el("sg-next-round-btn").addEventListener("click", () => void goToNextRound());
el("sg-play-again-btn").addEventListener("click", resetToSettings);
