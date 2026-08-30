/**
 * The fly-to-corner dismissal for auto-loading cards that turn out to be empty.
 *
 * Cards marked ``data-ext-panel-204`` used to simply ``remove()`` themselves when
 * they discovered they had nothing to show, vanishing instantly and shifting
 * whatever was below them with no warning. This shrinks the card and flies it
 * toward the tools FAB - the same corner that already holds manually-collapsed
 * sections - so the disappearance reads as "it went there" rather than a jump.
 *
 * Split out of the collapsible-sections block it shared a template ``<script>``
 * with: the two share no state, only DOM lookups.
 *
 * **Testing limitation, stated deliberately.** Everything below the element check
 * is geometry - ``getBoundingClientRect`` and ``getComputedStyle`` - which happy-dom
 * reports as zeros. The tests alongside this cover what is real there (a
 * disconnected element is removed; the transition and its timeout fallback each
 * finish exactly once) and cannot assert the animation itself. That part moved
 * knowingly untested rather than by accident.
 */

/** Where the card should fly to: the tools FAB's centre. */
function toolsFabTarget(): { x: number; y: number } {
    const btn = document.getElementById("tools-fab-btn");
    if (btn) {
        const rect = btn.getBoundingClientRect();
        if (rect.width) return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    }
    // The FAB is display:none with no box to measure when nothing is collapsed yet -
    // aim at its fixed on-screen position instead (see .tools-fab in _collapsible.scss).
    const remPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    return { x: 22 + 23, y: window.innerHeight - 4 * remPx - 23 };
}

export function flyToToolsFab(el: HTMLElement | null): void {
    // Most data-ext-panel-204 cards now start `hidden` and never paint before
    // their 204 arrives - nothing on screen to fly, so skip straight to
    // removal instead of pinning/animating an invisible element and waiting
    // out the 450ms fallback for a transitionend that display:none prevents
    // from ever firing.
    if (!el || !el.isConnected || el.hidden) {
        el?.remove();
        return;
    }

    const rect = el.getBoundingClientRect();
    const target = toolsFabTarget();
    const dx = target.x - (rect.left + rect.width / 2);
    const dy = target.y - (rect.top + rect.height / 2);

    // Pin the card to its current screen position and take it out of flow, so the rest
    // of the page reflows immediately while the card keeps flying on top, unaffected.
    el.style.position = "fixed";
    el.style.top = `${rect.top}px`;
    el.style.left = `${rect.left}px`;
    el.style.width = `${rect.width}px`;
    el.style.height = `${rect.height}px`;
    el.style.margin = "0";
    el.style.zIndex = "9998";
    el.style.pointerEvents = "none";
    void el.offsetWidth; // commit the pinned start position before animating away from it

    el.style.setProperty("--ext-panel-dismiss-x", `${dx}px`);
    el.style.setProperty("--ext-panel-dismiss-y", `${dy}px`);
    el.classList.add("ext-panel-dismissing");

    let done = false;
    const finish = (): void => {
        if (done) return;
        done = true;
        el.remove();
    };
    el.addEventListener("transitionend", finish, { once: true });
    // Fallback for when transitionend never fires - prefers-reduced-motion drops the
    // transform transition, and then nothing would ever remove the card.
    setTimeout(finish, 450);
}

declare global {
    interface Window {
        ulFlyToToolsFab?: typeof flyToToolsFab;
    }
}

export function installGlobalFlyToDismiss(): void {
    window.ulFlyToToolsFab = flyToToolsFab;
}
