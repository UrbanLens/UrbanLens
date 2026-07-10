import Sortable from "sortablejs";
import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";

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
        return item.querySelector(".priority-order-editor")?.querySelector(".priority-order-badge") ?? null;
    }

    function flashPriorityOrderSaved(item: HTMLElement): void {
        item.classList.remove("priority-item--order-saved");
        void item.offsetWidth;
        item.classList.add("priority-item--order-saved");
        const badge = priorityOrderBadge(item);
        if (badge) {
            badge.classList.remove("priority-order-badge--flash");
            void badge.offsetWidth;
            badge.classList.add("priority-order-badge--flash");
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

    async function savePriorityOrder(list: HTMLElement, flashItem: HTMLElement | null): Promise<void> {
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

        savePriorityOrder(list, edit.item);
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
            const opener = kind ? window._orgBulkEditByIds[kind] : undefined;
            if (opener) opener(ids);
            else toast.error("Bulk edit is not available for this type.");
        };
        const n = document.querySelectorAll("#priority-list .priority-item--selected").length;
        window._orgBulkSync(n, { hasEdit: true, hasMerge: false, hasDel: false });
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
        prioritySortable = new Sortable(list, {
            animation: 150,
            handle: ".priority-drag-handle",
            ghostClass: "priority-item--ghost",
            fallbackTolerance: 3,
            onEnd: () => {
                savePriorityOrder(list, null);
            },
        });
    }

    document.getElementById("priority-list")?.addEventListener("click", (e) => {
        const target = e.target as HTMLElement;
        const badge = target.closest<HTMLElement>(".priority-order-badge");
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
            if (jumpBtn.dataset.priorityJump === "top") list.insertBefore(jumpItem, list.firstElementChild);
            else list.appendChild(jumpItem);
            savePriorityOrder(list, jumpItem);
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

    if (document.getElementById("panel-priority") && !document.getElementById("panel-priority")!.hidden) {
        initPrioritySortable();
    }
}
