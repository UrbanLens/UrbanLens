/**
 * Vault > Photos' Albums overflow menu, and the cross-pin albums panel it reveals.
 */

export interface OverflowMenu {
    close(): void;
}

/** Toggle *panel* from *button*; an outside click or Escape closes it. */
export function bindOverflowMenu(button: HTMLElement, panel: HTMLElement): OverflowMenu {
    const container = button.closest(".pin-list-more-menu") ?? button.parentElement;
    const close = () => {
        button.setAttribute("aria-expanded", "false");
        panel.hidden = true;
    };
    button.addEventListener("click", () => {
        const opening = panel.hidden;
        panel.hidden = !opening;
        button.setAttribute("aria-expanded", String(opening));
    });
    document.addEventListener("click", (e) => {
        if (container && !container.contains(e.target as Node)) close();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") close();
    });
    return { close };
}

/** Wire the menu; a no-op on any page without it. */
export function initVaultAlbumsMenu(): void {
    const button = document.getElementById("vault-albums-more-btn");
    const panel = document.getElementById("vault-albums-more-menu-panel");
    if (!button || !panel) return;
    const menu = bindOverflowMenu(button, panel);

    let pinAlbumsRequested = false;
    document.getElementById("vault-pin-albums-toggle-btn")?.addEventListener("click", () => {
        menu.close();
        const pinAlbums = document.getElementById("vault-pin-albums-panel");
        if (!pinAlbums) return;
        pinAlbums.hidden = false;
        pinAlbums.scrollIntoView({ behavior: "smooth", block: "nearest" });
        // Most visits never open it, so it loads on first reveal rather than with the page.
        if (!pinAlbumsRequested && window.htmx && pinAlbums.dataset.url) {
            pinAlbumsRequested = true;
            void window.htmx.ajax("GET", pinAlbums.dataset.url, { target: "#vault-pin-albums-panel", swap: "innerHTML" });
        }
    });
}
