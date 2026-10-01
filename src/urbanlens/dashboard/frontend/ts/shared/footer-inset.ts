/**
 * Publish the sticky page footer's height as ``--ul-page-footer-height`` on the root element.
 *
 * The footer wraps to between one and three lines depending on the viewport and the map attribution text, so
 * controls fixed to the bottom of the viewport offset themselves from this rather than from a guessed constant.
 */

export const FOOTER_HEIGHT_PROPERTY = "--ul-page-footer-height";

function publish(footer: Element): void {
    document.documentElement.style.setProperty(FOOTER_HEIGHT_PROPERTY, `${Math.ceil(footer.getBoundingClientRect().height)}px`);
}

function track(): void {
    const footer = document.querySelector(".page-footer");
    if (!footer) return;
    publish(footer);
    if (typeof ResizeObserver === "undefined") return;
    new ResizeObserver(() => publish(footer)).observe(footer);
}

export function installGlobalFooterInset(): void {
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", track, { once: true });
    } else {
        track();
    }
}
