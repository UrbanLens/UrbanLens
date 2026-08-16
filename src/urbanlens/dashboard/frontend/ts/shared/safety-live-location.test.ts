import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import { installSafetyLiveLocation } from "./safety-live-location";

const realFetch = globalThis.fetch;

interface Harness {
    toggle: HTMLInputElement;
    errors: string[];
    /** Deliver a position to the watcher, as the browser would. */
    emit(lat?: number, lng?: number): void;
    /** Trigger the watcher's error callback with a GeolocationPositionError code. */
    emitError(code?: number): void;
    watching(): boolean;
    /** Advance the clock past the throttle window. */
    advance(ms?: number): void;
    controller: ReturnType<typeof installSafetyLiveLocation>;
}

let posted: { url: string; body: FormData }[] = [];

/** Answer every request with the given status. */
function stubFetch(status = 204): void {
    posted = [];
    globalThis.fetch = ((url: string, init: RequestInit) => {
        posted.push({ url: String(url), body: init.body as FormData });
        return Promise.resolve({ ok: status >= 200 && status < 300, status } as Response);
    }) as unknown as typeof fetch;
}

function stubNetworkFailure(): void {
    posted = [];
    globalThis.fetch = (() => Promise.reject(new Error("offline"))) as unknown as typeof fetch;
}

function setup(checked = false, hasGeolocation = true): Harness {
    let clock = 1_000_000;
    document.body.innerHTML = `<input type="checkbox" id="t" ${checked ? "checked" : ""}>`;
    const toggle = document.getElementById("t") as HTMLInputElement;
    const errors: string[] = [];

    let onPosition: ((p: GeolocationPosition) => void) | null = null;
    let onError: ((e: GeolocationPositionError) => void) | null = null;
    let nextId = 1;
    let activeId: number | null = null;

    const geolocation = {
        watchPosition: (success: (p: GeolocationPosition) => void, error: (e: GeolocationPositionError) => void) => {
            onPosition = success;
            onError = error;
            activeId = nextId++;
            return activeId;
        },
        clearWatch: () => {
            activeId = null;
            onPosition = null;
        },
    };

    const controller = installSafetyLiveLocation({
        toggle,
        toggleUrl: "/toggle/",
        updateUrl: "/update/",
        csrfToken: "tok",
        geolocation: hasGeolocation ? (geolocation as unknown as Geolocation) : null,
        notify: (_kind, message) => errors.push(message),
        now: () => clock,
    });

    return {
        toggle,
        errors,
        emit: (lat = 40, lng = -73) => onPosition?.({ coords: { latitude: lat, longitude: lng, accuracy: 5 } } as GeolocationPosition),
        emitError: (code = 2) => onError?.({ code } as GeolocationPositionError),
        watching: () => activeId !== null,
        advance: (ms = 31_000) => {
            clock += ms;
        },
        controller,
    };
}

/** Flip the toggle and let the request settle. */
async function flip(h: Harness, on: boolean): Promise<void> {
    h.toggle.checked = on;
    h.toggle.dispatchEvent(new Event("change"));
    await new Promise((resolve) => setTimeout(resolve, 5));
}

async function settle(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 5));
}

beforeEach(() => {
    stubFetch(204);
    document.body.innerHTML = "";
});

afterEach(() => {
    globalThis.fetch = realFetch;
});

describe("turning sharing on", () => {
    test("starts watching and tells the server", async () => {
        const h = setup();
        await flip(h, true);

        expect(h.watching()).toBe(true);
        expect(posted[0]?.url).toBe("/toggle/");
        expect(posted[0]?.body.get("enabled")).toBe("1");
    });

    test("starts watching immediately, without waiting for the server", () => {
        const h = setup();
        h.toggle.checked = true;
        h.toggle.dispatchEvent(new Event("change"));
        expect(h.watching()).toBe(true); // not yet awaited
    });

    test("already-on at page load starts watching", () => {
        const h = setup(true);
        expect(h.watching()).toBe(true);
    });
});

describe("when the server refuses the toggle", () => {
    // fetch does not reject on an HTTP error, so a .catch-only handler never ran
    // and the page went on claiming to share a location that was going nowhere.
    for (const status of [400, 403, 404, 500]) {
        test(`${status} reverts the switch and stops watching`, async () => {
            stubFetch(status);
            const h = setup();
            await flip(h, true);

            expect(h.toggle.checked).toBe(false);
            expect(h.watching()).toBe(false);
            expect(h.errors).toHaveLength(1);
        });
    }

    test("a network failure is handled the same way", async () => {
        stubNetworkFailure();
        const h = setup();
        await flip(h, true);

        expect(h.toggle.checked).toBe(false);
        expect(h.watching()).toBe(false);
        expect(h.errors).toHaveLength(1);
    });

    test("a refused turn-off restores the switch to on", async () => {
        const h = setup(true);
        stubFetch(500);
        await flip(h, false);

        expect(h.toggle.checked).toBe(true);
    });
});

describe("reporting positions", () => {
    test("posts the first fix straight away", async () => {
        const h = setup(true);
        h.emit(40.7, -73.9);
        await settle();

        const update = posted.find((p) => p.url === "/update/");
        expect(update?.body.get("latitude")).toBe("40.7");
        expect(update?.body.get("longitude")).toBe("-73.9");
        expect(update?.body.get("accuracy")).toBe("5");
    });

    test("throttles a burst to a single request", async () => {
        const h = setup(true);
        h.emit(40.1);
        h.emit(40.2);
        h.emit(40.3);
        await settle();

        expect(posted.filter((p) => p.url === "/update/")).toHaveLength(1);
    });

    test("a 204 counts as success - it is also what the rate limiter returns", async () => {
        const h = setup(true);
        h.emit();
        await settle();

        expect(h.controller.consecutiveFailures()).toBe(0);
        expect(h.errors).toHaveLength(0);
    });
});

describe("when position reports are rejected", () => {
    /** Report a position, having moved past the throttle window first. */
    async function report(h: Harness): Promise<void> {
        h.advance();
        h.emit();
        await settle();
    }

    test("the server's 400 is noticed at all, even though fetch resolves", async () => {
        // The original code only had a .catch, which an HTTP error never triggers.
        const h = setup(true);
        stubFetch(400);
        await report(h);

        expect(h.controller.consecutiveFailures()).toBe(1);
    });

    test("one failure does not warn - a single blip is noise", async () => {
        const h = setup(true);
        stubFetch(400);
        await report(h);

        expect(h.errors).toHaveLength(0);
    });

    test("two in a row warns the owner", async () => {
        const h = setup(true);
        stubFetch(400);
        await report(h);
        await report(h);

        expect(h.controller.consecutiveFailures()).toBe(2);
        expect(h.errors).toHaveLength(1);
        expect(h.errors[0]).toContain("old position");
    });

    test("a continuing outage does not nag every thirty seconds", async () => {
        const h = setup(true);
        stubFetch(400);
        for (let i = 0; i < 6; i += 1) await report(h);

        expect(h.controller.consecutiveFailures()).toBe(6);
        expect(h.errors).toHaveLength(1);
    });

    test("recovering resets the count, so the next blip is quiet again", async () => {
        const h = setup(true);
        stubFetch(400);
        await report(h);
        expect(h.controller.consecutiveFailures()).toBe(1);

        stubFetch(204);
        await report(h);
        expect(h.controller.consecutiveFailures()).toBe(0);
    });

    test("after recovering, a fresh outage can warn again", async () => {
        const h = setup(true);
        stubFetch(400);
        await report(h);
        await report(h);
        expect(h.errors).toHaveLength(1);

        stubFetch(204);
        await report(h);

        stubFetch(400);
        await report(h);
        await report(h);
        expect(h.errors).toHaveLength(2);
    });

    test("a network failure counts the same as a rejection", async () => {
        const h = setup(true);
        stubNetworkFailure();
        await report(h);
        await report(h);

        expect(h.errors).toHaveLength(1);
    });
});

describe("turning sharing off", () => {
    test("stops watching and tells the server", async () => {
        const h = setup(true);
        await flip(h, false);

        expect(h.watching()).toBe(false);
        expect(posted.find((p) => p.url === "/toggle/")?.body.get("enabled")).toBe("0");
    });

    test("no further positions are reported", async () => {
        const h = setup(true);
        await flip(h, false);
        posted = [];
        h.emit();
        await settle();

        expect(posted.filter((p) => p.url === "/update/")).toHaveLength(0);
    });
});

describe("when the browser cannot provide a location", () => {
    test("the owner is told", () => {
        const h = setup(true);
        h.emitError();
        expect(h.errors[0]).toContain("Could not get your location");
    });

    test("a transient failure keeps sharing on - it usually recovers", () => {
        const h = setup(true);
        h.emitError(3); // TIMEOUT

        expect(h.watching()).toBe(true);
        expect(h.toggle.checked).toBe(true);
    });

    test("a repeating transient failure is only said once", () => {
        const h = setup(true);
        for (let i = 0; i < 5; i += 1) h.emitError(2); // POSITION_UNAVAILABLE

        expect(h.errors).toHaveLength(1);
    });
});

describe("when location permission is denied", () => {
    // Denial is permanent until the user changes a browser setting, so leaving
    // the switch on left the server - and the partner watching the check-in -
    // expecting positions that would never be sent.
    test("sharing is switched off rather than left looking healthy", async () => {
        const h = setup(true);
        h.emitError(1); // PERMISSION_DENIED
        await settle();

        expect(h.watching()).toBe(false);
        expect(h.toggle.checked).toBe(false);
    });

    test("the server is told sharing has stopped", async () => {
        const h = setup(true);
        posted = [];
        h.emitError(1);
        await settle();

        expect(posted.filter((p) => p.url === "/toggle/").at(-1)?.body.get("enabled")).toBe("0");
    });

    test("the owner is told why, and how to fix it", async () => {
        const h = setup(true);
        h.emitError(1);
        await settle();

        expect(h.errors).toHaveLength(1);
        expect(h.errors[0]).toContain("permission");
    });

    test("no positions are reported afterwards", async () => {
        const h = setup(true);
        h.emitError(1);
        await settle();
        posted = [];
        h.emit();
        await settle();

        expect(posted.filter((p) => p.url === "/update/")).toHaveLength(0);
    });
});

describe("when the browser has no geolocation at all", () => {
    test("turning sharing on reverts instead of pretending to work", async () => {
        const h = setup(false, false);
        await flip(h, true);

        expect(h.toggle.checked).toBe(false);
        expect(h.errors).toHaveLength(1);
    });

    test("the server is never told sharing is on", async () => {
        const h = setup(false, false);
        await flip(h, true);

        expect(posted.some((p) => p.body.get("enabled") === "1")).toBe(false);
        expect(posted.filter((p) => p.url === "/toggle/").at(-1)?.body.get("enabled")).toBe("0");
    });

    test("already-on at page load is corrected", async () => {
        const h = setup(true, false);
        await settle();

        expect(h.toggle.checked).toBe(false);
        expect(posted.filter((p) => p.url === "/toggle/").at(-1)?.body.get("enabled")).toBe("0");
    });
});
