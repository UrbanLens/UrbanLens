/**
 * "Core" globals bundle: LocationSearchEngine + MarkupEngine + the createMarkupToolbar factory.
 */
import { installGlobalArticleSubtabs } from "../shared/article-subtabs";
import { installGlobalAssistantOverlay } from "../shared/assistant-overlay";
import { installGlobalAutosaveGuard } from "../shared/autosave-guard";
import { installGlobalCollapsibleSections } from "../shared/collapsible-sections";
import { installGlobalCommentCompose } from "../shared/comment-compose";
import { installGlobalConfirmDialog } from "../shared/confirm-dialog";
import { installGlobalCoverHero } from "../shared/cover-hero";
import { installGlobalDialogBackdrop } from "../shared/dialog-backdrop";
import { installGlobalDialogTriggers } from "../shared/dialog-triggers";
import { installGlobalDismissalRing } from "../shared/dismissal-ring";
import { installGlobalFetchJson } from "../shared/fetch-json";
import { installGlobalFlyToDismiss } from "../shared/fly-to-dismiss";
import { installGlobalFooterInset } from "../shared/footer-inset";
import { installGlobalHtmxActions } from "../shared/htmx-actions";
import { installGlobalLabelPicker } from "../shared/label-picker";
import { installGlobalLeaveConfirmation } from "../shared/leave-confirmation";
import { installGlobalLocationSearchEngine } from "../shared/location-search-engine";
import { installGlobalMapContextMenu } from "../shared/map-context-menu";
import { installGlobalMapExport } from "../shared/map-export";
import { installGlobalMapLayers } from "../shared/map-layers";
import { installGlobalMaplibreMarkup } from "../shared/maplibre-markup";
import { installGlobalMaplibreRasterStyle } from "../shared/maplibre-raster-style";
import { installGlobalMarkupEngine } from "../shared/markup-engine";
import { createMarkupToolbar } from "../shared/markup-toolbar";
import { installGlobalMentionAutocomplete } from "../shared/mention-autocomplete";
import { installGlobalPhotoLightbox } from "../shared/photo-lightbox";
import { installGlobalPhotoProcessing } from "../shared/photo-processing";
import { installGlobalPinCachePurge } from "../shared/pin-cache";
import { installGlobalPoller } from "../shared/poller";
import { installGlobalPriorityList } from "../shared/priority-list";
import { installGlobalPopupDismiss } from "../shared/popup-dismiss";
import { installGlobalReactionPicker } from "../shared/reaction-picker";
import { installGlobalRegionDelete } from "../shared/region-delete";
import { installGlobalSafetyLiveLocation } from "../shared/safety-live-location";
import { installGlobalScrollToHash } from "../shared/scroll-to-hash";
import { installUndoBar } from "../shared/undo-bar";
import { installGlobalUndoMapRefresh } from "../shared/undo-map-refresh";
import { installGlobalThumbMapBudget } from "../shared/thumb-map-budget";
import { installGlobalWebGLSupport } from "../shared/webgl-support";
import { installSiteRuntime } from "../shared/site-runtime";

// First: the rest may toast, and body scripts read window.csrftoken.
installSiteRuntime();
installGlobalArticleSubtabs();
installGlobalAssistantOverlay();
installGlobalAutosaveGuard();
installGlobalCollapsibleSections();
installGlobalCommentCompose();
installGlobalConfirmDialog();
installGlobalCoverHero();
installGlobalDialogBackdrop();
installGlobalDialogTriggers();
installGlobalDismissalRing();
installGlobalFetchJson();
installGlobalFlyToDismiss();
installGlobalFooterInset();
installGlobalHtmxActions();
installGlobalMentionAutocomplete();
installGlobalPoller();
installGlobalPriorityList();
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
installGlobalRegionDelete();
installGlobalLeaveConfirmation();
installGlobalPinCachePurge();
installGlobalPhotoLightbox();
installGlobalPhotoProcessing();
installGlobalWebGLSupport();
installGlobalMaplibreRasterStyle();
installGlobalMaplibreMarkup();
installGlobalThumbMapBudget();

window.createMarkupToolbar = createMarkupToolbar;

declare global {
    interface Window {
        createMarkupToolbar: typeof createMarkupToolbar;
    }
}
