import { IconPicker } from "./icon-picker";

/**
 * organize/index.html's icon picker additionally supports custom-image
 * upload (with a "clear custom icon" flag cleared/set alongside picks) and
 * notifies whichever bulk-edit tab owns a "-bulk-edit" picker id so its
 * Apply/Convert button label can refresh. Registered per-namespace by
 * organize.ts via `registerBulkStateUpdater` rather than hardcoding tab names
 * here, keeping this module agnostic of which tabs exist.
 */
const bulkStateUpdaters = new Map<string, () => void>();

export function registerBulkStateUpdater(nsPrefix: string, updater: () => void): void {
    bulkStateUpdaters.set(nsPrefix, updater);
}

export const OrganizeIconPicker = {
    ...IconPicker,

    pick(id: string, icon: string, btn: HTMLElement): void {
        IconPicker.pick(id, icon, btn);

        // Picking anything from the grid (a real icon or "None") replaces
        // whatever custom icon was previously uploaded for this badge.
        const clearFlag = document.getElementById(`edit-clear-custom-${id}`) as HTMLInputElement | null;
        if (clearFlag) clearFlag.value = "1";

        // When a real icon is picked, clear any pending upload file.
        const uploadInput = document.getElementById(`icon-upload-input-${id}`) as HTMLInputElement | null;
        if (icon && uploadInput) uploadInput.value = "";

        if (id.endsWith("-bulk-edit")) {
            const ns = id.slice(0, -"-bulk-edit".length);
            const nochange = document.getElementById(`${ns}-bulk-icon-nochange`) as HTMLInputElement | null;
            if (nochange) nochange.checked = false;
            bulkStateUpdaters.get(ns)?.();
        }
    },

    _handleUpload(id: string, input: HTMLInputElement): void {
        const file = input.files?.[0];
        if (!file) return;

        const clearFlag = document.getElementById(`edit-clear-custom-${id}`) as HTMLInputElement | null;
        if (clearFlag) clearFlag.value = "";

        const iconVal = document.getElementById(`icon-value-${id}`) as HTMLInputElement | null;
        if (iconVal) iconVal.value = "";
        document.getElementById(`icon-grid-${id}`)
            ?.querySelectorAll(".icon-picker-item")
            .forEach((b) => b.classList.remove("selected"));

        const reader = new FileReader();
        reader.onload = (e) => {
            const current = document.getElementById(`icon-current-${id}`);
            if (current) current.innerHTML = `<img src="${e.target?.result}" class="icon-picker-custom-preview" alt="Custom icon">`;
        };
        reader.readAsDataURL(file);

        document.getElementById(`icon-panel-${id}`)?.setAttribute("hidden", "");
    },
};

export function installGlobalOrganizeIconPicker(): void {
    window.IconPicker = OrganizeIconPicker;
}
