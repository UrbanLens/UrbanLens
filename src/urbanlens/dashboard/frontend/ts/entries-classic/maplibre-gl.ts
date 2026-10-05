/**
 * MapLibre GL JS as the `maplibregl` global that the map modules, `comment-map.js` and the Leaflet bridge
 * (`maplibregl_leaflet_js`) read.
 *
 * MapLibre 6 publishes only ES modules, so this classic build stands in for the UMD bundle it no longer ships: a
 * synchronous `<script>` defines the global before any later script on the page runs, which a module script could not.
 */

import * as maplibregl from "maplibre-gl";

/**
 * Where MapLibre should start its tile workers.
 *
 * A bundled MapLibre cannot find its worker from `import.meta.url`, so it is told. `bin/build-frontend.ts` writes the
 * worker beside this bundle, named for the version it was built from so a cached worker never outlives its library.
 * It is started through a `blob:` URL that imports it: a worker started from its own URL runs under whatever CSP the
 * static server sent with the file (none, behind nginx), while one started from a `blob:` URL runs under this page's.
 * @param scriptSrc - This bundle's own URL.
 */
function workerUrl(scriptSrc: string): string {
    const worker = new URL(`maplibre-gl-worker-${maplibregl.getVersion()}.js`, scriptSrc).href;
    return URL.createObjectURL(new Blob([`import ${JSON.stringify(worker)};`], { type: "text/javascript" }));
}

const script = document.currentScript;
if (script instanceof HTMLScriptElement && script.src) maplibregl.setWorkerUrl(workerUrl(script.src));
window.maplibregl = maplibregl;

declare global {
    interface Window {
        maplibregl: typeof maplibregl;
    }
}
