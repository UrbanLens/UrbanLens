import {
  renderIconGlyphHtml,
  renderTreeView,
  resetColorPicker,
  resetIconPicker
} from "./categories-we5ca0ws.js";
import {
  confirmAction,
  getCsrfToken,
  htmxProcess,
  toast
} from "./categories-agkcnzxy.js";

// src/urbanlens/dashboard/frontend/ts/shared/badge-bulk-manager.ts
function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
var MATERIAL_ICON_NAME = /^[a-z_]+$/;

class BulkEntityManager {
  cfg;
  selected = new Set;
  lastClickedIdx = -1;
  mergeTargetId = null;
  currentView;
  constructor(cfg) {
    this.cfg = cfg;
    this.currentView = localStorage.getItem(cfg.viewStorageKey) ?? "list";
  }
  init() {
    this.wireNewItemForm();
    this.wireViewToggle();
    this.wireSelection();
    this.wireBulkDelete();
    this.wireBulkEdit();
    this.wireMerge();
    this.wireHtmxHooks();
    this.applyCurrentView();
  }
  get rows() {
    return document.getElementById(this.cfg.rowsContainerId.replace(/^#/, ""));
  }
  wireNewItemForm() {
    const { formId, toggleButtonId } = this.cfg.newForm;
    document.getElementById(toggleButtonId)?.addEventListener("click", () => {
      const form = document.getElementById(formId);
      if (!form)
        return;
      if (form.style.display === "none") {
        this.resetNewItemForm();
        form.style.display = "block";
      } else {
        form.style.display = "none";
      }
    });
  }
  resetNewItemForm() {
    const { formId, iconPickerId, colorPickerId, colorValueId } = this.cfg.newForm;
    const container = document.getElementById(formId);
    container?.querySelector("form")?.reset();
    resetIconPicker(iconPickerId);
    resetColorPicker(colorPickerId, colorValueId);
    this.cfg.newForm.onReset?.();
  }
  wireViewToggle() {
    document.querySelectorAll(this.cfg.viewToggleSelector).forEach((btn) => {
      btn.addEventListener("click", () => {
        this.currentView = btn.dataset.view ?? "list";
        localStorage.setItem(this.cfg.viewStorageKey, this.currentView);
        this.applyCurrentView();
      });
    });
  }
  applyCurrentView() {
    const rows = this.rows;
    if (!rows)
      return;
    rows.classList.remove("tag-view--list", "tag-view--gallery", "tag-view--tree");
    rows.classList.add(`tag-view--${this.currentView}`);
    document.querySelectorAll(this.cfg.viewToggleSelector).forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === this.currentView);
    });
    if (this.currentView === "tree") {
      renderTreeView(rows, { cardSelector: this.cfg.cardSelector, ...this.cfg.treeViewConfig });
    } else {
      rows.querySelector(".tag-tree-root")?.remove();
      rows.querySelectorAll(".tag-card").forEach((card) => {
        card.style.display = "";
      });
    }
  }
  getAllIds() {
    const seen = new Set;
    const rows = this.rows;
    if (!rows)
      return [];
    rows.querySelectorAll(`:scope > ${this.cfg.cardSelector}`).forEach((card) => {
      if (!card.querySelector(this.cfg.checkboxSelector))
        return;
      const id = card.dataset[this.cfg.dataset.id];
      if (id)
        seen.add(id);
    });
    return Array.from(seen);
  }
  syncCheckboxes() {
    document.querySelectorAll(this.cfg.checkboxSelector).forEach((cb) => {
      cb.checked = this.selected.has(cb.dataset[this.cfg.checkboxIdKey] ?? "");
    });
    document.querySelectorAll(this.cfg.cardSelector).forEach((card) => {
      const id = card.dataset[this.cfg.dataset.id];
      card.classList.toggle("tag-card--selected", !!id && this.selected.has(id));
    });
  }
  updateSelectionBar() {
    const { barId, countId, deselectId, editId, deleteId, mergeId } = this.cfg.selectionBar;
    const n = this.selected.size;
    const bar = document.getElementById(barId);
    const countEl = document.getElementById(countId);
    const deselectBtn = document.getElementById(deselectId);
    const editBtn = document.getElementById(editId);
    const deleteBtn = document.getElementById(deleteId);
    const mergeBtn = document.getElementById(mergeId);
    if (!bar || !countEl || !deselectBtn || !editBtn || !deleteBtn || !mergeBtn)
      return;
    if (n > 0) {
      bar.classList.add("cat-sel-bar--active");
      countEl.textContent = n === 1 ? "1 selected" : `${n} selected`;
      countEl.hidden = false;
      deselectBtn.disabled = false;
      editBtn.disabled = false;
      deleteBtn.disabled = false;
      mergeBtn.disabled = n < 2;
    } else {
      bar.classList.remove("cat-sel-bar--active");
      countEl.hidden = true;
      deselectBtn.disabled = true;
      editBtn.disabled = true;
      deleteBtn.disabled = true;
      mergeBtn.disabled = true;
    }
  }
  wireSelection() {
    const rows = this.rows;
    if (!rows)
      return;
    rows.addEventListener("click", (e) => {
      const cb = e.target;
      if (!cb.matches?.(this.cfg.checkboxSelector))
        return;
      const allCbs = Array.from(rows.querySelectorAll(this.cfg.checkboxSelector));
      const idx = allCbs.indexOf(cb);
      if (e.shiftKey && this.lastClickedIdx >= 0 && idx >= 0) {
        e.preventDefault();
        const newChecked = !cb.checked;
        const lo = Math.min(idx, this.lastClickedIdx);
        const hi = Math.max(idx, this.lastClickedIdx);
        for (let i = lo;i <= hi; i++) {
          const id = allCbs[i]?.dataset[this.cfg.checkboxIdKey];
          if (!id)
            continue;
          allCbs[i].checked = newChecked;
          if (newChecked)
            this.selected.add(id);
          else
            this.selected.delete(id);
        }
        this.syncCheckboxes();
        this.updateSelectionBar();
      }
      if (idx >= 0)
        this.lastClickedIdx = idx;
    });
    rows.addEventListener("change", (e) => {
      const cb = e.target;
      if (!cb.matches(this.cfg.checkboxSelector))
        return;
      const id = cb.dataset[this.cfg.checkboxIdKey];
      if (!id)
        return;
      if (cb.checked)
        this.selected.add(id);
      else
        this.selected.delete(id);
      this.syncCheckboxes();
      this.updateSelectionBar();
    });
    document.getElementById(this.cfg.selectionBar.selectAllId)?.addEventListener("click", () => {
      this.getAllIds().forEach((id) => this.selected.add(id));
      this.syncCheckboxes();
      this.updateSelectionBar();
    });
    document.getElementById(this.cfg.selectionBar.deselectId)?.addEventListener("click", () => {
      this.selected.clear();
      this.syncCheckboxes();
      this.updateSelectionBar();
    });
  }
  onRowsUpdated = () => {
    this.selected.clear();
    this.updateSelectionBar();
    this.syncCheckboxes();
    this.applyCurrentView();
  };
  wireHtmxHooks() {
    this.rows?.addEventListener("htmx:afterSwap", this.onRowsUpdated);
    document.body.addEventListener("htmx:afterSwap", (e) => {
      const detail = e.detail;
      if (detail.target?.id !== this.cfg.editDialogBodyId)
        return;
      const body = detail.target;
      const titleEl = document.getElementById(this.cfg.editDialogTitleId);
      if (titleEl) {
        const isMerge = !!body.querySelector(`.${this.cfg.mergeFormClass}`);
        titleEl.textContent = isMerge ? `Merge ${this.cfg.labels.entitySingular}` : `Edit ${this.cfg.labels.entitySingular}`;
      }
      const dialog = document.getElementById(this.cfg.editDialogId);
      if (dialog && !dialog.open)
        dialog.showModal();
    });
  }
  wireBulkDelete() {
    document.getElementById(this.cfg.selectionBar.deleteId)?.addEventListener("click", async () => {
      const n = this.selected.size;
      if (n === 0)
        return;
      const { entitySingular, entityPlural, deleteExtraWarning } = this.cfg.labels;
      const msg = (n === 1 ? `Delete 1 ${entitySingular.toLowerCase()}?` : `Delete ${n} ${entityPlural.toLowerCase()}?`) + `
${deleteExtraWarning}`;
      if (!await confirmAction({ title: `Delete ${entityPlural}`, message: msg, confirmLabel: "Delete" }))
        return;
      const ids = Array.from(this.selected).map((id) => Number.parseInt(id, 10));
      try {
        const html = await this.postForHtml(this.cfg.endpoints.bulkDelete, { ids });
        this.replaceRows(html);
        this.onRowsUpdated();
        toast.success(n === 1 ? `1 ${entitySingular.toLowerCase()} deleted.` : `${n} ${entityPlural.toLowerCase()} deleted.`);
      } catch (err) {
        toast.error(`Delete failed: ${err.message}`);
      }
    });
  }
  updateBulkPickerState() {
    const { iconNochangeId, colorNochangeId, iconWrapId, colorPickerId } = this.cfg.bulkEditDialog;
    const iconNochange = document.getElementById(iconNochangeId);
    const colorNochange = document.getElementById(colorNochangeId);
    document.getElementById(iconWrapId)?.classList.toggle("cat-bulk-picker--disabled", !!iconNochange?.checked);
    document.getElementById(colorPickerId)?.classList.toggle("cat-bulk-picker--disabled", !!colorNochange?.checked);
  }
  openBulkEditDialog() {
    const d = this.cfg.bulkEditDialog;
    const ids = Array.from(this.selected);
    const iconSet = new Set;
    const colorSet = new Set;
    ids.forEach((id) => {
      const card = document.querySelector(`[data-${this.datasetAttr(this.cfg.dataset.id)}="${id}"]`);
      if (!card)
        return;
      iconSet.add(card.dataset[this.cfg.dataset.icon] ?? "");
      colorSet.add(card.dataset[this.cfg.dataset.color] ?? "");
    });
    const sharedIcon = iconSet.size === 1 ? Array.from(iconSet)[0] : null;
    const sharedColor = colorSet.size === 1 ? Array.from(colorSet)[0] : null;
    const iconNochange = document.getElementById(d.iconNochangeId);
    const iconValue = document.getElementById(`icon-value-${d.iconPickerId}`);
    const iconCurrent = document.getElementById(`icon-current-${d.iconPickerId}`);
    const iconGrid = document.getElementById(`icon-grid-${d.iconPickerId}`);
    iconGrid?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    if (sharedIcon !== null) {
      iconNochange.checked = false;
      if (iconValue)
        iconValue.value = sharedIcon;
      if (iconCurrent)
        iconCurrent.innerHTML = renderIconGlyphHtml(sharedIcon);
      if (sharedIcon && iconGrid)
        iconGrid.querySelector(`[data-icon="${sharedIcon}"]`)?.classList.add("selected");
      else
        iconGrid?.querySelector(".icon-picker-none")?.classList.add("selected");
    } else {
      iconNochange.checked = true;
      if (iconValue)
        iconValue.value = "";
      if (iconCurrent)
        iconCurrent.innerHTML = '<span class="icon-picker-none-label">No icon</span>';
      iconGrid?.querySelector(".icon-picker-none")?.classList.add("selected");
    }
    const colorNochange = document.getElementById(d.colorNochangeId);
    const colorPickerEl = document.getElementById(d.colorPickerId);
    const colorValue = document.getElementById(d.colorValueId);
    colorPickerEl?.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
    if (sharedColor !== null) {
      colorNochange.checked = false;
      if (colorValue)
        colorValue.value = sharedColor;
      if (sharedColor)
        colorPickerEl?.querySelector(`[data-color="${sharedColor}"]`)?.classList.add("selected");
    } else {
      colorNochange.checked = true;
      if (colorValue)
        colorValue.value = "";
    }
    document.querySelectorAll(`#${d.parentSelectId} .${d.parentCheckboxClass}`).forEach((cb) => {
      cb.checked = false;
    });
    const titleEl = document.getElementById(d.titleId);
    if (titleEl) {
      titleEl.textContent = `Edit ${ids.length} ${ids.length === 1 ? this.cfg.labels.entitySingular : this.cfg.labels.entityPlural}`;
    }
    const confirmBtn = document.getElementById(d.confirmId);
    confirmBtn.disabled = false;
    confirmBtn.innerHTML = '<i class="material-icons" style="font-size:1rem;vertical-align:middle">edit</i> Apply Changes';
    this.updateBulkPickerState();
    document.getElementById(d.dialogId).showModal();
  }
  wireBulkEdit() {
    const d = this.cfg.bulkEditDialog;
    document.getElementById(this.cfg.selectionBar.editId)?.addEventListener("click", () => {
      if (this.selected.size === 0)
        return;
      this.openBulkEditDialog();
    });
    document.getElementById(d.iconNochangeId)?.addEventListener("change", (e) => {
      if (e.target.checked) {
        resetIconPicker(d.iconPickerId);
      }
      this.updateBulkPickerState();
    });
    document.getElementById(d.colorNochangeId)?.addEventListener("change", (e) => {
      if (e.target.checked) {
        resetColorPicker(d.colorPickerId, d.colorValueId);
      }
      this.updateBulkPickerState();
    });
    document.getElementById(`icon-grid-${d.iconPickerId}`)?.addEventListener("click", (e) => {
      if (e.target.closest(".icon-picker-item")) {
        document.getElementById(d.iconNochangeId).checked = false;
        this.updateBulkPickerState();
      }
    });
    document.getElementById(d.confirmId)?.addEventListener("click", async () => {
      const ids = Array.from(this.selected).map((id) => Number.parseInt(id, 10));
      const body = { ids };
      if (!document.getElementById(d.iconNochangeId).checked) {
        body.icon = document.getElementById(`icon-value-${d.iconPickerId}`)?.value ?? "";
      }
      if (!document.getElementById(d.colorNochangeId).checked) {
        body.color = document.getElementById(d.colorValueId)?.value ?? "";
      }
      body.add_parent_ids = Array.from(document.querySelectorAll(`#${d.parentSelectId} .${d.parentCheckboxClass}:checked`)).map((cb) => Number.parseInt(cb.value, 10));
      const btn = document.getElementById(d.confirmId);
      const savedHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="cat-merge-spinner"></span> Saving…';
      try {
        const html = await this.postForHtml(this.cfg.endpoints.bulkEdit, body);
        document.getElementById(d.dialogId).close();
        this.replaceRows(html);
        this.onRowsUpdated();
        toast.success(`${this.cfg.labels.entityPlural} updated.`);
      } catch (err) {
        toast.error(`Edit failed: ${err.message}`);
        btn.disabled = false;
        btn.innerHTML = savedHtml;
      }
    });
  }
  getCardData(id) {
    const card = document.querySelector(`[data-${this.datasetAttr(this.cfg.dataset.id)}="${id}"]`);
    if (!card)
      return { id, name: "?", color: "", icon: "", pinCount: "0" };
    const data = {
      id,
      name: card.dataset[this.cfg.dataset.name] ?? "",
      color: card.dataset[this.cfg.dataset.color] ?? "",
      icon: card.dataset[this.cfg.dataset.icon] ?? "",
      pinCount: card.dataset[this.cfg.dataset.pinCount] ?? "0"
    };
    if (this.cfg.dataset.locationCount)
      data.locationCount = card.dataset[this.cfg.dataset.locationCount] ?? "0";
    if (this.cfg.dataset.customIcon)
      data.customIcon = card.dataset[this.cfg.dataset.customIcon] ?? "";
    return data;
  }
  miniCardHtml(data, isTarget) {
    const colorStyle = data.color ? `background:${data.color}22;border-color:${data.color}44;` : "";
    const iconColorStyle = data.color ? `color:${data.color}` : "";
    let iconHtml;
    if (data.customIcon) {
      iconHtml = `<img src="${escHtml(data.customIcon)}" style="width:24px;height:24px;object-fit:cover;border-radius:4px;" alt="">`;
    } else if (data.icon) {
      iconHtml = MATERIAL_ICON_NAME.test(data.icon) ? `<i class="material-icons" style="${iconColorStyle}">${escHtml(data.icon)}</i>` : `<span class="tag-icon-emoji">${escHtml(data.icon)}</span>`;
    } else {
      iconHtml = `<i class="material-icons tag-icon-empty">${this.cfg.labels.emptyIcon}</i>`;
    }
    const meta = this.cfg.dataset.locationCount ? `${data.pinCount} pins &middot; ${data.locationCount} locations` : `${data.pinCount} pins`;
    const swapBtn = isTarget ? "" : `<button type="button" class="cat-merge-swap-btn" data-swap-id="${data.id}" title="Make this the surviving ${this.cfg.labels.entitySingular.toLowerCase()}">` + '<i class="material-icons">swap_vert</i></button>';
    return `<div class="cat-merge-mini-card${isTarget ? " cat-merge-mini-card--target" : ""}" data-merge-id="${data.id}">` + `<div class="tag-card-icon cat-merge-mini-icon" style="${colorStyle}">${iconHtml}</div>` + `<div class="cat-merge-mini-info"><div class="cat-merge-mini-name">${escHtml(data.name)}</div>` + `<div class="cat-merge-mini-meta">${meta}</div></div>${swapBtn}</div>`;
  }
  renderMergeDialog() {
    const d = this.cfg.mergeDialog;
    const ids = Array.from(this.selected);
    if (!this.mergeTargetId || !this.selected.has(this.mergeTargetId)) {
      this.mergeTargetId = ids[0] ?? null;
    }
    const sourceIds = ids.filter((id) => id !== this.mergeTargetId);
    const targetData = this.getCardData(this.mergeTargetId);
    const titleEl = document.getElementById(d.titleId);
    if (titleEl)
      titleEl.textContent = `Merge ${ids.length} ${this.cfg.labels.entityPlural}`;
    const targetCard = document.getElementById(d.targetCardId);
    if (targetCard)
      targetCard.innerHTML = this.miniCardHtml(targetData, true);
    const sourcesList = document.getElementById(d.sourcesListId);
    if (sourcesList)
      sourcesList.innerHTML = sourceIds.map((id) => this.miniCardHtml(this.getCardData(id), false)).join("");
    const confirmBtn = document.getElementById(d.confirmId);
    confirmBtn.innerHTML = `<i class="material-icons" style="font-size:1rem;vertical-align:middle">merge</i> Merge into ${escHtml(targetData.name)}`;
    confirmBtn.disabled = false;
  }
  wireMerge() {
    const d = this.cfg.mergeDialog;
    document.getElementById(this.cfg.selectionBar.mergeId)?.addEventListener("click", () => {
      if (this.selected.size < 2)
        return;
      this.mergeTargetId = Array.from(this.selected)[0];
      this.renderMergeDialog();
      document.getElementById(d.dialogId).showModal();
    });
    document.getElementById(d.sourcesListId)?.addEventListener("click", (e) => {
      const btn = e.target.closest(".cat-merge-swap-btn");
      if (!btn)
        return;
      this.mergeTargetId = btn.dataset.swapId ?? null;
      this.renderMergeDialog();
    });
    document.getElementById(d.confirmId)?.addEventListener("click", async () => {
      const ids = Array.from(this.selected);
      const sourceIds = ids.filter((id) => id !== this.mergeTargetId);
      const btn = document.getElementById(d.confirmId);
      const savedHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="cat-merge-spinner"></span> Merging…';
      try {
        const html = await this.postForHtml(this.cfg.endpoints.multiMerge, {
          target_id: Number.parseInt(this.mergeTargetId, 10),
          source_ids: sourceIds.map((id) => Number.parseInt(id, 10))
        });
        document.getElementById(d.dialogId).close();
        this.replaceRows(html);
        this.mergeTargetId = null;
        this.onRowsUpdated();
        toast.success(`${this.cfg.labels.entityPlural} merged successfully.`);
      } catch (err) {
        toast.error(`Merge failed: ${err.message}`);
        btn.disabled = false;
        btn.innerHTML = savedHtml;
      }
    });
  }
  async postForHtml(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || response.statusText);
    }
    return response.text();
  }
  replaceRows(html) {
    const rows = this.rows;
    if (!rows)
      return;
    rows.innerHTML = html;
    htmxProcess(rows);
  }
  datasetAttr(camelKey) {
    return camelKey.replace(/([A-Z])/g, "-$1").toLowerCase();
  }
}

// src/urbanlens/dashboard/frontend/ts/shared/parent-search.ts
function filterParentOptions(input, containerId) {
  const q = input.value.toLowerCase().trim();
  const container = document.getElementById(containerId);
  if (!container)
    return;
  container.querySelectorAll(".tag-parent-option").forEach((opt) => {
    opt.style.display = !q || (opt.textContent ?? "").toLowerCase().includes(q) ? "" : "none";
  });
}
function installGlobalParentSearch() {
  window.filterParentOptions = filterParentOptions;
}

export { BulkEntityManager, installGlobalParentSearch };
