/**
 * "Core" globals bundle: LocationSearchEngine + MarkupEngine + the createMarkupToolbar factory.
 */
import { installGlobalAssistantOverlay } from "../shared/assistant-overlay";
import { installGlobalAutosaveGuard } from "../shared/autosave-guard";
import { installGlobalCollapsibleSections } from "../shared/collapsible-sections";
import { installGlobalCommentCompose } from "../shared/comment-compose";
import { installGlobalConfirmDialog } from "../shared/confirm-dialog";
import { installGlobalDialogBackdrop } from "../shared/dialog-backdrop";
import { installGlobalDismissalRing } from "../shared/dismissal-ring";
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
installGlobalDismissalRing();
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
