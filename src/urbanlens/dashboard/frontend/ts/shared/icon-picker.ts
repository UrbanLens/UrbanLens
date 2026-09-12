/**
 * Icon picker widget shared by categories/tags/organize's create and bulk-edit dialogs (dashboard/partials/ui/_icon_picker.html).
 */
const MATERIAL_ICON_NAME = /^[a-z_]+$/;

/**
 * The catalogue is fetched once per page rather than rendered into every picker.
 */
interface IconCatalogue {
    tabs: string;
    items: string;
}

let gridRequest: Promise<IconCatalogue> | null = null;

/** Split the one response into its two fragments, once, rather than per picker. */
function parseCatalogue(html: string): IconCatalogue {
    const holder = document.createElement("template");
    holder.innerHTML = html;
    return {
        tabs: holder.content.querySelector("[data-icon-picker-tabs]")?.innerHTML ?? "",
        items: holder.content.querySelector("[data-icon-picker-items]")?.innerHTML ?? "",
    };
}

function loadCatalogue(url: string): Promise<IconCatalogue> {
    if (!gridRequest) {
        gridRequest = fetch(url, { credentials: "same-origin" })
            .then((response) => {
                if (!response.ok) throw new Error(`icon grid: HTTP ${response.status}`);
                return response.text();
            })
            .then(parseCatalogue)
            .catch((error) => {
                // Dropped so the next open retries.
                gridRequest = null;
                throw error;
            });
    }
    return gridRequest;
}

/** Drops the cached catalogue request, so a test starts from an unfetched page. */
export function resetIconGridForTests(): void {
    gridRequest = null;
}

/**
 * Marks the item matching this picker's current value, which the server used to render.
 */
function markSelectedIcon(id: string, grid: HTMLElement): void {
    const input = document.getElementById(`icon-value-${id}`) as HTMLInputElement | null;
    const current = input?.value ?? "";
    if (!current) return;
    grid.querySelectorAll<HTMLElement>(".icon-picker-item").forEach((item) => {
        item.classList.toggle("selected", item.dataset.icon === current);
    });
}

function statusNode(grid: HTMLElement): HTMLElement {
    let node = grid.querySelector<HTMLElement>(".icon-picker-status");
    if (!node) {
        node = document.createElement("div");
        node.className = "icon-picker-status";
        grid.appendChild(node);
    }
    return node;
}

/** Fills a picker's tabs and grid from the shared catalogue, at most once per picker. */
export async function fillIconGrid(id: string): Promise<void> {
    const grid = document.getElementById(`icon-grid-${id}`);
    if (!grid || grid.dataset.iconsLoaded === "1") return;
    const url = grid.dataset.gridUrl;
    if (!url) return;

    const status = statusNode(grid);
    status.textContent = "Loading icons...";
    try {
        const catalogue = await loadCatalogue(url);
        // Re-checked: two opens can await the same promise, and the second must
        // not append a second copy of the catalogue.
        if (grid.dataset.iconsLoaded === "1") return;
        grid.insertAdjacentHTML("beforeend", catalogue.items);
        document.getElementById(`icon-tabs-${id}`)?.insertAdjacentHTML("beforeend", catalogue.tabs);
        grid.dataset.iconsLoaded = "1";
        status.remove();
        markSelectedIcon(id, grid);
    } catch {
        status.textContent = "Icons could not be loaded. Close and reopen to try again.";
    }
}

/** Re-applies whatever filter is showing, for items that arrived after it was set. */
function reapplyFilter(id: string): void {
    const panel = document.getElementById(`icon-panel-${id}`);
    const search = panel?.querySelector<HTMLInputElement>(".icon-picker-search-input");
    const query = search?.value.trim() ?? "";
    if (query) {
        IconPicker.search(id, query);
        return;
    }
    const activeTab = panel?.querySelector<HTMLElement>(".icon-tab.active");
    IconPicker.setTabSilent(id, activeTab?.dataset.cat ?? "");
}

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
            // Revealed first, filled second: the panel's chrome (search, tabs) is already there, so the fetch shows as a loading row inside an open.
            void fillIconGrid(id).then(() => reapplyFilter(id));
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

    pick(id: string, icon: string, btn: HTMLElement | null): void {
        const input = document.getElementById(`icon-value-${id}`) as HTMLInputElement | null;
        if (input) {
            input.value = icon;
            // Assigning .value fires nothing, so until now the only way to learn about a pick was to read the field at form-submit time.
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
        }

        const current = document.getElementById(`icon-current-${id}`);
        if (current) {
            current.innerHTML = renderIconGlyphHtml(icon);
        }

        const grid = document.getElementById(`icon-grid-${id}`);
        if (grid) {
            grid.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
            btn?.classList.add("selected");
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
