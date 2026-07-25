import {
  getCsrfToken,
  toast
} from "./article-wysiwyg-5jnnp4sj.js";
import"./article-wysiwyg-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/entries/trivia.ts
var urls = window.TRIVIA_URLS;
var sessionId = null;
var currentRound = null;
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
  for (const panelId of ["trivia-settings-panel", "trivia-empty-state", "trivia-round-panel", "trivia-summary-panel"]) {
    el(panelId).hidden = panelId !== id;
  }
}
function renderRound(round) {
  currentRound = round;
  el("trivia-round-index").textContent = `Question ${round.sequence_index + 1}`;
  el("trivia-prompt").textContent = round.prompt;
  el("trivia-answer-input").value = "";
  el("trivia-answer-form").hidden = false;
  el("trivia-reveal").hidden = true;
  showPanel("trivia-round-panel");
  el("trivia-answer-input").focus();
}
function renderSummary(summary) {
  const mine = summary.participants[0];
  el("trivia-summary-score").textContent = mine ? `You scored ${mine.total_points} points across ${summary.rounds_played} rounds.` : `Finished - ${summary.rounds_played} rounds played.`;
  showPanel("trivia-summary-panel");
}
async function handleStartOrRoundResponse(payload) {
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
  const payload = await postForm(urls.start, { difficulty, total_rounds: totalRounds });
  await handleStartOrRoundResponse(payload);
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
  el("trivia-reveal-result").textContent = payload.is_correct ? `Correct! +${payload.points} points` : `Not quite - the answer was "${payload.answer ?? "unknown"}".`;
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
  document.querySelectorAll("[data-vote]").forEach((button) => {
    button.addEventListener("click", () => void voteOnCurrentQuestion(button.dataset.vote));
  });
  if (window.TRIVIA_LAST_CONFIG?.difficulty !== undefined) {
    el("trivia-difficulty").value = String(window.TRIVIA_LAST_CONFIG.difficulty);
  }
}
document.addEventListener("DOMContentLoaded", init);
