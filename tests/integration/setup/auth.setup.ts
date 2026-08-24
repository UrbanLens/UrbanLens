/**
 * Signs in once per role and saves the browser state every other project reuses.
 *
 * Signing in is slow (a form POST, a redirect chain, and a full page render),
 * it is the same for every test, and doing it per test would make the login
 * rate limiter - which counts failures per identifier *and* per IP - a real
 * risk on a parallel run. Doing it once here and handing the rest of the suite
 * a saved session is Playwright's standard pattern and the reason this suite
 * can raise its worker count without tripping a lockout.
 *
 * This runs as a project dependency, so a failure here reports as
 * "setup failed" rather than as every UI test failing separately.
 */

import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

import { expect, test as setup } from "@playwright/test";

import { allAccounts, storageStatePath, type IntegrationAccount } from "../lib/accounts.js";
import { AppShell } from "../lib/pages/app-shell.js";
import { LoginPage } from "../lib/pages/login-page.js";

// Declared at collection time, one test per configured role, so a run with two
// accounts reports two setup steps rather than one opaque one.
for (const [role, account] of allAccounts()) {
    setup(`sign in as ${role}`, async ({ page }) => {
        await signIn(page, role, account);
    });
}

async function signIn(page: import("@playwright/test").Page, role: string, account: IntegrationAccount): Promise<void> {
    const login = new LoginPage(page);
    await login.signIn(account.username, account.password);

    // Landing anywhere other than the app means a precondition the provisioning
    // command is supposed to have set was not set. Each of these redirects is a
    // dead end for a headless run, so they are named rather than left to
    // surface as an inscrutable assertion failure later.
    const path = new URL(page.url()).pathname;
    const blockers: Record<string, string> = {
        "/dashboard/welcome/": "the account has not completed welcome onboarding",
        "/dashboard/profile/edit/": "the account has not completed profile setup",
        "/accounts/set-password/": "the account has no usable password set",
        "/dashboard/setup/": "the account is the bootstrap admin and is being sent to the setup wizard",
    };
    for (const [prefix, reason] of Object.entries(blockers)) {
        if (path.startsWith(prefix)) {
            throw new Error(`After signing in, "${account.username}" was redirected to ${path} because ${reason}. Re-run "manage.py provision_integration_env".`);
        }
    }

    const shell = new AppShell(page);
    await shell.expectSignedInAs(account.username);

    const statePath = storageStatePath(role);
    mkdirSync(dirname(statePath), { recursive: true });
    await page.context().storageState({ path: statePath });

    // Cheap insurance against saving a state with no session in it, which fails
    // silently later as "every page redirected to login".
    const saved = await page.context().storageState();
    expect(saved.cookies.some((cookie) => cookie.name === "sessionid"), "no sessionid cookie was saved; the sign-in did not establish a session").toBeTruthy();
}
