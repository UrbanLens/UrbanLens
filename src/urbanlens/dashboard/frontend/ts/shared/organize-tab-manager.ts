import { confirmAction, toast } from "./dialogs";
import { getCsrfToken } from "./csrf";
import { renderIconGlyphHtml, resetIconPicker } from "./icon-picker";
import { resetColorPicker } from "./color-picker";
import { renderTreeView } from "./tree-view";
import { BadgeRelPicker } from "./badge-rel-picker";
import { registerBulkStateUpdater } from "./organize-icon-picker";
import { applyOrgFilter, getOrgVisibleCards, type OrgNamespace } from "./organize-filter-engine";
import { orgHeader } from "./organize-header";

const MATERIAL_ICON_NAME = /^[a-z_]+$/;

function escHtml(s: string): string {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export interface ConvertTarget {
    kind: string;
    label: string;
    endpoint: string;
    /** GET url returning the destination tab's row list, for refreshing it after a convert. */
    rowsUrl?: string;
    /** CSS selector for the destination tab's rows container. */
    rowsTarget?: string;
    /** data-tab value of the destination tab's `.organize-tab` trigger, to switch to it after converting. */
    tabKey?: string;
}

export interface OrgTabManagerConfig {
    ns: OrgNamespace;
    nsCapitalized: string;
    rowsId: string;
    cardSelector: string;
    idKey: string;
    nameKey: string;
    iconKey: string;
    colorKey: string;
    parentsKey: string;
    pinCountKey: string;
    customIconKey?: string;
    locationCountKey?: string;
    checkboxSelector: string;
    entitySingular: string;
    entityPluralLower: string;
    entityPluralCap: string;
    emptyIcon: string;
    deleteWarning?: string;
    endpoints: { bulkDelete: string; bulkEdit: string; multiMerge: string; mergeEditTemplate?: string };
    supportsMergeEdit: boolean;
    isProtected?: (id: string) => boolean;
    convertTargets: ConvertTarget[];
    newForm: { dialogId: string; iconPickerId: string; colorPickerId: string; colorValueId: string; customPreviewId?: string } | null;
    bulkEditDialog: {
        dialogId: string;
        titleId: string;
        confirmId: string;
        iconPickerId: string;
        iconNochangeId: string;
        colorPickerId: string;
        colorValueId: string;
        colorNochangeId: string;
        orderValueId?: string;
        orderNochangeId?: string;
        descValueId?: string;
        descNochangeId?: string;
        convertHintId?: string;
    };
    mergeDialog: {
        dialogId: string;
        titleId: string;
        targetCardId: string;
        sourcesListId: string;
        confirmId: string;
        editNameId?: string;
        /** Icon-picker id (not an element id) - element ids are derived as `icon-value-${editIconId}` etc. */
        editIconId?: string;
        swapHintId?: string;
    };
}

interface CardData {
    id: string;
    name: string;
    color: string;
    icon: string;
    pinCount: string;
    customIcon?: string;
    locationCount?: string;
}

/**
 * Generic per-tab manager for organize/index.html's tag/category/status/people
 * tabs. Consolidates what used to be four separately copy-pasted ~350-450
 * line IIFEs differing mainly in id/dataset naming plus a handful of real
 * capability differences (kind-conversion, merge-time rename/re-icon/re-color,
 * a "protected" badge that can't be a merge source, custom-icon upload).
 *
 * Selection/toolbar model differs from categories.ts/tags.ts's
 * BulkEntityManager: organize selects by clicking anywhere on a card (not a
 * dedicated checkbox) and reports through the shared floating
 * `window._orgBulk` toolbar instead of a per-page selection bar.
 */
export class OrgTabManager {
    private readonly cfg: OrgTabManagerConfig;
    private selected = new Set<string>();
    private lastClickedIdx = -1;
    private mergeTargetId: string | null = null;
    private convertTarget: string | null = null;

    constructor(cfg: OrgTabManagerConfig) {
        this.cfg = cfg;
    }

    init(): void {
        this.wireSelection();
        this.wireRowEditIntercept();
        this.wireBulkEdit();
        this.wireMerge();
        this.wireHtmxHooks();
        registerBulkStateUpdater(this.cfg.ns, () => this.updateBulkState());
        const globalWindow = window as unknown as Record<string, unknown>;
        if (this.cfg.convertTargets.length > 0) {
            globalWindow[`_set${this.cfg.nsCapitalized}BulkConvert`] = (target: string) => this.setConvertTarget(target);
        }
        globalWindow[`_update${this.cfg.nsCapitalized}BulkState`] = () => this.updateBulkState();
        window._orgBulkEditByIds[this.cfg.ns] = (ids: string[]) => {
            this.selected = new Set(ids.map(String));
            this.syncSelectionUi();
            this.openBulkEditDialog();
        };
        window._orgRegisterSelectionClearer(() => {
            this.selected.clear();
            this.lastClickedIdx = -1;
            this.syncSelectionUi();
        });

        orgHeader.register(this.tabKey(), {
            filterTitle: `Filter ${this.cfg.entityPluralLower}`,
            viewAriaLabel: `${this.cfg.entitySingular} view mode`,
            createTitle: `New ${this.cfg.entitySingular}`,
            createHtml: '<i class="material-icons" style="font-size:1.2rem;">add</i>',
            applyView: () => this.applyView(),
            onSelAll: () => this.onSelectAll(),
            updateSelAllBtn: () => this.updateSelAllBtn(),
            onCreate: () => this.onCreate(),
        });

        this.applyView();
    }

    private tabKey(): string {
        return { tag: "tags", cat: "categories", status: "status", people: "people" }[this.cfg.ns] ?? this.cfg.ns;
    }

    private get rows(): HTMLElement | null {
        return document.getElementById(this.cfg.rowsId);
    }

    // ── View toggle ──────────────────────────────────────────────────────
    private applyView(): void {
        const rows = this.rows;
        if (!rows) return;
        const view = orgHeader.getSharedView();
        rows.classList.remove("tag-view--list", "tag-view--gallery", "tag-view--tree");
        rows.classList.add(`tag-view--${view}`);
        orgHeader.syncViewButtons(view);
        if (view === "tree") {
            renderTreeView(rows, { cardSelector: this.cfg.cardSelector, idKey: this.cfg.idKey, parentsKey: this.cfg.parentsKey });
        } else {
            rows.querySelector(".tag-tree-root")?.remove();
            rows.querySelectorAll<HTMLElement>(".tag-card").forEach((c) => {
                c.style.display = "";
            });
        }
    }

    // ── Selection (click-anywhere-on-card model) ────────────────────────
    private visibleCards(): HTMLElement[] {
        return getOrgVisibleCards(this.rows, this.cfg.cardSelector);
    }

    private getVisibleIds(): string[] {
        return this.visibleCards()
            .filter((c) => !this.cfg.isProtected?.(c.dataset[this.cfg.idKey] ?? ""))
            .map((c) => c.dataset[this.cfg.idKey] ?? "")
            .filter(Boolean);
    }

    private syncSelectionUi(): void {
        this.rows?.querySelectorAll<HTMLElement>(this.cfg.cardSelector).forEach((card) => {
            const id = card.dataset[this.cfg.idKey] ?? "";
            card.classList.toggle("tag-card--selected", this.selected.has(id));
            const cb = card.querySelector<HTMLInputElement>(this.cfg.checkboxSelector);
            if (cb) cb.checked = this.selected.has(id);
        });
        this.updateSelectionBar();
    }

    private updateSelAllBtn(): void {
        const btn = document.getElementById("org-header-sel-all");
        if (!btn) return;
        const visIds = this.getVisibleIds();
        const allSel = visIds.length > 0 && visIds.every((id) => this.selected.has(id));
        btn.classList.toggle("deselect-mode", allSel);
        btn.title = allSel ? "Deselect all" : "Select all";
        btn.innerHTML = allSel
            ? '<i class="material-symbols-outlined">remove_done</i>'
            : '<i class="material-symbols-outlined">checklist</i>';
    }

    private onSelectAll(): void {
        const visIds = this.getVisibleIds();
        const allSel = visIds.length > 0 && visIds.every((id) => this.selected.has(id));
        visIds.forEach((id) => {
            if (allSel) this.selected.delete(id);
            else this.selected.add(id);
        });
        this.lastClickedIdx = -1;
        this.syncSelectionUi();
    }

    private updateSelectionBar(): void {
        const n = this.selected.size;
        window._orgBulk.deselect = () => {
            this.selected.clear();
            this.syncSelectionUi();
        };
        window._orgBulk.edit = () => {
            if (!this.selected.size) return;
            if (this.selected.size === 1 && window._orgOpenSingleEdit(`data-${this.datasetAttr(this.cfg.idKey)}`, Array.from(this.selected)[0]!)) return;
            this.openBulkEditDialog();
        };
        window._orgBulk.merge = () => {
            if (this.selected.size < 2) return;
            this.mergeTargetId = Array.from(this.selected)[0]!;
            this.renderMergeDialog();
            (document.getElementById(this.cfg.mergeDialog.dialogId) as HTMLDialogElement).showModal();
        };
        window._orgBulk.del = () => this.bulkDelete();
        window._orgBulkSync(n, { hasEdit: true, hasMerge: true, hasDel: true });
        this.updateSelAllBtn();
    }

    private wireSelection(): void {
        this.rows?.addEventListener("click", (e) => {
            const target = e.target as HTMLElement;
            const card = target.closest<HTMLElement>(this.cfg.cardSelector);
            if (!card) return;

            const cb = target.closest<HTMLInputElement>(this.cfg.checkboxSelector);
            if (cb) {
                // Prevent the browser's native checkbox toggle so this handler stays
                // the single source of truth for `this.selected` - otherwise a direct
                // click on the checkbox flips its visual state without updating
                // `this.selected`, desyncing the bulk toolbar from what's checked.
                e.preventDefault();
            } else if (target.closest("a,button,input,select,textarea")) {
                return;
            }

            const cards = this.visibleCards();
            const idx = cards.indexOf(card);
            const id = card.dataset[this.cfg.idKey] ?? "";
            const isProtected = this.cfg.isProtected?.(id) ?? false;

            if (e.shiftKey && this.lastClickedIdx >= 0) {
                const lastCard = cards[this.lastClickedIdx];
                const lastIdx = lastCard ? cards.indexOf(lastCard) : -1;
                const lo = lastIdx >= 0 ? Math.min(idx, lastIdx) : idx;
                const hi = lastIdx >= 0 ? Math.max(idx, lastIdx) : idx;
                const targetState = !this.selected.has(id);
                for (let i = lo; i <= hi; i++) {
                    const cid = cards[i]?.dataset[this.cfg.idKey];
                    if (!cid) continue;
                    if (this.cfg.isProtected?.(cid)) continue;
                    if (targetState) this.selected.add(cid);
                    else this.selected.delete(cid);
                }
                if (isProtected) {
                    if (targetState) this.selected.add(id);
                    else this.selected.delete(id);
                }
            } else {
                if (this.selected.has(id)) this.selected.delete(id);
                else this.selected.add(id);
                this.lastClickedIdx = idx;
            }
            this.syncSelectionUi();
        });
    }

    // When multiple badges are selected, a row's own "Edit" pencil should
    // still open the bulk-edit dialog for the whole selection rather than
    // the single-item form - otherwise it silently edits just that one row
    // while the rest of the selection looks like it's being included. Runs
    // in the capture phase so it can veto the click before htmx's own
    // bubble-phase hx-get listener on the button fires.
    private wireRowEditIntercept(): void {
        this.rows?.addEventListener(
            "click",
            (e) => {
                if (this.selected.size <= 1) return;
                const btn = (e.target as HTMLElement).closest<HTMLElement>('.tag-card-actions .btn--icon[title="Edit"]');
                if (!btn) return;
                const card = btn.closest<HTMLElement>(this.cfg.cardSelector);
                const id = card?.dataset[this.cfg.idKey];
                if (!id || !this.selected.has(id)) return;
                e.preventDefault();
                e.stopImmediatePropagation();
                this.openBulkEditDialog();
            },
            true,
        );
    }

    private onRowsUpdated(): void {
        this.selected.clear();
        this.lastClickedIdx = -1;
        this.syncSelectionUi();
        this.applyView();
        applyOrgFilter(this.cfg.ns);
    }

    private wireHtmxHooks(): void {
        this.rows?.addEventListener("htmx:afterSwap", () => this.onRowsUpdated());
        document.addEventListener("org:filter-applied", (e) => {
            if ((e as CustomEvent).detail.ns === this.cfg.ns) this.updateSelAllBtn();
        });
    }

    // ── New-item creation ────────────────────────────────────────────────
    private onCreate(): void {
        const f = document.getElementById(this.cfg.newForm?.dialogId ?? "") as HTMLDialogElement | null;
        if (!f) return;
        if (this.cfg.newForm) {
            f.querySelector("form")?.reset();
            resetIconPicker(this.cfg.newForm.iconPickerId);
            resetColorPicker(this.cfg.newForm.colorPickerId, this.cfg.newForm.colorValueId);
            if (this.cfg.newForm.customPreviewId) {
                const preview = document.getElementById(this.cfg.newForm.customPreviewId) as HTMLImageElement | null;
                if (preview) {
                    preview.src = "";
                    preview.style.display = "none";
                }
            }
        } else {
            f.querySelector("form")?.reset();
        }
        if (!f.open) f.showModal();
    }

    // ── Bulk delete ──────────────────────────────────────────────────────
    private async bulkDelete(): Promise<void> {
        const n = this.selected.size;
        if (!n) return;
        const entity = n === 1 ? this.cfg.entitySingular.toLowerCase() : this.cfg.entityPluralLower;
        let message = `Delete ${n} ${entity}?`;
        if (this.cfg.deleteWarning) message += `\n${this.cfg.deleteWarning}`;
        if (!(await confirmAction({ title: `Delete ${this.cfg.entityPluralCap}`, message, confirmLabel: "Delete" }))) return;

        const ids = Array.from(this.selected).map((id) => Number.parseInt(id, 10));
        try {
            const html = await this.postForHtml(this.cfg.endpoints.bulkDelete, { ids });
            this.replaceRows(html);
            this.onRowsUpdated();
            toast.success(n === 1 ? `1 ${this.cfg.entitySingular.toLowerCase()} deleted.` : `${n} ${this.cfg.entityPluralLower} deleted.`);
        } catch (err) {
            toast.error(`Delete failed: ${(err as Error).message}`);
        }
    }

    // ── Bulk edit / convert ──────────────────────────────────────────────
    private setConvertTarget(target: string): void {
        const btns = document.querySelectorAll<HTMLElement>(`#${this.cfg.bulkEditDialog.dialogId} .kind-toggle-option`);
        if (this.convertTarget === target) {
            this.convertTarget = null;
            btns.forEach((b) => b.classList.remove("is-active"));
        } else {
            this.convertTarget = target;
            btns.forEach((b) => b.classList.remove("is-active"));
            document.getElementById(`${this.cfg.ns}-bulk-convert-to-${target}`)?.classList.add("is-active");
        }
        this.updateBulkState();
    }

    private updateBulkState(): void {
        const converting = !!this.convertTarget;
        const hintId = this.cfg.bulkEditDialog.convertHintId;
        if (hintId) {
            const hint = document.getElementById(hintId);
            if (hint) {
                hint.hidden = !converting;
                if (converting) {
                    const targetLabel = this.cfg.convertTargets.find((t) => t.kind === this.convertTarget)?.label ?? "";
                    hint.textContent = `All pin memberships will be migrated. Selected parent links will be added after conversion. You will be redirected to the ${targetLabel.toLowerCase()} tab.`;
                }
            }
        }
        const btn = document.getElementById(this.cfg.bulkEditDialog.confirmId) as HTMLButtonElement | null;
        if (btn && !btn.disabled) {
            const targetLabel = this.cfg.convertTargets.find((t) => t.kind === this.convertTarget)?.label ?? "";
            btn.innerHTML = converting
                ? `<i class="material-icons" style="font-size:1rem;vertical-align:middle">swap_horiz</i> Convert to ${targetLabel}`
                : '<i class="material-icons" style="font-size:1rem;vertical-align:middle">edit</i> Apply Changes';
        }
    }

    private openBulkEditDialog(): void {
        const d = this.cfg.bulkEditDialog;
        const ids = Array.from(this.selected);
        const iconSet = new Set<string>();
        const colorSet = new Set<string>();
        const customIconSet = new Set<string>();
        ids.forEach((id) => {
            const card = document.querySelector<HTMLElement>(`[data-${this.datasetAttr(this.cfg.idKey)}="${id}"]`);
            if (!card) return;
            iconSet.add(card.dataset[this.cfg.iconKey] ?? "");
            colorSet.add(card.dataset[this.cfg.colorKey] ?? "");
            if (this.cfg.customIconKey) customIconSet.add(card.dataset[this.cfg.customIconKey] ?? "");
        });
        const sharedIcon = iconSet.size === 1 ? Array.from(iconSet)[0]! : null;
        const sharedColor = colorSet.size === 1 ? Array.from(colorSet)[0]! : null;
        // A shared *custom uploaded* icon has no representation in the symbol/emoji
        // picker below - without this check, every selected badge sharing one was
        // computed as sharedIcon="" (empty symbol field), which unchecked "no
        // change" and would submit icon="" on save, silently wiping the custom
        // icon from all of them even for a bulk edit that only touched color/order.
        const sharedCustomIcon = this.cfg.customIconKey && customIconSet.size === 1 ? Array.from(customIconSet)[0]! : null;

        const iconNochange = document.getElementById(d.iconNochangeId) as HTMLInputElement;
        const iconValue = document.getElementById(`icon-value-${d.iconPickerId}`) as HTMLInputElement | null;
        const iconCurrent = document.getElementById(`icon-current-${d.iconPickerId}`);
        const iconGrid = document.getElementById(`icon-grid-${d.iconPickerId}`);
        iconGrid?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
        if (sharedCustomIcon) {
            iconNochange.checked = true;
            if (iconValue) iconValue.value = "";
            if (iconCurrent) iconCurrent.innerHTML = `<img src="${sharedCustomIcon}" alt="" class="tag-icon-img"> <span class="icon-picker-none-label">Custom icon (kept unless you pick a new one)</span>`;
        } else if (sharedIcon !== null) {
            iconNochange.checked = false;
            if (iconValue) iconValue.value = sharedIcon;
            if (iconCurrent) iconCurrent.innerHTML = renderIconGlyphHtml(sharedIcon);
            if (sharedIcon && iconGrid) iconGrid.querySelector(`[data-icon="${sharedIcon}"]`)?.classList.add("selected");
            else iconGrid?.querySelector(".icon-picker-none")?.classList.add("selected");
        } else {
            iconNochange.checked = true;
            if (iconValue) iconValue.value = "";
            if (iconCurrent) iconCurrent.innerHTML = '<span class="icon-picker-none-label">No icon</span>';
        }

        const colorNochange = document.getElementById(d.colorNochangeId) as HTMLInputElement;
        const colorPickerEl = document.getElementById(d.colorPickerId);
        const colorValue = document.getElementById(d.colorValueId) as HTMLInputElement | null;
        colorPickerEl?.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
        if (sharedColor !== null) {
            colorNochange.checked = false;
            if (colorValue) colorValue.value = sharedColor;
            if (sharedColor) colorPickerEl?.querySelector(`[data-color="${sharedColor}"]`)?.classList.add("selected");
        } else {
            colorNochange.checked = true;
            if (colorValue) colorValue.value = "";
        }

        if (d.orderValueId && d.orderNochangeId) {
            const orderNochange = document.getElementById(d.orderNochangeId) as HTMLInputElement;
            const orderValue = document.getElementById(d.orderValueId) as HTMLInputElement | null;
            orderNochange.checked = true;
            if (orderValue) {
                orderValue.value = "0";
                orderValue.dataset.bulkOriginal = "0";
            }
        }
        if (d.descValueId && d.descNochangeId) {
            const descNochange = document.getElementById(d.descNochangeId) as HTMLInputElement;
            const descValue = document.getElementById(d.descValueId) as HTMLInputElement | null;
            descNochange.checked = true;
            if (descValue) {
                descValue.value = "";
                descValue.dataset.bulkOriginal = "";
            }
        }

        BadgeRelPicker.reset(`${this.cfg.ns}-bulk`);
        this.convertTarget = null;
        document.querySelectorAll(`#${d.dialogId} .kind-toggle-option`).forEach((b) => b.classList.remove("is-active"));

        const titleEl = document.getElementById(d.titleId);
        if (titleEl) titleEl.textContent = `Edit ${ids.length} ${ids.length === 1 ? this.cfg.entitySingular : this.cfg.entityPluralCap}`;
        const confirmBtn = document.getElementById(d.confirmId) as HTMLButtonElement;
        confirmBtn.disabled = false;
        confirmBtn.innerHTML = '<i class="material-icons" style="font-size:1rem;vertical-align:middle">edit</i> Apply Changes';

        this.updateBulkState();
        (document.getElementById(d.dialogId) as HTMLDialogElement).showModal();
    }

    private wireBulkEdit(): void {
        const d = this.cfg.bulkEditDialog;

        document.getElementById(d.iconNochangeId)?.addEventListener("change", (e) => {
            if ((e.target as HTMLInputElement).checked) resetIconPicker(d.iconPickerId);
            this.updateBulkState();
        });
        document.getElementById(d.colorNochangeId)?.addEventListener("change", (e) => {
            if ((e.target as HTMLInputElement).checked) resetColorPicker(d.colorPickerId, d.colorValueId);
            this.updateBulkState();
        });
        document.getElementById(`icon-grid-${d.iconPickerId}`)?.addEventListener("click", (e) => {
            if ((e.target as Element).closest(".icon-picker-item")) {
                (document.getElementById(d.iconNochangeId) as HTMLInputElement).checked = false;
                this.updateBulkState();
            }
        });
        if (d.orderValueId && d.orderNochangeId) {
            document.getElementById(d.orderValueId)?.addEventListener("input", () => {
                (document.getElementById(d.orderNochangeId!) as HTMLInputElement).checked = false;
            });
            document.getElementById(d.orderNochangeId)?.addEventListener("change", (e) => {
                if ((e.target as HTMLInputElement).checked) {
                    const el = document.getElementById(d.orderValueId!) as HTMLInputElement | null;
                    if (el) el.value = el.dataset.bulkOriginal ?? "0";
                }
            });
        }
        if (d.descValueId && d.descNochangeId) {
            document.getElementById(d.descValueId)?.addEventListener("input", () => {
                (document.getElementById(d.descNochangeId!) as HTMLInputElement).checked = false;
            });
            document.getElementById(d.descNochangeId)?.addEventListener("change", (e) => {
                if ((e.target as HTMLInputElement).checked) {
                    const el = document.getElementById(d.descValueId!) as HTMLInputElement | null;
                    if (el) el.value = el.dataset.bulkOriginal ?? "";
                }
            });
        }

        document.getElementById(d.confirmId)?.addEventListener("click", async () => {
            const ids = Array.from(this.selected).map((id) => Number.parseInt(id, 10));
            const converting = !!this.convertTarget;
            const btn = document.getElementById(d.confirmId) as HTMLButtonElement;
            const saved = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = `<span class="cat-merge-spinner"></span> ${converting ? "Converting…" : "Saving…"}`;

            const body: Record<string, unknown> = { ids };
            if (d.orderNochangeId && !(document.getElementById(d.orderNochangeId) as HTMLInputElement).checked) {
                body.order = (document.getElementById(d.orderValueId!) as HTMLInputElement | null)?.value ?? "";
            }
            if (d.descNochangeId && !(document.getElementById(d.descNochangeId) as HTMLInputElement).checked) {
                body.description = (document.getElementById(d.descValueId!) as HTMLInputElement | null)?.value ?? "";
            }
            if (!(document.getElementById(d.iconNochangeId) as HTMLInputElement).checked) {
                body.icon = (document.getElementById(`icon-value-${d.iconPickerId}`) as HTMLInputElement | null)?.value ?? "";
            }
            if (!(document.getElementById(d.colorNochangeId) as HTMLInputElement).checked) {
                body.color = (document.getElementById(d.colorValueId) as HTMLInputElement | null)?.value ?? "";
            }
            body.add_parent_ids = BadgeRelPicker.getSelectedIds(`${this.cfg.ns}-bulk`, "parent");
            body.add_child_ids = BadgeRelPicker.getSelectedIds(`${this.cfg.ns}-bulk`, "child");

            try {
                const target = converting ? this.cfg.convertTargets.find((t) => t.kind === this.convertTarget) : undefined;
                const url = converting ? target!.endpoint : this.cfg.endpoints.bulkEdit;
                const html = await this.postForHtml(url, body);
                (document.getElementById(d.dialogId) as HTMLDialogElement).close();
                this.replaceRows(html);
                this.onRowsUpdated();
                if (converting) {
                    toast.success(ids.length === 1 ? `1 ${this.cfg.entitySingular.toLowerCase()} converted.` : `${ids.length} ${this.cfg.entityPluralLower} converted.`);
                    // This request bypassed htmx (plain fetch), so the destination tab's
                    // rows never see the converted items - refresh it explicitly and jump
                    // there, mirroring the single-item edit form's kindChanged handling.
                    if (target?.rowsUrl && target.rowsTarget) {
                        window.htmx?.ajax("GET", target.rowsUrl, { target: target.rowsTarget, swap: "innerHTML" });
                    }
                    if (target?.tabKey) {
                        document.querySelector<HTMLElement>(`.organize-tab[data-tab="${target.tabKey}"]`)?.click();
                    }
                } else {
                    toast.success(`${this.cfg.entityPluralCap} updated.`);
                }
            } catch (err) {
                toast.error(`${converting ? "Convert" : "Edit"} failed: ${(err as Error).message}`);
                btn.disabled = false;
                btn.innerHTML = saved;
            }
        });
    }

    // ── Merge dialog ─────────────────────────────────────────────────────
    private getCardData(id: string): CardData {
        const card = document.querySelector<HTMLElement>(`[data-${this.datasetAttr(this.cfg.idKey)}="${id}"]`);
        if (!card) return { id, name: "?", color: "", icon: "", pinCount: "0" };
        const data: CardData = {
            id,
            name: card.dataset[this.cfg.nameKey] ?? "",
            color: card.dataset[this.cfg.colorKey] ?? "",
            icon: card.dataset[this.cfg.iconKey] ?? "",
            pinCount: card.dataset[this.cfg.pinCountKey] ?? "0",
        };
        if (this.cfg.customIconKey) data.customIcon = card.dataset[this.cfg.customIconKey] ?? "";
        if (this.cfg.locationCountKey) data.locationCount = card.dataset[this.cfg.locationCountKey] ?? "0";
        return data;
    }

    private miniCardHtml(data: CardData, isTarget: boolean, hideSwap: boolean): string {
        const colorStyle = data.color ? `background:${data.color}22;border-color:${data.color}44;` : "";
        const iconColorStyle = data.color ? `color:${data.color}` : "";
        let iconHtml: string;
        if (data.customIcon) {
            iconHtml = `<img src="${escHtml(data.customIcon)}" style="width:24px;height:24px;object-fit:cover;border-radius:4px;" alt="">`;
        } else if (data.icon) {
            iconHtml = MATERIAL_ICON_NAME.test(data.icon)
                ? `<i class="material-icons" style="${iconColorStyle}">${escHtml(data.icon)}</i>`
                : `<span class="tag-icon-emoji">${escHtml(data.icon)}</span>`;
        } else {
            iconHtml = `<i class="material-icons tag-icon-empty">${this.cfg.emptyIcon}</i>`;
        }
        const swapBtn =
            isTarget || hideSwap
                ? ""
                : `<button type="button" class="cat-merge-swap-btn" data-swap-id="${data.id}" title="Make this the surviving ${this.cfg.entitySingular.toLowerCase()}"><i class="material-symbols-outlined">swap_vert</i></button>`;
        const meta = data.locationCount !== undefined ? `${data.pinCount} pins &middot; ${data.locationCount} locations` : `${data.pinCount} pins`;
        return (
            `<div class="cat-merge-mini-card${isTarget ? " cat-merge-mini-card--target" : ""}" data-merge-id="${data.id}">`
            + `<div class="tag-card-icon cat-merge-mini-icon" style="${colorStyle}">${iconHtml}</div>`
            + `<div class="cat-merge-mini-info"><div class="cat-merge-mini-name">${escHtml(data.name)}</div>`
            + `<div class="cat-merge-mini-meta">${meta}</div></div>${swapBtn}</div>`
        );
    }

    private setMergeColorPicker(color: string): void {
        const picker = document.getElementById(`${this.cfg.ns}-merge-color-picker`);
        const input = document.getElementById(`${this.cfg.ns}-merge-edit-color`) as HTMLInputElement | null;
        if (!picker || !input) return;
        picker.querySelectorAll(".color-swatch").forEach((s) => s.classList.remove("selected"));
        input.value = color;
        if (color) picker.querySelector(`[data-color="${color}"]`)?.classList.add("selected");
        else picker.querySelector(".color-clear")?.classList.add("selected");
    }

    private setMergeIconPicker(icon: string): void {
        const pickerId = this.cfg.mergeDialog.editIconId ?? `${this.cfg.ns}-merge-edit`;
        const iconValue = document.getElementById(`icon-value-${pickerId}`) as HTMLInputElement | null;
        const iconCurrent = document.getElementById(`icon-current-${pickerId}`);
        const iconGrid = document.getElementById(`icon-grid-${pickerId}`);
        iconGrid?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
        if (iconValue) iconValue.value = icon;
        if (iconCurrent) iconCurrent.innerHTML = renderIconGlyphHtml(icon);
        if (icon && iconGrid) iconGrid.querySelector(`[data-icon="${icon}"]`)?.classList.add("selected");
        else iconGrid?.querySelector(".icon-picker-none")?.classList.add("selected");
    }

    private renderMergeDialog(): void {
        const d = this.cfg.mergeDialog;
        const ids = Array.from(this.selected);
        const protectedId = this.cfg.isProtected ? ids.find((id) => this.cfg.isProtected!(id)) : undefined;
        if (protectedId) {
            this.mergeTargetId = protectedId;
        } else if (!this.mergeTargetId || !this.selected.has(this.mergeTargetId)) {
            this.mergeTargetId = ids[0] ?? null;
        }
        const targetIsProtected = this.cfg.isProtected?.(this.mergeTargetId ?? "") ?? false;
        const sourceIds = ids.filter((id) => id !== this.mergeTargetId);
        const data = this.getCardData(this.mergeTargetId!);

        const titleEl = document.getElementById(d.titleId);
        if (titleEl) titleEl.textContent = `Merge ${ids.length} ${this.cfg.entityPluralCap}`;
        const targetCard = document.getElementById(d.targetCardId);
        if (targetCard) targetCard.innerHTML = this.miniCardHtml(data, true, false);
        const sourcesList = document.getElementById(d.sourcesListId);
        if (sourcesList) sourcesList.innerHTML = sourceIds.map((id) => this.miniCardHtml(this.getCardData(id), false, targetIsProtected)).join("");

        if (this.cfg.supportsMergeEdit) {
            if (d.swapHintId) {
                const swapHint = document.getElementById(d.swapHintId);
                if (swapHint) swapHint.style.display = targetIsProtected ? "none" : "";
            }
            const nameEl = document.getElementById(d.editNameId ?? "") as HTMLInputElement | null;
            if (nameEl) {
                nameEl.value = data.name;
                nameEl.readOnly = targetIsProtected;
                nameEl.title = targetIsProtected ? "Protected status names cannot be changed" : "";
            }
            this.setMergeIconPicker(data.icon);
            this.setMergeColorPicker(data.color);
        }

        const confirmBtn = document.getElementById(d.confirmId) as HTMLButtonElement;
        confirmBtn.innerHTML = `<i class="material-icons" style="font-size:1rem;vertical-align:middle">merge</i> Merge into ${escHtml(data.name)}`;
        confirmBtn.disabled = false;
    }

    private wireMerge(): void {
        const d = this.cfg.mergeDialog;
        document.getElementById(d.sourcesListId)?.addEventListener("click", (e) => {
            const btn = (e.target as Element).closest<HTMLElement>(".cat-merge-swap-btn");
            if (!btn) return;
            this.mergeTargetId = btn.dataset.swapId ?? null;
            this.renderMergeDialog();
        });

        document.getElementById(d.confirmId)?.addEventListener("click", async () => {
            const ids = Array.from(this.selected);
            const sourceIds = ids.filter((id) => id !== this.mergeTargetId);
            const btn = document.getElementById(d.confirmId) as HTMLButtonElement;
            const saved = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="cat-merge-spinner"></span> Merging…';

            const capturedId = this.mergeTargetId!;
            const origData = this.getCardData(capturedId);
            let editName = "";
            let editIcon = "";
            let editColor = "";
            let hasEdits = false;
            if (this.cfg.supportsMergeEdit) {
                editName = ((document.getElementById(d.editNameId ?? "") as HTMLInputElement | null)?.value ?? "").trim() || origData.name;
                const iconPickerId = d.editIconId ?? `${this.cfg.ns}-merge-edit`;
                editIcon = (document.getElementById(`icon-value-${iconPickerId}`) as HTMLInputElement | null)?.value ?? "";
                editColor = (document.getElementById(`${this.cfg.ns}-merge-edit-color`) as HTMLInputElement | null)?.value ?? "";
                hasEdits = editName !== origData.name || editIcon !== origData.icon || editColor !== origData.color;
            }

            try {
                const mergeHtml = await this.postForHtml(this.cfg.endpoints.multiMerge, {
                    target_id: Number.parseInt(capturedId, 10),
                    source_ids: sourceIds.map((id) => Number.parseInt(id, 10)),
                });
                let html = mergeHtml;
                if (hasEdits && this.cfg.endpoints.mergeEditTemplate) {
                    const fd = new FormData();
                    fd.append("name", editName);
                    fd.append("icon", editIcon);
                    fd.append("color", editColor);
                    const editUrl = this.cfg.endpoints.mergeEditTemplate.replace("99999", capturedId);
                    const editResponse = await fetch(editUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body: fd });
                    if (!editResponse.ok) toast.warning("Merged, but could not save property changes.");
                    else html = await editResponse.text();
                }
                (document.getElementById(d.dialogId) as HTMLDialogElement).close();
                this.replaceRows(html);
                this.mergeTargetId = null;
                this.onRowsUpdated();
                toast.success(`${this.cfg.entityPluralCap} merged successfully.`);
            } catch (err) {
                toast.error(`Merge failed: ${(err as Error).message}`);
                btn.disabled = false;
                btn.innerHTML = saved;
            }
        });
    }

    // ── Shared fetch/DOM helpers ─────────────────────────────────────────
    private async postForHtml(url: string, body: unknown): Promise<string> {
        const response = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify(body),
        });
        if (!response.ok) {
            const text = await response.text();
            throw new Error(text || response.statusText);
        }
        return response.text();
    }

    private replaceRows(html: string): void {
        const rows = this.rows;
        if (!rows) return;
        rows.innerHTML = html;
        window.htmx?.process(rows);
    }

    private datasetAttr(camelKey: string): string {
        return camelKey.replace(/([A-Z])/g, "-$1").toLowerCase();
    }
}
