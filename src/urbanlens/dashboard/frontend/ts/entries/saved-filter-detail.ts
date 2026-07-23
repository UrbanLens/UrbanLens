import { installGlobalIconPicker } from "../shared/icon-picker";

// The saved-filter detail page (pages/pin_lists/saved_filter_detail.html)
// renders the shared _icon_picker.html partial (show_upload=False - no
// custom-icon upload, so the plain IconPicker suffices; OrganizeIconPicker's
// extra upload handling isn't needed here and isn't wired up on this page
// anyway) but never loaded any script defining window.IconPicker, so its
// trigger button's onclick="IconPicker.toggle(...)" threw a silent
// ReferenceError and did nothing.
installGlobalIconPicker();
