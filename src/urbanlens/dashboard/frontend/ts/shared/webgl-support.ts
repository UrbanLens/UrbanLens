/**
 * WebGL2 detection for the Leaflet->MapLibre engine choice (PL8). MapLibre GL JS requires WebGL2;
 * caniuse put global support at 95.73% in Aug 2026, so the remaining ~4.27% needs Leaflet kept on
 * hand as a genuine second rendering engine, not a plain "unsupported browser" message - see `D12`
 * (REData's `../REData/docs/DECISIONS.md`) and `docs/designs/leaflet-to-maplibre-migration.md`
 * item 2 for the reasoning this module exists to serve. Not wired into any map yet.
 */

/** The subset of `HTMLCanvasElement` this module actually calls, so a test can inject a fake one. */
export interface WebGL2ProbeCanvas {
    getContext(contextId: "webgl2"): unknown;
}

/**
 * Whether this browser can render a WebGL2 context.
 * @param createCanvas - Returns the canvas to probe. Defaults to a throwaway, never-attached
 *   `<canvas>` - overridden in tests, since the codebase's own DOM test harness (happy-dom) has no
 *   real WebGL2 backend and always answers `null`, indistinguishable from a genuinely unsupported browser.
 * @returns `false` on any thrown error (some browsers throw rather than returning `null` when
 *   WebGL2 is disabled by policy) as well as on a `null` context - never lets a probe failure
 *   propagate into an engine-selection crash.
 */
export function supportsWebGL2(createCanvas: () => WebGL2ProbeCanvas = () => document.createElement("canvas")): boolean {
    try {
        return createCanvas().getContext("webgl2") != null;
    } catch {
        return false;
    }
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
