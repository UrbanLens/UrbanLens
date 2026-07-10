/**
 * "Core" globals bundle: LocationSearchEngine + MarkupEngine + the
 * createMarkupToolbar factory.
 *
 * Unlike the other entries (categories/tags/organize), this one is built and
 * loaded as a classic (non-module) IIFE script - see package.json's build/
 * deploy scripts - rather than `type="module"`. It's included in base.html's
 * <head>, synchronously, exactly where the two inline <script> tags it
 * replaces used to live: several pages (map/index.html, the safety check-in
 * maps, the markup toolbar) have their own classic <script> tags later in
 * the document that call `LocationSearchEngine.create(...)` /
 * `MarkupEngine.createDrawSession(...)` / `createMarkupToolbar(...)`
 * synchronously as soon as they run - not inside a DOMContentLoaded handler.
 * `type="module"` scripts are always deferred until after the document has
 * finished parsing, which would run this *after* those classic scripts and
 * leave the globals undefined when they're needed. Loading as a blocking
 * classic script preserves the exact head-executes-before-body ordering the
 * site already depends on.
 */
import { installGlobalLocationSearchEngine } from "../shared/location-search-engine";
import { installGlobalMapLayers } from "../shared/map-layers";
import { installGlobalMarkupEngine } from "../shared/markup-engine";
import { createMarkupToolbar } from "../shared/markup-toolbar";

installGlobalLocationSearchEngine();
installGlobalMapLayers();
installGlobalMarkupEngine();

window.createMarkupToolbar = createMarkupToolbar;

declare global {
    interface Window {
        createMarkupToolbar: typeof createMarkupToolbar;
    }
}
