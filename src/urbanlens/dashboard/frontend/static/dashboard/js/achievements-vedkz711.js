// src/urbanlens/dashboard/frontend/ts/shared/game-shell.ts
var IMMERSIVE_PREFERENCE_KEY = "ul_game_immersive";
var PANEL_LEAVE_MS = 200;
var PANEL_LEAVE_ANIMATION = "ul-game-panel-out";
var FULLSCREEN_SETTLE_MS = 200;
var IMMERSIVE_SETTLE_MS = 260;
var RESIZE_DEBOUNCE_MS = 100;
var STAGE_FLASH_MS = 700;
var ADOPTED_OVERLAY_IDS = ["toast-container", "confirm-dialog"];
function readImmersivePreference() {
  try {
    return window.localStorage.getItem(IMMERSIVE_PREFERENCE_KEY) !== "0";
  } catch {
    return true;
  }
}
function writeImmersivePreference(on) {
  try {
    window.localStorage.setItem(IMMERSIVE_PREFERENCE_KEY, on ? "1" : "0");
  } catch {}
}
function isTypingTarget(target) {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  if (target.isContentEditable) {
    return true;
  }
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}
function playEntrance(element, skip = false) {
  if (!element) {
    return;
  }
  element.classList.remove("is-entering");
  if (skip) {
    return;
  }
  element.offsetWidth;
  element.classList.add("is-entering");
}
function createGameShell(opts) {
  const { root, shell } = opts;
  const playingPanels = opts.playingPanels ?? ["game"];
  const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const leaving = new Map;
  const adopted = [];
  let overlayObserver = null;
  let currentPanelName = null;
  let immersivePreference = readImmersivePreference();
  let immersive = false;
  let flashTimer = 0;
  const reducedMotion = () => motionQuery.matches;
  const fireResize = () => {
    opts.onResize?.();
  };
  const scheduleResize = () => {
    window.requestAnimationFrame(fireResize);
  };
  function syncChromeTop() {
    const nav = document.querySelector(".app-nav");
    const top = nav ? Math.max(0, Math.round(nav.getBoundingClientRect().bottom)) : 64;
    document.body.style.setProperty("--ul-game-chrome-top", `${top}px`);
  }
  const shellFs = shell;
  const doc = document;
  const fullscreenSupported = Boolean(shellFs.requestFullscreen ?? shellFs.webkitRequestFullscreen);
  function fullscreenElement() {
    return doc.fullscreenElement ?? doc.webkitFullscreenElement ?? null;
  }
  function isFullscreen() {
    return fullscreenElement() === shell;
  }
  function enterFullscreen() {
    const request = shellFs.requestFullscreen ?? shellFs.webkitRequestFullscreen;
    if (!request) {
      return;
    }
    try {
      Promise.resolve(request.call(shell)).catch(() => {});
    } catch {}
  }
  function exitFullscreen() {
    const exit = doc.exitFullscreen ?? doc.webkitExitFullscreen;
    if (!exit || !fullscreenElement()) {
      return;
    }
    try {
      Promise.resolve(exit.call(document)).catch(() => {});
    } catch {}
  }
  function adoptOverlay(node) {
    if (shell.contains(node) || !node.parentNode) {
      return;
    }
    adopted.push({ node, parent: node.parentNode, nextSibling: node.nextSibling });
    shell.appendChild(node);
  }
  function adoptOverlays() {
    for (const id of ADOPTED_OVERLAY_IDS) {
      const node = document.getElementById(id);
      if (node) {
        adoptOverlay(node);
      }
    }
    if (overlayObserver) {
      return;
    }
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
  function releaseOverlays() {
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
  function syncFocusButtons() {
    for (const button of shell.querySelectorAll("[data-game-focus-toggle]")) {
      button.setAttribute("aria-pressed", immersive ? "true" : "false");
      const label = immersive ? "Exit focus mode" : "Focus mode";
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      const icon = button.querySelector("[data-game-focus-icon]");
      if (icon) {
        icon.textContent = immersive ? "close_fullscreen" : "open_in_full";
      }
    }
  }
  function setImmersive(on) {
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
  function panelElement(name) {
    const id = opts.panels[name];
    if (!id) {
      console.error(`[game-shell] no panel registered under "${name}"`);
      return null;
    }
    const element = document.getElementById(id);
    if (!element) {
      console.error(`[game-shell] panel element #${id} is missing`);
      return null;
    }
    return element;
  }
  function clearLeaving(element) {
    const handle = leaving.get(element);
    if (!handle) {
      return;
    }
    window.clearTimeout(handle.timer);
    handle.controller.abort();
    leaving.delete(element);
  }
  function finishLeaving(element) {
    clearLeaving(element);
    element.classList.remove("ul-game-panel--leaving");
    element.hidden = true;
  }
  function beginLeaving(element) {
    clearLeaving(element);
    element.classList.add("ul-game-panel--leaving");
    const controller = new AbortController;
    const timer = window.setTimeout(() => finishLeaving(element), PANEL_LEAVE_MS);
    leaving.set(element, { timer, controller });
    const settle = (event) => {
      if (event.target !== element || event.animationName !== PANEL_LEAVE_ANIMATION) {
        return;
      }
      finishLeaving(element);
    };
    element.addEventListener("animationend", settle, { signal: controller.signal });
    element.addEventListener("animationcancel", settle, { signal: controller.signal });
  }
  function syncHudVisibility(name) {
    for (const element of shell.querySelectorAll("[data-hud-when]")) {
      const when = element.dataset.hudWhen ?? "";
      element.hidden = !when.split(",").map((part) => part.trim()).includes(name);
    }
  }
  function showPanel(name) {
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
  function finishLeavingIfPending(element) {
    clearLeaving(element);
    element.classList.remove("ul-game-panel--leaving");
  }
  function setRailOpen(open) {
    shell.classList.toggle("is-rail-open", open);
    for (const button of shell.querySelectorAll("[data-game-rail-toggle]")) {
      button.setAttribute("aria-pressed", open ? "true" : "false");
    }
    const scrim = shell.querySelector("[data-game-rail-scrim]");
    if (scrim) {
      scrim.hidden = !open;
    }
    scheduleResize();
  }
  function setRailAvailable(available) {
    if (!available) {
      setRailOpen(false);
    }
    for (const rail of shell.querySelectorAll("[data-game-rail]")) {
      rail.hidden = !available;
    }
    for (const button of shell.querySelectorAll("[data-game-rail-toggle]")) {
      button.hidden = !available;
    }
  }
  function syncFullscreenButtons() {
    const active = isFullscreen();
    for (const button of shell.querySelectorAll("[data-game-fullscreen]")) {
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.setAttribute("aria-label", active ? "Exit full screen" : "Full screen");
      const icon = button.querySelector("[data-game-fullscreen-icon]");
      if (icon) {
        icon.textContent = active ? "fullscreen_exit" : "fullscreen";
      }
    }
  }
  function onFullscreenChange() {
    const active = isFullscreen();
    shell.classList.toggle("is-fullscreen", active);
    if (active) {
      adoptOverlays();
    } else {
      releaseOverlays();
    }
    syncFullscreenButtons();
    window.requestAnimationFrame(() => window.requestAnimationFrame(fireResize));
    window.setTimeout(fireResize, FULLSCREEN_SETTLE_MS);
  }
  for (const button of shell.querySelectorAll("[data-game-fullscreen]")) {
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
  for (const button of shell.querySelectorAll("[data-game-focus-toggle]")) {
    button.addEventListener("click", () => {
      const next = !immersive;
      immersivePreference = next;
      writeImmersivePreference(next);
      setImmersive(next);
    });
  }
  for (const button of shell.querySelectorAll("[data-game-rail-toggle]")) {
    button.addEventListener("click", () => setRailOpen(!shell.classList.contains("is-rail-open")));
  }
  for (const scrim of shell.querySelectorAll("[data-game-rail-scrim]")) {
    scrim.addEventListener("click", () => setRailOpen(false));
  }
  document.addEventListener("fullscreenchange", onFullscreenChange);
  document.addEventListener("webkitfullscreenchange", onFullscreenChange);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "f" && event.key !== "F") {
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey || isTypingTarget(event.target)) {
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
  const observed = shell.querySelector("[data-game-stage]") ?? shell.querySelector("[data-game-body]");
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
  setRailAvailable(false);
  return {
    showPanel,
    currentPanel: () => currentPanelName,
    setProgress(current, total) {
      const ratio = total > 0 ? Math.min(1, Math.max(0, current / total)) : 0;
      shell.style.setProperty("--ul-game-progress", String(ratio));
    },
    setImmersive,
    isImmersive: () => immersive,
    isFullscreen,
    setRailAvailable,
    mountOverlay(node) {
      shell.appendChild(node);
      return node;
    },
    flashStage(kind) {
      const stage = shell.querySelector("[data-game-stage]");
      if (!stage || reducedMotion()) {
        return;
      }
      const className = kind === "good" ? "is-flash-good" : "is-flash-bad";
      stage.classList.remove("is-flash-good", "is-flash-bad");
      stage.offsetWidth;
      stage.classList.add(className);
      window.clearTimeout(flashTimer);
      flashTimer = window.setTimeout(() => stage.classList.remove("is-flash-good", "is-flash-bad"), STAGE_FLASH_MS);
    },
    reducedMotion,
    cssVar(name) {
      return getComputedStyle(root).getPropertyValue(`--ul-game-${name}`).trim();
    }
  };
}

export { playEntrance, createGameShell };
