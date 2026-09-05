/**
 * The sign-in form.
 *
 * Used both by the specs that test signing in and by `auth.setup.ts`, which
 * mints the session every other project runs on - so this is the one page
 * object whose breakage stops the entire suite, and the reason its failure
 * message goes out of its way to report the form's own error text rather than
 * "timed out waiting for navigation".
 */

import { expect, type Locator, type Page } from "@playwright/test";

import { publicRoutes } from "../routes.js";

export class LoginPage {
    readonly username: Locator;
    readonly password: Locator;
    readonly submit: Locator;
    readonly errors: Locator;
    readonly signedInNav: Locator;

    constructor(private readonly page: Page) {
        this.username = page.locator("#id_username");
        this.password = page.locator("#id_password");
        this.submit = page.locator("#password-login-form button[type=submit]");
        this.errors = page.locator(".auth-errors, .errorlist");
        // Rendered only for an authenticated user; see `AppShell`, which uses
        // the same locator as its "this page rendered as signed in" marker.
        this.signedInNav = page.locator("nav.app-nav");
    }

    async goto(): Promise<void> {
        await this.page.goto(publicRoutes.login);
        await expect(this.username).toBeVisible();
    }

    /** Fills the form and submits it, without asserting the outcome. */
    async submitCredentials(username: string, password: string): Promise<void> {
        await this.username.fill(username);
        await this.password.fill(password);
        // The form's submit handler may derive the credential in the browser
        // first (see `UrbanLensE2EE.wireLoginForm`), so the outcome is awaited
        // after the click rather than raced against it.
        await this.submit.click();
        await this.dismissRecoveryKeyDialog();
        // Waited on by what rendered, not by the URL. A URL predicate has to
        // hold at a moment Playwright happens to observe, and the page that
        // follows sign-in rewrites its own URL client-side - so "the path is no
        // longer /accounts/login" can be missed even when sign-in worked, which
        // is how this failed *after* signing in successfully.
        //
        // `Promise.any`, not `race`: a 2FA challenge is also a successful
        // sign-in, and `signIn` reports it far better than a timeout does.
        // `race` settles on the first *rejection* too, so the two identical
        // timeouts would make it a coin toss which message came out.
        await Promise.any([
            this.signedInNav.waitFor({ state: "visible" }),
            this.page.waitForURL((url) => url.pathname.startsWith("/accounts/login/2fa")),
        ]).catch((error: unknown) => {
            // `AggregateError.message` is "All promises were rejected", which
            // says less than either timeout did. Re-throw the first real one.
            const [first] = (error as AggregateError).errors ?? [];
            throw first instanceof Error ? first : (error as Error);
        });
    }

    /**
     * A first login after `provision_integration_env` regenerates an
     * account's keys shows a blocking "save your recovery key" overlay
     * before the app navigates anywhere - `e2ee-client.ts`'s
     * `showRecoveryDialog` is awaited before `window.location.assign`.
     * "Remind me later" unblocks it; the suite has no use for the key
     * itself. Swallowed on timeout, so an already-enrolled account (the
     * overlay never appears) costs only the timeout, not a failure.
     */
    private async dismissRecoveryKeyDialog(): Promise<void> {
        await this.page
            .locator(".e2ee-recovery-later")
            .click({ timeout: 3000 })
            .catch(() => {});
    }

    /**
     * Signs in and asserts a session was actually established.
     *
     * @throws When sign-in did not happen, saying why. Three quite different
     *     causes all present as "the URL never changed": the form came back
     *     with an error ("your email hasn't been verified", "too many failed
     *     attempts" - both ordinary states of a staging account), the POST was
     *     rejected before the view ran (a CSRF origin mismatch renders Django's
     *     own 403 page at the same URL), or the page's JavaScript never
     *     submitted at all. Reporting only the timeout leaves all three looking
     *     identical.
     */
    async signIn(username: string, password: string): Promise<void> {
        await this.goto();
        try {
            await this.submitCredentials(username, password);
        } catch (error) {
            throw new Error(`Sign-in as "${username}" did not happen. ${await this.diagnose()}\n\n${(error as Error).message}`);
        }

        // A 2FA challenge is not something a headless run can answer. Provisioned
        // accounts have their factors cleared, so this means the account was
        // touched by hand rather than provisioned.
        if (this.page.url().includes("/accounts/login/2fa")) {
            throw new Error(`"${username}" has a second factor registered, which this suite cannot answer. Re-run provision_integration_env to clear it.`);
        }
    }

    /** Whatever the page can say about why it is still here. */
    private async diagnose(): Promise<string> {
        // Guarded like every later read in this method: the page may still be
        // navigating when the error path runs, and an unguarded read throws
        // "Execution context was destroyed" over whatever it was about to say.
        const reported = (await this.errors.allInnerTexts().catch(() => [] as string[])).join(" | ").trim();
        if (reported) {
            return `The form reported: ${reported}`;
        }

        // Django renders its own 403/500 pages at the URL that was posted to,
        // so the URL alone cannot distinguish them from a form that simply
        // re-rendered. The heading can.
        const heading = (await this.page.locator("h1").first().innerText().catch(() => "")).trim();
        if (heading && !/sign in|welcome/i.test(heading)) {
            const detail = (await this.page.locator("p").first().innerText().catch(() => "")).trim();
            const hint = /csrf/i.test(`${heading} ${detail}`)
                ? "\nThis is almost always UL_E2E_BASE_URL not matching the deployment's own UL_SITE_URL: Django only trusts origins it was configured for, so every POST is refused while every GET works."
                : "";
            return `The server answered "${heading}"${detail ? `: ${detail}` : ""}.${hint}`;
        }

        if (await this.submit.isVisible().catch(() => false)) {
            return "The sign-in form is still on screen with no error, which usually means its JavaScript never submitted it.";
        }
        return `The page is at ${this.page.url()} and no longer shows the sign-in form.`;
    }
}
