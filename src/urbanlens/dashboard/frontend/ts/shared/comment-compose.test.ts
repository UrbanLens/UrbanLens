import { beforeEach, describe, expect, mock, test } from "bun:test";

import { installGlobalCommentCompose, settleCommentImage, toggleReplyForm, watchCommentImages } from "./comment-compose";
import { processingPlaceholder } from "./photo-processing";

// Installed at import, as core.js does in production, so the file also passes when run alone.
installGlobalCommentCompose();

beforeEach(() => {
    document.body.innerHTML = "";
    delete window.tripHighlightMarker;
});

describe("toggleReplyForm", () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="reply-for-1" hidden><textarea></textarea></div>';
    });

    test("shows a hidden form and hides a shown one", () => {
        const form = document.getElementById("reply-for-1")!;
        toggleReplyForm("reply-for-1");
        expect(form.hidden).toBe(false);
        toggleReplyForm("reply-for-1");
        expect(form.hidden).toBe(true);
    });

    test("focuses the textarea once the form is open", async () => {
        toggleReplyForm("reply-for-1");
        await new Promise((resolve) => setTimeout(resolve, 60));
        expect(document.activeElement).toBe(document.querySelector("textarea"));
    });

    test("an unknown id is ignored rather than throwing", () => {
        expect(() => toggleReplyForm("no-such-form")).not.toThrow();
    });

    test("a form with no textarea is fine", () => {
        document.body.innerHTML = '<div id="bare" hidden></div>';
        expect(() => toggleReplyForm("bare")).not.toThrow();
        expect(document.getElementById("bare")?.hidden).toBe(false);
    });
});

describe("the image preview", () => {
    beforeEach(() => {
        document.body.innerHTML = `
          <div class="comment-compose-actions">
            <input type="file" class="comment-image-input">
            <span class="comment-image-preview"></span>
          </div>`;
    });

    const input = () => document.querySelector<HTMLInputElement>(".comment-image-input")!;
    const preview = () => document.querySelector<HTMLElement>(".comment-image-preview")!;

    function choose(files: File[]): void {
        const dt = new DataTransfer();
        files.forEach((f) => dt.items.add(f));
        input().files = dt.files;
        input().dispatchEvent(new Event("change", { bubbles: true }));
    }

    test("names the chosen file", () => {
        choose([new File(["x"], "ruin.jpg", { type: "image/jpeg" })]);
        expect(preview().textContent).toContain("ruin.jpg");
    });

    test("clears when the selection is emptied", () => {
        choose([new File(["x"], "ruin.jpg")]);
        choose([]);
        expect(preview().textContent).toBe("");
    });

    test("ignores changes on other inputs", () => {
        document.body.insertAdjacentHTML("beforeend", '<input id="other" type="file">');
        document.getElementById("other")!.dispatchEvent(new Event("change", { bubbles: true }));
        expect(preview().textContent).toBe("");
    });
});

describe("activity mention hover", () => {
    beforeEach(() => {
        document.body.innerHTML = '<a class="mention--activity" data-activity-id="7"><span>#7</span></a>';
    });

    const link = () => document.querySelector<HTMLElement>(".mention--activity")!;

    test("highlights the trip marker on, then off", () => {
        const highlight = mock((_id: string, _on: boolean) => {});
        window.tripHighlightMarker = highlight;

        link().dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        link().dispatchEvent(new MouseEvent("mouseout", { bubbles: true }));

        expect(highlight.mock.calls).toEqual([["7", true], ["7", false]]);
    });

    test("works when the hover lands on a child element", () => {
        const highlight = mock((_id: string, _on: boolean) => {});
        window.tripHighlightMarker = highlight;

        link().querySelector("span")!.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        expect(highlight).toHaveBeenCalledTimes(1);
    });

    test("is inert on pages with no trip map, where the same markup renders", () => {
        // The pin and wiki pages render these mentions but own no map.
        expect(() => link().dispatchEvent(new MouseEvent("mouseover", { bubbles: true }))).not.toThrow();
    });

    test("ignores a mention with no activity id", () => {
        const highlight = mock((_id: string, _on: boolean) => {});
        window.tripHighlightMarker = highlight;
        document.body.innerHTML = '<a class="mention--activity">#?</a>';

        document.querySelector<HTMLElement>(".mention--activity")!.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        expect(highlight).not.toHaveBeenCalled();
    });
});

describe("installGlobalCommentCompose", () => {
    test("exposes the global the comment partials call from onclick", () => {
        expect(typeof window.toggleReplyForm).toBe("function");
    });

    // Last in the file: without the guard this binds a second set of listeners for good.
    test("installing a second time does not stack a second set of listeners", () => {
        installGlobalCommentCompose();
        const highlight = mock((_id: string, _on: boolean) => {});
        window.tripHighlightMarker = highlight;
        document.body.innerHTML = '<a class="mention--activity" data-activity-id="7">#7</a>';

        document.querySelector<HTMLElement>(".mention--activity")!.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));

        expect(highlight).toHaveBeenCalledTimes(1);
    });
});

describe("comment images still being processed", () => {
    const READY = { id: 3, url: "/media/comment_images/e.webp", thumb_url: "/media/comment_images/e.webp", processing: false, processing_failed: false };

    function pendingCommentImage(): HTMLAnchorElement {
        const link = document.createElement("a");
        link.className = "comment-image-link";
        link.dataset.id = "3";
        link.dataset.processing = "pending";
        link.dataset.processingUrl = "/comments/images/processing/";
        link.append(processingPlaceholder("comment-image comment-image--processing"));
        document.body.append(link);
        return link;
    }

    test("a settled image replaces the placeholder and becomes the link's target", () => {
        const link = pendingCommentImage();

        settleCommentImage(link, READY);

        expect(link.querySelector("img")?.getAttribute("src")).toBe(READY.url);
        expect(link.querySelector("img")?.className).toBe("comment-image");
        expect(link.getAttribute("href")).toBe(READY.url);
    });

    test("an image that is gone leaves the comment", () => {
        const link = pendingCommentImage();

        settleCommentImage(link, null);

        expect(link.isConnected).toBe(false);
    });

    test("only comment images and picker photos are watched", () => {
        window.urbanlensProcessingPollers?.forEach((poller) => poller.stop());
        window.urbanlensProcessingPollers?.clear();
        pendingCommentImage();
        document.body.insertAdjacentHTML(
            "beforeend",
            `<ul class="cip-picker-grid" data-processing-url="/vault/photos/processing/">
                <li><button class="cip-picker-item" data-id="9" data-processing="pending" disabled></button></li>
             </ul>
             <ul data-processing-url="/other/"><li data-id="4" data-processing="pending"></li></ul>`,
        );

        watchCommentImages(document);

        expect(window.urbanlensProcessingPollers?.get("/comments/images/processing/")?.size).toBe(1);
        expect(window.urbanlensProcessingPollers?.get("/vault/photos/processing/")?.size).toBe(1);
        expect(window.urbanlensProcessingPollers?.get("/other/")).toBeUndefined();
        window.urbanlensProcessingPollers?.forEach((poller) => poller.stop());
        window.urbanlensProcessingPollers?.clear();
    });
});
