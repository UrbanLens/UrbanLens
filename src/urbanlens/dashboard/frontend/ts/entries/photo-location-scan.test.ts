import { afterEach, beforeEach, describe, expect, test } from "bun:test";

/** A minimal JPEG whose EXIF puts it at *lat*, *lng* (degrees and minutes only, which is all the test needs). */
function jpegAt(lat: number, lng: number): Uint8Array<ArrayBuffer> {
    const tiff = new DataView(new ArrayBuffer(128));
    tiff.setUint16(0, 0x4949);
    tiff.setUint16(2, 42, true);
    tiff.setUint32(4, 8, true);
    // IFD0: one entry pointing at the GPS IFD.
    tiff.setUint16(8, 1, true);
    tiff.setUint16(10, 0x8825, true);
    tiff.setUint16(12, 4, true);
    tiff.setUint32(14, 1, true);
    tiff.setUint32(18, 26, true);
    tiff.setUint32(22, 0, true);
    // GPS IFD: refs inline, coordinates as three rationals each.
    const entries: [number, number, number, number][] = [
        [1, 2, 2, (lat >= 0 ? "N" : "S").charCodeAt(0)],
        [2, 5, 3, 80],
        [3, 2, 2, (lng >= 0 ? "E" : "W").charCodeAt(0)],
        [4, 5, 3, 104],
    ];
    tiff.setUint16(26, entries.length, true);
    entries.forEach(([tag, type, count, value], index) => {
        const at = 28 + index * 12;
        tiff.setUint16(at, tag, true);
        tiff.setUint16(at + 2, type, true);
        tiff.setUint32(at + 4, count, true);
        tiff.setUint32(at + 8, value, true);
    });
    tiff.setUint32(28 + entries.length * 12, 0, true);
    [Math.abs(lat), Math.abs(lng)].forEach((degrees, index) => {
        const at = 80 + index * 24;
        const whole = Math.floor(degrees);
        tiff.setUint32(at, whole, true);
        tiff.setUint32(at + 4, 1, true);
        tiff.setUint32(at + 8, Math.round((degrees - whole) * 60), true);
        tiff.setUint32(at + 12, 1, true);
        tiff.setUint32(at + 16, 0, true);
        tiff.setUint32(at + 20, 1, true);
    });
    const exif = [..."Exif"].map((c) => c.charCodeAt(0)).concat([0, 0]);
    const length = 2 + exif.length + tiff.byteLength;
    return new Uint8Array([0xff, 0xd8, 0xff, 0xe1, length >> 8, length & 0xff, ...exif, ...new Uint8Array(tiff.buffer), 0xff, 0xd9]);
}

const PAGE = `
    <div id="photo-scan-root" data-upload-url="/scan/upload/" data-upload-photo-url="/scan/photo/" data-profile-uuid="me">
        <button type="button" class="btn btn--primary" id="photo-scan-start-btn">Choose folder</button>
        <button type="button" class="btn btn--danger" id="photo-scan-stop-btn" hidden>Stop</button>
        <button type="button" class="btn btn--secondary" id="photo-scan-upload-btn" hidden disabled>Upload results</button>
        <div class="photo-scan-progress" id="photo-scan-progress" hidden>
            <span id="photo-scan-progress-text"></span>
            <span id="photo-scan-progress-count"></span>
            <div id="photo-scan-progress-bar" style="width: 0%;"></div>
        </div>
        <ul id="photo-scan-results"></ul>
        <p id="photo-scan-empty" hidden>No results yet.</p>
    </div>`;

interface Deferred {
    url: string;
    resolve: (response: Response) => void;
}

const realFetch = globalThis.fetch;
const realCreateObjectUrl = URL.createObjectURL;
let pending: Deferred[] = [];
let resultsStatus = 200;
const toasts: string[] = [];

async function until(condition: () => boolean, what: string): Promise<void> {
    for (let attempt = 0; attempt < 400; attempt++) {
        if (condition()) return;
        await new Promise((resolve) => setTimeout(resolve, 5));
    }
    throw new Error(`timed out waiting for ${what}`);
}

const byId = (id: string): HTMLElement => document.getElementById(id)!;
const progress = (): { text: string; count: string; width: string; hidden: boolean } => ({
    text: byId("photo-scan-progress-text").textContent ?? "",
    count: byId("photo-scan-progress-count").textContent ?? "",
    width: byId("photo-scan-progress-bar").style.width,
    hidden: byId("photo-scan-progress").hidden === true,
});

/** Scan two photos taken at one place, opt both into upload, and press Upload. */
async function scanAndUpload({ waitForPhotos = true } = {}): Promise<void> {
    document.body.innerHTML = PAGE;
    await import("./photo-location-scan");
    document.dispatchEvent(new Event("DOMContentLoaded"));
    const input = document.querySelector<HTMLInputElement>('#photo-scan-root input[type="file"]')!;
    const files = [new File([jpegAt(42.5, -71.25)], "a.jpg", { type: "image/jpeg" }), new File([jpegAt(42.5, -71.25)], "b.jpg", { type: "image/jpeg" })];
    Object.defineProperty(input, "files", { value: files });
    input.dispatchEvent(new Event("change"));
    await until(() => progress().text.startsWith("Done"), "the scan to finish");
    for (const box of document.querySelectorAll<HTMLInputElement>(".photo-scan-thumb input")) box.click();
    byId("photo-scan-upload-btn").click();
    if (waitForPhotos) await until(() => pending.some((call) => call.url === "/scan/photo/"), "the first photo upload");
}

beforeEach(() => {
    pending = [];
    resultsStatus = 200;
    toasts.length = 0;
    (window as { toastr?: unknown }).toastr = { success: (m: string) => toasts.push(m), error: (m: string) => toasts.push(m), info: () => {}, warning: () => {} };
    URL.createObjectURL = () => "blob:thumb";
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/scan/upload/") {
            if (resultsStatus !== 200) return Response.json({ error: "Try again later." }, { status: resultsStatus });
            const { clusters } = JSON.parse(String(init?.body)) as { clusters: { id: string }[] };
            const ids = Object.fromEntries(clusters.map((cluster, index) => [cluster.id, index + 1]));
            return Response.json({ review_url: "/memories/", new_pin_suggestions: clusters.length, suggestion_ids: ids });
        }
        return new Promise<Response>((resolve) => pending.push({ url, resolve }));
    }, realFetch);
});

afterEach(() => {
    globalThis.fetch = realFetch;
    URL.createObjectURL = realCreateObjectUrl;
});

describe("uploading the opted-in photos after the results", () => {
    test("shows progress through each photo, and a spinner on the button until done", async () => {
        await scanAndUpload();
        const button = byId("photo-scan-upload-btn");
        expect(toasts).toContain("Uploaded - found 1 suggestion(s). Review them in Memories.");
        expect(button.classList.contains("is-loading")).toBe(true);
        expect(progress()).toEqual({ text: "Uploading 2 preview photos...", count: "0 / 2", width: "0%", hidden: false });

        pending.shift()!.resolve(new Response(null, { status: 201 }));
        await until(() => progress().count === "1 / 2", "the first photo to count");
        expect(progress().width).toBe("50%");

        await until(() => pending.length > 0, "the second photo upload");
        pending.shift()!.resolve(new Response(null, { status: 201 }));
        await until(() => !button.classList.contains("is-loading"), "the upload to finish");
        expect(progress()).toEqual({ text: "Uploaded 2 preview photos.", count: "2 / 2", width: "100%", hidden: false });
    });

    test("a failed results upload stops the spinner and lets the user try again", async () => {
        resultsStatus = 503;
        await scanAndUpload({ waitForPhotos: false });
        const button = document.querySelector<HTMLButtonElement>("#photo-scan-upload-btn")!;
        await until(() => toasts.includes("Try again later."), "the error toast");
        expect(button.classList.contains("is-loading")).toBe(false);
        expect(button.disabled).toBe(false);
        expect(pending).toEqual([]);
    });

    test("says how many made it when one fails", async () => {
        await scanAndUpload();
        pending.shift()!.resolve(new Response(null, { status: 500 }));
        await until(() => pending.length > 0, "the second photo upload");
        pending.shift()!.resolve(new Response(null, { status: 201 }));
        await until(() => !byId("photo-scan-upload-btn").classList.contains("is-loading"), "the upload to finish");
        expect(progress().text).toBe("Uploaded 1 of 2 preview photos.");
        expect(toasts).toContain("1 preview photo could not be uploaded, but your location results were saved.");
    });
});
