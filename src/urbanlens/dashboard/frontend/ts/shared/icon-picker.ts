/**
 * Icon picker widget shared by categories/tags/organize's create and bulk-edit
 * dialogs (dashboard/partials/_icon_picker.html). The partial's markup calls
 * `IconPicker.toggle/setTab/search/pick(...)` via inline onclick/oninput
 * attributes, including markup injected later via HTMX (edit dialogs) - so
 * this stays a `window.IconPicker` global rather than an imported class,
 * matching the existing contract instead of rewriting every template that
 * includes that partial (including pages outside this migration's scope).
 */
const MATERIAL_ICON_NAME = /^[a-z_]+$/;

export const IconPicker = {
    toggle(id: string): void {
        const panel = document.getElementById(`icon-panel-${id}`);
        if (!panel) return;
        const isHidden = panel.hasAttribute("hidden");
        document.querySelectorAll(".icon-picker-panel").forEach((p) => p.setAttribute("hidden", ""));
        if (isHidden) {
            panel.removeAttribute("hidden");
            const search = panel.querySelector<HTMLInputElement>(".icon-picker-search-input");
            if (search) {
                search.value = "";
                search.focus();
            }
            IconPicker.setTabSilent(id, "");
        }
    },

    setTabSilent(id: string, cat: string): void {
        const panel = document.getElementById(`icon-panel-${id}`);
        if (!panel) return;
        panel.querySelectorAll<HTMLElement>(".icon-tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === cat));
        const grid = document.getElementById(`icon-grid-${id}`);
        if (!grid) return;
        grid.querySelectorAll<HTMLElement>(".icon-picker-item").forEach((item) => {
            item.style.display = !cat || item.dataset.cat === cat || !item.dataset.cat ? "" : "none";
        });
    },

    setTab(id: string, cat: string, btn: HTMLElement): void {
        const panel = document.getElementById(`icon-panel-${id}`);
        if (!panel) return;
        panel.querySelectorAll(".icon-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const search = panel.querySelector<HTMLInputElement>(".icon-picker-search-input");
        if (search) search.value = "";
        const grid = document.getElementById(`icon-grid-${id}`);
        if (!grid) return;
        grid.querySelectorAll<HTMLElement>(".icon-picker-item").forEach((item) => {
            item.style.display = !cat || item.dataset.cat === cat || !item.dataset.cat ? "" : "none";
        });
    },

    search(id: string, query: string): void {
        const q = query.toLowerCase().trim();
        const panel = document.getElementById(`icon-panel-${id}`);
        if (!panel) return;
        panel.querySelectorAll<HTMLElement>(".icon-tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === ""));
        const grid = document.getElementById(`icon-grid-${id}`);
        if (!grid) return;
        grid.querySelectorAll<HTMLElement>(".icon-picker-item").forEach((item) => {
            if (!q) {
                item.style.display = "";
                return;
            }
            const label = item.dataset.label ?? "";
            const icon = item.dataset.icon ?? "";
            const keywords = item.dataset.keywords ?? "";
            item.style.display = label.includes(q) || icon === q || keywords.includes(q) ? "" : "none";
        });
    },

    pick(id: string, icon: string, btn: HTMLElement): void {
        const input = document.getElementById(`icon-value-${id}`) as HTMLInputElement | null;
        if (input) input.value = icon;

        const current = document.getElementById(`icon-current-${id}`);
        if (current) {
            current.innerHTML = renderIconGlyphHtml(icon);
        }

        const grid = document.getElementById(`icon-grid-${id}`);
        if (grid) {
            grid.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
            btn.classList.add("selected");
        }

        const panel = document.getElementById(`icon-panel-${id}`);
        if (panel) panel.setAttribute("hidden", "");
    },
};

/** Shared by the picker itself and by bulk-edit dialogs pre-filling a shared icon. */
export function renderIconGlyphHtml(icon: string): string {
    if (!icon) return '<span class="icon-picker-none-label">No icon</span>';
    return MATERIAL_ICON_NAME.test(icon)
        ? `<i class="material-icons icon-picker-current-mi">${icon}</i>`
        : `<span class="icon-picker-current-glyph">${icon}</span>`;
}

/** Resets an icon picker instance back to "no icon" (used by new-item form resets). */
export function resetIconPicker(pickerId: string): void {
    const input = document.getElementById(`icon-value-${pickerId}`) as HTMLInputElement | null;
    if (input) input.value = "";
    const current = document.getElementById(`icon-current-${pickerId}`);
    if (current) current.innerHTML = '<span class="icon-picker-none-label">No icon</span>';
    const grid = document.getElementById(`icon-grid-${pickerId}`);
    if (grid) {
        grid.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
        grid.querySelector(".icon-picker-none")?.classList.add("selected");
    }
}

document.addEventListener("click", (e) => {
    if (!(e.target as Element).closest(".icon-picker-dropdown")) {
        document.querySelectorAll(".icon-picker-panel").forEach((p) => p.setAttribute("hidden", ""));
    }
});

export function installGlobalIconPicker(): void {
    window.IconPicker = IconPicker;
}

declare global {
    interface Window {
        IconPicker: typeof IconPicker;
    }
}
