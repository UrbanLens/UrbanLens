/**
 * Watches a page for the failures an assertion never looks at.
 *
 * "The heading rendered" is a weak claim when a script threw before it could
 * wire up the page's behaviour, when the JavaScript bundle 404'd, or when an
 * HTMX swap came back 500 and the only trace is a toast that has since faded.
 * Every one of those leaves the page looking approximately right, and every one
 * is exactly the regression a staging run exists to catch.
 *
 * The `page` fixture attaches one of these to every UI test and asserts on it
 * during teardown, so a spec gets the check without opting in. A spec that
 * legitimately provokes an error calls {@link PageGuard.allow} to narrow it.
 */

import type { Page, Request, Response, ConsoleMessage } from "@playwright/test";

/** One thing that went wrong on the page, in the order it happened. */
export interface PageProblem {
    kind: "console" | "pageerror" | "requestfailed" | "http";
    detail: string;
    url: string;
}

/**
 * Noise that is not evidence of a defect in this application.
 *
 * Kept deliberately short. Every entry is a place where a real failure could
 * hide, so the bar for adding one is that it fires on a *healthy* deployment.
 */
const DEFAULT_ALLOWED: RegExp[] = [
    // A page that navigates mid-flight cancels its own in-flight requests.
    /net::ERR_ABORTED/,
    // Third-party raster tiles rate-limit and time out on their own schedule.
    // Their absence is visible to a human but is not an UrbanLens regression,
    // and letting it fail the suite makes every map test intermittently red.
    /^https?:\/\/[a-z]?\.?tile\.openstreetmap\.org\//,
    /^https?:\/\/server\.arcgisonline\.com\//,
    /^https?:\/\/[a-z0-9-]+\.basemaps\.cartocdn\.com\//,
    /^https?:\/\/tiles\.stadiamaps\.com\//,
    // Browsers log this for any site without a favicon at the default path.
    /favicon\.ico/,
    // Chromium emits this for third-party cookies it declines to send. It is a
    // browser policy notice, not a page error.
    /third-party cookie/i,
];

export class PageGuard {
    private readonly problems: PageProblem[] = [];
    private readonly allowed: RegExp[] = [...DEFAULT_ALLOWED];
    private detached = false;

    private constructor(private readonly page: Page) {}

    /** Starts watching `page`. Call once, before the first navigation. */
    static attach(page: Page): PageGuard {
        const guard = new PageGuard(page);

        page.on("console", guard.onConsole);
        page.on("pageerror", guard.onPageError);
        page.on("requestfailed", guard.onRequestFailed);
        page.on("response", guard.onResponse);

        return guard;
    }

    /**
     * Stops watching.
     *
     * Necessary because a page can outlive the check - teardown navigations and
     * context close both produce cancelled requests that are not the test's
     * business.
     */
    detach(): void {
        if (this.detached) {
            return;
        }
        this.detached = true;
        this.page.off("console", this.onConsole);
        this.page.off("pageerror", this.onPageError);
        this.page.off("requestfailed", this.onRequestFailed);
        this.page.off("response", this.onResponse);
    }

    /**
     * Stops treating anything matching `pattern` as a problem.
     *
     * Applies to already-recorded problems as well as future ones, so it can be
     * called after the action that provokes the error rather than before.
     */
    allow(pattern: RegExp | string): void {
        this.allowed.push(typeof pattern === "string" ? new RegExp(escapeRegExp(pattern)) : pattern);
    }

    /** Everything recorded so far that is not covered by an allow rule. */
    get failures(): PageProblem[] {
        return this.problems.filter((problem) => !this.allowed.some((pattern) => pattern.test(problem.detail) || pattern.test(problem.url)));
    }

    /** A report suitable for an assertion message, or null when clean. */
    describe(): string | null {
        const failures = this.failures;
        if (failures.length === 0) {
            return null;
        }
        const lines = failures.map((problem) => `  [${problem.kind}] ${problem.detail}${problem.url ? `\n            ${problem.url}` : ""}`);
        return `${failures.length} page problem(s) on ${this.page.url()}:\n${lines.join("\n")}`;
    }

    // Bound properties rather than methods, so `page.off` can remove the exact
    // same function reference `page.on` was given.

    private readonly onConsole = (message: ConsoleMessage): void => {
        if (message.type() !== "error") {
            return;
        }
        // Chromium logs this for every non-2xx response, including the page's
        // own document - so a test that deliberately asserts a 404 would fail
        // on the console instead. Nothing is lost by dropping it: the response
        // and requestfailed handlers below see the same events with the method,
        // the resource type and the status, and decide properly.
        if (/^Failed to load resource/.test(message.text())) {
            return;
        }
        this.problems.push({ kind: "console", detail: message.text(), url: message.location().url });
    };

    private readonly onPageError = (error: Error): void => {
        this.problems.push({
            kind: "pageerror",
            detail: `Uncaught ${error.name}: ${error.message}`,
            url: this.page.url(),
        });
    };

    private readonly onRequestFailed = (request: Request): void => {
        const failure = request.failure();
        this.problems.push({
            kind: "requestfailed",
            detail: `${request.method()} failed: ${failure?.errorText ?? "unknown error"}`,
            url: request.url(),
        });
    };

    private readonly onResponse = (response: Response): void => {
        const status = response.status();
        if (status < 400) {
            return;
        }
        // A 5xx is always this application's problem. A 4xx is only reported
        // for subresources - a page-level 4xx is frequently the thing under
        // test (an unauthorised page, a deliberate 404), and the spec asserting
        // it should own that assertion rather than having it duplicated here.
        const isDocument = response.request().resourceType() === "document";
        if (status < 500 && isDocument) {
            return;
        }
        this.problems.push({
            kind: "http",
            detail: `${response.request().method()} returned ${status} for a ${response.request().resourceType()}`,
            url: response.url(),
        });
    };
}

function escapeRegExp(literal: string): string {
    return literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
