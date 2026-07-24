/**
 * SpotGuessr (UL-391) - solo Photos-mode gameplay.
 *
 * Server-authoritative: this file never computes a score or reveals an
 * answer itself - it only collects a guess (map click or pin search),
 * posts it, and renders whatever `services.spotguessr.session` decided.
 */
import { getCsrfToken } from "../shared/csrf";

declare const L: typeof import("leaflet");
import type {} from "leaflet-draw";

declare global {
    interface Window {
        SPOTGUESSR_URLS: { start: string; pins: string; settings: string };
    }
}

interface RoundPayload {
    round_id: number;
    session_id: number;
    sequence_index: number;
    revealed: boolean;
    image_url?: string;
}

interface RevealPayload {
    distance_meters: number;
    points: number;
    date_points: number;
    actual_latitude: number;
    actual_longitude: number;
    location_name: string;
    error?: string;
}

interface SummaryParticipant {
    profile_id: number;
    username: string;
    total_points: number;
}

interface SummaryPayload {
    session_id: number;
    rounds_played: number;
    total_rounds: number;
    participants: SummaryParticipant[];
}

interface PinOption {
    label: string;
    latitude: number;
    longitude: number;
}

const urls = window.SPOTGUESSR_URLS;
const DEFAULT_CENTER: L.LatLngExpression = [39.5, -98.35];
const DEFAULT_ZOOM = 4;

let sessionId: number | null = null;
let currentRoundId: number | null = null;
let totalRounds = 0;
let sessionScore = 0;
let dateGuessingEnabled = false;

let guessMap: L.Map | null = null;
let guessMarker: L.Marker | null = null;
let actualMarker: L.Marker | null = null;
let resultLine: L.Polyline | null = null;

let areaMap: L.Map | null = null;
let areaDrawnItems: L.FeatureGroup | null = null;

let pinOptions: PinOption[] = [];

function el<T extends HTMLElement = HTMLElement>(id: string): T {
    const found = document.getElementById(id);
    if (!found) throw new Error(`SpotGuessr: missing #${id}`);
    return found as T;
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

function initDifficultySlider(): void {
    const slider = el<HTMLInputElement>("sg-difficulty");
    const label = el("sg-difficulty-label");
    const describe = (value: number): string => (value < 33 ? "Easy" : value < 66 ? "Medium" : "Hard");
    slider.addEventListener("input", () => {
        label.textContent = describe(Number(slider.value));
    });
}

function initRatingsToggle(): void {
    const checkbox = el<HTMLInputElement>("sg-show-ratings-to-friends");
    checkbox.addEventListener("change", () => {
        void postForm(urls.settings, { show_ratings_to_friends: checkbox.checked ? "on" : "off" });
    });
}

function initAreaRestriction(): void {
    const toggle = el<HTMLInputElement>("sg-restrict-area");
    const wrap = el("sg-area-map-wrap");
    toggle.addEventListener("change", () => {
        wrap.hidden = !toggle.checked;
        if (!toggle.checked) return;
        if (areaMap) {
            areaMap.invalidateSize();
            return;
        }
        areaMap = L.map("sg-area-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(areaMap);
        areaDrawnItems = new L.FeatureGroup();
        areaMap.addLayer(areaDrawnItems);
        const drawControl = new L.Control.Draw({
            draw: { rectangle: {}, polygon: false, circle: false, marker: false, polyline: false, circlemarker: false },
            edit: { featureGroup: areaDrawnItems },
        });
        areaMap.addControl(drawControl);
        areaMap.on(L.Draw.Event.CREATED, (event: L.LeafletEvent) => {
            const { layer } = event as unknown as { layer: L.Layer };
            areaDrawnItems?.clearLayers();
            areaDrawnItems?.addLayer(layer);
        });
    });
}

function currentGeoBoundsGeoJson(): string | null {
    if (!areaDrawnItems) return null;
    const [layer] = areaDrawnItems.getLayers();
    if (!layer || !("toGeoJSON" in layer)) return null;
    const feature = (layer as L.Polygon).toGeoJSON();
    return JSON.stringify(feature.geometry);
}

async function loadPinOptions(): Promise<void> {
    const data = await getJson(urls.pins);
    pinOptions = data.pins ?? [];
    const datalist = el<HTMLDataListElement>("sg-pin-options");
    datalist.innerHTML = "";
    for (const pin of pinOptions) {
        const option = document.createElement("option");
        option.value = pin.label;
        datalist.appendChild(option);
    }
}

function initPinSearch(): void {
    const input = el<HTMLInputElement>("sg-pin-search");
    input.addEventListener("change", () => {
        const match = pinOptions.find((pin) => pin.label === input.value);
        if (match) placeGuessMarker(L.latLng(match.latitude, match.longitude));
    });
}

function ensureGuessMap(): L.Map {
    if (guessMap) return guessMap;
    guessMap = L.map("sg-guess-map").setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors" }).addTo(guessMap);
    guessMap.on("click", (event) => placeGuessMarker(event.latlng));
    return guessMap;
}

function placeGuessMarker(latlng: L.LatLng): void {
    const map = ensureGuessMap();
    if (guessMarker) {
        guessMarker.setLatLng(latlng);
    } else {
        guessMarker = L.marker(latlng, { draggable: true }).addTo(map);
    }
    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = false;
}

function resetGuessMap(): void {
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
    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = true;
    map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
}

function renderRound(round: RoundPayload, roundNumber: number): void {
    currentRoundId = round.round_id;
    el("sg-settings-panel").hidden = true;
    el("sg-summary-panel").hidden = true;
    el("sg-game-panel").hidden = false;
    el("sg-reveal-panel").hidden = true;
    el("sg-round-status").textContent = `Round ${roundNumber} of ${totalRounds}`;
    el("sg-score-status").textContent = `Score: ${sessionScore}`;
    el<HTMLImageElement>("sg-round-photo").src = round.image_url ?? "";
    el("sg-date-field").hidden = !dateGuessingEnabled;
    resetGuessMap();
    // The map container was hidden (display:none) while the settings panel
    // was showing, so Leaflet needs a nudge once it's visible again.
    setTimeout(() => guessMap?.invalidateSize(), 0);
}

function showReveal(reveal: RevealPayload): void {
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

    el<HTMLButtonElement>("sg-submit-guess-btn").disabled = true;
    el("sg-reveal-panel").hidden = false;
    el("sg-reveal-title").textContent = reveal.location_name || "Revealed!";
    const distanceKm = (reveal.distance_meters / 1000).toFixed(2);
    let detail = `${reveal.points} points – ${distanceKm} km away`;
    if (reveal.date_points) detail += ` (+${reveal.date_points} for the date guess)`;
    el("sg-reveal-detail").textContent = detail;

    sessionScore += reveal.points + reveal.date_points;
    el("sg-score-status").textContent = `Score: ${sessionScore}`;
}

function showSummary(summary: SummaryPayload): void {
    el("sg-game-panel").hidden = true;
    el("sg-summary-panel").hidden = false;
    const [mine] = summary.participants;
    el("sg-summary-score").textContent = mine
        ? `You scored ${mine.total_points} points over ${summary.rounds_played} round${summary.rounds_played === 1 ? "" : "s"}.`
        : "";
}

async function startGame(event: Event): Promise<void> {
    event.preventDefault();
    dateGuessingEnabled = el<HTMLInputElement>("sg-date-guessing").checked;
    sessionScore = 0;

    const geoBounds = el<HTMLInputElement>("sg-restrict-area").checked ? currentGeoBoundsGeoJson() : null;
    const payload: Record<string, string> = {
        difficulty: String(Number(el<HTMLInputElement>("sg-difficulty").value) / 100),
        total_rounds: el<HTMLInputElement>("sg-rounds").value,
        external_media_only: el<HTMLInputElement>("sg-external-media-only").checked ? "on" : "off",
        require_visited_all: el<HTMLInputElement>("sg-require-visited-all").checked ? "on" : "off",
        date_guessing_enabled: dateGuessingEnabled ? "on" : "off",
    };
    if (geoBounds) payload.geo_bounds = geoBounds;

    const response = await postForm(urls.start, payload);
    if (response.error) {
        window.alert(response.error);
        return;
    }

    sessionId = response.session_id;
    totalRounds = Number(el<HTMLInputElement>("sg-rounds").value);

    if (response.finished) {
        showSummary(response.summary);
        return;
    }

    await loadPinOptions();
    renderRound(response.round, response.round.sequence_index + 1);
}

async function submitGuess(): Promise<void> {
    if (!guessMarker || sessionId === null || currentRoundId === null) return;
    const latlng = guessMarker.getLatLng();
    const payload: Record<string, string> = { latitude: String(latlng.lat), longitude: String(latlng.lng) };
    if (dateGuessingEnabled) {
        const dateValue = el<HTMLInputElement>("sg-guessed-date").value;
        if (dateValue) payload.guessed_date = dateValue;
    }

    const reveal: RevealPayload = await postForm(`/spotguessr/session/${sessionId}/round/${currentRoundId}/guess/`, payload);
    if (reveal.error) {
        window.alert(reveal.error);
        return;
    }
    showReveal(reveal);
}

async function goToNextRound(): Promise<void> {
    if (sessionId === null) return;
    const data = await getJson(`/spotguessr/session/${sessionId}/round/`);
    if (data.finished) {
        showSummary(data.summary);
        return;
    }
    renderRound(data.round, data.round.sequence_index + 1);
}

function resetToSettings(): void {
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
