import Sortable from "sortablejs";

import { handleSuggestionKey } from "./suggestion-keys";

/**
 * Parent/child relationship chip picker used by organize's create/edit/bulk-edit dialogs (dashboard/partials/labels/*).
 */
type RelType = "parent" | "child";

export const LabelRelPicker = {
    toggle(instanceId: string, relType: RelType, _triggerBtn: HTMLElement): void {
        const popup = document.getElementById(`${instanceId}-popup-${relType}`);
        if (!popup) return;
        const wasHidden = (popup as HTMLElement).hidden;
        document.querySelectorAll<HTMLElement>(".label-rel-popup").forEach((p) => {
            p.hidden = true;
        });
        if (!wasHidden) return;
        LabelRelPicker._loadCandidates(instanceId, relType);
        (popup as HTMLElement).hidden = false;
        const search = popup.querySelector<HTMLInputElement>(".label-rel-search");
        if (search) {
            search.value = "";
            search.focus();
        }
    },

    /** Selects a suggestion into the picker and direction whose popup holds it. */
    pick(btn: HTMLElement): void {
        const instanceId = btn.closest<HTMLElement>(".label-rel-picker")?.dataset.pickerId;
        const relType = btn.closest<HTMLElement>(".label-rel-suggestions")?.dataset.relType;
        if (instanceId && (relType === "parent" || relType === "child")) LabelRelPicker.select(instanceId, relType, btn);
    },

    /** Copies the page's shared candidate list into a popup the first time it opens. */
    _loadCandidates(instanceId: string, relType: RelType): void {
        const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
        const source = container?.dataset.candidatesFrom;
        if (!container || !source || container.dataset.candidatesLoaded === "1") return;
        const template = document.getElementById(`label-rel-candidates-${source}`);
        if (!(template instanceof HTMLTemplateElement)) return;
        const offered = new Set(Array.from(container.querySelectorAll<HTMLElement>(".label-rel-suggestion"), (button) => button.dataset.id));
        const chosen = new Set([...LabelRelPicker.getSelectedIds(instanceId, "parent"), ...LabelRelPicker.getSelectedIds(instanceId, "child")].map(String));
        const copies = document.createDocumentFragment();
        template.content.querySelectorAll<HTMLElement>(".label-rel-suggestion").forEach((candidate) => {
            if (offered.has(candidate.dataset.id)) return;
            const copy = candidate.cloneNode(true) as HTMLElement;
            if (chosen.has(copy.dataset.id ?? "")) copy.classList.add("label-rel-suggestion--hidden");
            copies.appendChild(copy);
        });
        container.prepend(copies);
        container.dataset.candidatesLoaded = "1";
        LabelRelPicker._applyFilters(instanceId, relType);
    },

    select(instanceId: string, relType: RelType, btn: HTMLElement): void {
        if (btn.classList.contains("label-rel-suggestion--hidden")) return;
        const group = document.getElementById(`${instanceId}-sel-${relType}`);
        if (!group) return;
        const id = btn.dataset.id;
        if (!id || group.querySelector(`.label-rel-chip[data-id="${id}"]`)) return;

        const picker = document.querySelector<HTMLElement>(`[data-picker-id="${instanceId}"]`);
        const pill = document.createElement("span");
        pill.className = "tag-chip";
        const color = btn.style.getPropertyValue("--tag-color");
        if (color) pill.style.setProperty("--tag-color", color);
        pill.innerHTML = btn.innerHTML;
        pill.querySelector(".label-kind-chip")?.remove();
        if (picker?.dataset.mode === "replace") {
            const hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = `${relType}_ids`;
            hidden.value = id;
            pill.appendChild(hidden);
        }

        const chip = document.createElement("span");
        chip.className = "label-rel-chip";
        chip.dataset.id = id;
        chip.appendChild(pill);

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "tag-chip-remove";
        removeBtn.title = "Remove";
        removeBtn.innerHTML = "&times;";
        chip.appendChild(removeBtn);

        group.appendChild(chip);
        LabelRelPicker._hideSuggestion(instanceId, id);
        LabelRelPicker._updateEmptyHints(instanceId);
    },

    remove(instanceId: string, chipEl: HTMLElement | null): void {
        if (!chipEl) return;
        const id = chipEl.dataset.id;
        chipEl.remove();
        if (id) LabelRelPicker._showSuggestion(instanceId, id);
        LabelRelPicker._updateEmptyHints(instanceId);
    },

    _hideSuggestion(instanceId: string, id: string): void {
        (["parent", "child"] as RelType[]).forEach((relType) => {
            const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
            container?.querySelector(`.label-rel-suggestion[data-id="${id}"]`)?.classList.add("label-rel-suggestion--hidden");
        });
    },

    _showSuggestion(instanceId: string, id: string): void {
        (["parent", "child"] as RelType[]).forEach((relType) => {
            const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
            const btn = container?.querySelector(`.label-rel-suggestion[data-id="${id}"]`);
            if (btn) {
                btn.classList.remove("label-rel-suggestion--hidden");
                LabelRelPicker._applyFilters(instanceId, relType);
            }
        });
    },

    setTab(instanceId: string, relType: RelType, kind: string, btn: HTMLElement): void {
        const popup = document.getElementById(`${instanceId}-popup-${relType}`);
        if (!popup) return;
        popup.querySelectorAll(".label-rel-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
        if (container) container.dataset.activeTab = kind;
        LabelRelPicker._applyFilters(instanceId, relType);
    },

    filter(instanceId: string, relType: RelType, query: string): void {
        const popup = document.getElementById(`${instanceId}-popup-${relType}`);
        if (popup) popup.dataset.searchQuery = query.toLowerCase().trim();
        LabelRelPicker._applyFilters(instanceId, relType);
    },

    _applyFilters(instanceId: string, relType: RelType): void {
        const popup = document.getElementById(`${instanceId}-popup-${relType}`);
        const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
        if (!popup || !container) return;
        const q = popup.dataset.searchQuery ?? "";
        const tab = container.dataset.activeTab ?? "";
        container.querySelectorAll<HTMLElement>(".label-rel-suggestion").forEach((btn) => {
            const matchesTab = !tab || btn.dataset.kind === tab;
            const matchesSearch = !q || (btn.dataset.name ?? "").indexOf(q) !== -1;
            btn.style.display = matchesTab && matchesSearch ? "" : "none";
        });
    },

    _updateEmptyHints(instanceId: string): void {
        (["parent", "child"] as RelType[]).forEach((relType) => {
            const group = document.getElementById(`${instanceId}-sel-${relType}`);
            const hint = group?.parentElement?.querySelector<HTMLElement>(".label-rel-empty-hint");
            if (hint) hint.hidden = (group?.children.length ?? 0) > 0;
        });
    },

    getSelectedIds(instanceId: string, relType: RelType): number[] {
        const group = document.getElementById(`${instanceId}-sel-${relType}`);
        if (!group) return [];
        return Array.from(group.querySelectorAll<HTMLElement>(".label-rel-chip")).map((c) => Number.parseInt(c.dataset.id ?? "0", 10));
    },

    reset(instanceId: string): void {
        (["parent", "child"] as RelType[]).forEach((relType) => {
            const group = document.getElementById(`${instanceId}-sel-${relType}`);
            if (!group) return;
            Array.from(group.querySelectorAll<HTMLElement>(".label-rel-chip")).forEach((chip) => LabelRelPicker.remove(instanceId, chip));
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
            LabelRelPicker._updateEmptyHints(instanceId);
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
                onAdd: (evt: { item: HTMLElement }) => LabelRelPicker.remove(instanceId, evt.item),
            });
        }
    },

    _initAll(root?: ParentNode): void {
        (root ?? document).querySelectorAll<HTMLElement>(".label-rel-picker").forEach((picker) => {
            if (picker.dataset.relInit === "1") return;
            picker.dataset.relInit = "1";
            if (picker.dataset.pickerId) LabelRelPicker._makeSortable(picker.dataset.pickerId);
        });
    },
};

function relTypeOf(el: Element | null | undefined): RelType | null {
    const relType = el instanceof HTMLElement ? el.dataset.relType : undefined;
    return relType === "parent" || relType === "child" ? relType : null;
}

/** The picker a control belongs to, and the direction its popup (or Add button) serves. */
function controlContext(control: Element): { instanceId: string; relType: RelType | null } | null {
    const instanceId = control.closest<HTMLElement>(".label-rel-picker")?.dataset.pickerId;
    if (!instanceId) return null;
    return { instanceId, relType: relTypeOf(control.closest(".label-rel-popup, .label-rel-add-btn")) };
}

/** The picker's controls name their action in markup: an Add button, a popup's kind tabs and search, its suggestions, and each chip's remove button. */
function onPickerClick(event: MouseEvent): void {
    const target = event.target instanceof Element ? event.target : null;
    const control = target?.closest<HTMLElement>(".label-rel-add-btn, .label-rel-tab, .label-rel-suggestion, .label-rel-chip .tag-chip-remove");
    const context = control ? controlContext(control) : null;
    if (!control || !context) return;
    if (control.classList.contains("label-rel-suggestion")) LabelRelPicker.pick(control);
    else if (control.classList.contains("tag-chip-remove")) LabelRelPicker.remove(context.instanceId, control.closest<HTMLElement>(".label-rel-chip"));
    else if (!context.relType) return;
    else if (control.classList.contains("label-rel-add-btn")) LabelRelPicker.toggle(context.instanceId, context.relType, control);
    else LabelRelPicker.setTab(context.instanceId, context.relType, control.dataset.kind ?? "", control);
}

function onPickerKeydown(event: KeyboardEvent): void {
    const field = event.target;
    if (!(field instanceof HTMLInputElement) || !field.classList.contains("label-rel-search")) return;
    const popup = field.closest<HTMLElement>(".label-rel-popup");
    if (!popup) return;
    handleSuggestionKey(event, {
        items: () => Array.from(popup.querySelectorAll<HTMLElement>(".label-rel-suggestion:not(.label-rel-suggestion--hidden)")).filter((button) => button.style.display !== "none"),
        isOpen: () => !popup.hidden,
        close: () => {
            popup.hidden = true;
        },
    });
}

function onPickerInput(event: Event): void {
    const field = event.target;
    if (!(field instanceof HTMLInputElement) || !field.classList.contains("label-rel-search")) return;
    const context = controlContext(field);
    if (context?.relType) LabelRelPicker.filter(context.instanceId, context.relType, field.value);
}

export function installGlobalLabelRelPicker(): void {
    window.LabelRelPicker = LabelRelPicker;
    LabelRelPicker._initAll();
    document.body.addEventListener("htmx:afterSettle", () => LabelRelPicker._initAll());
    document.addEventListener("click", (e) => {
        if (!(e.target as Element).closest(".label-rel-add-dropdown")) {
            document.querySelectorAll<HTMLElement>(".label-rel-popup").forEach((p) => {
                p.hidden = true;
            });
        }
    });
    document.addEventListener("click", onPickerClick);
    document.addEventListener("input", onPickerInput);
    document.addEventListener("keydown", onPickerKeydown);
}

declare global {
    interface Window {
        LabelRelPicker: typeof LabelRelPicker;
    }
}
