/**
 * User-defined custom fields: the definitions, and a value on a real photo.
 *
 * Worth a deployed test because the definition and the value live apart. A
 * field is defined once, against an `entity_type`, and then set per object
 * through a *different* endpoint - so "the field exists" and "a value can be
 * stored against it" are two claims, and a suite that only makes the first one
 * passes while the feature does nothing.
 *
 * The `entity_type` enum matters more than it looks: a field defined for
 * `photo` must not be settable on a pin. That is the kind of cross-check that
 * is easy to omit when each endpoint is written and tested on its own.
 */

import { expect, test } from "../../lib/fixtures.js";
import { apiUrl, resourceName } from "../../lib/env.js";

interface Page<T> {
    count: number;
    results: T[];
}

interface CustomField {
    id?: number;
    field_id?: number;
    uuid?: string;
    name: string;
    entity_type: string;
    field_type?: string;
}

/** The identifier the item routes take, whichever key it arrives under. */
function fieldId(field: CustomField): string | number | undefined {
    return field.id ?? field.field_id ?? field.uuid;
}

/** A 1x1 transparent PNG - a real one, because the upload path checks. */
const ONE_PIXEL_PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.describe("custom fields", () => {
    test("a field definition can be created, listed, renamed and deleted", async ({ api }) => {
        const name = resourceName("field");
        const created = await api.json<CustomField>("post", "custom-fields/", { name, entity_type: "pin", field_type: "text" });
        const id = fieldId(created);
        expect(id, `the created field carries no identifier: ${JSON.stringify(created).slice(0, 200)}`).toBeTruthy();
        api.track("custom-field", String(id), () => api.delete(`custom-fields/${id}/`));

        const page = await api.json<Page<CustomField>>("get", "custom-fields/", { page_size: "100" });
        expect(page.results.some((field) => fieldId(field) === id), "a field just defined is missing from the list").toBeTruthy();

        const renamed = `${name} edited`;
        const patched = await api.patch(`custom-fields/${id}/`, { name: renamed });
        expect(patched.status(), `PATCH answered ${patched.status()}: ${(await patched.text()).slice(0, 200)}`).toBe(200);

        const removed = await api.delete(`custom-fields/${id}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();
    });

    test("an entity type outside the enum is refused", async ({ api }) => {
        // pin / photo / profile / markup_map. A field defined against anything
        // else can never be set on anything, so accepting it stores a row that
        // is inert by construction.
        const response = await api.post("custom-fields/", { name: resourceName("bad entity"), entity_type: "definitely_not_an_entity" });
        expect(response.status(), `an unknown entity_type answered ${response.status()}`).toBe(400);
        expect(await response.json()).toHaveProperty("error");
    });

    test("a field definition needs a name", async ({ api }) => {
        const response = await api.post("custom-fields/", { entity_type: "pin" });
        expect(response.status()).toBe(400);
        expect(await response.json()).toHaveProperty("error");
    });

    test("a value can be stored against a photo and read back", async ({ api, apiRequestContext, account }) => {
        const field = await api.json<CustomField>("post", "custom-fields/", {
            name: resourceName("photo field"),
            entity_type: "photo",
            field_type: "text",
        });
        const id = fieldId(field);
        api.track("custom-field", String(id), () => api.delete(`custom-fields/${id}/`));

        const upload = await apiRequestContext.post(apiUrl("photos/"), {
            headers: { Authorization: `Bearer ${account.apiKey ?? ""}` },
            multipart: {
                file: { name: "e2e-field-host.png", mimeType: "image/png", buffer: ONE_PIXEL_PNG },
                caption: resourceName("field host"),
            },
        });
        expect(upload.status(), `uploading answered ${upload.status()}: ${(await upload.text()).slice(0, 250)}`).toBeLessThan(300);
        const photo = (await upload.json()) as { uuid: string };
        api.track("photo", photo.uuid, () => api.delete(`photos/${photo.uuid}/`));

        const value = resourceName("field value");
        const stored = await api.put(`photos/${photo.uuid}/custom-fields/${id}/`, { value });
        expect(stored.status(), `storing a custom field value answered ${stored.status()}: ${(await stored.text()).slice(0, 250)}`).toBeLessThan(300);

        const readBack = await api.get(`photos/${photo.uuid}/custom-fields/`);
        expect(readBack.status(), `reading a photo's custom fields answered ${readBack.status()}`).toBe(200);
        expect(await readBack.text(), "a custom field value just stored is not readable").toContain(value);

        const cleared = await api.delete(`photos/${photo.uuid}/custom-fields/${id}/`);
        expect(cleared.ok(), `clearing the value answered ${cleared.status()}`).toBeTruthy();
    });

    test("a field defined for pins cannot be set on a photo", async ({ api, apiRequestContext, account }) => {
        // The cross-check. Each endpoint is written and tested on its own, so
        // the pairing between a field's entity_type and the object it is being
        // set on is exactly the rule that goes unenforced.
        const field = await api.json<CustomField>("post", "custom-fields/", {
            name: resourceName("pin only field"),
            entity_type: "pin",
            field_type: "text",
        });
        const id = fieldId(field);
        api.track("custom-field", String(id), () => api.delete(`custom-fields/${id}/`));

        const upload = await apiRequestContext.post(apiUrl("photos/"), {
            headers: { Authorization: `Bearer ${account.apiKey ?? ""}` },
            multipart: { file: { name: "e2e-mismatch.png", mimeType: "image/png", buffer: ONE_PIXEL_PNG }, caption: resourceName("mismatch host") },
        });
        expect(upload.status()).toBeLessThan(300);
        const photo = (await upload.json()) as { uuid: string };
        api.track("photo", photo.uuid, () => api.delete(`photos/${photo.uuid}/`));

        const response = await api.put(`photos/${photo.uuid}/custom-fields/${id}/`, { value: "should not be accepted" });
        expect(
            [400, 404, 409],
            `a field defined for pins was accepted on a photo (${response.status()}), so entity_type constrains nothing`,
        ).toContain(response.status());
    });

    test("a key without the custom_fields scope cannot define one", async ({ restrictedApi }) => {
        const response = await restrictedApi.post("custom-fields/", { name: resourceName("forbidden field"), entity_type: "pin" });
        expect(response.status(), "a profile:read key defined a custom field").toBe(403);
    });
});
