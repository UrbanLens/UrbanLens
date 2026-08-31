import Sortable from "sortablejs";
import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";
import { ORG_NS_BY_LABEL_KIND } from "./organize-filter-engine";

/**
 * Priority tab: plain drag-handle reordering (via Sortable) plus a manual
 * click-based multi-select (shift-range) that dispatches to whichever tab's
 * bulk-edit dialog matches the selected items' kind.
 *
 * The original template additionally tried to enable Sortable's MultiDrag
 * plugin (`Sortable.mount(new Sortable.MultiDrag())`, `opts.multiDrag = true`)
 * gated on `window.Sortable.MultiDrag` being truthy. That property was never
 * actually exposed by the sortablejs version in use (1.15.x's UMD bundle
 * auto-mounts the plugin internally without exposing the class), so the
 * guard was always false - multiDrag was never enabled, and worse,
 * `_setPrioritySelected` always took the `Sortable.utils.select/deselect`
 * branch (since `Sortable.utils` itself IS populated by the auto-mount) which
 * silently no-ops without `options.multiDrag`, so clicking a priority item
 * never visibly selected it. This port drops the dead MultiDrag branch
 * entirely and always toggles the selection class directly, which is the
 * only path that ever actually worked.
 */
export function initOrganizePriority(): void {
    let prioritySortable: Sortable | null = null;
    let priorityOrderEditing: {
        item: HTMLElement;
        editor: HTMLElement;
        badge: HTMLElement;
        input: HTMLInputElement;
        saveBtn: HTMLElement;
        originalValue: number;
        list: HTMLElement;
        cancelled: boolean;
    } | null = null;
    let lastClickedIdx = -1;

    function priorityOrderBadge(item: HTMLElement): HTMLElement | null {
        return item.querySelector(".priority-order-editor")?.querySelector(".priority-order-chip") ?? null;
    }

    function flashPriorityOrderSaved(item: HTMLElement): void {
        item.classList.remove("priority-item--order-saved");
        void item.offsetWidth;
        item.classList.add("priority-item--order-saved");
        const badge = priorityOrderBadge(item);
        if (badge) {
            badge.classList.remove("priority-order-chip--flash");
            void badge.offsetWidth;
            badge.classList.add("priority-order-chip--flash");
        }
        window.setTimeout(() => item.classList.remove("priority-item--order-saved"), 650);
    }

    function closeOrderEditor(restoreValue: number): void {
        if (!priorityOrderEditing) return;
        const edit = priorityOrderEditing;
        priorityOrderEditing = null;
        edit.item.classList.remove("priority-item--editing-order");
        edit.editor.classList.remove("is-editing");
        edit.badge.textContent = String(restoreValue);
        edit.input.value = String(restoreValue);
        edit.input.setAttribute("aria-hidden", "true");
        edit.input.tabIndex = -1;
        edit.saveBtn.setAttribute("aria-hidden", "true");
        edit.saveBtn.tabIndex = -1;
    }

    /** Reorder failed - put the list back the way the server still has it,
     * rather than leaving a drag/jump shown as if it landed when it didn't. */
    function restorePriorityOrder(list: HTMLElement, previousOrder: HTMLElement[]): void {
        previousOrder.forEach((el) => list.appendChild(el));
        previousOrder.forEach((el, i) => {
            const badge = priorityOrderBadge(el);
            if (badge) badge.textContent = String(i + 1);
        });
    }

    async function savePriorityOrder(list: HTMLElement, flashItem: HTMLElement | null, previousOrder: HTMLElement[]): Promise<void> {
        const items = Array.from(list.querySelectorAll<HTMLElement>(".priority-item[data-id]")).map((el, i) => {
            const badge = priorityOrderBadge(el);
            if (badge) badge.textContent = String(i + 1);
            return { id: Number.parseInt(el.dataset.id ?? "0", 10) };
        });
        try {
            const response = await fetch(list.dataset.saveUrl ?? "", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ items }),
            });
            if (!response.ok) {
                const text = await response.text();
                throw new Error(text || response.statusText);
            }
            if (flashItem) flashPriorityOrderSaved(flashItem);
            toast.success("Display order saved.");
        } catch (err) {
            toast.error(`Save failed: ${(err as Error).message}`);
            restorePriorityOrder(list, previousOrder);
        }
    }

    function commitOrderEditor(): void {
        if (!priorityOrderEditing) return;
        const edit = priorityOrderEditing;
        const list = edit.list;
        const total = list.querySelectorAll(".priority-item[data-id]").length;
        const newPos = Number.parseInt(edit.input.value, 10);

        if (Number.isNaN(newPos)) {
            closeOrderEditor(edit.originalValue);
            return;
        }
        const clampedPos = Math.max(1, Math.min(total, newPos));

        const items = Array.from(list.querySelectorAll<HTMLElement>(".priority-item[data-id]"));
        const currentIdx = items.indexOf(edit.item);
        const targetIdx = clampedPos - 1;

        closeOrderEditor(clampedPos);
        if (currentIdx === targetIdx) return;

        edit.item.remove();
        const remaining = Array.from(list.querySelectorAll<HTMLElement>(".priority-item[data-id]"));
        if (targetIdx >= remaining.length) list.appendChild(edit.item);
        else list.insertBefore(edit.item, remaining[targetIdx]!);

        savePriorityOrder(list, edit.item, items);
    }

    function cancelOrderEditor(): void {
        if (priorityOrderEditing) closeOrderEditor(priorityOrderEditing.originalValue);
    }

    function beginPriorityOrderEdit(badge: HTMLElement): void {
        if (priorityOrderEditing) {
            if (priorityOrderEditing.badge === badge) return;
            cancelOrderEditor();
        }

        const editor = badge.closest<HTMLElement>(".priority-order-editor");
        const item = badge.closest<HTMLElement>(".priority-item");
        const list = document.getElementById("priority-list");
        if (!editor || !item || !list) return;

        const input = editor.querySelector<HTMLInputElement>(".priority-order-input");
        const saveBtn = editor.querySelector<HTMLElement>(".priority-order-save");
        if (!input || !saveBtn) return;
        const originalValue = Number.parseInt(badge.textContent ?? "0", 10);
        const total = list.querySelectorAll(".priority-item[data-id]").length;

        input.min = "1";
        input.max = String(total);
        input.value = String(originalValue);
        input.removeAttribute("aria-hidden");
        input.tabIndex = 0;
        saveBtn.removeAttribute("aria-hidden");
        saveBtn.tabIndex = 0;

        editor.classList.add("is-editing");
        item.classList.add("priority-item--editing-order");
        priorityOrderEditing = { item, editor, badge, input, saveBtn, originalValue, list, cancelled: false };

        window.requestAnimationFrame(() => {
            input.focus();
            input.select();
        });

        input.onkeydown = (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                commitOrderEditor();
            } else if (e.key === "Escape") {
                e.preventDefault();
                e.stopPropagation();
                if (priorityOrderEditing) priorityOrderEditing.cancelled = true;
                cancelOrderEditor();
            }
        };
        input.onblur = () => {
            window.setTimeout(() => {
                if (!priorityOrderEditing || priorityOrderEditing.input !== input) return;
                if (priorityOrderEditing.cancelled) return;
                const active = document.activeElement;
                if (active === saveBtn || saveBtn.contains(active)) return;
                commitOrderEditor();
            }, 0);
        };
        saveBtn.onpointerdown = (e) => e.preventDefault();
        saveBtn.onclick = (e) => {
            e.preventDefault();
            commitOrderEditor();
        };
    }

    function priorityItems(): HTMLElement[] {
        const list = document.getElementById("priority-list");
        return list ? Array.from(list.querySelectorAll<HTMLElement>(".priority-item[data-id]")) : [];
    }

    function setPrioritySelected(item: HTMLElement, selected: boolean): void {
        item.classList.toggle("priority-item--selected", selected);
    }

    function updatePrioritySelBar(): void {
        window._orgBulk.deselect = clearPrioritySelection;
        window._orgBulk.edit = () => {
            const items = document.querySelectorAll<HTMLElement>("#priority-list .priority-item--selected");
            if (!items.length) return;
            if (items.length === 1) {
                items[0]!.querySelector<HTMLElement>(".priority-edit-btn")?.click();
                return;
            }
            const kinds = new Set<string>();
            const ids: string[] = [];
            items.forEach((item) => {
                if (item.dataset.kind) kinds.add(item.dataset.kind);
                if (item.dataset.id) ids.push(item.dataset.id);
            });
            if (kinds.size > 1) {
                toast.warning("Select only tags, only categories, or only statuses to bulk edit them together.");
                return;
            }
            const kind = Array.from(kinds)[0];
            const opener = kind ? window._orgBulkEditByIds[ORG_NS_BY_LABEL_KIND[kind] ?? kind] : undefined;
            if (opener) opener(ids);
            else toast.error("Bulk edit is not available for this type.");
        };
        window._orgBulk.merge = () => {
            const picked = selectedPriorityItems();
            if (picked.ids.length < 2) return;
            if (picked.kinds.size > 1) {
                toast.warning("Select only tags, only categories, or only statuses to merge them together.");
                return;
            }
            const opener = picked.kind ? window._orgBulkMergeByIds[ORG_NS_BY_LABEL_KIND[picked.kind] ?? picked.kind] : undefined;
            if (opener) opener(picked.ids);
            else toast.error("Merge is not available for this type.");
        };
        window._orgBulk.del = () => {
            const picked = selectedPriorityItems();
            if (!picked.ids.length) return;
            // Delete is per-kind for the same reason edit and merge are: each kind's
            // rows live in a different panel, and the bulk-delete endpoint and the
            // rows it re-renders are chosen by kind.
            if (picked.kinds.size > 1) {
                toast.warning("Select only tags, only categories, or only statuses to delete them together.");
                return;
            }
            const opener = picked.kind ? window._orgBulkDeleteByIds[ORG_NS_BY_LABEL_KIND[picked.kind] ?? picked.kind] : undefined;
            if (opener) opener(picked.ids);
            else toast.error("Delete is not available for this type.");
        };
        const n = document.querySelectorAll("#priority-list .priority-item--selected").length;
        window._orgBulkSync(n, { hasEdit: true, hasMerge: true, hasDel: true });
    }

    /** The current selection's ids, and the kind(s) they span. */
    function selectedPriorityItems(): { ids: string[]; kinds: Set<string>; kind: string | undefined } {
        const items = document.querySelectorAll<HTMLElement>("#priority-list .priority-item--selected");
        const kinds = new Set<string>();
        const ids: string[] = [];
        items.forEach((item) => {
            if (item.dataset.kind) kinds.add(item.dataset.kind);
            if (item.dataset.id) ids.push(item.dataset.id);
        });
        return { ids, kinds, kind: Array.from(kinds)[0] };
    }

    function clearPrioritySelection(): void {
        priorityItems().forEach((item) => setPrioritySelected(item, false));
        lastClickedIdx = -1;
        updatePrioritySelBar();
    }

    window._orgRegisterSelectionClearer(clearPrioritySelection);

    function initPrioritySortable(): void {
        const list = document.getElementById("priority-list");
        if (!list) return;
        prioritySortable?.destroy();
        // Captured on drag start, not derived after the fact: two rapid
        // drags can each fire a save while the earlier one is still in
        // flight, and restoring a snapshot taken *before this specific
        // drag* is what makes a failed save undo only its own change.
        let dragStartOrder: HTMLElement[] = [];
        prioritySortable = new Sortable(list, {
            animation: 150,
            handle: ".priority-drag-handle",
            ghostClass: "priority-item--ghost",
            fallbackTolerance: 3,
            onStart: () => {
                dragStartOrder = priorityItems();
            },
            onEnd: () => {
                savePriorityOrder(list, null, dragStartOrder);
            },
        });
    }

    // Delegated from #panel-priority, not #priority-list, for the same reason
    // as the htmx:afterSwap binding below: #priority-list may not exist yet
    // when a deferred Priority tab first attaches its listeners.
    document.getElementById("panel-priority")?.addEventListener("click", (e) => {
        const target = e.target as HTMLElement;
        const badge = target.closest<HTMLElement>(".priority-order-chip");
        if (badge) {
            e.preventDefault();
            beginPriorityOrderEdit(badge);
            return;
        }
        const jumpBtn = target.closest<HTMLElement>("[data-priority-jump]");
        if (jumpBtn) {
            const jumpItem = jumpBtn.closest<HTMLElement>(".priority-item");
            const list = document.getElementById("priority-list");
            if (!jumpItem || !list) return;
            const previousOrder = priorityItems();
            if (jumpBtn.dataset.priorityJump === "top") list.insertBefore(jumpItem, list.firstElementChild);
            else list.appendChild(jumpItem);
            savePriorityOrder(list, jumpItem, previousOrder);
            return;
        }

        const item = target.closest<HTMLElement>(".priority-item");
        if (!item) return;
        if (target.closest(".priority-drag-handle,.priority-order-editor,a,button,input,select,textarea")) return;

        const items = priorityItems();
        const idx = items.indexOf(item);
        const isSelected = item.classList.contains("priority-item--selected");

        if (e.shiftKey && lastClickedIdx >= 0) {
            const lo = Math.min(idx, lastClickedIdx);
            const hi = Math.max(idx, lastClickedIdx);
            const targetState = !isSelected;
            for (let i = lo; i <= hi; i++) {
                const el = items[i];
                if (el) setPrioritySelected(el, targetState);
            }
        } else {
            setPrioritySelected(item, !isSelected);
            lastClickedIdx = idx;
        }
        updatePrioritySelBar();
    });

    window._initPrioritySortable = initPrioritySortable;

    // Bound to #panel-priority (always present), not #priority-list itself:
    // the Priority tab's own content loads lazily via `hx-trigger="revealed"`
    // when it isn't the tab shown on page load (see build_organize_page_context's
    // deferral of the other label tabs, applied here too), so #priority-list
    // may not exist yet when this listener is attached. htmx:afterSwap bubbles,
    // so this still fires for that first load, and for the list's own
    // subsequent self-refresh (`_priority_list.html`'s
    // hx-trigger="refreshPriority from:body") - either way #priority-list's
    // children (and Sortable's references to them) don't survive an innerHTML
    // swap, so it needs rebinding same as the tab-switch case in organize-header.ts.
    document.getElementById("panel-priority")?.addEventListener("htmx:afterSwap", () => {
        clearPrioritySelection();
        initPrioritySortable();
    });

    if (document.getElementById("panel-priority") && !document.getElementById("panel-priority")!.hidden) {
        initPrioritySortable();
    }
}
