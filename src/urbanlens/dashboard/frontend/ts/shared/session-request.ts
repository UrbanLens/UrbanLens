/**
 * The three game clients' HTTP helpers, in one place and checking their responses.
 *
 * SpotGuessr, Trivia and Consensus each carried a near-identical `postForm` and
 * `getJson`, and none of the six checked `response.ok`. `fetch` resolves for a
 * 400 or a 500 - it only rejects when the request never completed - so
 * `response.json()` ran against whatever the error page produced: usually a
 * `SyntaxError` thrown inside a `void`-ed promise, which is a page that quietly
 * stops working (P11). They also had no timeout, so a request that never
 * answered hung its button forever.
 *
 * Both keep resolving with the parsed body rather than throwing, because that is
 * the contract 62 call sites already have. A refusal is surfaced *in that
 * shape*: `{ error: "<the server's own message>" }`, which is exactly what every
 * `postForm` caller already tests for. Those call sites start handling non-2xx
 * responses without being touched, and this file is the only place that had to
 * learn the difference.
 *
 * `getJson` toasts and `postForm` does not, which is asymmetric on purpose: all
 * 33 `getJson` call sites ignore the result's shape entirely (`data.friends ??
 * []`, so a failure renders an empty list and says nothing), while the
 * `postForm` ones test `.error` and toast it themselves. Toasting in both would
 * double up on every refused write.
 */

import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";
import { fetchJson } from "./fetch-json";

/** A refusal, in the shape the call sites already read. */
interface RequestFailure {
    error: string;
}

/**
 * What a bodyless success resolves to.
 *
 * `fetchJson` answers a 204 with `null`, correctly - a DRF delete has nothing to
 * return. These two must not pass that through: every call site reads a property
 * off the result (`response.error`, `data.friends ?? []`), so a `null` is a
 * `TypeError` rather than a quiet no-op. None of the three games' endpoints
 * answers 204 today; turning one of them into a bodyless delete should not be
 * the change that crashes its caller.
 */
const EMPTY_BODY = {} as const;

/** What went wrong, preferring the server's own words. */
function describe(error: unknown): string {
    const message = error instanceof Error ? error.message : String(error);
    if (error instanceof DOMException && error.name === "AbortError") {
        return "The server took too long to answer. Please try again.";
    }
    return message || "Something went wrong. Please try again.";
}

/**
 * POST form-encoded data and return the parsed body.
 *
 * Args:
 *     url: Always `urlFor(urls.<name>, ...)` - a same-origin, server-rendered
 *         path template with only numeric ids substituted, never an arbitrary
 *         or external url.
 *     data: Fields to send.
 *
 * Returns:
 *     The parsed response body, or `{ error }` when the request was refused or
 *     never completed.
 */
export async function postForm(url: string, data: Record<string, string> | URLSearchParams): Promise<any> {
    const body = data instanceof URLSearchParams ? data : new URLSearchParams(data);
    try {
        return (await fetchJson(url, {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
            body,
            // The caller checks `.error` and toasts it; base.html's generic
            // "Request failed (HTTP 503)." on top of that is a second toast
            // saying less.
            reportsItsOwnErrors: true,
        })) ?? EMPTY_BODY;
    } catch (error) {
        return { error: describe(error) } satisfies RequestFailure;
    }
}

/**
 * GET JSON and return the parsed body, reporting a failure to the user.
 *
 * Args:
 *     url: Same-origin `urlFor(...)` path template - see {@link postForm}.
 *
 * Returns:
 *     The parsed response body, or `{ error }` when the request was refused or
 *     never completed. The caller is not expected to check: a lobby or chat
 *     history that could not be loaded has already been reported here.
 */
export async function getJson(url: string): Promise<any> {
    try {
        return (await fetchJson(url, { headers: { "X-Requested-With": "XMLHttpRequest" }, reportsItsOwnErrors: true })) ?? EMPTY_BODY;
    } catch (error) {
        const message = describe(error);
        toast.error(message);
        return { error: message } satisfies RequestFailure;
    }
}
