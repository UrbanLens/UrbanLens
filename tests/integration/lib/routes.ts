/**
 * Site paths, in one place.
 *
 * Specs address pages through these rather than through string literals, so a
 * route that moves is one edit here instead of a grep across the suite. The
 * values are the output of Django's own `reverse()` against this urlconf, not
 * transcriptions of the pattern strings - `dashboard/urls.py` nests `include()`
 * several levels deep and the assembled path is not obvious from reading it.
 *
 * Deliberately not exhaustive. A route earns an entry when a spec navigates to
 * it; the "every page in the navigation still loads" sweep discovers its
 * targets from the rendered menu instead, so it stays correct as pages are
 * added.
 */

/** Reachable without signing in. */
export const publicRoutes = {
    index: "/",
    login: "/accounts/login/",
    signup: "/signup/",
    health: "/health/",
    healthLive: "/health/live",
    healthReady: "/health/ready",
    healthPrimary: "/health/primary",
} as const;

/**
 * The signed-in application, as every deployment has it.
 *
 * Only pages that are unconditionally present belong here, because the sweep
 * over this list treats anything but a rendered page as a failure. Anything a
 * deployment can legitimately switch off lives in {@link optionalRoutes}.
 */
export const appRoutes = {
    home: "/dashboard/home/",
    map: "/dashboard/map/",
    lists: "/dashboard/lists/",
    trips: "/dashboard/trips/",
    memories: "/dashboard/memories/",
    vaultHome: "/dashboard/vault/",
    vaultPhotos: "/dashboard/vault/photos/",
    vaultDocuments: "/dashboard/vault/documents/",
    messages: "/dashboard/messages/",
    organize: "/dashboard/organize/",
    safety: "/dashboard/safety/",
    tools: "/dashboard/tools/",
    achievements: "/dashboard/achievements/",
    profile: "/dashboard/profile/",
    settings: "/dashboard/settings/",
} as const;

/**
 * Pages a deployment may deliberately not serve.
 *
 * Each is gated on something an operator controls, and a 404 or a redirect from
 * one is the gate working rather than a fault - so the sweep reports them as
 * unavailable instead of failing. When one *is* served it still has to render
 * properly, which is the half worth testing.
 */
export const optionalRoutes: ReadonlyArray<{ name: string; path: string; gate: string }> = [
    { name: "games", path: "/dashboard/games/", gate: "AlphaFeatureRequiredMixin - the account needs the alpha flag" },
    { name: "assistant", path: "/dashboard/assistant/", gate: "the account needs a subscription including AI features" },
    { name: "costs", path: "/dashboard/costs/", gate: "SiteSettings.public_costs_page_enabled, off by default" },
];

/** Informational pages, reachable signed in or out. */
export const contentRoutes = {
    about: "/dashboard/about/",
    values: "/dashboard/values/",
    faq: "/dashboard/faq/",
    terms: "/dashboard/terms/",
    privacy: "/dashboard/privacy/",
    thanks: "/dashboard/thanks/",
} as const;

/** Staff-only surfaces. */
export const staffRoutes = {
    siteAdmin: "/dashboard/site-admin/",
    siteAdminUsers: "/dashboard/site-admin/users/",
    siteAdminSettings: "/dashboard/site-admin/settings/",
    siteAdminStats: "/dashboard/site-admin/stats/",
    djangoAdmin: "/admin/",
    djangoAdminLogin: "/admin/login/",
} as const;

/** Internal session-authenticated REST. Not the published external API. */
export const restRoutes = {
    pins: "/dashboard/rest/pins/",
    pin: (uuid: string) => `/dashboard/rest/pins/${uuid}/`,
    review: (pinPk: number | string) => `/dashboard/rest/reviews/create_or_update/${pinPk}/`,
} as const;

export const toolsRoutes = {
    exportStart: "/dashboard/tools/export/start/",
    exportStatus: (jobId: string) => `/dashboard/tools/export/status/${jobId}/`,
    exportDownload: (jobId: string) => `/dashboard/tools/export/download/${jobId}/`,
    adminTools: "/dashboard/tools/admin/",
} as const;

export const mediaRoute = (path: string): string => `/media/${path.replace(/^\/+/, "")}`;

export const oauthRoutes = {
    authorize: "/oauth/authorize/",
    token: "/oauth/token/",
    introspect: "/oauth/introspect/",
} as const;

/**
 * Fragment endpoints the shell fetches over HTMX on every page.
 *
 * Each is loaded by an `hx-trigger` rather than by a click, so a failure is
 * silent: the badge simply never fills in, the banner never appears.
 */
export const shellFragmentRoutes = {
    notificationCount: "/dashboard/notifications/unread-count/",
    notificationDropdown: "/dashboard/notifications/dropdown/",
    messageCount: "/dashboard/messages/unread-count/",
    messageDropdown: "/dashboard/messages/dropdown/",
    safetyBanner: "/dashboard/safety/nav-banner/",
} as const;

/** JSON endpoints the map page itself calls, useful as service-level probes. */
export const mapDataRoutes = {
    pins: "/dashboard/map/pins/",
    pinsMeta: "/dashboard/map/pins/meta/",
    pinList: "/dashboard/map/pins/list/",
    search: "/dashboard/map/search/",
    basemapSources: "/dashboard/map/basemap-tiles/sources/",
    // JSON, not a page - it backs the Filters tab's region picker. Listed here
    // rather than among the app routes because it is exactly the kind of
    // endpoint a "does every page render" sweep wrongly picks up.
    regionSearch: "/dashboard/region-search/",
} as const;

/** Detail page for one of the signed-in user's pins. */
export function pinDetail(slug: string): string {
    return `/dashboard/map/pin/${slug}/`;
}

/** Another user's public profile. */
export function profileFor(slug: string): string {
    return `/dashboard/profile/${slug}/`;
}

/** Every route the "nothing 500s" sweep visits as a signed-in user. */
export const signedInSweep: ReadonlyArray<{ name: string; path: string }> = [
    ...Object.entries(appRoutes).map(([name, path]) => ({ name, path })),
    ...Object.entries(contentRoutes).map(([name, path]) => ({ name, path })),
];
