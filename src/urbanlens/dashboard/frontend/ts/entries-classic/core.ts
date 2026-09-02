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
import { installGlobalAssistantOverlay } from "../shared/assistant-overlay";
import { installGlobalAutosaveGuard } from "../shared/autosave-guard";
import { installGlobalCollapsibleSections } from "../shared/collapsible-sections";
import { installGlobalCommentCompose } from "../shared/comment-compose";
import { installGlobalConfirmDialog } from "../shared/confirm-dialog";
import { installGlobalDialogBackdrop } from "../shared/dialog-backdrop";
import { installGlobalFetchJson } from "../shared/fetch-json";
import { installGlobalFlyToDismiss } from "../shared/fly-to-dismiss";
import { installGlobalLabelPicker } from "../shared/label-picker";
import { installGlobalLeaveConfirmation } from "../shared/leave-confirmation";
import { installGlobalLocationSearchEngine } from "../shared/location-search-engine";
import { installGlobalMapContextMenu } from "../shared/map-context-menu";
import { installGlobalMapExport } from "../shared/map-export";
import { installGlobalMapLayers } from "../shared/map-layers";
import { installGlobalMarkupEngine } from "../shared/markup-engine";
import { createMarkupToolbar } from "../shared/markup-toolbar";
import { installGlobalMentionAutocomplete } from "../shared/mention-autocomplete";
import { installGlobalPinCachePurge } from "../shared/pin-cache";
import { installGlobalPopupDismiss } from "../shared/popup-dismiss";
import { installGlobalReactionPicker } from "../shared/reaction-picker";
import { installGlobalSafetyLiveLocation } from "../shared/safety-live-location";
import { installGlobalScrollToHash } from "../shared/scroll-to-hash";
import { installUndoBar } from "../shared/undo-bar";
import { installGlobalUndoMapRefresh } from "../shared/undo-map-refresh";

installGlobalAssistantOverlay();
installGlobalAutosaveGuard();
installGlobalCollapsibleSections();
installGlobalCommentCompose();
installGlobalConfirmDialog();
installGlobalDialogBackdrop();
installGlobalFetchJson();
installGlobalFlyToDismiss();
installGlobalMentionAutocomplete();
installGlobalPopupDismiss();
installGlobalReactionPicker();
installGlobalSafetyLiveLocation();
installGlobalScrollToHash();
installGlobalUndoMapRefresh();
installUndoBar();
installGlobalLocationSearchEngine();
installGlobalMapContextMenu();
installGlobalMapLayers();
installGlobalMarkupEngine();
installGlobalMapExport();
installGlobalLabelPicker();
installGlobalLeaveConfirmation();
installGlobalPinCachePurge();

window.createMarkupToolbar = createMarkupToolbar;

declare global {
    interface Window {
        createMarkupToolbar: typeof createMarkupToolbar;
    }
}
