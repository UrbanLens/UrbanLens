/**
 * Site-admin achievements editor: installs the same shared icon and colour pickers organize's label/tag/category/status forms use,.
 */
import { installGlobalOrganizeIconPicker } from "../shared/organize-icon-picker";
import { installGlobalColorPicker, pickColor } from "../shared/color-picker";

/**
 * Apply a swatch, and tint the medal preview above it.
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
