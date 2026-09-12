/** Pure hostname-matching logic behind the production-write guard. */

/**
 * Whether *hostname* is one this suite refuses to run against.
 *
 * @param hostname - The candidate host, e.g. from `UL_E2E_BASE_URL`.
 * @param productionHosts - The denylist to check against (already lowercased, as `env.ts`'s `readList` produces).
 */
export function isProductionHost(hostname: string, productionHosts: string[]): boolean {
    return productionHosts.includes(hostname.toLowerCase());
}
