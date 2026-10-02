/**
 * Site-admin achievements editor: installs the same shared icon and colour pickers organize's label/tag/category/status forms use.
 */
import { installGlobalOrganizeIconPicker } from "../shared/organize-icon-picker";
import { installGlobalColorPicker } from "../shared/color-picker";

/**
 * Tint the medal preview a swatch's picker names; the core bundle's picker actions apply the swatch itself.
 */
function tintMedal(event: MouseEvent): void {
    const swatch = event.target instanceof Element ? event.target.closest<HTMLElement>(".color-swatch") : null;
    const picker = swatch?.closest<HTMLElement>(".color-picker[data-color-medal-id]");
    if (!swatch || !picker) return;
    document.getElementById(picker.dataset.colorMedalId ?? "")?.style.setProperty("--achievement-color", swatch.dataset.color ?? "");
}

installGlobalOrganizeIconPicker();
installGlobalColorPicker();
document.addEventListener("click", tintMedal);
