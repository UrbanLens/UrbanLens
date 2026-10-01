/**
 * Reorderable ranked lists (partials/ui/_priority_list.html): arrow buttons, drag and drop, and a "Ranked"
 * toggle per item. The ranked slugs, in order, go into the hidden input named by the list's
 * ``data-hidden-id``, which fires ``change`` so an autosaving form picks it up.
 *
 * Delegated on document, so a list htmx brings in later works too.
 */

const LIST = ".priority-list";
const ITEM = "priority-list__item";
const UNRANKED = "priority-list__item--unranked";

function hiddenFor(list: HTMLElement): HTMLInputElement | null {
    const hidden = document.getElementById(list.dataset.hiddenId ?? "");
    return hidden instanceof HTMLInputElement ? hidden : null;
}

function items(list: HTMLElement): HTMLElement[] {
    return Array.from(list.children).filter((li): li is HTMLElement => li instanceof HTMLElement && li.classList.contains(ITEM) && !!li.dataset.slug);
}

function rankedItems(list: HTMLElement): HTMLElement[] {
    return items(list).filter((li) => !li.classList.contains(UNRANKED));
}

function firstUnranked(list: HTMLElement): Element | null {
    return list.querySelector(`.${UNRANKED}`);
}

export function renumber(list: HTMLElement): void {
    let rank = 0;
    for (const li of items(list)) {
        const label = li.querySelector(".priority-list__rank");
        const unranked = li.classList.contains(UNRANKED);
        if (!unranked) rank += 1;
        if (label) label.textContent = unranked ? "—" : String(rank);
    }
}

function sync(list: HTMLElement, hidden: HTMLInputElement): void {
    renumber(list);
    hidden.value = rankedItems(list)
        .map((li) => li.dataset.slug)
        .join(",");
    hidden.dispatchEvent(new Event("change", { bubbles: true }));
}

/** The list and its hidden input for an event inside one, or null. */
function target(event: Event): { list: HTMLElement; hidden: HTMLInputElement; el: Element } | null {
    const el = event.target instanceof Element ? event.target : null;
    const list = el?.closest<HTMLElement>(LIST);
    const hidden = list ? hiddenFor(list) : null;
    return el && list && hidden ? { list, hidden, el } : null;
}

let dragging: HTMLElement | null = null;

function onClick(event: MouseEvent): void {
    const hit = target(event);
    const btn = hit?.el.closest<HTMLElement>(".priority-list__btn");
    const li = btn?.closest<HTMLElement>(`.${ITEM}`);
    if (!hit || !btn || !li || li.classList.contains(UNRANKED)) return;
    if (btn.dataset.action === "up") {
        const prev = li.previousElementSibling;
        if (prev) hit.list.insertBefore(li, prev);
    } else if (btn.dataset.action === "down") {
        const next = li.nextElementSibling;
        if (next && !next.classList.contains(UNRANKED)) hit.list.insertBefore(next, li);
    }
    sync(hit.list, hit.hidden);
}

function onChange(event: Event): void {
    const hit = target(event);
    const toggle = hit?.el.closest("[data-role='rank-toggle']");
    const li = toggle?.closest<HTMLElement>(`.${ITEM}`);
    if (!hit || !(toggle instanceof HTMLInputElement) || !li) return;
    if (toggle.checked) {
        li.classList.remove(UNRANKED);
        li.draggable = true;
        hit.list.insertBefore(li, firstUnranked(hit.list));
    } else {
        li.classList.add(UNRANKED);
        li.draggable = false;
        hit.list.appendChild(li);
    }
    sync(hit.list, hit.hidden);
}

function onDragStart(event: DragEvent): void {
    const hit = target(event);
    const li = hit?.el.closest<HTMLElement>(`.${ITEM}`);
    if (!hit || !li) return;
    if (li.classList.contains(UNRANKED)) {
        event.preventDefault();
        return;
    }
    dragging = li;
    li.classList.add("priority-list__item--dragging");
    if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
}

function onDragEnd(event: DragEvent): void {
    const hit = target(event);
    if (!dragging) return;
    dragging.classList.remove("priority-list__item--dragging");
    dragging = null;
    if (hit) sync(hit.list, hit.hidden);
}

function onDragOver(event: DragEvent): void {
    const hit = target(event);
    if (!hit || !dragging || !hit.list.contains(dragging)) return;
    event.preventDefault();
    const moving = dragging;
    const after = rankedItems(hit.list).find((li) => {
        if (li === moving) return false;
        const rect = li.getBoundingClientRect();
        return event.clientY < rect.top + rect.height / 2;
    });
    hit.list.insertBefore(moving, after ?? firstUnranked(hit.list));
}

function renumberWithin(root: Element | Document): void {
    const lists = Array.from(root.querySelectorAll<HTMLElement>(LIST));
    if (root instanceof HTMLElement && root.matches(LIST)) lists.push(root);
    for (const list of lists) if (hiddenFor(list)) renumber(list);
}

export function installGlobalPriorityList(): void {
    document.addEventListener("click", onClick);
    document.addEventListener("change", onChange);
    document.addEventListener("dragstart", onDragStart);
    document.addEventListener("dragend", onDragEnd);
    document.addEventListener("dragover", onDragOver);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => renumberWithin(document), { once: true });
    else renumberWithin(document);
    document.addEventListener("htmx:load", (event) => {
        if (event.target instanceof Element) renumberWithin(event.target);
    });
}
