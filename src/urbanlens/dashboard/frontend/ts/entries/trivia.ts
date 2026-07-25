/**
 * Trivia (Phase 1: solo play only) - gameplay loop.
 *
 * Server-authoritative: this file never decides whether an answer is
 * correct or computes points itself - it only collects an answer, posts it,
 * and renders whatever `services.trivia.session` decided. Mirrors
 * spotguessr.ts's fetch/render shape, minus the map/photo/lobby machinery
 * Trivia doesn't need.
 */
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";

declare global {
    interface Window {
        TRIVIA_URLS: {
            start: string;
            round: string;
            answer: string;
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
    question_id: number;
    sequence_index: number;
    prompt: string;
    revealed_at: string | null;
}

interface StartOrRoundResponse {
    session_id?: number;
    finished: boolean;
    round?: RoundPayload;
    no_eligible_questions?: boolean;
    error_code?: string;
    summary?: SummaryPayload;
}

interface RevealResponse {
    round_id: number;
    question_id: number;
    is_correct: boolean;
    points: number;
    answer?: string;
    error?: string;
}

interface SummaryPayload {
    session_id: number;
    status: string;
    total_rounds: number;
    rounds_played: number;
    participants: { profile_id: number; username: string; total_points: number }[];
}

const urls = window.TRIVIA_URLS;

let sessionId: number | null = null;
let currentRound: RoundPayload | null = null;

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
    for (const panelId of ["trivia-settings-panel", "trivia-empty-state", "trivia-round-panel", "trivia-summary-panel"]) {
        el(panelId).hidden = panelId !== id;
    }
}

function renderRound(round: RoundPayload): void {
    currentRound = round;
    el<HTMLParagraphElement>("trivia-round-index").textContent = `Question ${round.sequence_index + 1}`;
    el<HTMLHeadingElement>("trivia-prompt").textContent = round.prompt;
    el<HTMLInputElement>("trivia-answer-input").value = "";
    el<HTMLFormElement>("trivia-answer-form").hidden = false;
    el("trivia-reveal").hidden = true;
    showPanel("trivia-round-panel");
    el<HTMLInputElement>("trivia-answer-input").focus();
}

function renderSummary(summary: SummaryPayload): void {
    const mine = summary.participants[0];
    el<HTMLParagraphElement>("trivia-summary-score").textContent = mine
        ? `You scored ${mine.total_points} points across ${summary.rounds_played} rounds.`
        : `Finished - ${summary.rounds_played} rounds played.`;
    showPanel("trivia-summary-panel");
}

async function handleStartOrRoundResponse(payload: StartOrRoundResponse): Promise<void> {
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
    const payload = await postForm(urls.start, { difficulty, total_rounds: totalRounds });
    await handleStartOrRoundResponse(payload);
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
    el<HTMLParagraphElement>("trivia-reveal-result").textContent = payload.is_correct
        ? `Correct! +${payload.points} points`
        : `Not quite - the answer was "${payload.answer ?? "unknown"}".`;
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

    document.querySelectorAll<HTMLButtonElement>("[data-vote]").forEach((button) => {
        button.addEventListener("click", () => void voteOnCurrentQuestion(button.dataset.vote as string));
    });

    if (window.TRIVIA_LAST_CONFIG?.difficulty !== undefined) {
        el<HTMLInputElement>("trivia-difficulty").value = String(window.TRIVIA_LAST_CONFIG.difficulty);
    }
}

document.addEventListener("DOMContentLoaded", init);
