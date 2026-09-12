/**
 * The three game clients' HTTP helpers, in one place and checking their responses.
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
 */
export async function postForm(url: string, data: Record<string, string> | URLSearchParams): Promise<any> {
    const body = data instanceof URLSearchParams ? data : new URLSearchParams(data);
    try {
        return (await fetchJson(url, {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
            body,
            // The caller checks `.error` and toasts it; base.html's generic "Request failed (HTTP 503)." on top of that is a second toast saying.
            reportsItsOwnErrors: true,
        })) ?? EMPTY_BODY;
    } catch (error) {
        return { error: describe(error) } satisfies RequestFailure;
    }
}

/**
 * POST multipart form data (a file upload) and return the parsed body.
 */
export async function postMultipart(url: string, data: FormData): Promise<any> {
    try {
        return (await fetchJson(url, {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken() },
            body: data,
            // Same reasoning as postForm: the caller checks `.error` and
            // toasts it itself.
            reportsItsOwnErrors: true,
        })) ?? EMPTY_BODY;
    } catch (error) {
        return { error: describe(error) } satisfies RequestFailure;
    }
}

/**
 * GET JSON and return the parsed body, reporting a failure to the user.
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
