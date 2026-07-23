export interface OnboardingCard {
    id: string;
    icon: string;
    target: string;
    eyebrow: string;
    title: string;
    body: string;
    button: string;
    watchSelector?: string;
    watchEvent?: string;
    action: () => void;
    ready: () => boolean;
}

export interface OnboardingTourConfig {
    /** localStorage/sessionStorage key prefix, e.g. 'ul_onboarding_v1_organize'. */
    prefix: string;
    /** id of the element the tour card is appended into. */
    hostSelector: string;
    cards: OnboardingCard[];
    /** custom event name (in addition to htmx:afterSettle) that should retrigger tryShow. */
    retryEvent?: string;
    /** ms to wait before the first tryShow attempt. Defaults to 900. */
    initialDelayMs?: number;
}

/**
 * Dismissible onboarding-card tour, shared by organize/location/wiki/trip
 * pages (each previously carried its own byte-identical copy differing only
 * in the prefix/host/cards/retry-event below).
 */
export function initOnboardingTour(config: OnboardingTourConfig): void {
    const sessionKey = `${config.prefix}_later`;
    // The card currently on screen, if any - tracked so a tab change (or any
    // other retryEvent) can tell whether it's still relevant, not just
    // whether *a* card happens to be showing. Without this, a card whose
    // target lives on one Organize tab (e.g. "drag-priority", anchored to
    // #priority-list) stayed on screen after switching to an unrelated tab,
    // since tryShow() used to bail out early whenever any card was visible,
    // never re-checking that specific card's own ready() after the switch.
    let activeCard: OnboardingCard | null = null;

    function dismissed(id: string): boolean {
        try {
            return localStorage.getItem(`${config.prefix}_${id}_dismissed`) === "1";
        } catch {
            return false;
        }
    }
    function dismiss(id: string): void {
        try {
            localStorage.setItem(`${config.prefix}_${id}_dismissed`, "1");
        } catch {
            /* storage unavailable - ignore */
        }
    }
    function later(): void {
        try {
            sessionStorage.setItem(sessionKey, "1");
        } catch {
            /* storage unavailable - ignore */
        }
    }
    function laterSet(): boolean {
        try {
            return sessionStorage.getItem(sessionKey) === "1";
        } catch {
            return false;
        }
    }
    function isCardTargetVisible(card: OnboardingCard): boolean {
        // A card's target selector often stays present in the DOM even on an
        // unrelated tab (tab switching just toggles a panel's `hidden`
        // attribute rather than removing its content), so ready()'s plain
        // existence check alone can't tell a truly-gone target apart from one
        // that's merely off-screen right now. offsetParent is null for any
        // element that (or whose ancestor) has display:none - a reliable,
        // cheap "is this actually rendered" signal.
        const el = document.querySelector<HTMLElement>(card.target);
        return !!el && el.offsetParent !== null;
    }
    function clear(): void {
        document.querySelector(config.hostSelector)?.replaceChildren();
        document.querySelectorAll(".onboarding-focus").forEach((el) => el.classList.remove("onboarding-focus"));
        activeCard = null;
    }
    function registerAutoDismiss(card: OnboardingCard): void {
        if (dismissed(card.id) || !card.watchSelector) return;
        document.querySelectorAll(card.watchSelector).forEach((el) => {
            el.addEventListener(card.watchEvent ?? "click", () => dismiss(card.id), { once: true });
        });
    }
    function show(card: OnboardingCard): void {
        const host = document.querySelector(config.hostSelector);
        if (!host) return;
        clear();
        activeCard = card;
        document.querySelector(card.target)?.classList.add("onboarding-focus");
        const el = document.createElement("section");
        el.className = "page-onboarding-card";
        el.innerHTML =
            `<div class="page-onboarding-card__icon"><i class="material-icons">${card.icon}</i></div>` +
            `<div class="page-onboarding-card__body"><div class="page-onboarding-card__eyebrow">${card.eyebrow}</div>` +
            `<h2>${card.title}</h2><p>${card.body}</p><div class="page-onboarding-card__actions">` +
            `<button type="button" class="btn btn--primary js-onboarding-action">${card.button}</button>` +
            `<button type="button" class="btn btn--ghost js-onboarding-later">Later</button>` +
            `<button type="button" class="page-onboarding-dismiss js-onboarding-dismiss">Don't show again</button></div></div>` +
            `<button type="button" class="page-onboarding-x js-onboarding-later" aria-label="Close"><i class="material-symbols-outlined">close</i></button>`;
        host.appendChild(el);
        el.querySelector(".js-onboarding-action")?.addEventListener("click", () => {
            dismiss(card.id);
            clear();
            card.action();
        });
        el.querySelectorAll(".js-onboarding-later").forEach((btn) =>
            btn.addEventListener("click", () => {
                later();
                clear();
            }),
        );
        el.querySelector(".js-onboarding-dismiss")?.addEventListener("click", () => {
            dismiss(card.id);
            clear();
        });
    }
    function tryShow(): void {
        if (laterSet()) return;
        // Re-validate the card already on screen (if any) instead of just
        // leaving it up indefinitely - its target may no longer apply after
        // whatever triggered this call (e.g. switching Organize tabs away
        // from the one its target lives on).
        if (activeCard && (!activeCard.ready() || !isCardTargetVisible(activeCard))) clear();
        if (document.querySelector(".page-onboarding-card")) return;
        const card = config.cards.find((c) => c.ready() && isCardTargetVisible(c) && !dismissed(c.id));
        if (card) show(card);
    }

    config.cards.forEach(registerAutoDismiss);
    setTimeout(tryShow, config.initialDelayMs ?? 900);
    if (config.retryEvent) {
        document.addEventListener(config.retryEvent, () => setTimeout(tryShow, 250));
    } else {
        document.body.addEventListener("htmx:afterSettle", () => setTimeout(tryShow, 250));
    }
}
