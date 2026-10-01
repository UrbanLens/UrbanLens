/**
 * Same-page tabs (``<a href="#section" data-section-tab>``, as on the FAQ) mark the section in view as active, the way
 * a page's own tab is marked server-side on the other pages.
 */

/** Returns a function that stops watching. */
export function watchSectionTabs(root: Document): () => void {
    const pairs = Array.from(root.querySelectorAll<HTMLAnchorElement>("a[data-section-tab]")).flatMap((tab) => {
        const section = root.getElementById(tab.getAttribute("href")?.slice(1) ?? "");
        return section ? [{ tab, section }] : [];
    });
    if (!pairs.length || typeof IntersectionObserver === "undefined") return () => {};
    const observer = new IntersectionObserver(
        (entries) => {
            for (const entry of entries) {
                if (!entry.isIntersecting) continue;
                for (const { tab, section } of pairs) tab.classList.toggle("active", section === entry.target);
            }
        },
        { rootMargin: "-45% 0px -50% 0px" },
    );
    for (const { section } of pairs) observer.observe(section);
    return () => observer.disconnect();
}

export function installGlobalSectionTabs(): void {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => watchSectionTabs(document), { once: true });
    else watchSectionTabs(document);
}
