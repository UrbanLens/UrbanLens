/**
 * The Messages page's photo lightbox (``#dm-lightbox``).
 *
 * Its photos are every image loaded in the open thread, gathered when it opens, so prev/next crosses message
 * boundaries and includes older pages fetched since. It does not wrap: the arrow at either end hides.
 */

import { byId } from "./dom";

const THREAD_IMAGES = "#dm-messages .dm-bubble__image-link img";

export class DmLightbox {
    private urls: string[] = [];
    private index = 0;

    get current(): number {
        return this.index;
    }

    get count(): number {
        return this.urls.length;
    }

    open(link: HTMLElement): void {
        const dialog = byId("dm-lightbox", HTMLDialogElement);
        if (!dialog) return;
        const imgs = Array.from(document.querySelectorAll<HTMLImageElement>(THREAD_IMAGES));
        const clicked = link.querySelector("img");
        this.urls = imgs.map((img) => img.src);
        this.index = Math.max(0, clicked ? imgs.indexOf(clicked) : 0);
        this.buildThumbs();
        this.show();
        if (!dialog.open) dialog.showModal();
    }

    step(delta: number): void {
        this.goTo(this.index + delta);
    }

    goTo(index: number): void {
        if (index < 0 || index >= this.urls.length) return;
        this.index = index;
        this.show();
    }

    private show(): void {
        const img = byId("dm-lightbox-img", HTMLImageElement);
        if (img) img.src = this.urls[this.index] ?? "";
        const prev = document.getElementById("dm-lightbox-prev");
        const next = document.getElementById("dm-lightbox-next");
        if (prev) prev.hidden = this.index <= 0;
        if (next) next.hidden = this.index >= this.urls.length - 1;
        const thumbs = document.getElementById("dm-lightbox-thumbs");
        if (!thumbs) return;
        Array.from(thumbs.children).forEach((el, i) => el.classList.toggle("dm-lightbox-thumb--active", i === this.index));
        thumbs.children[this.index]?.scrollIntoView?.({ block: "nearest", inline: "center" });
    }

    private buildThumbs(): void {
        const thumbs = document.getElementById("dm-lightbox-thumbs");
        if (!thumbs) return;
        thumbs.replaceChildren();
        thumbs.hidden = this.urls.length < 2;
        this.urls.forEach((url, i) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "dm-lightbox-thumb";
            btn.setAttribute("role", "listitem");
            btn.setAttribute("aria-label", `Photo ${i + 1} of ${this.urls.length}`);
            btn.dataset.dmLightboxIndex = String(i);
            const img = document.createElement("img");
            img.src = url;
            img.alt = "";
            btn.appendChild(img);
            thumbs.appendChild(btn);
        });
    }

    /** Opens from a thread photo, and steps on the arrows, the thumbnails and the arrow keys. */
    install(): void {
        document.addEventListener("click", (e) => {
            const el = e.target instanceof Element ? e.target : null;
            const link = el?.closest<HTMLElement>("#dm-messages .dm-bubble__image-link");
            if (link) {
                this.open(link);
                return;
            }
            const stepper = el?.closest<HTMLElement>("[data-dm-lightbox-step]");
            if (stepper) {
                this.step(Number(stepper.dataset.dmLightboxStep));
                return;
            }
            const thumb = el?.closest<HTMLElement>("[data-dm-lightbox-index]");
            if (thumb) this.goTo(Number(thumb.dataset.dmLightboxIndex));
        });
        document.addEventListener("keydown", (e) => {
            if (!byId("dm-lightbox", HTMLDialogElement)?.open) return;
            if (e.key === "ArrowLeft") this.step(-1);
            else if (e.key === "ArrowRight") this.step(1);
        });
    }
}
