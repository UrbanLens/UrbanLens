/**
 * Two-thumb range sliders (``partials/ui/_dual_range_slider.html``): the map sidebar's and the saved-filter
 * form's rating and score ranges.
 *
 * Each ``[data-ul-dual-range-slider]`` holds two range inputs (``data-role="min"``/``"max"``), a fill bar, a
 * label, and hidden inputs that carry the bounds, left empty at either end of the range so an untouched
 * slider filters nothing.
 */

import { htmxDetail } from "./htmx-events";

function part<T extends HTMLElement>(root: ParentNode, role: string, type: new () => T): T | null {
    const el = root.querySelector(`[data-role="${role}"]`);
    return el instanceof type ? el : null;
}

function intAttr(el: Element, name: string, fallback: number): number {
    const raw = el.getAttribute(name);
    if (raw === null || raw === "") return fallback;
    const value = Number.parseInt(raw, 10);
    return Number.isFinite(value) ? value : fallback;
}

function atFullRange(root: Element): boolean {
    const min = part(root, "min", HTMLInputElement);
    const max = part(root, "max", HTMLInputElement);
    if (!min || !max) return true;
    return Number.parseInt(min.value, 10) === Number.parseInt(min.min, 10) && Number.parseInt(max.value, 10) === Number.parseInt(max.max, 10);
}

function formatValue(format: string, value: number, max: number): string {
    return format === "stars" ? "★".repeat(value) + "☆".repeat(Math.max(0, max - value)) : String(value);
}

/** What the label says for a range of *lo* to *hi* within *min* to *max*. */
export function rangeLabel(format: string, lo: number, hi: number, min: number, max: number): string {
    if (lo === min && hi === max) return "Any";
    if (lo === min) return `up to ${formatValue(format, hi, max)}`;
    if (hi === max) return `${formatValue(format, lo, max)} or higher`;
    return `${formatValue(format, lo, max)} - ${formatValue(format, hi, max)}`;
}

/** Bring the fill, label and hidden inputs in line with the thumbs, and stop the thumbs crossing. */
export function syncSlider(root: HTMLElement): void {
    const minSlider = part(root, "min", HTMLInputElement);
    const maxSlider = part(root, "max", HTMLInputElement);
    const fill = part(root, "fill", HTMLElement);
    if (!minSlider || !maxSlider || !fill) return;
    const min = Number.parseInt(minSlider.min, 10);
    const max = Number.parseInt(maxSlider.max, 10);
    let lo = Number.parseInt(minSlider.value, 10);
    let hi = Number.parseInt(maxSlider.value, 10);
    if (lo > hi) {
        // The thumb being dragged stops at the other one.
        if (document.activeElement === minSlider) {
            lo = hi;
            minSlider.value = String(lo);
        } else {
            hi = lo;
            maxSlider.value = String(hi);
        }
    }
    const span = max - min || 1;
    fill.style.left = `${((lo - min) / span) * 100}%`;
    fill.style.width = `${((hi - lo) / span) * 100}%`;
    // At the bottom of the range the min thumb sits under the max one; lift it so it can still be grabbed.
    minSlider.style.zIndex = lo === min ? "5" : "3";
    maxSlider.style.zIndex = lo === min ? "3" : "5";

    const label = part(root, "value-label", HTMLElement);
    if (label) label.textContent = rangeLabel(root.dataset.format ?? "default", lo, hi, min, max);
    const minHidden = part(root, "min-hidden", HTMLInputElement);
    if (minHidden) minHidden.value = lo === min ? "" : String(lo);
    const maxHidden = part(root, "max-hidden", HTMLInputElement);
    if (maxHidden) maxHidden.value = hi === max ? "" : String(hi);

    // A sidebar accordion is marked active while any of its sliders is narrowed.
    const accordion = root.dataset.accordion ? document.getElementById(root.dataset.accordion) : null;
    if (accordion) {
        const active = Array.from(accordion.querySelectorAll("[data-ul-dual-range-slider]")).some((slider) => !atFullRange(slider));
        accordion.classList.toggle("fp-acc-active", active);
    }
}

/** A slider outside the form it filters tells that form itself; inside it, the native change already has. */
function notifyForm(root: HTMLElement): void {
    const form = root.dataset.htmxForm ? document.getElementById(root.dataset.htmxForm) : null;
    if (form && !form.contains(root)) window.htmx?.trigger(form, "change");
}

export function initSlider(root: HTMLElement): void {
    if (root.dataset.ulDualRangeReady === "true") return;
    root.dataset.ulDualRangeReady = "true";
    const minSlider = part(root, "min", HTMLInputElement);
    const maxSlider = part(root, "max", HTMLInputElement);
    if (!minSlider || !maxSlider) return;
    for (const slider of [minSlider, maxSlider]) {
        slider.addEventListener("input", () => syncSlider(root));
        slider.addEventListener("change", () => {
            syncSlider(root);
            notifyForm(root);
        });
    }
    syncSlider(root);
}

/** Put the thumbs back to the slider's defaults (``data-min-value``/``data-max-value``), else the full range. */
export function resetSlider(root: HTMLElement): void {
    const minSlider = part(root, "min", HTMLInputElement);
    const maxSlider = part(root, "max", HTMLInputElement);
    if (!minSlider || !maxSlider) return;
    minSlider.value = String(intAttr(root, "data-min-value", Number.parseInt(minSlider.min, 10)));
    maxSlider.value = String(intAttr(root, "data-max-value", Number.parseInt(maxSlider.max, 10)));
    syncSlider(root);
}

function sliders(scope: ParentNode): HTMLElement[] {
    const found = Array.from(scope.querySelectorAll<HTMLElement>("[data-ul-dual-range-slider]"));
    if (scope instanceof HTMLElement && scope.matches("[data-ul-dual-range-slider]")) found.unshift(scope);
    return found;
}

export function initAll(scope: ParentNode = document): void {
    sliders(scope).forEach(initSlider);
}

const api = {
    init: initSlider,
    initAll,
    sync: syncSlider,
    reset: resetSlider,
    resetAll(scope: ParentNode = document): void {
        sliders(scope).forEach(resetSlider);
    },
};

declare global {
    interface Window {
        UrbanLensDualRangeSlider?: typeof api;
    }
}

let installed = false;

export function installGlobalDualRangeSlider(): void {
    if (installed) return;
    installed = true;
    window.UrbanLensDualRangeSlider = api;
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => initAll());
    else initAll();
    // On document, not body: the core bundle runs in <head>.
    document.addEventListener("htmx:afterSwap", (event) => {
        const target = htmxDetail(event).target;
        if (target) initAll(target);
    });
}
