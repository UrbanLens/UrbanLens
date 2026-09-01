import Sortable from "sortablejs";
import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";

/**
 * Site-admin tag mapping page: drag-and-drop between equivalence groups (via
 * Sortable, one instance per `.exttag-group-list`, all sharing one `group:`
 * name so a chip can move between any of them) plus click/shift-click
 * multi-select on the ungrouped pool for the "Group selected" bulk action.
 *
 * Both interactions are ports of the exact patterns already proven in
 * `organize-priority.ts`: a stable outer container (`#panel-external-tags`,
 * never itself swapped) owns delegated click listeners and the
 * `htmx:afterSwap` re-init, since `#external-tag-mapping-body` and
 * everything inside it *is* swapped wholesale by every server action here -
 * listeners bound directly to its descendants would go stale after the
 * first swap.
 */
export function initExternalTagMapping(): void {
    const panel = document.getElementById("panel-external-tags");
    if (!panel) return;

    let sortables: Sortable[] = [];
    let lastClickedIdx = -1;

    function poolChips(): HTMLElement[] {
        const pool = document.getElementById("exttag-ungrouped-pool");
        return pool ? Array.from(pool.querySelectorAll<HTMLElement>(".exttag-vocab-chip[data-entry-id]")) : [];
    }

    function setSelected(chip: HTMLElement, selected: boolean): void {
        chip.classList.toggle("exttag-vocab-chip--selected", selected);
    }

    function updateBulkBar(): void {
        const bar = document.getElementById("exttag-bulk-bar");
        if (!bar) return;
        const count = document.querySelectorAll("#exttag-ungrouped-pool .exttag-vocab-chip--selected").length;
        // A single selection is a valid action too - it creates a singleton
        // group, which is how an admin vetoes a coincidental default match.
        bar.hidden = count < 1;
        const countEl = bar.querySelector(".exttag-bulk-count");
        if (countEl) countEl.textContent = `${count} selected`;
    }

    function clearSelection(): void {
        poolChips().forEach((chip) => setSelected(chip, false));
        lastClickedIdx = -1;
        updateBulkBar();
    }

    function destroySortables(): void {
        sortables.forEach((s) => s.destroy());
        sortables = [];
    }

    function initSortableLists(): void {
        document.querySelectorAll<HTMLElement>(".exttag-group-list").forEach((list) => {
            sortables.push(
                new Sortable(list, {
                    group: "exttag-map",
                    animation: 150,
                    ghostClass: "exttag-chip--ghost",
                    // The preferred-star button lives inside each draggable
                    // chip; without this a click on it can be swallowed as a
                    // nascent drag instead of reaching its own hx-post handler.
                    filter: ".exttag-preferred-btn",
                    preventOnFilter: false,
                    onEnd: (evt) => {
                        // A reorder within the same list has no server meaning -
                        // only cross-list moves (group membership changes) do.
                        if (evt.from === evt.to) return;
                        const item = evt.item;
                        const entryId = item.dataset.entryId ?? "";
                        void moveEntry(item, entryId, (evt.to as HTMLElement).dataset.groupId ?? "", evt.from as HTMLElement);
                    },
                }),
            );
        });
    }

    async function moveEntry(item: HTMLElement, entryId: string, targetGroupId: string, fromList: HTMLElement): Promise<void> {
        const body = document.getElementById("external-tag-mapping-body");
        const moveUrl = body?.dataset.moveUrl ?? "";
        try {
            const response = await fetch(moveUrl, {
                method: "POST",
                headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": getCsrfToken() },
                body: new URLSearchParams({ entry_id: entryId, target_group_id: targetGroupId }),
            });
            const data = (await response.json()) as { ok: boolean; message?: string; emptied_group_id?: number | null };
            if (!response.ok || !data.ok) throw new Error(data.message || "Move failed");
            if (data.emptied_group_id) {
                document.querySelector(`.exttag-group-card[data-group-id="${data.emptied_group_id}"]`)?.remove();
            }
            toast.success("Tag moved.");
        } catch (err) {
            // The drag already relocated the DOM node - put it back. Exact
            // position within the origin list doesn't matter (chip order
            // within a group/pool carries no meaning), just which list.
            fromList.appendChild(item);
            toast.error(`Move failed: ${(err as Error).message}`);
        }
    }

    function replaceBody(html: string): void {
        const wrapper = document.getElementById("external-tag-mapping-body");
        if (!wrapper) return;
        const temp = document.createElement("div");
        temp.innerHTML = html.trim();
        const newBody = temp.firstElementChild;
        if (!newBody) return;
        wrapper.replaceWith(newBody);
        boot();
    }

    async function groupSelected(): Promise<void> {
        const selected = Array.from(document.querySelectorAll<HTMLElement>("#exttag-ungrouped-pool .exttag-vocab-chip--selected"));
        if (selected.length < 1) return;
        const body = document.getElementById("external-tag-mapping-body");
        const groupUrl = body?.dataset.groupUrl ?? "";
        const search = body?.dataset.search ?? "";
        const formData = new FormData();
        selected.forEach((chip) => formData.append("entry_id", chip.dataset.entryId ?? ""));
        formData.append("search", search);
        try {
            const response = await fetch(groupUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body: formData });
            const html = await response.text();
            if (!response.ok) throw new Error("Grouping failed");
            replaceBody(html);
            toast.success("Tags grouped.");
        } catch (err) {
            toast.error(`Grouping failed: ${(err as Error).message}`);
        }
    }

    function boot(): void {
        destroySortables();
        initSortableLists();
        lastClickedIdx = -1;
        updateBulkBar();
    }

    panel.addEventListener("click", (e) => {
        const target = e.target as HTMLElement;

        if (target.closest("#exttag-clear-selection-btn")) {
            clearSelection();
            return;
        }
        if (target.closest("#exttag-group-selected-btn")) {
            void groupSelected();
            return;
        }

        const chip = target.closest<HTMLElement>("#exttag-ungrouped-pool .exttag-vocab-chip[data-entry-id]");
        if (!chip) return;
        const chips = poolChips();
        const idx = chips.indexOf(chip);
        const isSelected = chip.classList.contains("exttag-vocab-chip--selected");
        if (e.shiftKey && lastClickedIdx >= 0) {
            const lo = Math.min(idx, lastClickedIdx);
            const hi = Math.max(idx, lastClickedIdx);
            for (let i = lo; i <= hi; i++) {
                const el = chips[i];
                if (el) setSelected(el, true);
            }
        } else {
            setSelected(chip, !isSelected);
            lastClickedIdx = idx;
        }
        updateBulkBar();
    });

    // #external-tag-mapping-body is swapped wholesale by the search box, the
    // preferred-star buttons, and the "Confirm as group" forms (all plain
    // htmx hx-post/hx-get with hx-swap="outerHTML") - Sortable references and
    // the selection state don't survive that, same reasoning as
    // organize-priority.ts's own afterSwap rebind.
    panel.addEventListener("htmx:afterSwap", (e) => {
        const detail = (e as CustomEvent).detail as { target?: HTMLElement };
        if (detail.target?.id === "external-tag-mapping-body") boot();
    });

    boot();
}
