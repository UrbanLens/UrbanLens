/**
 * Registering through the signup form, then being signed in, refused and invited by any spelling of the username or
 * of the Gmail address - one account registered with a `+tag` up front, one without.
 *
 * No mail can reach a person. The addresses are Gmail addresses whose mailbox name holds a hyphen, which Gmail never
 * issues, so nobody can own one; and the site's mail guard (`services/security/mail_guard.py`) refuses exactly that
 * shape before its relay. The refusal is what makes a DEBUG deployment show its development-only verification link,
 * which is how these accounts are activated - so on a deployment without DEBUG the spec skips rather than waiting on
 * an inbox.
 *
 * The accounts are left behind: usernames `ule2e_<run>…`, addresses `ul-e2e-<run>…@gmail.com`.
 */

import { randomUUID } from "node:crypto";

import type { Browser, BrowserContext, Page } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import type { ApiClient } from "../../lib/api-client.js";
import { env, resourceName } from "../../lib/env.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { LoginPage } from "../../lib/pages/login-page.js";
import { publicRoutes } from "../../lib/routes.js";
import { waitForOrNull } from "../../lib/waiting.js";

/** Not a secret: the accounts are throwaway. Kept free of the letters in the usernames and addresses, which the similarity validator would refuse. */
const PASSWORD = "Pty-3Chiwok-7Qvxzd-#9!";
const DELIVERY_TIMEOUT_MS = 60_000;

interface Registrant {
    username: string;
    /** The mailbox name, which the undeliverable-address guard recognises by its hyphens. */
    mailbox: string;
    /** The address as typed at signup. */
    email: string;
}

const run = randomUUID().replace(/-/g, "").slice(0, 8);
const tagged: Registrant = { username: `ule2e_${run}p`, mailbox: `ul-e2e-${run}p`, email: `ul-e2e-${run}p+urbanlens@gmail.com` };
const plain: Registrant = { username: `ule2e_${run}q`, mailbox: `ul-e2e-${run}q`, email: `ul-e2e-${run}q@gmail.com` };

function usernameSpellings(username: string): string[] {
    return [username.toUpperCase(), username.replace("_", "."), `_${username.replace("_", "").split("").join("-")}-`];
}

function dotted(mailbox: string): string {
    return mailbox.split("").join(".");
}

/** Every one Gmail would deliver to the same mailbox, whichever form was registered. */
function addressSpellings(mailbox: string): string[] {
    return [`${mailbox}@gmail.com`, `${dotted(mailbox)}+other@gmail.com`, `${mailbox.toUpperCase()}@googlemail.com`, `${mailbox}+urbanlens@gmail.com`];
}

async function anonymousContext(browser: Browser): Promise<{ context: BrowserContext; page: Page }> {
    const context = await browser.newContext({ baseURL: env.baseUrl, storageState: { cookies: [], origins: [] }, ignoreHTTPSErrors: env.ignoreHttpsErrors });
    return { context, page: await context.newPage() };
}

/** Fills and submits the signup form; the page is left wherever the site sent it. */
async function submitSignup(page: Page, username: string, email: string): Promise<void> {
    const response = await page.goto(publicRoutes.signup);
    test.skip(response?.status() === 403, "signup is restricted to invitations on this deployment");
    await page.locator("#id_email").fill(email);
    await page.locator("#id_username").fill(username);
    await page.locator("#id_password1").fill(PASSWORD);
    await page.locator("#id_password2").fill(PASSWORD);
    // The form derives its credential in the browser before it really submits.
    await Promise.all([page.waitForURL((url) => url.pathname !== publicRoutes.signup || url.search !== "", { timeout: env.navigationTimeoutMs }).catch(() => undefined), page.locator("#signup-form button[type=submit]").click()]);
    await page.waitForLoadState("domcontentloaded");
}

async function register(browser: Browser, registrant: Registrant): Promise<void> {
    const { context, page } = await anonymousContext(browser);
    try {
        await submitSignup(page, registrant.username, registrant.email);
        await expect(page, `signing up ${registrant.username} did not reach the "check your email" page`).toHaveURL(/\/verify-email\/sent\//);
        const link = page.locator(".auth-debug-block a");
        test.skip((await link.count()) === 0, "no development verification link: this deployment is not DEBUG, or its mail relay accepted the address");
        await page.goto((await link.getAttribute("href"))!);
    } finally {
        await context.close();
    }
}

async function signsInAs(browser: Browser, identifier: string, username: string): Promise<void> {
    const { context, page } = await anonymousContext(browser);
    try {
        await new LoginPage(page).signIn(identifier, PASSWORD);
        await new AppShell(page).expectSignedInAs(username);
    } finally {
        await context.close();
    }
}

async function isRefused(browser: Browser, identifier: string): Promise<boolean> {
    const { context, page } = await anonymousContext(browser);
    try {
        const login = new LoginPage(page);
        await login.goto();
        await login.username.fill(identifier);
        await login.password.fill(PASSWORD);
        await login.submit.click();
        await expect(login.errors.first()).toBeVisible({ timeout: env.navigationTimeoutMs });
        return (await login.signedInNav.count()) === 0;
    } finally {
        await context.close();
    }
}

/** Waits for a notification whose text matches, on the invitee's own notifications page. */
async function waitForNotificationText(page: Page, pattern: RegExp): Promise<boolean> {
    const found = await waitForOrNull(
        async () => {
            await page.goto("/dashboard/notifications/");
            return pattern.test(await page.locator("main").innerText());
        },
        (seen) => seen,
        { what: `a notification matching ${pattern}`, timeoutMs: DELIVERY_TIMEOUT_MS, intervalMs: 3_000 },
    );
    return found === true;
}

async function invitee(browser: Browser, registrant: Registrant): Promise<{ context: BrowserContext; page: Page }> {
    const { context, page } = await anonymousContext(browser);
    await new LoginPage(page).signIn(registrant.username, PASSWORD);
    return { context, page };
}

async function inviteToTrip(api: ApiClient, email: string): Promise<string> {
    const trip = await api.json<{ slug: string; name: string }>("post", "trips/", { name: resourceName(`permutation trip ${randomUUID().slice(0, 8)}`) });
    api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));
    const invited = await api.post(`trips/${trip.slug}/invitations/`, { email });
    expect(invited.status(), `inviting ${email} answered ${invited.status()}`).toBe(202);
    return trip.name;
}

test.describe.configure({ mode: "serial" });

test.describe("identity permutations", () => {
    test.beforeAll(async ({ browser }) => {
        await register(browser, tagged);
        await register(browser, plain);
    });

    test("an address registered with a +tag signs in by every spelling of it and of the username", async ({ browser }) => {
        for (const identifier of [tagged.username, ...usernameSpellings(tagged.username), tagged.email, ...addressSpellings(tagged.mailbox)]) {
            await test.step(identifier, () => signsInAs(browser, identifier, tagged.username));
        }
    });

    test("an address registered without a tag signs in by every spelling of it, tagged or not", async ({ browser }) => {
        for (const identifier of [...usernameSpellings(plain.username), ...addressSpellings(plain.mailbox)]) {
            await test.step(identifier, () => signsInAs(browser, identifier, plain.username));
        }
    });

    test("no second account can be registered with a spelling of a taken username or address", async ({ browser }) => {
        const { context, page } = await anonymousContext(browser);
        try {
            await submitSignup(page, `_${plain.username.toUpperCase()}_`, `ul-e2e-${run}s@gmail.com`);
            await expect(page.locator("#id_username ~ .errorlist, .errorlist")).toContainText(/already exists/);
        } finally {
            await context.close();
        }

        const latecomers: [string, string][] = [
            [`ule2e_${run}r`, `${dotted(plain.mailbox)}+again@googlemail.com`],
            [`ule2e_${run}t`, `${tagged.mailbox}@gmail.com`],
        ];
        for (const [username, email] of latecomers) {
            await test.step(`${username} with ${email}`, async () => {
                const attempt = await anonymousContext(browser);
                try {
                    await submitSignup(attempt.page, username, email);
                    // Whatever the page says, no account may exist to sign in to - including after "verification".
                    const link = attempt.page.locator(".auth-debug-block a");
                    if ((await link.count()) > 0) {
                        await attempt.page.goto((await link.getAttribute("href"))!);
                    }
                } finally {
                    await attempt.context.close();
                }
                expect(await isRefused(browser, username), `a second account ${username} was created for ${email}`).toBe(true);
            });
        }
    });

    test("trip and friend invitations to any spelling reach the account", async ({ browser, api }) => {
        const tripName = await inviteToTrip(api, `${dotted(tagged.mailbox)}@gmail.com`);
        const friendSent = await api.post("friend-invites/", { email: `${plain.mailbox.toUpperCase()}+invited@googlemail.com` });
        expect(friendSent.status()).toBe(200);

        const taggedSession = await invitee(browser, tagged);
        try {
            expect(await waitForNotificationText(taggedSession.page, new RegExp(tripName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))), "the trip invitation did not reach the +tag-registered account").toBe(true);
        } finally {
            await taggedSession.context.close();
        }
        const plainSession = await invitee(browser, plain);
        try {
            expect(await waitForNotificationText(plainSession.page, /wants to be your friend/), "the friend invitation did not reach the untagged account").toBe(true);
        } finally {
            await plainSession.context.close();
        }
    });
});
