/**
 * Inline article images are embedded by their stable link (`media.image`), which answers the uploader with a
 * placeholder until the upload's re-encode lands. Once it has, the editor asks for the link again.
 */

import { type ProcessingItem, processingStateOf } from "./photo-processing";

export interface InlineImageAttrs {
    src?: unknown;
    alt?: unknown;
    title?: unknown;
}

/** The link with a query it ignores, so the browser does not reuse the placeholder it already holds. */
export function refreshedSrc(url: string, stamp: number): string {
    return `${url}${url.includes("?") ? "&" : "?"}v=${stamp}`;
}

/**
 * Render an image node's attributes onto *img*. The node's own src is kept in `data-link`, so a refreshed
 * request survives an update that leaves the src alone.
 */
export function applyInlineImageAttrs(img: HTMLImageElement, attrs: InlineImageAttrs): void {
    const src = String(attrs.src ?? "");
    if (img.dataset.link !== src) {
        img.dataset.link = src;
        img.setAttribute("src", src);
    }
    img.alt = String(attrs.alt ?? "");
    if (attrs.title) img.title = String(attrs.title);
    else img.removeAttribute("title");
}

/** Request every rendered copy of *url* under *container* again, in an editor or not. Returns how many. */
export function refreshInlineImages(container: ParentNode, url: string, stamp = Date.now()): number {
    let count = 0;
    container.querySelectorAll<HTMLImageElement>("img").forEach((img) => {
        if ((img.dataset.link ?? img.getAttribute("src")) !== url) return;
        img.setAttribute("src", refreshedSrc(url, stamp));
        count += 1;
    });
    return count;
}

/** What a settled poll result means for an inline image; a photo that is gone has failed too. */
export function inlineImageSettled(item: ProcessingItem | null): "ready" | "failed" {
    return item && processingStateOf(item) === "" ? "ready" : "failed";
}
