/**
 * Paged Organize rows (P66). A tab renders its first page and a sentinel row (`organize_label_rows.html`) loads
 * the next as it scrolls into view; whatever needs every row of a kind - a filter, the tree view, select-all,
 * a bulk action started from Display Order - calls {@link loadAllRows} first.
 */

export const ROWS_MORE_SELECTOR = ".organize-rows-more";
const ROWS_PLACEHOLDER_SELECTOR = ".organize-section-loading";
const ROWS_CONTAINER_SELECTOR = ".organize-label-rows";

/** Tells a write's re-render how many rows the client has loaded; read by `controllers/labels.py`. */
export const ROWS_LOADED_HEADER = "X-Org-Rows-Loaded";

/** Requests one load may make before giving up: the rest normally arrives in one, after at most one in flight. */
const MAX_LOAD_STEPS = 5;

/** Whether rows of this kind exist that are not in the container: a sentinel, or a tab that has not loaded. */
export function hasUnloadedRows(rows: HTMLElement | null): boolean {
    return !!rows && !!rows.querySelector(`${ROWS_MORE_SELECTOR}, ${ROWS_PLACEHOLDER_SELECTOR}`);
}

/**
 * Whether the container has loaded a page and has more to come. A tab that has not loaded is left to its own
 * deferred load, which brings the sentinel this looks for.
 */
export function hasMoreRows(rows: HTMLElement | null): boolean {
    return !!rows?.querySelector(ROWS_MORE_SELECTOR);
}

/** The value of {@link ROWS_LOADED_HEADER} for a write that re-renders *rows*; undefined while nothing has loaded. */
export function rowsLoadedHeaderValue(rows: HTMLElement): string | undefined {
    if (rows.querySelector(ROWS_PLACEHOLDER_SELECTOR)) return undefined;
    if (!rows.querySelector(ROWS_MORE_SELECTOR)) return "all";
    // Direct children only: the tree view nests clones of the same cards.
    return String(Array.from(rows.children).filter((child) => child.classList.contains("tag-card")).length);
}

/** Headers for a fetch whose response replaces *rows*. */
export function rowsLoadedHeaders(rows: HTMLElement | null): Record<string, string> {
    const value = rows ? rowsLoadedHeaderValue(rows) : undefined;
    return value ? { [ROWS_LOADED_HEADER]: value } : {};
}

function requestFinished(el: HTMLElement): Promise<void> {
    return new Promise((resolve) => el.addEventListener("htmx:afterRequest", () => resolve(), { once: true }));
}

const loading = new WeakMap<HTMLElement, Promise<void>>();

async function loadRest(rows: HTMLElement): Promise<void> {
    const htmx = window.htmx;
    if (!htmx) throw new Error("htmx is not loaded");
    for (let step = 0; step < MAX_LOAD_STEPS; step++) {
        const more = rows.querySelector<HTMLElement>(ROWS_MORE_SELECTOR);
        if (more) {
            // htmx queues a second request from the same element behind the first and resolves at once, so a
            // next-page load already under way has to finish before the rest is asked for.
            if (more.classList.contains("htmx-request")) {
                await requestFinished(more);
                continue;
            }
            await htmx.ajax("GET", more.dataset.restUrl ?? "", { source: more, target: more, swap: "outerHTML" });
            if (more.isConnected) break;
            continue;
        }
        if (!rows.querySelector(ROWS_PLACEHOLDER_SELECTOR)) return;
        await htmx.ajax("GET", `${rows.dataset.rowsUrl ?? ""}?all=1`, { source: rows, target: rows, swap: "innerHTML" });
        if (rows.querySelector(ROWS_PLACEHOLDER_SELECTOR)) break;
    }
    if (hasUnloadedRows(rows)) throw new Error("Could not load the rest of the list.");
}

/**
 * Load every row of *rows* that is not in the DOM yet. Concurrent calls share one load.
 *
 * @returns Resolves once they are in; rejects if the server would not send them.
 */
export function loadAllRows(rows: HTMLElement): Promise<void> {
    const pending = loading.get(rows);
    if (pending) return pending;
    if (!hasUnloadedRows(rows)) return Promise.resolve();
    const load = loadRest(rows).finally(() => loading.delete(rows));
    loading.set(rows, load);
    return load;
}

/** Sends {@link ROWS_LOADED_HEADER} with every htmx request that re-renders a rows container. Call once. */
export function installRowsLoadedHeader(): void {
    document.addEventListener("htmx:configRequest", (e) => {
        const detail = (e as CustomEvent).detail as { target?: Element | null; headers?: Record<string, string> };
        const target = detail.target;
        if (!(target instanceof HTMLElement) || !target.matches(ROWS_CONTAINER_SELECTOR) || !detail.headers) return;
        const value = rowsLoadedHeaderValue(target);
        if (value) detail.headers[ROWS_LOADED_HEADER] = value;
    });
}
