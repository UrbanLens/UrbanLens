/**
 * The Channels half of the deployment.
 *
 * WebSockets are the part of the stack no page-load assertion reaches: a
 * different container (Daphne, not gunicorn), a different path through the
 * proxy (which has to be configured to upgrade), and a different backing
 * service (the channel layer in Valkey). Every one of those can be broken while
 * the site looks entirely healthy, and the visible symptom - notifications
 * quietly stop arriving - is one nobody reports for weeks.
 *
 * The idle-hold test is opt-in because it is slow by nature: proving a
 * keep-alive works means being idle for longer than the proxy tolerates.
 */

import { expect, test } from "../../lib/fixtures.js";
import { env } from "../../lib/env.js";
import { appRoutes } from "../../lib/routes.js";
import { observePageSockets, probeWebSocket } from "../../lib/websocket.js";

/** Close code `UserNotificationConsumer` uses for "not allowed, do not retry". */
const REFUSED = 4404;

/**
 * How long to hold a quiet socket open, in seconds.
 *
 * 0 skips the test. Cloudflare closes a tunnelled socket that has carried no
 * traffic for about 100 seconds, so 120 is the smallest value that actually
 * proves the 45-second keep-alive ping is doing its job.
 */
const IDLE_HOLD_SECONDS = Number.parseInt(process.env.UL_E2E_WS_IDLE_SECONDS ?? "0", 10);

test.describe("notification socket", () => {
    test("a signed-in session can open one", async ({ page }) => {
        // Navigated first so the handshake carries the session cookie; a socket
        // opened from about:blank has no credentials to present.
        await page.goto(appRoutes.home);

        const probe = await probeWebSocket(page, "/ws/notifications/");

        expect(probe.opened, `the socket did not open (close code ${probe.closeCode}, ${probe.closeReason})`).toBeTruthy();
        expect(probe.closeCode, "the socket was closed by the server before the probe finished").not.toBe(REFUSED);
        expect(probe.openLatencyMs).toBeLessThan(10_000);
    });

    test("an unauthenticated connection is refused, not accepted and ignored", async ({ browser }) => {
        const context = await browser.newContext({ storageState: { cookies: [], origins: [] }, ignoreHTTPSErrors: env.ignoreHttpsErrors });
        try {
            const page = await context.newPage();
            await page.goto(`${env.baseUrl}/accounts/login/`);

            const probe = await probeWebSocket(page, "/ws/notifications/");

            // What must hold is that the connection never becomes usable.
            // Accepting it and then sending nothing would look identical from
            // the client's side and would mean an anonymous connection sitting
            // in a profile's broadcast group.
            expect(probe.opened, "an anonymous WebSocket was accepted").toBeFalsy();
            expect(probe.stillOpenAtEnd).toBeFalsy();

            // The close *code* is not assertable here, and the reason is worth
            // recording. `UserNotificationConsumer` calls `close(4404)` before
            // `accept()`, so Channels never completes the handshake and Daphne
            // rejects it at the HTTP layer - the browser therefore reports 1006
            // (abnormal closure, no close frame received) and never sees 4404.
            // The documented 4404 is what a client that *did* connect and was
            // later refused would observe.
            expect([REFUSED, 1006, 1002, 1015], `unexpected close code ${probe.closeCode}`).toContain(probe.closeCode);
        } finally {
            await context.close();
        }
    });

    test("the application opens its own socket on a normal page", async ({ page }) => {
        const observer = observePageSockets(page);
        await page.goto(appRoutes.home);

        // `_notification_push.html` connects on load for any authenticated user.
        // If it silently degrades to badge polling, live notifications are dead
        // and nothing on the page says so.
        await expect
            .poll(() => observer.sockets.filter((socket) => socket.url.includes("/ws/notifications/")).length, {
                message: "the page never opened its notification socket",
            })
            .toBeGreaterThan(0);

        const socket = observer.sockets.find((entry) => entry.url.includes("/ws/notifications/"));
        expect(socket?.url.startsWith(env.websocketOrigin), `socket URL ${socket?.url} is not on the site's own origin`).toBeTruthy();
        expect(socket?.closed, "the socket closed again immediately, which is what a proxy that will not upgrade looks like").toBeFalsy();
    });

    test("a quiet socket survives the proxy's idle timeout", async ({ page }) => {
        test.skip(IDLE_HOLD_SECONDS <= 0, "Set UL_E2E_WS_IDLE_SECONDS=120 to run the idle keep-alive check.");
        test.setTimeout((IDLE_HOLD_SECONDS + 60) * 1000);

        await page.goto(appRoutes.home);

        const probe = await probeWebSocket(page, "/ws/notifications/", {
            holdMs: IDLE_HOLD_SECONDS * 1000,
            // The same keep-alive the page itself sends, at the same interval.
            // The consumer ignores it deliberately; its only job is to be traffic.
            send: [{ type: "ping" }],
            sendEveryMs: 45_000,
        });

        expect(probe.opened).toBeTruthy();
        expect(probe.stillOpenAtEnd, `the socket closed after ${IDLE_HOLD_SECONDS}s with code ${probe.closeCode} despite the keep-alive`).toBeTruthy();
    });
});
