/**
 * Site-admin achievements editor: installs the same shared icon and colour
 * pickers organize's label/tag/category/status forms use (see
 * organize-icon-picker.ts, color-picker.ts), without pulling in the rest of
 * organize.ts's tab/bulk-edit machinery this page has no use for.
 */
import { installGlobalOrganizeIconPicker } from "../shared/organize-icon-picker";
import { installGlobalColorPicker, pickColor } from "../shared/color-picker";

/**
 * Apply a swatch, and tint the medal preview above it.
 *
 * Exists to keep the ids out of every swatch's `onclick`: this page renders 21
 * of them per award plus 21 more for the create form, and spelling both ids and
 * the colour into each handler cost 297 bytes a swatch - the second-largest
 * per-row cost after the icon grid (P68). The ids live on the enclosing
 * `.color-picker` instead, and are read back here.
 *
 * A window global rather than a delegated listener, matching color-picker.ts's
 * own rationale: these rows are replaced wholesale by HTMX on every save,
 * create, delete and backfill.
 */
function pickAchievementColor(btn: HTMLElement): void {
    const picker = btn.closest<HTMLElement>(".color-picker");
    if (!picker) return;
    const hex = btn.dataset.color ?? "";
    pickColor(picker.id, picker.dataset.colorValueId ?? "", hex, btn);
    document.getElementById(picker.dataset.colorMedalId ?? "")?.style.setProperty("--achievement-color", hex);
}

installGlobalOrganizeIconPicker();
installGlobalColorPicker();
window.pickAchievementColor = pickAchievementColor;

declare global {
    interface Window {
        pickAchievementColor: typeof pickAchievementColor;
    }
}
