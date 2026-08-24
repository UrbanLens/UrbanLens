/**
 * What can be attached to a photo once it is stored.
 *
 * `services/media-storage.spec.ts` proves the bytes survive leaving the
 * process. This is the other half: the row that describes them. Labels, votes
 * and the pin association are each written through a different endpoint and
 * read back through the photo serializer, so each is a place where a write can
 * succeed and simply not show up.
 *
 * Every upload here embeds the run id in its bytes. The store detects duplicates
 * by content, so a reused payload answers 409 on the second test - which reads
 * as a refusal and is really the store recognising a file it already has. That
 * trap is recorded in docs/PROBLEMS.md; it has now caught two specs.
 */

import { expect, test } from "../../lib/fixtures.js";
import { apiUrl, resourceName } from "../../lib/env.js";
import type { ApiClient } from "../../lib/api-client.js";
import type { APIRequestContext } from "@playwright/test";

/** A 1x1 transparent PNG. Real, because the upload path checks the bytes. */
const ONE_PIXEL_PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

/** The same pixel with unique trailing bytes - ignored by decoders, hashes differently. */
function uniquePng(marker: string): Buffer {
    return Buffer.concat([ONE_PIXEL_PNG, Buffer.from(`\n${marker}`, "utf-8")]);
}

interface Photo {
    uuid: string;
    url?: string;
    caption?: string;
    labels?: unknown[];
    pin_slug?: string | null;
}

/** Uploads a photo whose bytes are unique to this call, and tracks it for cleanup. */
async function uploadPhoto(
    api: ApiClient,
    request: APIRequestContext,
    apiKey: string,
    label: string,
    extra: Record<string, string> = {},
): Promise<Photo> {
    const marker = resourceName(label);
    const response = await request.post(apiUrl("photos/"), {
        headers: { Authorization: `Bearer ${apiKey}` },
        multipart: {
            file: { name: "e2e-photo.png", mimeType: "image/png", buffer: uniquePng(marker) },
            caption: marker,
            ...extra,
        },
    });
    expect(response.status(), `uploading answered ${response.status()}: ${(await response.text()).slice(0, 250)}`).toBeLessThan(300);

    const photo = (await response.json()) as Photo;
    expect(photo.uuid, `the upload response carries no uuid: ${JSON.stringify(photo).slice(0, 200)}`).toBeTruthy();
    api.track("photo", photo.uuid, () => api.delete(`photos/${photo.uuid}/`));
    return photo;
}

test.describe("photo metadata", () => {
    test("a photo can be attached to a pin at upload time", async ({ api, apiRequestContext, account }) => {
        const pin = await api.createPin({ name: resourceName("photo owner") });
        const photo = await uploadPhoto(api, apiRequestContext, account.apiKey ?? "", "attached photo", { pin: pin.slug });

        const detail = await api.json<Photo>("get", `photos/${photo.uuid}/`);
        expect(detail.pin_slug, `the photo was uploaded against ${pin.slug} but reads back as ${detail.pin_slug}`).toBe(pin.slug);
    });

    test("labels can be set on a photo and read back", async ({ api, apiRequestContext, account }) => {
        const label = await api.json<{ uuid: string; name: string }>("post", "labels/", { name: resourceName("photo label"), kind: "tag" });
        api.track("label", label.uuid, () => api.delete(`labels/${label.uuid}/`));
        const photo = await uploadPhoto(api, apiRequestContext, account.apiKey ?? "", "labelled photo");

        const applied = await api.put(`photos/${photo.uuid}/labels/`, { labels: [label.uuid] });
        expect(applied.status(), `setting labels answered ${applied.status()}: ${(await applied.text()).slice(0, 250)}`).toBeLessThan(300);

        const detail = await api.get(`photos/${photo.uuid}/`);
        expect(detail.status()).toBe(200);
        expect(await detail.text(), "a label just applied is not on the photo").toContain(label.uuid);
    });

    test("setting labels replaces rather than appends", async ({ api, apiRequestContext, account }) => {
        // PUT, not POST - the whole set is submitted each time. An endpoint that
        // appends instead makes it impossible to remove a label, and the bug is
        // invisible until somebody tries.
        const first = await api.json<{ uuid: string }>("post", "labels/", { name: resourceName("first label"), kind: "tag" });
        const second = await api.json<{ uuid: string }>("post", "labels/", { name: resourceName("second label"), kind: "tag" });
        api.track("label", first.uuid, () => api.delete(`labels/${first.uuid}/`));
        api.track("label", second.uuid, () => api.delete(`labels/${second.uuid}/`));
        const photo = await uploadPhoto(api, apiRequestContext, account.apiKey ?? "", "relabelled photo");

        await api.put(`photos/${photo.uuid}/labels/`, { labels: [first.uuid] });
        await api.put(`photos/${photo.uuid}/labels/`, { labels: [second.uuid] });

        const body = await (await api.get(`photos/${photo.uuid}/`)).text();
        expect(body, "the second label was not applied").toContain(second.uuid);
        expect(body, "the first label is still attached, so setting labels appends instead of replacing").not.toContain(first.uuid);
    });

    test("clearing labels leaves none", async ({ api, apiRequestContext, account }) => {
        const label = await api.json<{ uuid: string }>("post", "labels/", { name: resourceName("temp label"), kind: "tag" });
        api.track("label", label.uuid, () => api.delete(`labels/${label.uuid}/`));
        const photo = await uploadPhoto(api, apiRequestContext, account.apiKey ?? "", "cleared photo");

        await api.put(`photos/${photo.uuid}/labels/`, { labels: [label.uuid] });
        const cleared = await api.put(`photos/${photo.uuid}/labels/`, { labels: [] });
        expect(cleared.status(), `clearing labels answered ${cleared.status()}`).toBeLessThan(300);

        expect(await (await api.get(`photos/${photo.uuid}/`)).text(), "a cleared label is still attached").not.toContain(label.uuid);
    });

    test("a vote is accepted and can be withdrawn", async ({ api, apiRequestContext, account }) => {
        const photo = await uploadPhoto(api, apiRequestContext, account.apiKey ?? "", "voted photo");

        for (const value of [1, -1, 0]) {
            const response = await api.post(`photos/${photo.uuid}/vote/`, { value });
            expect(response.status(), `voting ${value} answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBeLessThan(300);
        }
    });

    test("a vote outside the allowed values is refused", async ({ api, apiRequestContext, account }) => {
        const photo = await uploadPhoto(api, apiRequestContext, account.apiKey ?? "", "overvoted photo");

        const response = await api.post(`photos/${photo.uuid}/vote/`, { value: 5 });
        expect(response.status(), `a vote of 5 answered ${response.status()}`).toBe(400);
    });

    test("a photo uuid that belongs to nobody is refused", async ({ api }) => {
        const response = await api.get("photos/00000000-0000-4000-8000-000000000000/");
        expect(response.status()).toBe(404);
        expect(await response.json()).toHaveProperty("error");
    });

    test("a deleted photo stops being served", async ({ api, apiRequestContext, account }) => {
        const photo = await uploadPhoto(api, apiRequestContext, account.apiKey ?? "", "doomed photo");

        const removed = await api.delete(`photos/${photo.uuid}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();
        expect((await api.get(`photos/${photo.uuid}/`)).status(), "a deleted photo is still readable").toBe(404);
    });
});
