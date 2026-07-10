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
    function clear(): void {
        document.querySelector(config.hostSelector)?.replaceChildren();
        document.querySelectorAll(".onboarding-focus").forEach((el) => el.classList.remove("onboarding-focus"));
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
        const card = config.cards.find((c) => c.ready() && !dismissed(c.id));
        if (card) show(card);
    }

    config.cards.forEach(registerAutoDismiss);
    setTimeout(tryShow, config.initialDelayMs ?? 900);
    if (config.retryEvent) {
        document.addEventListener(config.retryEvent, () => {
            if (!document.querySelector(".page-onboarding-card")) setTimeout(tryShow, 250);
        });
    } else {
        document.body.addEventListener("htmx:afterSettle", () => {
            if (!document.querySelector(".page-onboarding-card")) setTimeout(tryShow, 250);
        });
    }
}
