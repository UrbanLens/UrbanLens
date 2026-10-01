/**
 * The Memories section's tab strip (``partials/memories/_photos_tabs.html``, marked ``data-memories-nav``), on every
 * Memories page. Those pages confirm visits, create pins from suggestions and log visits, none of which the map
 * sees, so any successful write there marks the map's pins dirty; and the Visits tab's badge follows the server's
 * ``unloggedVisitsCountChanged`` trigger.
 */

import { markPinsDirty } from "./pin-cache";

function onAfterRequest(event: Event): void {
    if (!document.querySelector("[data-memories-nav]")) return;
    const detail: unknown = event instanceof CustomEvent ? event.detail : null;
    if (!detail || typeof detail !== "object" || !Reflect.get(detail, "xhr") || Reflect.get(detail, "successful") !== true) return;
    const config: unknown = Reflect.get(detail, "requestConfig");
    if (config && typeof config === "object" && Reflect.get(config, "verb") === "get") return;
    markPinsDirty();
}

function onCountChanged(event: Event): void {
    const detail: unknown = event instanceof CustomEvent ? event.detail : null;
    const count: unknown = detail && typeof detail === "object" ? Reflect.get(detail, "count") : null;
    if (typeof count !== "number") return;
    if (count <= 0) {
        document.getElementById("memories-visits-tab")?.remove();
        return;
    }
    const badge = document.getElementById("memories-visits-tab-badge");
    if (badge) badge.textContent = String(count);
}

let installed = false;

export function installMemoriesNav(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("htmx:afterRequest", onAfterRequest);
    document.addEventListener("unloggedVisitsCountChanged", onCountChanged);
}
