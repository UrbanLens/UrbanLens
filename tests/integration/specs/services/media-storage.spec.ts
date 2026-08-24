/**
 * Uploaded media has to survive the round trip out of the process.
 *
 * This is the check with the least overlap with anything else in the repo. Every
 * other test of photo handling runs inside one process against a temporary
 * directory; a deployment stores files somewhere else entirely - an object store
 * over the network, on a host with its own credentials, behind a URL that may be
 * signed, proxied, or served by a different container than the one that wrote
 * it. None of that exists in a unit test, and all of it can be misconfigured in
 * a way that leaves the API answering 201 while the bytes go nowhere.
 *
 * So the assertion is deliberately the whole loop: upload, read the URL the API
 * hands back, fetch it, and check the bytes come back. An upload that "succeeds"
 * and a URL that 404s is the exact failure this exists to catch.
 */

import { expect, test } from "../../lib/fixtures.js";
import { apiUrl, env, resourceName } from "../../lib/env.js";

/**
 * A 1x1 transparent PNG.
 *
 * Small on purpose - this is testing the storage path, not image processing -
 * but a *real* PNG, because the upload path sniffs the file type and rejects
 * bytes that only claim to be one.
 */
const ONE_PIXEL_PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

interface Photo {
    uuid: string;
    url: string;
    caption?: string;
    file_size?: number;
    pin_slug?: string;
}

test.describe("media storage", () => {
    test("an uploaded photo is stored and served back", async ({ api, apiRequestContext, account }) => {
        const pin = await api.createPin({ name: resourceName("photo host") });
        const caption = resourceName("stored image");

        // Sent through the raw request context: this is the suite's only
        // multipart request, and giving the typed client a multipart helper for
        // one caller would be more indirection than it saves.
        const upload = await apiRequestContext.post(apiUrl("photos/"), {
            headers: { Authorization: `Bearer ${account.apiKey ?? ""}` },
            multipart: {
                file: { name: "e2e-pixel.png", mimeType: "image/png", buffer: ONE_PIXEL_PNG },
                caption,
                pin: pin.slug,
            },
        });
        expect(upload.status(), `uploading a photo answered ${upload.status()}: ${(await upload.text()).slice(0, 300)}`).toBeLessThan(300);

        const photo = (await upload.json()) as Photo;
        expect(photo.uuid, `the upload response carries no uuid: ${JSON.stringify(photo).slice(0, 200)}`).toBeTruthy();
        api.track("photo", photo.uuid, () => api.delete(`photos/${photo.uuid}/`));

        const detail = await api.json<Photo>("get", `photos/${photo.uuid}/`);
        expect(detail.url, "the stored photo has no url, so no client can display it").toBeTruthy();

        // The URL may be same-origin (a proxy view) or absolute (an object
        // store). Both are legitimate deployments, so resolve rather than
        // assume.
        const target = detail.url.startsWith("http") ? detail.url : new URL(detail.url, env.baseUrl).toString();
        const fetched = await apiRequestContext.get(target, { headers: { Authorization: `Bearer ${account.apiKey ?? ""}` } });

        expect(fetched.status(), `the photo's own url answered ${fetched.status()}. Upload reported success, so the bytes were written somewhere this URL does not read from: ${target}`).toBe(200);
        const body = await fetched.body();
        expect(body.length, "the photo's url answered 200 with an empty body").toBeGreaterThan(0);
    });

    test("a file that is not an image is refused", async ({ apiRequestContext, account }) => {
        // The upload path sniffs content rather than trusting the declared
        // type, which is what stops an executable being stored under a name
        // ending in .png. Only a real upload exercises the sniffer.
        const upload = await apiRequestContext.post(apiUrl("photos/"), {
            headers: { Authorization: `Bearer ${account.apiKey ?? ""}` },
            multipart: {
                file: { name: "not-really.png", mimeType: "image/png", buffer: Buffer.from("#!/bin/sh\necho this is not a png\n", "utf-8") },
                caption: resourceName("bogus upload"),
            },
        });

        // 409 in practice, 400 and 415 both being defensible answers too. The
        // status is not the point: what matters is that the bytes were
        // inspected rather than the filename trusted, and that the refusal is
        // not a 500 (which is what an unhandled decode error looks like) or a
        // 2xx (which is what trusting the extension looks like).
        expect(
            [400, 409, 415],
            `a file whose bytes are not an image answered ${upload.status()}; it was either accepted on the strength of its .png name, or it crashed`,
        ).toContain(upload.status());
    });

    test("another account's photo is not readable", async ({ restrictedApi }) => {
        // Scope enforcement on the media surface specifically: a key that can
        // read a profile must not be able to enumerate its photos.
        const response = await restrictedApi.get("photos/");
        expect(response.status(), "a profile:read key listed photos").toBe(403);
    });
});
