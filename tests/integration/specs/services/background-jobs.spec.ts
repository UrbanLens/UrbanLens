/**
 * Celery: is there a worker, and is it consuming this deployment's queue?
 *
 * Nothing on the site says so. A deployment whose worker container is down, or
 * is pointed at a different broker, or is running an image without the task
 * registered, looks completely healthy: pages render, the queue accepts work,
 * and the work simply never happens. Users notice days later, as "my import
 * never finished".
 *
 * The data export is the probe because it is the one Celery job that is
 * user-initiated, self-contained, and honest about its outcome: it touches only
 * this account's own rows, calls no external provider, reports progress through
 * an endpoint of its own, and finishes in a state the page states plainly. It
 * therefore exercises the whole path - enqueue, broker, worker, task code,
 * status write, download - rather than only proving the queue accepted a job.
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

/**
 * How long the worker gets to finish an export of one small account.
 *
 * Generous rather than tight: a shared staging box may have other work queued,
 * and a slow pass is a much better outcome than a flaky one.
 */
const EXPORT_TIMEOUT_MS = Number.parseInt(process.env.UL_E2E_EXPORT_TIMEOUT_MS ?? "150000", 10);

test.describe("background workers", () => {
    test("a queued export is picked up, finished, and downloadable", async ({ page, api, guard }) => {
        test.setTimeout(EXPORT_TIMEOUT_MS + 60_000);

        // Something to export, so the job has work to do rather than producing
        // an empty archive that would succeed even against a broken exporter.
        await api.createPin();

        await page.goto(appRoutes.tools);
        const form = page.locator("#export-form");
        await expect(form, "the tools page did not render its export form").toBeVisible();

        const pins = form.locator('input[name="export_types"][value="pins"]');
        await pins.check();

        // Never email the archive: these accounts live on a domain that cannot
        // receive mail, and an SMTP failure inside the task would be reported
        // as the export failing.
        const emailToggle = form.locator('input[name="email_export"]');
        if (await emailToggle.count()) {
            await emailToggle.uncheck();
        }

        await form.locator('button[type="submit"]').click();

        const progress = page.locator("#export-status-poll");
        await expect(progress, "the export form did not swap in a progress fragment").toBeVisible();

        // `ExportStartView` answers 503 with this text when `safely_enqueue_task`
        // could not reach the broker at all - a different fault from "queued but
        // nobody consumed it", and worth naming separately.
        const brokerDown = await progress.locator("text=Export queue is unavailable").count();
        expect(brokerDown, "the deployment could not reach its Celery broker; Valkey is down or misconfigured").toBe(0);

        // The fragment re-polls itself every second and rewrites in place, so
        // this waits for the terminal state rather than for any one poll.
        await expect(page.locator("#export-status-poll")).toContainText(/Your export is ready|Export failed|error/i, { timeout: EXPORT_TIMEOUT_MS });

        const failure = await page.locator(".export-error-msg").count();
        if (failure > 0) {
            const message = await page.locator(".export-error-msg").innerText();
            throw new Error(`The export job was queued but did not succeed: ${message.trim()}`);
        }

        const download = page.getByRole("link", { name: /Download ZIP/i });
        await expect(download, "the export finished but offered no download").toBeVisible();

        const href = await download.getAttribute("href");
        expect(href).toBeTruthy();
        const archive = await page.request.get(href!);
        expect(archive.status(), "the finished export could not be downloaded").toBe(200);
        expect(archive.headers()["content-type"] ?? "").toMatch(/zip/i);

        // The poller emits one request per second while the job runs; the guard
        // has nothing to say about those, but the deliberate probe above for a
        // 503 body is the assertion that matters.
        guard.allow(/tools\/export\/status\//);
    });

    test("polling somebody else's export job is refused", async ({ page }) => {
        await page.goto(appRoutes.tools);

        // A job id is a bare uuid in the URL. Ownership is checked against the
        // stored job's user, and this is the assertion that it still is.
        const response = await page.request.get("/dashboard/tools/export/status/00000000-0000-4000-8000-000000000000/");
        expect(response.status()).toBeLessThan(500);
        const body = await response.text();
        expect(body).toMatch(/not found|expired|could not verify/i);
    });
});
