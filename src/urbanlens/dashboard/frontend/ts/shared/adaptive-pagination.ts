/**
 * Client-side pagination for external-data cards whose items vary in height: the page size is however many
 * rendered items fit on screen, measured rather than guessed.
 *
 * A list opts in with ``data-adaptive-pagination-list`` inside a ``.card``; its items carry
 * ``data-adaptive-pagination-item``, and the card's ``data-adaptive-pagination-controls`` element (see
 * _pagination_controls.html) gets the Prev/Next buttons. Server batches chain through the controls'
 * ``data-next-url``/``data-prev-url``.
 */

/** Items the Media gallery's active source tab excludes are not on any page. */
function itemsOf(card: HTMLElement): HTMLElement[] {
    return Array.from(card.querySelectorAll<HTMLElement>("[data-adaptive-pagination-item]")).filter((el) => !el.classList.contains("media-tab-excluded"));
}

function availableListHeight(card: HTMLElement, list: HTMLElement): number {
    const viewportBottom = window.innerHeight || document.documentElement.clientHeight || 800;
    const availableCardHeight = Math.max(180, viewportBottom - card.getBoundingClientRect().top - 24);
    const height = Math.max(120, availableCardHeight - (card.offsetHeight - list.offsetHeight));
    // A list's own CSS max-height still caps it on a tall viewport, or the page overflows once the
    // measurement's inline override is removed.
    const cssMaxHeight = Number.parseFloat(getComputedStyle(list).maxHeight);
    return Number.isNaN(cssMaxHeight) ? height : Math.min(height, cssMaxHeight);
}

/** Shows every item, clips the list to the space available, and counts the items still wholly visible. */
function measurePageSize(card: HTMLElement, list: HTMLElement, items: HTMLElement[]): number {
    if (!items.length) return 1;
    const minPageSize = Number.parseInt(list.dataset.adaptivePaginationMinPageSize || "1", 10) || 1;
    const previousMaxHeight = list.style.maxHeight;
    const previousOverflow = list.style.overflow;
    const controls = card.querySelector<HTMLElement>("[data-adaptive-pagination-controls]");
    const available = availableListHeight(card, list);

    for (const item of items) item.hidden = false;
    if (controls) controls.hidden = true;
    list.style.maxHeight = `${available}px`;
    list.style.overflow = "hidden";

    const listRect = list.getBoundingClientRect();
    const visibleBottom = Math.min(listRect.bottom, listRect.top + available);
    let shown = 0;
    for (const item of items) {
        const rect = item.getBoundingClientRect();
        if (rect.top < listRect.top || rect.bottom > visibleBottom) break;
        shown += 1;
    }

    list.style.maxHeight = previousMaxHeight;
    list.style.overflow = previousOverflow;
    if (controls) controls.hidden = false;
    return Math.max(minPageSize, shown);
}

function pageButton(label: string, icon: string): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pagination-btn";
    btn.setAttribute("aria-label", label);
    const glyph = document.createElement("i");
    glyph.className = "material-symbols-outlined";
    glyph.textContent = icon;
    btn.append(glyph);
    return btn;
}

function loadBatch(card: HTMLElement, url: string): void {
    card.dataset.userPaginated = "1";
    void window.htmx?.ajax("GET", url, { target: card, swap: "outerHTML" });
}

function renderControls(card: HTMLElement, controls: HTMLElement, page: number, pageSize: number, totalItems: number): void {
    const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
    const serverPage = Number.parseInt(controls.dataset.serverPage || "1", 10) || 1;
    const serverPages = Number.parseInt(controls.dataset.serverPages || "1", 10) || 1;
    const nextUrl = controls.dataset.nextUrl || "";
    const prevUrl = controls.dataset.prevUrl || "";
    controls.replaceChildren();
    if (totalPages <= 1 && !nextUrl && !prevUrl) {
        controls.hidden = true;
        return;
    }
    controls.hidden = false;

    const prev = pageButton("Previous page", "chevron_left");
    prev.disabled = page <= 1 && !prevUrl;
    if (page > 1) {
        prev.addEventListener("click", () => {
            card.dataset.userPaginated = "1";
            showPage(card, page - 1, pageSize);
        });
    } else if (prevUrl) {
        prev.addEventListener("click", () => loadBatch(card, prevUrl));
    }

    const label = document.createElement("span");
    label.className = "pagination-label";
    label.textContent = `Page ${page} of ${totalPages}${serverPages > 1 ? ` · batch ${serverPage} of ${serverPages}` : ""}`;

    const next = pageButton("Next page", "chevron_right");
    if (page < totalPages) {
        next.addEventListener("click", () => {
            card.dataset.userPaginated = "1";
            showPage(card, page + 1, pageSize);
        });
    } else if (nextUrl) {
        next.addEventListener("click", () => loadBatch(card, nextUrl));
    } else {
        next.disabled = true;
    }
    controls.append(prev, label, next);
}

function showPage(card: HTMLElement, page: number, pageSize: number): void {
    const items = itemsOf(card);
    const start = (page - 1) * pageSize;
    items.forEach((item, index) => {
        item.hidden = index < start || index >= start + pageSize;
    });
    card.dataset.adaptivePaginationCurrentPage = String(page);
    const controls = card.querySelector<HTMLElement>("[data-adaptive-pagination-controls]");
    if (controls) renderControls(card, controls, page, pageSize, items.length);
}

/**
 * Paginates every opted-in list under ``root``.
 *
 * Args:
 *     root: Where to look.
 *     force: Re-measure lists already paginated (a resize, a thumbnail finishing, more items arriving). The
 *         viewer stays on the page they were reading, clamped if the page count shrank.
 */
export function initAdaptivePagination(root: ParentNode = document, force = false): void {
    root.querySelectorAll<HTMLElement>("[data-adaptive-pagination-list]").forEach((list) => {
        const card = list.closest<HTMLElement>(".card");
        if (!card) return;
        const alreadyReady = card.dataset.adaptivePaginationReady === "1";
        if (!force && alreadyReady) return;
        const items = itemsOf(card);
        if (!items.length) return;
        const pageSize = measurePageSize(card, list, items);
        const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
        const page = alreadyReady ? Math.min(Number.parseInt(card.dataset.adaptivePaginationCurrentPage || "1", 10) || 1, totalPages) : 1;
        card.dataset.adaptivePaginationPageSize = String(pageSize);
        card.dataset.adaptivePaginationReady = "1";
        showPage(card, page, pageSize);
        card.querySelectorAll<HTMLImageElement>("img").forEach((img) => {
            if (img.dataset.adaptivePaginationObserved) return;
            img.dataset.adaptivePaginationObserved = "1";
            img.addEventListener("load", () => initAdaptivePagination(card, true), { once: true });
        });
    });
}

export function installAdaptivePagination(): void {
    window.addEventListener("resize", () => initAdaptivePagination(document, true));
    // An outerHTML swap's target is the detached element, so scan the document for the fresh card.
    document.body.addEventListener("htmx:afterSwap", () => initAdaptivePagination(document, false));
    initAdaptivePagination(document, false);
}
