/** A photo attached in the direct-message composer, before it is sent. */

import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import { ensureFriends } from "../../lib/friendship.js";

const TINY_JPEG_BASE = Buffer.from(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k=",
    "base64",
);

test.describe("direct message photo attachment", () => {
    ifSharingPair()("the composer chip is a placeholder until the photo is re-encoded, then the photo", async ({ sharerApi, shareeApi, sharerPage }) => {
        const { b: sharee } = await ensureFriends(sharerApi, shareeApi);
        const missing: string[] = [];
        sharerPage.on("response", (response) => {
            if (response.status() === 404 && /\/media\//.test(response.url())) missing.push(response.url());
        });

        await sharerPage.goto(`/dashboard/messages/${sharee.slug}/`);
        await expect(sharerPage.locator("#dm-attach-photo-input")).toBeAttached();

        const uploaded = sharerPage.waitForResponse((response) => response.request().method() === "POST" && response.url().includes("/messages/upload-image/"));
        await sharerPage.setInputFiles("#dm-attach-photo-input", { name: "dm.jpg", mimeType: "image/jpeg", buffer: Buffer.concat([TINY_JPEG_BASE, Buffer.from(`${Date.now()}`)]) });
        const response = await uploaded;
        expect(response.status(), await response.text()).toBe(201);
        const body = (await response.json()) as { url: string | null; processing: boolean };
        expect(body.url, "a fresh upload names no file").toBeFalsy();
        expect(body.processing).toBe(true);

        const chip = sharerPage.locator("#dm-composer-image-chips .dm-composer-attachment-chip").first();
        const seen = new Set<string>();
        await expect
            .poll(
                async () => {
                    const state = await chip.evaluate((el) => {
                        const img = el.querySelector("img");
                        if (img) return img.complete ? (img.naturalWidth > 0 ? "photo" : "broken") : "loading";
                        return el.querySelector('.media-processing[role="img"]') ? "placeholder" : "empty";
                    });
                    seen.add(state);
                    return state;
                },
                { timeout: 90000, intervals: [250] },
            )
            .toBe("photo");
        expect([...seen].filter((state) => !["placeholder", "loading", "photo"].includes(state)), `chip states: ${[...seen].join(", ")}`).toEqual([]);
        expect([...seen][0], "the chip starts as a placeholder").toBe("placeholder");
        expect(missing, "no media request may 404").toEqual([]);
    });
});
