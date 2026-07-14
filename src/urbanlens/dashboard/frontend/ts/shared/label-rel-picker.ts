import Sortable from "sortablejs";

/**
 * Parent/child relationship chip picker used by organize's create/edit/bulk-edit
 * dialogs (dashboard/partials/badges/*). Templates call this via inline
 * onclick attributes, so it stays a window global - see icon-picker.ts for
 * the same rationale.
 */
type RelType = "parent" | "child";

export const BadgeRelPicker = {
    toggle(instanceId: string, relType: RelType, _triggerBtn: HTMLElement): void {
        const popup = document.getElementById(`${instanceId}-popup-${relType}`);
        if (!popup) return;
        const wasHidden = (popup as HTMLElement).hidden;
        document.querySelectorAll<HTMLElement>(".badge-rel-popup").forEach((p) => {
            p.hidden = true;
        });
        if (!wasHidden) return;
        (popup as HTMLElement).hidden = false;
        const search = popup.querySelector<HTMLInputElement>(".badge-rel-search");
        if (search) {
            search.value = "";
            search.focus();
        }
    },

    select(instanceId: string, relType: RelType, btn: HTMLElement): void {
        if (btn.classList.contains("badge-rel-suggestion--hidden")) return;
        const group = document.getElementById(`${instanceId}-sel-${relType}`);
        if (!group) return;
        const id = btn.dataset.id;
        if (!id || group.querySelector(`.badge-rel-chip[data-id="${id}"]`)) return;

        const picker = document.querySelector<HTMLElement>(`[data-picker-id="${instanceId}"]`);
        const pill = document.createElement("span");
        pill.className = "tag-chip";
        const color = btn.style.getPropertyValue("--tag-color");
        if (color) pill.style.setProperty("--tag-color", color);
        pill.innerHTML = btn.innerHTML;
        pill.querySelector(".badge-kind-chip")?.remove();
        if (picker?.dataset.mode === "replace") {
            const hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = `${relType}_ids`;
            hidden.value = id;
            pill.appendChild(hidden);
        }

        const chip = document.createElement("span");
        chip.className = "badge-rel-chip";
        chip.dataset.id = id;
        chip.appendChild(pill);

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "tag-chip-remove";
        removeBtn.title = "Remove";
        removeBtn.innerHTML = "&times;";
        removeBtn.onclick = () => BadgeRelPicker.remove(instanceId, chip);
        chip.appendChild(removeBtn);

        group.appendChild(chip);
        BadgeRelPicker._hideSuggestion(instanceId, id);
        BadgeRelPicker._updateEmptyHints(instanceId);
    },

    remove(instanceId: string, chipEl: HTMLElement | null): void {
        if (!chipEl) return;
        const id = chipEl.dataset.id;
        chipEl.remove();
        if (id) BadgeRelPicker._showSuggestion(instanceId, id);
        BadgeRelPicker._updateEmptyHints(instanceId);
    },

    _hideSuggestion(instanceId: string, id: string): void {
        (["parent", "child"] as RelType[]).forEach((relType) => {
            const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
            container?.querySelector(`.badge-rel-suggestion[data-id="${id}"]`)?.classList.add("badge-rel-suggestion--hidden");
        });
    },

    _showSuggestion(instanceId: string, id: string): void {
        (["parent", "child"] as RelType[]).forEach((relType) => {
            const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
            const btn = container?.querySelector(`.badge-rel-suggestion[data-id="${id}"]`);
            if (btn) {
                btn.classList.remove("badge-rel-suggestion--hidden");
                BadgeRelPicker._applyFilters(instanceId, relType);
            }
        });
    },

    setTab(instanceId: string, relType: RelType, kind: string, btn: HTMLElement): void {
        const popup = document.getElementById(`${instanceId}-popup-${relType}`);
        if (!popup) return;
        popup.querySelectorAll(".badge-rel-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
        if (container) container.dataset.activeTab = kind;
        BadgeRelPicker._applyFilters(instanceId, relType);
    },

    filter(instanceId: string, relType: RelType, query: string): void {
        const popup = document.getElementById(`${instanceId}-popup-${relType}`);
        if (popup) popup.dataset.searchQuery = query.toLowerCase().trim();
        BadgeRelPicker._applyFilters(instanceId, relType);
    },

    _applyFilters(instanceId: string, relType: RelType): void {
        const popup = document.getElementById(`${instanceId}-popup-${relType}`);
        const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
        if (!popup || !container) return;
        const q = popup.dataset.searchQuery ?? "";
        const tab = container.dataset.activeTab ?? "";
        container.querySelectorAll<HTMLElement>(".badge-rel-suggestion").forEach((btn) => {
            const matchesTab = !tab || btn.dataset.kind === tab;
            const matchesSearch = !q || (btn.dataset.name ?? "").indexOf(q) !== -1;
            btn.style.display = matchesTab && matchesSearch ? "" : "none";
        });
    },

    _updateEmptyHints(instanceId: string): void {
        (["parent", "child"] as RelType[]).forEach((relType) => {
            const group = document.getElementById(`${instanceId}-sel-${relType}`);
            const hint = group?.parentElement?.querySelector<HTMLElement>(".badge-rel-empty-hint");
            if (hint) hint.hidden = (group?.children.length ?? 0) > 0;
        });
    },

    getSelectedIds(instanceId: string, relType: RelType): number[] {
        const group = document.getElementById(`${instanceId}-sel-${relType}`);
        if (!group) return [];
        return Array.from(group.querySelectorAll<HTMLElement>(".badge-rel-chip")).map((c) => Number.parseInt(c.dataset.id ?? "0", 10));
    },

    reset(instanceId: string): void {
        (["parent", "child"] as RelType[]).forEach((relType) => {
            const group = document.getElementById(`${instanceId}-sel-${relType}`);
            if (!group) return;
            Array.from(group.querySelectorAll<HTMLElement>(".badge-rel-chip")).forEach((chip) => BadgeRelPicker.remove(instanceId, chip));
        });
    },

    _makeSortable(instanceId: string): void {
        const groupName = `${instanceId}-rel`;
        const parentList = document.getElementById(`${instanceId}-sel-parent`);
        const childList = document.getElementById(`${instanceId}-sel-child`);
        const trash = document.getElementById(`${instanceId}-trash`);
        if (!parentList || !childList) return;

        const showTrash = () => trash?.classList.add("is-active");
        const hideTrash = () => trash?.classList.remove("is-active");
        const onEnd = () => {
            hideTrash();
            BadgeRelPicker._updateEmptyHints(instanceId);
        };
        const makeOnAdd = (relType: RelType) => (evt: { item: HTMLElement }) => {
            const hidden = evt.item.querySelector<HTMLInputElement>('input[type="hidden"]');
            if (hidden) hidden.name = `${relType}_ids`;
        };

        new Sortable(parentList, {
            group: groupName,
            animation: 150,
            filter: ".tag-chip-remove",
            preventOnFilter: false,
            onStart: showTrash,
            onEnd,
            onAdd: makeOnAdd("parent"),
        });
        new Sortable(childList, {
            group: groupName,
            animation: 150,
            filter: ".tag-chip-remove",
            preventOnFilter: false,
            onStart: showTrash,
            onEnd,
            onAdd: makeOnAdd("child"),
        });
        if (trash) {
            new Sortable(trash, {
                group: { name: groupName, put: true, pull: false },
                animation: 150,
                onAdd: (evt: { item: HTMLElement }) => BadgeRelPicker.remove(instanceId, evt.item),
            });
        }
    },

    _initAll(root?: ParentNode): void {
        (root ?? document).querySelectorAll<HTMLElement>(".badge-rel-picker").forEach((picker) => {
            if (picker.dataset.relInit === "1") return;
            picker.dataset.relInit = "1";
            if (picker.dataset.pickerId) BadgeRelPicker._makeSortable(picker.dataset.pickerId);
        });
    },
};

export function installGlobalBadgeRelPicker(): void {
    window.BadgeRelPicker = BadgeRelPicker;
    BadgeRelPicker._initAll();
    document.body.addEventListener("htmx:afterSettle", () => BadgeRelPicker._initAll());
    document.addEventListener("click", (e) => {
        if (!(e.target as Element).closest(".badge-rel-add-dropdown")) {
            document.querySelectorAll<HTMLElement>(".badge-rel-popup").forEach((p) => {
                p.hidden = true;
            });
        }
    });
}

declare global {
    interface Window {
        BadgeRelPicker: typeof BadgeRelPicker;
    }
}
