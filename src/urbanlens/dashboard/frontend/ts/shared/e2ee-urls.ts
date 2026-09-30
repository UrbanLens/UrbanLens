/**
 * The E2EE endpoint URLs, read from ``data-url-*`` attributes a template renders with ``{% url %}``.
 */

import type { E2EEUrls } from "./e2ee-client";

/** What a template passes to ``{% url %}`` for the slug and uuid path segments the client fills in itself. */
export const SLUG_TOKEN = "e2ee-slug-token";
export const UUID_TOKEN = "11111111-1111-1111-1111-111111111111";

/** The collection URL a per-item URL hangs off: ``/e2ee/keys/e2ee-slug-token/`` becomes ``/e2ee/keys/``. */
function baseOf(url: string | undefined, token: string): string {
    return (url ?? "").replace(`${token}/`, "");
}

export function e2eeUrlsFromDataset(d: DOMStringMap): E2EEUrls {
    return {
        loginParams: d.urlLoginParams ?? "",
        enroll: d.urlEnroll ?? "",
        keys: d.urlKeys ?? "",
        rewrap: d.urlRewrap ?? "",
        validatePassword: d.urlValidatePassword,
        rewrapAll: d.urlRewrapAll,
        reset: d.urlReset ?? "",
        partnerKeyBase: d.urlKeys ?? "",
        conversationKeyBase: baseOf(d.urlConversationKey, SLUG_TOKEN),
        groupKeyBase: d.urlGroupKey === undefined ? undefined : baseOf(d.urlGroupKey, UUID_TOKEN),
        changePassword: d.urlChangePassword,
        login: d.urlLogin ?? "",
        faqUrl: d.urlFaq,
        passkeyWrap: d.urlPasskeyWrap,
        passkeyRegisterOptions: d.urlPasskeyRegisterOptions,
        passkeyRegister: d.urlPasskeyRegister,
        // The passkey collection, which per-credential actions hang off.
        passkeyBase: d.urlPasskeyRegister,
    };
}
