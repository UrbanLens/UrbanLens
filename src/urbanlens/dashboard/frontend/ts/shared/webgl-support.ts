/**
 * WebGL2 detection for the Leaflet->MapLibre engine choice (PL8). MapLibre GL JS requires WebGL2,
 * which caniuse put at 95.73% support *globally* in Aug 2026 - this deployment's own analytics have
 * never been measured, so the complement is not a figure for UrbanLens traffic. What actually makes
 * Leaflet a genuine second rendering engine rather than a plain "unsupported browser" message is
 * that nearly every map here is still Leaflet, and that does not expire when WebGL2 does - see `D12`
 * (REData's `../REData/docs/DECISIONS.md`) and `docs/designs/leaflet-to-maplibre-migration.md`
 * item 2 for the reasoning this module exists to serve.
 *
 * This is the branch point every converted map reads: a false answer here is what keeps the
 * pre-existing Leaflet path alive for that browser, so it must never throw.
 */

/** The subset of `HTMLCanvasElement` this module actually calls, so a test can inject a fake one. */
export interface WebGL2ProbeCanvas {
    getContext(contextId: "webgl2"): unknown;
}

/** Memoized result of probing the default canvas - never touched by a call that injects its own `createCanvas`. */
let cachedDefaultSupport: boolean | null = null;

function probe(createCanvas: () => WebGL2ProbeCanvas): boolean {
    try {
        return createCanvas().getContext("webgl2") != null;
    } catch {
        return false;
    }
}

/**
 * Whether this browser can render a WebGL2 context.
 *
 * The default probe's result is cached for the page's lifetime: creating a WebGL2 context is
 * expensive and briefly consumes one of the browser's limited contexts, and call sites like
 * `comment-map.js`'s thumbnail scroll handler ask on every thumbnail. A caller supplying its own
 * `createCanvas` always gets a fresh, uncached probe.
 * @param createCanvas - Returns the canvas to probe. Defaults to a throwaway, never-attached
 *   `<canvas>` - overridden in tests, since the codebase's own DOM test harness (happy-dom) has no
 *   real WebGL2 backend and always answers `null`, indistinguishable from a genuinely unsupported browser.
 * @returns `false` on any thrown error (some browsers throw rather than returning `null` when
 *   WebGL2 is disabled by policy) as well as on a `null` context - never lets a probe failure
 *   propagate into an engine-selection crash.
 */
export function supportsWebGL2(createCanvas?: () => WebGL2ProbeCanvas): boolean {
    if (createCanvas) return probe(createCanvas);
    if (cachedDefaultSupport === null) cachedDefaultSupport = probe(() => document.createElement("canvas"));
    return cachedDefaultSupport;
}

export const WebGLSupport = { supportsWebGL2 };

/** Publishes the detector on window for the classic inline template scripts and hand-written vanilla JS (e.g. `comment-map.js`). */
export function installGlobalWebGLSupport(): void {
    window.WebGLSupport = WebGLSupport;
}

declare global {
    interface Window {
        WebGLSupport: typeof WebGLSupport;
    }
}
