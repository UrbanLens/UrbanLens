/**
 * What an external-data panel shows when its request comes back empty or fails.
 *
 * htmx performs no swap for a 204, so without this a "Loading..." placeholder stays forever. The error
 * handlers run in the capture phase, ahead of base.html's generic error toast, and stop it: a provider being
 * down is shown in place rather than announced.
 */

interface HtmxRequestDetail {
    elt?: Element;
    xhr?: XMLHttpRequest;
    target?: Element;
}

function detailOf(event: Event): HtmxRequestDetail {
    return (event as CustomEvent<HtmxRequestDetail>).detail ?? {};
}

function isAutoPanel(el: Element | undefined): el is HTMLElement {
    return el instanceof HTMLElement && el.hasAttribute("data-ext-panel-204");
}

function isPluginTabButton(el: Element | undefined): el is HTMLElement {
    return el instanceof HTMLElement && el.classList.contains("pin-plugin-tab-btn");
}

function dismiss(el: HTMLElement): void {
    window.ulFlyToToolsFab?.(el);
}

/** A panel that also has its own tab button (Wikipedia, under Article) takes the button with it. */
function hideOwnTab(el: HTMLElement): void {
    const tabBtn = document.getElementById(el.getAttribute("data-ext-panel-204-hide-tab") ?? "");
    if (!tabBtn) return;
    const wasActive = tabBtn.classList.contains("active");
    tabBtn.hidden = true;
    if (wasActive) tabBtn.closest(".card-tabs")?.querySelector<HTMLElement>(".pin-plugin-tab-btn:not([hidden])")?.click();
}

function tabPlaceholder(message: string): HTMLParagraphElement {
    const p = document.createElement("p");
    p.className = "pin-plugin-tab-placeholder";
    p.textContent = message;
    return p;
}

/** A tab button targets a body shared by its strip, so the message goes in that body. */
function showTabPlaceholder(btn: HTMLElement, message: string): void {
    const selector = btn.getAttribute("hx-target");
    const body = selector ? document.querySelector(selector) : null;
    body?.replaceChildren(tabPlaceholder(message));
}

/** A still-fetching panel polling inside a tab body is the tab's only content: it ends in a message, not a blank tab. */
function endsTab(panel: HTMLElement, message: string): boolean {
    if (!panel.closest(".pin-plugin-tab-body")) return false;
    panel.replaceWith(tabPlaceholder(message));
    return true;
}

const NO_DATA = "No data available.";
const UNAVAILABLE = "This data is temporarily unavailable.";

function onAfterOnLoad(event: Event): void {
    const { elt, xhr } = detailOf(event);
    if (xhr?.status !== 204) return;
    if (isAutoPanel(elt)) {
        if (endsTab(elt, NO_DATA)) return;
        hideOwnTab(elt);
        dismiss(elt);
    } else if (isPluginTabButton(elt)) {
        showTabPlaceholder(elt, NO_DATA);
    }
}

function onResponseError(event: Event): void {
    const { elt, xhr } = detailOf(event);
    if (isAutoPanel(elt)) {
        event.stopImmediatePropagation();
        if (endsTab(elt, UNAVAILABLE)) return;
        // Web search answers a non-subscriber with a real partial (the upsell) on its 403.
        if (elt.id === "web-search-section" && xhr?.status === 403 && xhr.responseText) {
            elt.outerHTML = xhr.responseText;
            return;
        }
        if (elt.id === "loopnet-section") {
            dismiss(elt);
            return;
        }
        elt.innerHTML = '<div class="card"><div class="card-body"><span class="view-muted">External data temporarily unavailable.</span></div></div>';
    } else if (isPluginTabButton(elt)) {
        event.stopImmediatePropagation();
        showTabPlaceholder(elt, UNAVAILABLE);
    }
}

/** A network failure or timeout dismisses the panel quietly. */
function onSendFailure(event: Event): void {
    const { elt } = detailOf(event);
    if (isAutoPanel(elt)) {
        event.stopImmediatePropagation();
        if (!endsTab(elt, UNAVAILABLE)) dismiss(elt);
    } else if (isPluginTabButton(elt)) {
        event.stopImmediatePropagation();
        showTabPlaceholder(elt, UNAVAILABLE);
    }
}

/** A card's Overview names the tabs it found empty (HX-Trigger: pinTabsEmpty); they go. */
function onTabsEmpty(event: Event): void {
    const keys = (event as CustomEvent<{ keys?: string[] }>).detail?.keys ?? [];
    if (!keys.length) return;
    for (const sectionId of ["location-data-section", "property-records-section"]) {
        const section = document.getElementById(sectionId);
        if (!section) continue;
        for (const key of keys) {
            const btn = section.querySelector<HTMLElement>(`.pin-plugin-tab-btn[data-panel-key="${CSS.escape(key)}"]`);
            if (!btn) continue;
            const wasActive = btn.classList.contains("active");
            btn.remove();
            if (wasActive) section.querySelector<HTMLElement>(".pin-plugin-tab-btn")?.click();
        }
    }
}

/** Location Data tabs show their last response at once, or a spinner, while the request runs. */
function bindLocationDataTabCache(): void {
    const body = document.getElementById("location-data-body");
    if (!body) return;
    const cache = new Map<string, string>();
    const keyOf = (btn: Element): string => btn.getAttribute("hx-get") ?? "";
    const remember = (): void => {
        const active = document.querySelector("#location-data-section .pin-plugin-tab-btn.active");
        const key = active ? keyOf(active) : "";
        if (key && !body.querySelector(".view-loading, .pin-plugin-tab-placeholder")) cache.set(key, body.innerHTML);
    };
    document.querySelectorAll<HTMLElement>("#location-data-section .pin-plugin-tab-btn").forEach((btn) => {
        btn.addEventListener(
            "click",
            () => {
                remember();
                const cached = cache.get(keyOf(btn));
                if (cached) {
                    body.innerHTML = cached;
                    window.htmx?.process(body);
                    return;
                }
                body.innerHTML = '<div class="view-loading"><i class="material-icons spin">autorenew</i> Loading...</div>';
            },
            true,
        );
    });
    document.body.addEventListener("htmx:afterSwap", (event) => {
        if (detailOf(event).target === body) remember();
    });
}

export function installExternalPanelFallbacks(): void {
    document.body.addEventListener("htmx:afterOnLoad", onAfterOnLoad);
    document.body.addEventListener("htmx:responseError", onResponseError, true);
    document.body.addEventListener("htmx:sendError", onSendFailure, true);
    document.body.addEventListener("htmx:timeout", onSendFailure, true);
    document.body.addEventListener("pinTabsEmpty", onTabsEmpty);
    bindLocationDataTabCache();
}
