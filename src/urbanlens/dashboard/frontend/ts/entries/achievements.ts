/**
 * Site-admin achievements editor: installs the same shared icon and colour
 * pickers organize's label/tag/category/status forms use (see
 * organize-icon-picker.ts, color-picker.ts), without pulling in the rest of
 * organize.ts's tab/bulk-edit machinery this page has no use for.
 */
import { installGlobalOrganizeIconPicker } from "../shared/organize-icon-picker";
import { installGlobalColorPicker } from "../shared/color-picker";

installGlobalOrganizeIconPicker();
installGlobalColorPicker();
