/**
 * The credentials half of the run configuration.
 *
 * Accounts come from one of two places, checked in this order:
 *
 * 1. A manifest written by `manage.py provision_integration_env --format json`
 *    on the target deployment, pointed at by `UL_E2E_ACCOUNTS_FILE`. This is
 *    the intended path: it provisions every role at once, marks each account
 *    verified and active (sign-up alone leaves an account inactive pending an
 *    emailed link, which a test runner cannot click), and mints API keys with
 *    the scopes the API specs need.
 * 2. Plain environment variables, for a one-off run against an account that
 *    already exists.
 *
 * Roles are resolved lazily. A spec that only needs `primary` runs fine on a
 * deployment where no `secondary` account was provisioned; the specs that do
 * need one skip themselves rather than failing.
 */

import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

import { ConfigurationError, INTEGRATION_ROOT } from "./env.js";

/** One provisioned account and everything needed to act as it. */
export interface IntegrationAccount {
    /** Stable name specs refer to, e.g. `primary`. */
    role: string;
    username: string;
    email: string;
    password: string;
    /** Raw `ulk_...` external-API key, or null when the account has none. */
    apiKey: string | null;
    /** Scopes granted to `apiKey`, for specs that assert scope enforcement. */
    scopes: string[];
    /**
     * A second, deliberately under-scoped key for the same account.
     *
     * The only way to tell "scope enforcement works" apart from "the endpoint
     * happens to be reachable" is a credential that is valid and insufficient.
     */
    restrictedApiKey: string | null;
    /** Scopes granted to {@link IntegrationAccount.restrictedApiKey}. */
    restrictedScopes: string[];
    /** Profile UUID, when the provisioner reported one. */
    profileUuid: string | null;
    /** Whether this account has staff/site-admin rights. */
    isStaff: boolean;
}

/** The shape `provision_integration_env --format json` writes. */
interface AccountsManifest {
    generated_at?: string;
    site_url?: string;
    environment?: string;
    accounts: Array<{
        role: string;
        username: string;
        email?: string;
        password: string;
        api_key?: string | null;
        scopes?: string[];
        restricted_api_key?: string | null;
        restricted_scopes?: string[];
        profile_uuid?: string | null;
        is_staff?: boolean;
    }>;
}

export const PRIMARY_ROLE = "primary";
export const SECONDARY_ROLE = "secondary";
export const STAFF_ROLE = "staff";

function fromManifest(path: string): Map<string, IntegrationAccount> {
    const absolute = isAbsolute(path) ? path : resolve(INTEGRATION_ROOT, path);
    if (!existsSync(absolute)) {
        throw new ConfigurationError(`UL_E2E_ACCOUNTS_FILE points at "${absolute}", which does not exist.`);
    }

    let manifest: AccountsManifest;
    try {
        manifest = JSON.parse(readFileSync(absolute, "utf8")) as AccountsManifest;
    } catch (error) {
        throw new ConfigurationError(`could not parse the accounts manifest at "${absolute}": ${(error as Error).message}`);
    }

    if (!Array.isArray(manifest.accounts) || manifest.accounts.length === 0) {
        throw new ConfigurationError(`the accounts manifest at "${absolute}" lists no accounts.`);
    }

    cachedSiteUrl = manifest.site_url ?? null;

    const accounts = new Map<string, IntegrationAccount>();
    for (const entry of manifest.accounts) {
        if (!entry.role || !entry.username || !entry.password) {
            throw new ConfigurationError(`an entry in "${absolute}" is missing role, username or password.`);
        }
        accounts.set(entry.role, {
            role: entry.role,
            username: entry.username,
            email: entry.email ?? "",
            password: entry.password,
            apiKey: entry.api_key ?? null,
            scopes: entry.scopes ?? [],
            restrictedApiKey: entry.restricted_api_key ?? null,
            restrictedScopes: entry.restricted_scopes ?? [],
            profileUuid: entry.profile_uuid ?? null,
            isStaff: entry.is_staff ?? false,
        });
    }
    return accounts;
}

function fromEnvironment(): Map<string, IntegrationAccount> {
    const accounts = new Map<string, IntegrationAccount>();

    const define = (role: string, prefix: string, required: boolean): void => {
        const username = process.env[`${prefix}USERNAME`]?.trim();
        const password = process.env[`${prefix}PASSWORD`]?.trim();
        if (!username || !password) {
            if (required) {
                throw new ConfigurationError(
                    `no accounts configured. Set UL_E2E_ACCOUNTS_FILE to a manifest from ` +
                        `"manage.py provision_integration_env", or set ${prefix}USERNAME and ${prefix}PASSWORD.`,
                );
            }
            return;
        }
        accounts.set(role, {
            role,
            username,
            password,
            email: process.env[`${prefix}EMAIL`]?.trim() ?? "",
            apiKey: process.env[`${prefix}API_KEY`]?.trim() || null,
            scopes: (process.env[`${prefix}SCOPES`]?.trim() ?? "").split(",").map((s) => s.trim()).filter(Boolean),
            restrictedApiKey: process.env[`${prefix}RESTRICTED_API_KEY`]?.trim() || null,
            restrictedScopes: (process.env[`${prefix}RESTRICTED_SCOPES`]?.trim() ?? "").split(",").map((s) => s.trim()).filter(Boolean),
            profileUuid: process.env[`${prefix}PROFILE_UUID`]?.trim() || null,
            isStaff: role === STAFF_ROLE,
        });
    };

    define(PRIMARY_ROLE, "UL_E2E_", true);
    define(SECONDARY_ROLE, "UL_E2E_SECONDARY_", false);
    define(STAFF_ROLE, "UL_E2E_STAFF_", false);
    return accounts;
}

let cached: Map<string, IntegrationAccount> | null = null;
let cachedSiteUrl: string | null = null;

/** Every configured account, keyed by role. Parsed once per process. */
export function allAccounts(): Map<string, IntegrationAccount> {
    if (cached === null) {
        const manifestPath = process.env.UL_E2E_ACCOUNTS_FILE?.trim();
        cached = manifestPath ? fromManifest(manifestPath) : fromEnvironment();
    }
    return cached;
}

/**
 * The deployment's own `UL_SITE_URL`, as the provisioner reported it.
 *
 * Null when accounts came from environment variables rather than a manifest.
 * Worth having because Django only trusts origins it was configured for: a run
 * pointed at a URL the deployment does not know itself by renders every page
 * perfectly and has every POST refused, which reads as a broken application.
 */
export function reportedSiteUrl(): string | null {
    allAccounts();
    return cachedSiteUrl;
}

/** The account for `role`, or null when this run has none. */
export function optionalAccount(role: string): IntegrationAccount | null {
    return allAccounts().get(role) ?? null;
}

/** The account for `role`, failing loudly when it was not provisioned. */
export function requireAccount(role: string): IntegrationAccount {
    const account = optionalAccount(role);
    if (!account) {
        throw new ConfigurationError(
            `no "${role}" account is configured. Re-run "manage.py provision_integration_env" ` +
                `with --roles including "${role}", or set the matching UL_E2E_* variables.`,
        );
    }
    return account;
}

/** The account for `role`, failing loudly when it has no external-API key. */
export function requireApiKey(role: string): string {
    const account = requireAccount(role);
    if (!account.apiKey) {
        throw new ConfigurationError(
            `the "${role}" account has no API key. Provision with --with-api-keys, or set the matching *_API_KEY variable.`,
        );
    }
    return account.apiKey;
}

/** Path of the browser session state minted for `role` by auth.setup.ts. */
export function storageStatePath(role: string): string {
    return resolve(INTEGRATION_ROOT, ".auth", `${role}.json`);
}
