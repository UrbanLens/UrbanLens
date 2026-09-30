/**
 * Custom-field definition forms (``partials/custom_fields/_definition_fields.html``, on the Settings page and a pin's
 * custom-fields panel) and the pin panel's draggable fixed fields. Both panels arrive and re-render through htmx.
 */

import type { FetchInit } from "./site-runtime";

type StylesByType = Record<string, [string, string][]>;

const FORM = "form.cf-def-form";
const MAX_LEFT = 92;
const MAX_TOP = 88;

/** ``STYLES_BY_TYPE`` from ``#cf-styles-data``: each field type's display styles as ``[value, label]`` pairs. */
function stylesByType(): StylesByType {
    let raw: unknown;
    try {
        raw = JSON.parse(document.getElementById("cf-styles-data")?.textContent || "{}");
    } catch {
        return {};
    }
    if (!raw || typeof raw !== "object") return {};
    const out: StylesByType = {};
    for (const [type, pairs] of Object.entries(raw)) {
        if (!Array.isArray(pairs)) continue;
        out[type] = pairs.flatMap((pair: unknown) => (Array.isArray(pair) && typeof pair[0] === "string" && typeof pair[1] === "string" ? [[pair[0], pair[1]] satisfies [string, string]] : []));
    }
    return out;
}

function rebuildStyles(form: HTMLFormElement): void {
    const typeSel = form.querySelector<HTMLSelectElement>(".cf-type-select");
    const styleSel = form.querySelector<HTMLSelectElement>(".cf-style-select");
    if (!typeSel || !styleSel) return;
    const styles = stylesByType()[typeSel.value] ?? [];
    const current = styleSel.value || styleSel.dataset.current || "";
    styleSel.replaceChildren(
        ...styles.map(([value, label]) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = label;
            option.selected = value === current;
            return option;
        }),
    );
    styleSel.hidden = styles.length === 0;
}

function showTypeInputs(form: HTMLFormElement): void {
    const type = form.querySelector<HTMLSelectElement>(".cf-type-select")?.value ?? "";
    const style = form.querySelector<HTMLSelectElement>(".cf-style-select")?.value ?? "";
    const options = form.querySelector<HTMLTextAreaElement>(".cf-options-input");
    if (options) {
        options.hidden = type !== "select";
        options.required = type === "select";
    }
    const bounds = form.querySelector<HTMLElement>(".cf-slider-bounds");
    if (bounds) bounds.hidden = !(type === "number" && style === "slider");
    const refKind = form.querySelector<HTMLSelectElement>(".cf-ref-kind-select");
    if (refKind) {
        refKind.hidden = type !== "reference";
        refKind.required = type === "reference";
    }
}

function wireWithin(root: ParentNode): void {
    const forms = root instanceof HTMLFormElement && root.matches(FORM) ? [root] : Array.from(root.querySelectorAll<HTMLFormElement>(FORM));
    for (const form of forms) {
        if (form.dataset.cfWired) continue;
        form.dataset.cfWired = "1";
        rebuildStyles(form);
        showTypeInputs(form);
    }
}

export function installCustomFieldForms(): void {
    document.addEventListener("change", (event) => {
        const target = event.target instanceof HTMLSelectElement ? event.target : null;
        const form = target?.closest<HTMLFormElement>(FORM);
        if (!target || !form) return;
        if (target.matches(".cf-type-select")) rebuildStyles(form);
        if (target.matches(".cf-type-select, .cf-style-select")) showTypeInputs(form);
    });
    document.addEventListener("htmx:load", (event) => {
        if (event.target instanceof Element) wireWithin(event.target);
    });
    wireWithin(document);
}

async function savePosition(item: HTMLElement): Promise<void> {
    const init: FetchInit = {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.csrftoken ?? "" },
        body: JSON.stringify({ left: Number.parseFloat(item.style.left), top: Number.parseFloat(item.style.top) }),
        __ulReported: true,
    };
    try {
        const response = await fetch(item.dataset.cfPositionUrl ?? "", init);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch {
        window.toastr?.error("Failed to save the field position.");
    }
}

export function installFixedFieldDrag(): void {
    document.addEventListener("pointerdown", (down) => {
        const handle = down.target instanceof Element ? down.target.closest(".cf-fixed-handle") : null;
        const item = handle?.closest<HTMLElement>(".cf-fixed-item");
        if (!item) return;
        down.preventDefault();
        const rect = item.getBoundingClientRect();
        const offsetX = down.clientX - rect.left;
        const offsetY = down.clientY - rect.top;
        item.classList.add("is-dragging");
        const onMove = (move: PointerEvent): void => {
            item.style.left = `${Math.min(Math.max(((move.clientX - offsetX) / window.innerWidth) * 100, 0), MAX_LEFT)}%`;
            item.style.top = `${Math.min(Math.max(((move.clientY - offsetY) / window.innerHeight) * 100, 0), MAX_TOP)}%`;
        };
        const onUp = (): void => {
            document.removeEventListener("pointermove", onMove);
            item.classList.remove("is-dragging");
            void savePosition(item);
        };
        document.addEventListener("pointermove", onMove);
        document.addEventListener("pointerup", onUp, { once: true });
    });
}
