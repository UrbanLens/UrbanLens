/**
 * Mirrors `_anchor_slug` in `services/articles.py` exactly, so heading ids
 * assigned client-side in the WYSIWYG canvas (see entries/article-wysiwyg.ts)
 * match the anchors the server already burned into the saved article's TOC
 * (`article.toc`, rendered by `_article_panel.html`'s `<nav class="article-toc">`).
 */

const SLUG_STRIP = /[^\p{L}\p{N}_\s-]/gu;
const SLUG_DASH = /[\s_-]+/gu;

/** Derive a unique, URL-safe anchor id for a heading title, given anchors already used in this document. */
export function anchorSlug(title: string, used: Set<string>): string {
    const base = title.trim().toLowerCase().replace(SLUG_STRIP, "").replace(SLUG_DASH, "-").replace(/^-+|-+$/g, "") || "section";
    let candidate = base;
    let counter = 2;
    while (used.has(candidate)) {
        candidate = `${base}-${counter}`;
        counter += 1;
    }
    used.add(candidate);
    return candidate;
}
