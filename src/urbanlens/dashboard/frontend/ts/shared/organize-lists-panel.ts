/**
 * Organize's Lists panel (``partials/pin_lists/_organize_lists_panel.html``), which htmx loads and reloads: the search
 * box narrows the cards, and the sort control fetches the panel again in the chosen order.
 */

function filterCards(query: string): void {
    const q = query.trim().toLowerCase();
    document.querySelectorAll<HTMLElement>("#pin-lists-grid .pin-list-card").forEach((card) => {
        card.style.display = !q || (card.dataset.search ?? "").includes(q) ? "" : "none";
    });
}

function onInput(event: Event): void {
    const field = event.target;
    if (field instanceof HTMLInputElement && field.id === "pin-lists-search") filterCards(field.value);
}

function onChange(event: Event): void {
    const select = event.target;
    if (!(select instanceof HTMLSelectElement) || select.id !== "pin-lists-sort-select" || !select.dataset.panelUrl) return;
    void window.htmx?.ajax("GET", `${select.dataset.panelUrl}&sort=${encodeURIComponent(select.value)}`, { target: "#panel-lists", swap: "innerHTML" });
}

let installed = false;

export function installOrganizeListsPanel(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("input", onInput);
    document.addEventListener("change", onChange);
}
