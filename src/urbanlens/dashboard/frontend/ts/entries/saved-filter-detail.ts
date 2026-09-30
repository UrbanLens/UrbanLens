import { installGlobalIconPicker } from "../shared/icon-picker";
import { installSavedFilterForm } from "../shared/saved-filter-form";
import { SavedFilterPreview } from "../shared/saved-filter-preview";

// The saved-filter detail page (pages/pin_lists/saved_filter_detail.html): the filter's form, inline, beside a
// live map of what it matches. It renders the shared _icon_picker.html partial too.
installGlobalIconPicker();
installSavedFilterForm({ inline: true });
const previewMap = document.getElementById("saved-filter-preview-map");
if (previewMap) new SavedFilterPreview(previewMap).install();
