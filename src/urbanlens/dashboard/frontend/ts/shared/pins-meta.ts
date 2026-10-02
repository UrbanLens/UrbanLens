/**
 * The map's background check for pin changes. It runs on a timer whether or not anyone is looking, so an
 * unreachable server shows as the map's offline indicator rather than a toast per tick.
 */

import { toast } from "./dialogs";
import { type FetchInit, SESSION_ENDED_MESSAGE } from "./site-runtime";

export interface PinsMeta {
    app_uuid?: string;
    fingerprint?: string;
    last_updated?: string;
}

/**
 * @param url - The pins-meta endpoint.
 * @param setOffline - Shows or hides the map's offline indicator.
 * @returns One check: the server's stamp, or null when there is none to compare.
 */
export function pinsMetaChecker(url: string, setOffline: (offline: boolean) => void): () => Promise<PinsMeta | null> {
    // An ended session is said once, until a check succeeds again.
    let sessionEndedShown = false;
    return async () => {
        const init: FetchInit = { headers: { "X-Requested-With": "XMLHttpRequest" }, __ulReported: true };
        try {
            const response = await fetch(url, init);
            if (response.status === 401) {
                setOffline(false);
                if (!sessionEndedShown) toast.error(SESSION_ENDED_MESSAGE);
                sessionEndedShown = true;
                return null;
            }
            if (!response.ok) {
                setOffline(true);
                return null;
            }
            const meta = (await response.json()) as PinsMeta;
            setOffline(false);
            sessionEndedShown = false;
            return meta;
        } catch {
            setOffline(true);
            return null;
        }
    };
}
