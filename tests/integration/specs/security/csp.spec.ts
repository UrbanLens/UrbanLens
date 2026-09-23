/**
 * The Content-Security-Policy is enforced in the browser, and the page guard notices when it bites.
 *
 * Every other spec relies on the second half: a violation any page raises fails the spec that
 * opened it, so a regression that needs a new origin or `eval` cannot pass the suite unnoticed.
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

test.describe("the Content-Security-Policy", () => {
    test("refuses a script from an origin it does not name", async ({ page, guard }) => {
        await page.goto(appRoutes.home);

        const ran = await page.evaluate(
            () =>
                new Promise<boolean>((resolve) => {
                    const script = document.createElement("script");
                    script.src = "https://csp-probe.invalid/probe.js";
                    script.onload = () => resolve(true);
                    script.onerror = () => resolve(false);
                    document.head.appendChild(script);
                }),
        );

        expect(ran, "a script from an unlisted origin loaded").toBe(false);
        await expect
            .poll(() => guard.failures.filter((problem) => problem.kind === "csp").map((problem) => problem.detail), { message: "the refusal never reached the page guard" })
            .toContainEqual(expect.stringMatching(/^enforce script-src(-elem)? refused https:\/\/csp-probe\.invalid/));
        guard.allow(/csp-probe\.invalid/);
    });

    test("refuses eval, and the page guard records it", async ({ page, guard }) => {
        await page.goto(appRoutes.home);

        // From a page task: eval inside DevTools' own evaluation is exempt from the page's policy.
        await page.evaluate(() => {
            const script = document.createElement("script");
            script.textContent = "try { eval('1'); document.documentElement.dataset.cspEval = 'ran'; } catch (e) { document.documentElement.dataset.cspEval = 'refused'; }";
            setTimeout(() => document.head.appendChild(script), 0);
        });
        await page.waitForFunction(() => document.documentElement.dataset.cspEval !== undefined);
        const threw = await page.evaluate(() => document.documentElement.dataset.cspEval === "refused");

        expect(threw, "eval ran, so 'unsafe-eval' is allowed or the policy is not enforced").toBe(true);
        await expect.poll(() => guard.failures.filter((problem) => problem.kind === "csp").map((problem) => problem.detail)).toContainEqual(expect.stringMatching(/^enforce script-src refused eval/));
        // A caught eval logs nothing to the console; only the violation event shows it.
        guard.allow(/^enforce script-src refused eval/);
    });
});
