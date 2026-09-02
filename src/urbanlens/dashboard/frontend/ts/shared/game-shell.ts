/**
 * Cross-game shell behaviour for SpotGuessr / Trivia / Consensus.
 *
 * Owns panel swapping, the progress rail, focus mode, true fullscreen, the
 * players/chat drawer, and the resize hooks Leaflet needs. Each game entry
 * constructs one of these with its own root, shell and panel-id map; nothing
 * here knows anything game-specific.
 *
 * Two independent immersion tiers:
 *   1. Focus mode - CSS only (`.is-immersive` + `body.ul-game-immersive`), so
 *      iOS Safari, which has no Element.requestFullscreen, degrades instead of
 *      breaking.
 *   2. True fullscreen - requestFullscreen() on the shell element itself. The
 *      shell never leaves the DOM, so `.ul-game [hidden]` and the
 *      `body.page-<game>` custom properties keep applying inside it.
 */

import { isTypingTarget, matchesHotkey } from "./hotkeys";

export interface GameShellOptions {
    /** The page root - `.ul-game`, carrying the game's legacy page class. */
    root: HTMLElement;
    /** `.ul-game-shell` - the element promoted to fullscreen and pinned in focus mode. */
    shell: HTMLElement;
    /** Panel name -> element id. Names are the game's own vocabulary. */
    panels: Record<string, string>;
    /** Panel names that count as "in play" (HUD visible, focus mode auto-engaged). Defaults to `["game"]`. */
    playingPanels?: string[];
    /** Called whenever the stage box may have changed - invalidate any Leaflet maps here. */
    onResize?: () => void;
}

export interface GameShell {
    /** Show exactly one panel, animating the outgoing one out. Never throws. */
    showPanel(name: string): void;
    currentPanel(): string | null;
    /** Drives `--ul-game-progress` (0..1) on the shell. */
    setProgress(current: number, total: number): void;
    setImmersive(on: boolean): void;
    isImmersive(): boolean;
    isFullscreen(): boolean;
    /**
     * Shows or hides the players/chat drawer and its HUD toggle together.
     *
     * The rail only ever holds multiplayer content, so a solo game must turn it
     * off rather than offer a button that opens an empty drawer.
     */
    setRailAvailable(available: boolean): void;
    /** Appends a node into the shell so it survives true fullscreen, and returns it. */
    mountOverlay<T extends HTMLElement>(node: T): T;
    /** Adds `.is-flash-good` / `.is-flash-bad` to `[data-game-stage]`; self-clearing. */
    flashStage(kind: "good" | "bad"): void;
    reducedMotion(): boolean;
    /** Reads a `--ul-game-*` custom property off the root, e.g. `cssVar("bad")`. */
    cssVar(name: string): string;
}

/** Vendor-prefixed fullscreen surface, still the only one WebKit exposes on some versions. */
interface WebkitFullscreenElement extends HTMLElement {
    webkitRequestFullscreen?: () => Promise<void> | void;
}

interface WebkitFullscreenDocument extends Document {
    webkitExitFullscreen?: () => Promise<void> | void;
    webkitFullscreenElement?: Element | null;
}

/** Where a node lived before being adopted into the shell for true fullscreen. */
interface AdoptedNode {
    node: HTMLElement;
    parent: Node;
    nextSibling: Node | null;
}

/** Everything that has to be torn down when a panel stops leaving. */
interface LeaveHandle {
    timer: number;
    /** Aborts the animation listeners so a re-shown panel cannot inherit them. */
    controller: AbortController;
}

const IMMERSIVE_PREFERENCE_KEY = "ul_game_immersive";
const PANEL_LEAVE_MS = 200;
const PANEL_LEAVE_ANIMATION = "ul-game-panel-out";
const FULLSCREEN_SETTLE_MS = 200;
const IMMERSIVE_SETTLE_MS = 260;
const RESIZE_DEBOUNCE_MS = 100;
const STAGE_FLASH_MS = 700;
/** Nodes base.html owns that must be pulled into the fullscreen element to stay painted. */
const ADOPTED_OVERLAY_IDS = ["toast-container", "confirm-dialog"];

function readImmersivePreference(): boolean {
    try {
        return window.localStorage.getItem(IMMERSIVE_PREFERENCE_KEY) !== "0";
    } catch {
        return true;
    }
}

function writeImmersivePreference(on: boolean): void {
    try {
        window.localStorage.setItem(IMMERSIVE_PREFERENCE_KEY, on ? "1" : "0");
    } catch {
        /* Private-mode storage denial is not worth surfacing. */
    }
}

/**
 * Replays an element's `.is-entering` animation.
 *
 * The play panel is never re-mounted between rounds, so an animation declared
 * straight on a clue/photo fires exactly once - when the panel is first shown -
 * and every later round swaps its content with no transition at all. The
 * entrance therefore hangs off a class the round renderer re-applies.
 *
 * Args:
 *     element: The element to replay, or null when the game has none this round.
 *     skip: True under prefers-reduced-motion; leaves the class off entirely.
 */
export function playEntrance(element: HTMLElement | null | undefined, skip = false): void {
    if (!element) {
        return;
    }
    element.classList.remove("is-entering");
    if (skip) {
        return;
    }
    // Forcing layout between the two class writes is what restarts the animation.
    void element.offsetWidth;
    element.classList.add("is-entering");
}

export function createGameShell(opts: GameShellOptions): GameShell {
    const { root, shell } = opts;
    const playingPanels = opts.playingPanels ?? ["game"];
    const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

    const leaving = new Map<HTMLElement, LeaveHandle>();
    const adopted: AdoptedNode[] = [];
    let overlayObserver: MutationObserver | null = null;
    let currentPanelName: string | null = null;
    let immersivePreference = readImmersivePreference();
    let immersive = false;
    let flashTimer = 0;

    const reducedMotion = (): boolean => motionQuery.matches;

    const fireResize = (): void => {
        opts.onResize?.();
    };

    const scheduleResize = (): void => {
        window.requestAnimationFrame(fireResize);
    };

    // -- Chrome measurement ------------------------------------------------------
    // .app-nav is position: relative (not sticky), so a scrolled page would
    // otherwise report a negative offset; every caller scrolls to top first.
    function syncChromeTop(): void {
        const nav = document.querySelector<HTMLElement>(".app-nav");
        const top = nav ? Math.max(0, Math.round(nav.getBoundingClientRect().bottom)) : 64;
        document.body.style.setProperty("--ul-game-chrome-top", `${top}px`);
    }

    // -- Fullscreen --------------------------------------------------------------
    const shellFs = shell as WebkitFullscreenElement;
    const doc = document as WebkitFullscreenDocument;
    const fullscreenSupported = Boolean(shellFs.requestFullscreen ?? shellFs.webkitRequestFullscreen);

    function fullscreenElement(): Element | null {
        return doc.fullscreenElement ?? doc.webkitFullscreenElement ?? null;
    }

    function isFullscreen(): boolean {
        return fullscreenElement() === shell;
    }

    function enterFullscreen(): void {
        const request = shellFs.requestFullscreen ?? shellFs.webkitRequestFullscreen;
        if (!request) {
            return;
        }
        try {
            void Promise.resolve(request.call(shell)).catch(() => {});
        } catch {
            /* Rejected without a user gesture, or disallowed by policy. */
        }
    }

    function exitFullscreen(): void {
        const exit = doc.exitFullscreen ?? doc.webkitExitFullscreen;
        if (!exit || !fullscreenElement()) {
            return;
        }
        try {
            void Promise.resolve(exit.call(document)).catch(() => {});
        } catch {
            /* Already exited. */
        }
    }

    // Nodes outside the fullscreen element are not painted at all, so toasts and
    // the shared confirm dialog would silently vanish mid-game. The original
    // next sibling is recorded, not just the parent, so repeated enter/exit
    // cycles cannot reorder base.html's DOM.
    function adoptOverlay(node: HTMLElement): void {
        if (shell.contains(node) || !node.parentNode) {
            return;
        }
        adopted.push({ node, parent: node.parentNode, nextSibling: node.nextSibling });
        shell.appendChild(node);
    }

    function adoptOverlays(): void {
        for (const id of ADOPTED_OVERLAY_IDS) {
            const node = document.getElementById(id);
            if (node) {
                adoptOverlay(node);
            }
        }
        if (overlayObserver) {
            return;
        }
        // toastr builds #toast-container lazily, on its first notification. A
        // player who enters fullscreen before any toast has fired would
        // otherwise have every later toast appended to document.body - outside
        // the fullscreen element, so never painted, so no error feedback at all.
        overlayObserver = new MutationObserver((records) => {
            for (const record of records) {
                for (const added of record.addedNodes) {
                    if (added instanceof HTMLElement && ADOPTED_OVERLAY_IDS.includes(added.id)) {
                        adoptOverlay(added);
                    }
                }
            }
        });
        overlayObserver.observe(document.body, { childList: true });
    }

    function releaseOverlays(): void {
        overlayObserver?.disconnect();
        overlayObserver = null;
        while (adopted.length > 0) {
            const entry = adopted.pop();
            if (!entry) {
                break;
            }
            entry.parent.insertBefore(entry.node, entry.nextSibling);
        }
    }

    // -- Immersive (focus mode) --------------------------------------------------
    function syncFocusButtons(): void {
        for (const button of shell.querySelectorAll<HTMLElement>("[data-game-focus-toggle]")) {
            button.setAttribute("aria-pressed", immersive ? "true" : "false");
            const label = immersive ? "Exit focus mode" : "Focus mode";
            button.setAttribute("aria-label", label);
            button.setAttribute("title", label);
            const icon = button.querySelector<HTMLElement>("[data-game-focus-icon]");
            if (icon) {
                icon.textContent = immersive ? "close_fullscreen" : "open_in_full";
            }
        }
    }

    function setImmersive(on: boolean): void {
        if (on === immersive) {
            syncFocusButtons();
            return;
        }
        immersive = on;
        if (on) {
            window.scrollTo(0, 0);
            syncChromeTop();
        } else {
            if (isFullscreen()) {
                exitFullscreen();
            }
            window.scrollTo(0, 0);
        }
        shell.classList.toggle("is-immersive", on);
        document.body.classList.toggle("ul-game-immersive", on);
        syncFocusButtons();
        scheduleResize();
        window.setTimeout(fireResize, IMMERSIVE_SETTLE_MS);
    }

    // -- Panels -------------------------------------------------------------------
    function panelElement(name: string): HTMLElement | null {
        const id = opts.panels[name];
        if (!id) {
            console.error(`[game-shell] no panel registered under "${name}"`);
            return null;
        }
        const element = document.getElementById(id);
        if (!element) {
            // Degrading one panel beats throwing and killing the whole page,
            // which is what the per-game el() helpers do today.
            console.error(`[game-shell] panel element #${id} is missing`);
            return null;
        }
        return element;
    }

    /**
     * Drops the pending hide-timer and animation listeners for `element`.
     *
     * Both exits from the leaving state go through here. The listeners are torn
     * down with the timer rather than left to fire once: the 200 ms guard beats
     * the .22s exit animation, and hiding the element cancels that animation
     * instead of ending it, so an `animationend` listener left armed would
     * survive until the panel is next shown - where the entrance animation would
     * then hide the panel that had just appeared.
     */
    function clearLeaving(element: HTMLElement): void {
        const handle = leaving.get(element);
        if (!handle) {
            return;
        }
        window.clearTimeout(handle.timer);
        handle.controller.abort();
        leaving.delete(element);
    }

    function finishLeaving(element: HTMLElement): void {
        clearLeaving(element);
        element.classList.remove("ul-game-panel--leaving");
        element.hidden = true;
    }

    function beginLeaving(element: HTMLElement): void {
        clearLeaving(element);
        element.classList.add("ul-game-panel--leaving");
        const controller = new AbortController();
        const timer = window.setTimeout(() => finishLeaving(element), PANEL_LEAVE_MS);
        leaving.set(element, { timer, controller });
        const settle = (event: AnimationEvent): void => {
            // Descendant entrances (stage, dock, score rows) bubble to here too.
            if (event.target !== element || event.animationName !== PANEL_LEAVE_ANIMATION) {
                return;
            }
            finishLeaving(element);
        };
        element.addEventListener("animationend", settle, { signal: controller.signal });
        element.addEventListener("animationcancel", settle, { signal: controller.signal });
    }

    function syncHudVisibility(name: string): void {
        for (const element of shell.querySelectorAll<HTMLElement>("[data-hud-when]")) {
            const when = element.dataset.hudWhen ?? "";
            element.hidden = !when.split(",").map((part) => part.trim()).includes(name);
        }
    }

    function showPanel(name: string): void {
        const next = panelElement(name);
        if (!next) {
            return;
        }

        const previousName = currentPanelName;
        const previous = previousName !== null && previousName !== name ? panelElement(previousName) : null;

        finishLeavingIfPending(next);

        for (const otherName of Object.keys(opts.panels)) {
            if (otherName === name) {
                continue;
            }
            const element = panelElement(otherName);
            if (!element) {
                continue;
            }
            if (element === previous && !reducedMotion() && !element.hidden) {
                beginLeaving(element);
            } else {
                finishLeaving(element);
            }
        }

        next.hidden = false;
        currentPanelName = name;

        const playing = playingPanels.includes(name);
        const wasPlaying = shell.classList.contains("is-playing");
        shell.classList.toggle("is-playing", playing);
        if (playing !== wasPlaying) {
            setImmersive(playing && immersivePreference);
        }

        syncHudVisibility(name);
        scheduleResize();
    }

    function finishLeavingIfPending(element: HTMLElement): void {
        clearLeaving(element);
        element.classList.remove("ul-game-panel--leaving");
    }

    // -- Rail drawer ----------------------------------------------------------------
    function setRailOpen(open: boolean): void {
        shell.classList.toggle("is-rail-open", open);
        for (const button of shell.querySelectorAll<HTMLElement>("[data-game-rail-toggle]")) {
            button.setAttribute("aria-pressed", open ? "true" : "false");
        }
        const scrim = shell.querySelector<HTMLElement>("[data-game-rail-scrim]");
        if (scrim) {
            scrim.hidden = !open;
        }
        scheduleResize();
    }

    function setRailAvailable(available: boolean): void {
        if (!available) {
            setRailOpen(false);
        }
        for (const rail of shell.querySelectorAll<HTMLElement>("[data-game-rail]")) {
            rail.hidden = !available;
        }
        for (const button of shell.querySelectorAll<HTMLElement>("[data-game-rail-toggle]")) {
            button.hidden = !available;
        }
    }

    // -- Wiring ----------------------------------------------------------------------
    function syncFullscreenButtons(): void {
        const active = isFullscreen();
        for (const button of shell.querySelectorAll<HTMLElement>("[data-game-fullscreen]")) {
            button.setAttribute("aria-pressed", active ? "true" : "false");
            button.setAttribute("aria-label", active ? "Exit full screen" : "Full screen");
            const icon = button.querySelector<HTMLElement>("[data-game-fullscreen-icon]");
            if (icon) {
                icon.textContent = active ? "fullscreen_exit" : "fullscreen";
            }
        }
    }

    function onFullscreenChange(): void {
        const active = isFullscreen();
        shell.classList.toggle("is-fullscreen", active);
        if (active) {
            adoptOverlays();
        } else {
            releaseOverlays();
        }
        syncFullscreenButtons();
        // Double rAF plus a timer: Leaflet needs the post-transition box, and
        // neither alone is reliable across engines here.
        window.requestAnimationFrame(() => window.requestAnimationFrame(fireResize));
        window.setTimeout(fireResize, FULLSCREEN_SETTLE_MS);
    }

    for (const button of shell.querySelectorAll<HTMLElement>("[data-game-fullscreen]")) {
        if (!fullscreenSupported) {
            button.hidden = true;
            continue;
        }
        button.addEventListener("click", () => {
            if (isFullscreen()) {
                exitFullscreen();
            } else {
                enterFullscreen();
            }
        });
    }

    // A toggle, not a one-way exit: leaving focus mode also writes the
    // preference, so without a way back the control would permanently disable
    // the immersive layout for that browser profile on all three games.
    for (const button of shell.querySelectorAll<HTMLElement>("[data-game-focus-toggle]")) {
        button.addEventListener("click", () => {
            const next = !immersive;
            immersivePreference = next;
            writeImmersivePreference(next);
            setImmersive(next);
        });
    }

    for (const button of shell.querySelectorAll<HTMLElement>("[data-game-rail-toggle]")) {
        button.addEventListener("click", () => setRailOpen(!shell.classList.contains("is-rail-open")));
    }

    for (const scrim of shell.querySelectorAll<HTMLElement>("[data-game-rail-scrim]")) {
        scrim.addEventListener("click", () => setRailOpen(false));
    }

    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange);

    // Esc in focus mode is deliberately not handled: it must never abandon a
    // live round. Native Esc still leaves true fullscreen.
    document.addEventListener("keydown", (event: KeyboardEvent) => {
        if (!matchesHotkey(event, "toggleFullscreen")) {
            return;
        }
        if (isTypingTarget(event.target)) {
            return;
        }
        if (!shell.classList.contains("is-playing")) {
            return;
        }
        event.preventDefault();
        if (isFullscreen()) {
            exitFullscreen();
        } else {
            enterFullscreen();
        }
    });

    window.addEventListener("resize", syncChromeTop);
    window.addEventListener("orientationchange", syncChromeTop);

    let resizeDebounce = 0;
    const observed = shell.querySelector<HTMLElement>("[data-game-stage]") ?? shell.querySelector<HTMLElement>("[data-game-body]");
    if (observed && typeof ResizeObserver !== "undefined") {
        const observer = new ResizeObserver(() => {
            window.clearTimeout(resizeDebounce);
            resizeDebounce = window.setTimeout(fireResize, RESIZE_DEBOUNCE_MS);
        });
        observer.observe(observed);
    }

    syncChromeTop();
    syncFullscreenButtons();
    syncFocusButtons();
    // Solo is the default state for every game; a game turns the rail back on
    // once it knows it has a socket and other players.
    setRailAvailable(false);

    return {
        showPanel,
        currentPanel: () => currentPanelName,
        setProgress(current: number, total: number): void {
            const ratio = total > 0 ? Math.min(1, Math.max(0, current / total)) : 0;
            shell.style.setProperty("--ul-game-progress", String(ratio));
        },
        setImmersive,
        isImmersive: () => immersive,
        isFullscreen,
        setRailAvailable,
        mountOverlay<T extends HTMLElement>(node: T): T {
            shell.appendChild(node);
            return node;
        },
        flashStage(kind: "good" | "bad"): void {
            const stage = shell.querySelector<HTMLElement>("[data-game-stage]");
            if (!stage || reducedMotion()) {
                return;
            }
            const className = kind === "good" ? "is-flash-good" : "is-flash-bad";
            stage.classList.remove("is-flash-good", "is-flash-bad");
            // Force a reflow so the animation restarts on a repeated verdict.
            void stage.offsetWidth;
            stage.classList.add(className);
            window.clearTimeout(flashTimer);
            flashTimer = window.setTimeout(() => stage.classList.remove("is-flash-good", "is-flash-bad"), STAGE_FLASH_MS);
        },
        reducedMotion,
        cssVar(name: string): string {
            return getComputedStyle(root).getPropertyValue(`--ul-game-${name}`).trim();
        },
    };
}
