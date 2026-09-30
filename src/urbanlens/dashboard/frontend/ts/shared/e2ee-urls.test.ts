import { describe, expect, test } from "bun:test";

import { e2eeUrlsFromDataset } from "./e2ee-urls";

function dataset(attrs: Record<string, string>): DOMStringMap {
    const el = document.createElement("div");
    for (const [name, value] of Object.entries(attrs)) el.setAttribute(`data-${name}`, value);
    return el.dataset;
}

describe("e2eeUrlsFromDataset", () => {
    test("per-item URLs become the collection base the client appends to", () => {
        const urls = e2eeUrlsFromDataset(
            dataset({
                "url-keys": "/dashboard/e2ee/keys/",
                "url-conversation-key": "/dashboard/e2ee/conversation-key/e2ee-slug-token/",
                "url-group-key": "/dashboard/e2ee/group-key/11111111-1111-1111-1111-111111111111/",
            }),
        );
        expect(urls.partnerKeyBase).toBe("/dashboard/e2ee/keys/");
        expect(urls.conversationKeyBase).toBe("/dashboard/e2ee/conversation-key/");
        expect(urls.groupKeyBase).toBe("/dashboard/e2ee/group-key/");
    });

    test("an endpoint the page does not offer stays absent rather than empty", () => {
        const urls = e2eeUrlsFromDataset(dataset({ "url-login": "/login/" }));
        expect(urls.login).toBe("/login/");
        expect(urls.groupKeyBase).toBeUndefined();
        expect(urls.changePassword).toBeUndefined();
        expect(urls.passkeyBase).toBeUndefined();
    });
});
