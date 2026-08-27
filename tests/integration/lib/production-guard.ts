/**
 * Pure hostname-matching logic behind the production-write guard.
 *
 * Split out of `env.ts` so it can be imported and tested on its own, without
 * triggering that module's eager, side-effecting environment validation
 * (which throws immediately at import time if `UL_E2E_BASE_URL` is unset).
 * See `env.ts`'s own `DEFAULT_PRODUCTION_HOSTS` docstring for why this
 * suite refuses production by default and why the match must be exact.
 */

/**
 * Whether *hostname* is one this suite refuses to run against.
 *
 * Matching is exact, never a substring or suffix: `s1.dev.urbanlens.org`
 * must not be caught by an entry for `urbanlens.org`, since a run against a
 * dev/staging host that merely shares a domain suffix with production is the
 * entire point of the suite existing.
 *
 * @param hostname - The candidate host, e.g. from `UL_E2E_BASE_URL`.
 * @param productionHosts - The denylist to check against (already
 *   lowercased, as `env.ts`'s `readList` produces).
 */
export function isProductionHost(hostname: string, productionHosts: string[]): boolean {
    return productionHosts.includes(hostname.toLowerCase());
}
