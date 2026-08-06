import {
  installGlobalColorPicker,
  installGlobalOrganizeIconPicker,
  registerBulkStateUpdater,
  resetColorPicker
} from "./photo-location-scan-mrczscvs.js";
import {
  sortable_esm_default
} from "./photo-location-scan-351ta66q.js";
import {
  renderIconGlyphHtml,
  resetIconPicker
} from "./photo-location-scan-ee3vhq76.js";
import {
  confirmAction,
  getCsrfToken,
  htmxProcess,
  toast
} from "./photo-location-scan-5jnnp4sj.js";
import"./photo-location-scan-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/shared/label-rel-picker.ts
var LabelRelPicker = {
  toggle(instanceId, relType, _triggerBtn) {
    const popup = document.getElementById(`${instanceId}-popup-${relType}`);
    if (!popup)
      return;
    const wasHidden = popup.hidden;
    document.querySelectorAll(".label-rel-popup").forEach((p) => {
      p.hidden = true;
    });
    if (!wasHidden)
      return;
    popup.hidden = false;
    const search = popup.querySelector(".label-rel-search");
    if (search) {
      search.value = "";
      search.focus();
    }
  },
  select(instanceId, relType, btn) {
    if (btn.classList.contains("label-rel-suggestion--hidden"))
      return;
    const group = document.getElementById(`${instanceId}-sel-${relType}`);
    if (!group)
      return;
    const id = btn.dataset.id;
    if (!id || group.querySelector(`.label-rel-chip[data-id="${id}"]`))
      return;
    const picker = document.querySelector(`[data-picker-id="${instanceId}"]`);
    const pill = document.createElement("span");
    pill.className = "tag-chip";
    const color = btn.style.getPropertyValue("--tag-color");
    if (color)
      pill.style.setProperty("--tag-color", color);
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
    removeBtn.onclick = () => LabelRelPicker.remove(instanceId, chip);
    chip.appendChild(removeBtn);
    group.appendChild(chip);
    LabelRelPicker._hideSuggestion(instanceId, id);
    LabelRelPicker._updateEmptyHints(instanceId);
  },
  remove(instanceId, chipEl) {
    if (!chipEl)
      return;
    const id = chipEl.dataset.id;
    chipEl.remove();
    if (id)
      LabelRelPicker._showSuggestion(instanceId, id);
    LabelRelPicker._updateEmptyHints(instanceId);
  },
  _hideSuggestion(instanceId, id) {
    ["parent", "child"].forEach((relType) => {
      const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
      container?.querySelector(`.label-rel-suggestion[data-id="${id}"]`)?.classList.add("label-rel-suggestion--hidden");
    });
  },
  _showSuggestion(instanceId, id) {
    ["parent", "child"].forEach((relType) => {
      const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
      const btn = container?.querySelector(`.label-rel-suggestion[data-id="${id}"]`);
      if (btn) {
        btn.classList.remove("label-rel-suggestion--hidden");
        LabelRelPicker._applyFilters(instanceId, relType);
      }
    });
  },
  setTab(instanceId, relType, kind, btn) {
    const popup = document.getElementById(`${instanceId}-popup-${relType}`);
    if (!popup)
      return;
    popup.querySelectorAll(".label-rel-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
    if (container)
      container.dataset.activeTab = kind;
    LabelRelPicker._applyFilters(instanceId, relType);
  },
  filter(instanceId, relType, query) {
    const popup = document.getElementById(`${instanceId}-popup-${relType}`);
    if (popup)
      popup.dataset.searchQuery = query.toLowerCase().trim();
    LabelRelPicker._applyFilters(instanceId, relType);
  },
  _applyFilters(instanceId, relType) {
    const popup = document.getElementById(`${instanceId}-popup-${relType}`);
    const container = document.getElementById(`${instanceId}-suggestions-${relType}`);
    if (!popup || !container)
      return;
    const q = popup.dataset.searchQuery ?? "";
    const tab = container.dataset.activeTab ?? "";
    container.querySelectorAll(".label-rel-suggestion").forEach((btn) => {
      const matchesTab = !tab || btn.dataset.kind === tab;
      const matchesSearch = !q || (btn.dataset.name ?? "").indexOf(q) !== -1;
      btn.style.display = matchesTab && matchesSearch ? "" : "none";
    });
  },
  _updateEmptyHints(instanceId) {
    ["parent", "child"].forEach((relType) => {
      const group = document.getElementById(`${instanceId}-sel-${relType}`);
      const hint = group?.parentElement?.querySelector(".label-rel-empty-hint");
      if (hint)
        hint.hidden = (group?.children.length ?? 0) > 0;
    });
  },
  getSelectedIds(instanceId, relType) {
    const group = document.getElementById(`${instanceId}-sel-${relType}`);
    if (!group)
      return [];
    return Array.from(group.querySelectorAll(".label-rel-chip")).map((c) => Number.parseInt(c.dataset.id ?? "0", 10));
  },
  reset(instanceId) {
    ["parent", "child"].forEach((relType) => {
      const group = document.getElementById(`${instanceId}-sel-${relType}`);
      if (!group)
        return;
      Array.from(group.querySelectorAll(".label-rel-chip")).forEach((chip) => LabelRelPicker.remove(instanceId, chip));
    });
  },
  _makeSortable(instanceId) {
    const groupName = `${instanceId}-rel`;
    const parentList = document.getElementById(`${instanceId}-sel-parent`);
    const childList = document.getElementById(`${instanceId}-sel-child`);
    const trash = document.getElementById(`${instanceId}-trash`);
    if (!parentList || !childList)
      return;
    const showTrash = () => trash?.classList.add("is-active");
    const hideTrash = () => trash?.classList.remove("is-active");
    const onEnd = () => {
      hideTrash();
      LabelRelPicker._updateEmptyHints(instanceId);
    };
    const makeOnAdd = (relType) => (evt) => {
      const hidden = evt.item.querySelector('input[type="hidden"]');
      if (hidden)
        hidden.name = `${relType}_ids`;
    };
    new sortable_esm_default(parentList, {
      group: groupName,
      animation: 150,
      filter: ".tag-chip-remove",
      preventOnFilter: false,
      onStart: showTrash,
      onEnd,
      onAdd: makeOnAdd("parent")
    });
    new sortable_esm_default(childList, {
      group: groupName,
      animation: 150,
      filter: ".tag-chip-remove",
      preventOnFilter: false,
      onStart: showTrash,
      onEnd,
      onAdd: makeOnAdd("child")
    });
    if (trash) {
      new sortable_esm_default(trash, {
        group: { name: groupName, put: true, pull: false },
        animation: 150,
        onAdd: (evt) => LabelRelPicker.remove(instanceId, evt.item)
      });
    }
  },
  _initAll(root) {
    (root ?? document).querySelectorAll(".label-rel-picker").forEach((picker) => {
      if (picker.dataset.relInit === "1")
        return;
      picker.dataset.relInit = "1";
      if (picker.dataset.pickerId)
        LabelRelPicker._makeSortable(picker.dataset.pickerId);
    });
  }
};
function installGlobalLabelRelPicker() {
  window.LabelRelPicker = LabelRelPicker;
  LabelRelPicker._initAll();
  document.body.addEventListener("htmx:afterSettle", () => LabelRelPicker._initAll());
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".label-rel-add-dropdown")) {
      document.querySelectorAll(".label-rel-popup").forEach((p) => {
        p.hidden = true;
      });
    }
  });
}

// src/urbanlens/dashboard/frontend/ts/shared/organize-filter-engine.ts
var ORG_FILTER_NAMESPACES = ["tag", "cat", "status", "people"];
var NS_LABELS = { tag: "tags", cat: "categories", status: "statuses", people: "people" };
var NS_CONFIG = {
  tag: { rowsId: "tag-rows", cardSel: ".tag-card[data-tag-id]", idKey: "tagId", nameKey: "tagName", iconKey: "tagIcon", customIconKey: "tagCustomIcon", colorKey: "tagColor", parentsKey: "tagParents" },
  cat: { rowsId: "category-rows", cardSel: ".tag-card[data-category-id]", idKey: "categoryId", nameKey: "categoryName", iconKey: "categoryIcon", customIconKey: "categoryCustomIcon", colorKey: "categoryColor", parentsKey: "categoryParents" },
  status: { rowsId: "status-rows", cardSel: ".tag-card[data-status-id]", idKey: "statusId", nameKey: "statusName", iconKey: "statusIcon", customIconKey: "statusCustomIcon", colorKey: "statusColor", parentsKey: "statusParents" },
  people: { rowsId: "people-label-rows", cardSel: ".tag-card[data-people-id]", idKey: "peopleId", nameKey: "peopleName", iconKey: "peopleIcon", colorKey: "peopleColor", parentsKey: "peopleParents" }
};
function loadSharedFilter() {
  const params = new URLSearchParams(window.location.search);
  return {
    search: params.get("filter_search") ?? "",
    chips: new Set((params.get("filter_chips") ?? "").split(",").filter(Boolean)),
    color: (params.get("filter_color") ?? "").toLowerCase()
  };
}
var sharedFilter = loadSharedFilter();
var filterBarOpen = false;
function saveSharedFilter() {
  const params = new URLSearchParams(window.location.search);
  if (sharedFilter.search)
    params.set("filter_search", sharedFilter.search);
  else
    params.delete("filter_search");
  const chips = Array.from(sharedFilter.chips);
  if (chips.length > 0)
    params.set("filter_chips", chips.join(","));
  else
    params.delete("filter_chips");
  if (sharedFilter.color)
    params.set("filter_color", sharedFilter.color);
  else
    params.delete("filter_color");
  const newUrl = window.location.pathname + (params.toString() ? `?${params.toString()}` : "");
  window.history.replaceState({}, "", newUrl);
}
function captureFilterFromBar(ns) {
  const bar = document.getElementById(`${ns}-filter-bar`);
  const si = document.getElementById(`${ns}-filter-search`);
  sharedFilter.search = si ? si.value : "";
  sharedFilter.chips = new Set;
  sharedFilter.color = "";
  if (bar) {
    bar.querySelectorAll(".org-filter-chip.active").forEach((c) => {
      if (c.dataset.filter)
        sharedFilter.chips.add(c.dataset.filter);
    });
    const colorDot = bar.querySelector(".org-filter-color-dot.active");
    if (colorDot)
      sharedFilter.color = (colorDot.dataset.filter ?? "").replace("color:", "").toLowerCase();
  }
  saveSharedFilter();
}
function syncOrgFilterUI() {
  ORG_FILTER_NAMESPACES.forEach((ns) => {
    const bar = document.getElementById(`${ns}-filter-bar`);
    const si = document.getElementById(`${ns}-filter-search`);
    if (si)
      si.value = sharedFilter.search;
    bar?.querySelectorAll(".org-filter-chip, .org-filter-color-dot").forEach((el) => {
      const f = el.dataset.filter;
      if (!f)
        return;
      if (f.startsWith("color:"))
        el.classList.toggle("active", f.replace("color:", "").toLowerCase() === sharedFilter.color);
      else
        el.classList.toggle("active", sharedFilter.chips.has(f));
    });
  });
}
function updateFilterBtn() {
  const filterBtn = document.getElementById("org-header-filter-btn");
  if (!filterBtn)
    return;
  const hasActive = sharedFilter.search.trim() || sharedFilter.chips.size > 0 || sharedFilter.color;
  filterBtn.classList.toggle("has-filter", !!hasActive);
}
function syncFilterBtnActive() {
  document.getElementById("org-header-filter-btn")?.classList.toggle("btn--active", !!document.querySelector(".org-filter-bar.open"));
}
function getOrgVisibleCards(rows, cardSel) {
  if (!rows)
    return [];
  const inTreeView = rows.classList.contains("tag-view--tree");
  const cards = Array.from(rows.querySelectorAll(inTreeView ? `.tag-tree-root ${cardSel}` : cardSel));
  return cards.filter((c) => {
    if (inTreeView) {
      const treeItem = c.closest(".tag-tree-item");
      return !treeItem || treeItem.style.display !== "none";
    }
    return c.style.display !== "none";
  });
}
function applyFilterForNs(ns) {
  const cfg = NS_CONFIG[ns];
  const rows = document.getElementById(cfg.rowsId);
  if (!rows)
    return;
  const search = sharedFilter.search.toLowerCase().trim();
  const activeChips = sharedFilter.chips;
  const activeColor = sharedFilter.color;
  const inTreeView = rows.classList.contains("tag-view--tree");
  const allCards = Array.from(rows.querySelectorAll(inTreeView ? `.tag-tree-root ${cfg.cardSel}` : cfg.cardSel));
  const hasChildrenSet = new Set;
  if (activeChips.has("has-children")) {
    const childSourceCards = inTreeView ? Array.from(rows.querySelectorAll(cfg.cardSel)) : allCards;
    childSourceCards.forEach((c) => {
      (c.dataset[cfg.parentsKey] ?? "").split(",").map((s) => s.trim()).filter(Boolean).forEach((pid) => hasChildrenSet.add(pid));
    });
  }
  allCards.forEach((card) => {
    const idVal = card.dataset[cfg.idKey];
    const name = (card.dataset[cfg.nameKey] ?? "").toLowerCase();
    const icon = card.dataset[cfg.iconKey] ?? "";
    const customIcon = cfg.customIconKey ? card.dataset[cfg.customIconKey] ?? "" : "";
    const anyIcon = icon || customIcon;
    const color = (card.dataset[cfg.colorKey] ?? "").toLowerCase();
    const parents = card.dataset[cfg.parentsKey] ?? "";
    const hasParents = parents.split(",").some((p) => p.trim() !== "");
    let show = true;
    if (search && !name.includes(search))
      show = false;
    if (activeChips.has("has-icon") && !anyIcon)
      show = false;
    if (activeChips.has("no-icon") && anyIcon)
      show = false;
    if (activeChips.has("has-color") && !color)
      show = false;
    if (activeChips.has("no-color") && color)
      show = false;
    if (activeChips.has("has-children") && !hasChildrenSet.has(String(idVal)))
      show = false;
    if (activeChips.has("has-parents") && !hasParents)
      show = false;
    if (activeColor && color !== activeColor)
      show = false;
    if (inTreeView) {
      const treeItem = card.closest(".tag-tree-item");
      (treeItem ?? card).style.display = show ? "" : "none";
    } else {
      card.style.display = show ? "" : "none";
    }
  });
  document.dispatchEvent(new CustomEvent("org:filter-applied", { detail: { ns } }));
}
function applyOrgFilter(ns) {
  applyAllOrgFilters(ns);
}
function applyAllOrgFilters(triggerNs) {
  if (triggerNs) {
    const si = document.getElementById(`${triggerNs}-filter-search`);
    if (si) {
      sharedFilter.search = si.value;
      saveSharedFilter();
      syncOrgFilterUI();
    }
  }
  ORG_FILTER_NAMESPACES.forEach((ns) => applyFilterForNs(ns));
  updateFilterBtn();
}
function hasAnyOrgFilter() {
  return !!(sharedFilter.search || sharedFilter.chips.size > 0 || sharedFilter.color);
}
function countVisibleCards(ns) {
  const cfg = NS_CONFIG[ns];
  const rows = document.getElementById(cfg.rowsId);
  if (!rows)
    return 0;
  const inTreeView = rows.classList.contains("tag-view--tree");
  const scope = inTreeView ? `.tag-tree-root ${cfg.cardSel}` : cfg.cardSel;
  return Array.from(rows.querySelectorAll(scope)).filter((c) => c.style.display !== "none").length;
}
function updateCrossTabCounts() {
  if (!hasAnyOrgFilter()) {
    ORG_FILTER_NAMESPACES.forEach((ns) => {
      const countEl = document.getElementById(`org-tab-count-${ns}`);
      if (countEl)
        countEl.hidden = true;
      const footer = document.getElementById(`org-cross-tab-${ns}`);
      if (footer)
        footer.hidden = true;
    });
    return;
  }
  const counts = { tag: 0, cat: 0, status: 0, people: 0 };
  ORG_FILTER_NAMESPACES.forEach((ns) => {
    counts[ns] = countVisibleCards(ns);
  });
  const activeTabEl = document.querySelector(".organize-tab.active[data-filter-ns]");
  const activeNs = activeTabEl?.dataset.filterNs ?? null;
  ORG_FILTER_NAMESPACES.forEach((ns) => {
    const countEl = document.getElementById(`org-tab-count-${ns}`);
    if (!countEl)
      return;
    if (ns === activeNs) {
      countEl.hidden = true;
    } else {
      countEl.textContent = String(counts[ns]);
      countEl.hidden = false;
    }
  });
  ORG_FILTER_NAMESPACES.forEach((ns) => {
    const footer = document.getElementById(`org-cross-tab-${ns}`);
    if (!footer)
      return;
    if (ns !== activeNs) {
      footer.hidden = true;
      return;
    }
    const otherParts = ORG_FILTER_NAMESPACES.filter((otherNs) => otherNs !== ns && counts[otherNs] > 0).map((otherNs) => ({
      ns: otherNs,
      n: counts[otherNs],
      label: NS_LABELS[otherNs]
    }));
    if (otherParts.length === 0) {
      footer.hidden = true;
      return;
    }
    const selfCount = counts[ns];
    const prefix = selfCount === 0 ? `No ${NS_LABELS[ns]} match, but ` : "";
    const parts = otherParts.map((p) => {
      const tabKey = p.ns === "cat" ? "categories" : p.ns === "tag" ? "tags" : p.ns;
      const tabBtn = document.querySelector(`.organize-tab[data-tab="${tabKey}"]`);
      return tabBtn ? `<button class="org-cross-tab-link" type="button" data-org-tab="${tabKey}">${p.n} ${p.label}</button>` : `${p.n} ${p.label}`;
    });
    let partsHtml;
    if (parts.length === 1)
      partsHtml = parts[0];
    else if (parts.length === 2)
      partsHtml = `${parts[0]} and ${parts[1]}`;
    else
      partsHtml = `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
    footer.innerHTML = `<i class="material-symbols-outlined">info</i><span>${prefix}${partsHtml} also match this search.</span>`;
    footer.hidden = false;
  });
}
function syncOrgFilterBarVisibility(activeNs) {
  ORG_FILTER_NAMESPACES.forEach((ns) => document.getElementById(`${ns}-filter-bar`)?.classList.remove("open"));
  if (activeNs && (filterBarOpen || hasAnyOrgFilter())) {
    const activeBar = document.getElementById(`${activeNs}-filter-bar`);
    if (activeBar) {
      activeBar.classList.add("open");
      filterBarOpen = true;
    }
  }
  syncFilterBtnActive();
}
function toggleOrgFilter(ns) {
  const bar = document.getElementById(`${ns}-filter-bar`);
  if (!bar)
    return;
  const willOpen = !bar.classList.contains("open");
  ORG_FILTER_NAMESPACES.forEach((otherNs) => document.getElementById(`${otherNs}-filter-bar`)?.classList.remove("open"));
  if (willOpen) {
    bar.classList.add("open");
    filterBarOpen = true;
  } else {
    filterBarOpen = false;
    clearOrgFilter(ns);
  }
  syncFilterBtnActive();
}
function toggleOrgChip(btn, ns) {
  btn.classList.toggle("active");
  const mutex = btn.dataset.mutex;
  if (mutex && btn.classList.contains("active")) {
    document.getElementById(`${ns}-filter-bar`)?.querySelectorAll(`[data-filter="${mutex}"]`).forEach((m) => m.classList.remove("active"));
  }
  if (btn.dataset.filter?.startsWith("color:") && btn.classList.contains("active")) {
    document.getElementById(`${ns}-filter-bar`)?.querySelectorAll(".org-filter-color-dot.active").forEach((d) => {
      if (d !== btn)
        d.classList.remove("active");
    });
  }
  captureFilterFromBar(ns);
  syncOrgFilterUI();
  applyAllOrgFilters();
}
function clearOrgFilter(_ns) {
  sharedFilter.search = "";
  sharedFilter.chips = new Set;
  sharedFilter.color = "";
  saveSharedFilter();
  syncOrgFilterUI();
  applyAllOrgFilters();
}
function installOrgFilterEngine() {
  let crossTabPendingId = null;
  document.addEventListener("org:filter-applied", () => {
    if (crossTabPendingId)
      clearTimeout(crossTabPendingId);
    crossTabPendingId = setTimeout(updateCrossTabCounts, 0);
  });
  document.addEventListener("org:tab-changed", () => {
    if (hasAnyOrgFilter())
      updateCrossTabCounts();
  });
  document.addEventListener("click", (e) => {
    const target = e.target;
    const crossTabLink = target.closest(".org-cross-tab-link[data-org-tab]");
    if (crossTabLink) {
      document.querySelector(`.organize-tab[data-tab="${crossTabLink.dataset.orgTab}"]`)?.click();
      return;
    }
    const chip = target.closest(".org-filter-chip, .org-filter-color-dot");
    if (chip && !chip.classList.contains("org-filter-clear") && !chip.classList.contains("org-filter-close")) {
      const bar = chip.closest(".org-filter-bar");
      const ns = bar?.dataset.filterNs;
      if (ns)
        toggleOrgChip(chip, ns);
      return;
    }
    const clearBtn = target.closest(".org-filter-clear");
    if (clearBtn) {
      const bar = clearBtn.closest(".org-filter-bar");
      const ns = bar?.dataset.filterNs;
      if (ns)
        clearOrgFilter(ns);
      return;
    }
    const closeBtn = target.closest(".org-filter-close");
    if (closeBtn) {
      const bar = closeBtn.closest(".org-filter-bar");
      const ns = bar?.dataset.filterNs;
      if (ns)
        toggleOrgFilter(ns);
    }
  });
  document.addEventListener("input", (e) => {
    const target = e.target;
    if (!target.classList.contains("org-filter-search"))
      return;
    const bar = target.closest(".org-filter-bar");
    const ns = bar?.dataset.filterNs;
    if (ns)
      applyOrgFilter(ns);
  });
}

// src/urbanlens/dashboard/frontend/ts/shared/organize-header.ts
var TAB_FILTER_NS = { categories: "cat", tags: "tag", status: "status", people: "people" };

class OrganizeHeader {
  tabs = new Map;
  activeTab;
  sharedView;
  actionsEl = null;
  headerActionsEl = null;
  filterBtn = null;
  createBtn = null;
  viewToggle = null;
  wired = false;
  constructor(initialTab) {
    this.activeTab = initialTab;
    this.sharedView = this.loadSharedView();
  }
  loadSharedView() {
    return localStorage.getItem("organize_view") ?? localStorage.getItem("tag_view") ?? localStorage.getItem("category_view") ?? localStorage.getItem("status_view") ?? localStorage.getItem("people_view") ?? "list";
  }
  register(tabKey, cfg) {
    this.tabs.set(tabKey, cfg);
  }
  getFilterNs() {
    return TAB_FILTER_NS[this.activeTab] ?? null;
  }
  getSharedView() {
    return this.sharedView;
  }
  setSharedView(view) {
    this.sharedView = view;
    localStorage.setItem("organize_view", view);
    this.syncViewButtons(view);
    this.tabs.forEach((cfg) => cfg.applyView());
    applyAllOrgFilters();
  }
  syncViewButtons(view) {
    document.querySelectorAll(".org-header-view-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === view);
    });
  }
  syncCreateButton(cfg) {
    if (!this.createBtn)
      return;
    this.createBtn.className = "btn btn--primary";
    this.createBtn.style.padding = ".4rem .55rem";
    this.createBtn.style.minWidth = "0";
    this.createBtn.title = cfg.createTitle;
    this.createBtn.innerHTML = cfg.createHtml;
  }
  setTab(tabKey) {
    this.activeTab = tabKey;
    this.headerActionsEl ??= document.querySelector(".organize-page-header-actions");
    this.actionsEl ??= document.getElementById("org-header-actions");
    const cfg = this.tabs.get(tabKey);
    if (this.headerActionsEl)
      this.headerActionsEl.hidden = tabKey === "priority";
    if (this.actionsEl)
      this.actionsEl.hidden = !cfg;
    if (!cfg)
      return;
    if (this.viewToggle)
      this.viewToggle.setAttribute("aria-label", cfg.viewAriaLabel);
    if (this.filterBtn)
      this.filterBtn.title = cfg.filterTitle;
    this.syncCreateButton(cfg);
    this.syncViewButtons(this.sharedView);
    cfg.updateSelAllBtn();
  }
  wireButtons() {
    if (this.wired)
      return;
    this.wired = true;
    this.actionsEl = document.getElementById("org-header-actions");
    this.filterBtn = document.getElementById("org-header-filter-btn");
    const selAllBtn = document.getElementById("org-header-sel-all");
    this.createBtn = document.getElementById("org-header-create-btn");
    this.viewToggle = document.getElementById("org-view-toggle");
    document.querySelectorAll(".org-header-view-btn").forEach((btn) => {
      btn.addEventListener("click", () => this.setSharedView(btn.dataset.view ?? "list"));
    });
    selAllBtn?.addEventListener("click", () => this.tabs.get(this.activeTab)?.onSelAll());
    this.filterBtn?.addEventListener("click", () => {
      const ns = this.getFilterNs();
      if (ns)
        toggleOrgFilter(ns);
    });
    this.createBtn?.addEventListener("click", () => this.tabs.get(this.activeTab)?.onCreate());
  }
  enforceMobileGalleryFallback() {
    if (!window.matchMedia("(max-width: 767px)").matches)
      return;
    if (this.sharedView === "gallery")
      this.setSharedView("list");
  }
  init() {
    this.wireButtons();
    this.enforceMobileGalleryFallback();
    this.setTab(this.activeTab);
    syncOrgFilterUI();
    applyAllOrgFilters();
    syncOrgFilterBarVisibility(this.getFilterNs());
    window.addEventListener("resize", () => this.enforceMobileGalleryFallback());
  }
}
var orgHeader;
function createOrganizeHeader(initialTab) {
  orgHeader = new OrganizeHeader(initialTab);
  return orgHeader;
}
function resetOrgBulk() {
  return { deselect: null, edit: null, merge: null, del: null };
}
function installOrgBulkToolbar() {
  window._orgBulk = resetOrgBulk();
  window._orgSelectionClearers = window._orgSelectionClearers ?? [];
  window._orgBulkEditByIds = window._orgBulkEditByIds ?? {};
  window._orgRegisterSelectionClearer = (fn) => {
    window._orgSelectionClearers.push(fn);
  };
  window._orgBulkClear = () => {
    document.getElementById("org-bulk-bar")?.classList.remove("visible");
    document.querySelector(".organize-page")?.classList.remove("org-page--has-selection");
    window._orgBulk = resetOrgBulk();
  };
  window._orgClearAllSelections = () => {
    window._orgSelectionClearers.forEach((fn) => fn());
    window._orgBulkClear();
  };
  window._orgBulkSync = (n, opts) => {
    const bar = document.getElementById("org-bulk-bar");
    const countEl = document.getElementById("org-bulk-count");
    const editBtn = document.getElementById("org-bulk-edit-btn");
    const mergeBtn = document.getElementById("org-bulk-merge-btn");
    const deleteBtn = document.getElementById("org-bulk-delete-btn");
    if (!bar)
      return;
    bar.classList.toggle("visible", n > 0);
    document.querySelector(".organize-page")?.classList.toggle("org-page--has-selection", n > 0);
    if (n > 0 && countEl)
      countEl.textContent = n === 1 ? "1 selected" : `${n} selected`;
    if (editBtn)
      editBtn.hidden = !opts.hasEdit;
    if (mergeBtn) {
      mergeBtn.hidden = !opts.hasMerge;
      mergeBtn.disabled = n < 2;
    }
    if (deleteBtn)
      deleteBtn.hidden = !opts.hasDel;
  };
  window._orgOpenSingleEdit = (dataAttr, id) => {
    const card = document.querySelector(`[${dataAttr}="${id}"]`);
    const btn = card?.querySelector('.tag-card-actions .btn--icon[title="Edit"]');
    if (!btn)
      return false;
    btn.click();
    return true;
  };
}
function installOrgTabSwitching() {
  const tabs = document.querySelectorAll(".organize-tab");
  const panels = document.querySelectorAll(".organize-panel");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.tab;
      if (!target)
        return;
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      panels.forEach((p) => {
        p.hidden = true;
      });
      const panel = document.getElementById(`panel-${target}`);
      if (panel)
        panel.hidden = false;
      orgHeader.setTab(target);
      localStorage.setItem("organize_tab", target);
      const url = new URL(window.location.href);
      url.searchParams.set("tab", target);
      window.history.replaceState({}, "", url.toString());
      if (target === "priority")
        window._initPrioritySortable?.();
      document.dispatchEvent(new CustomEvent("org:tab-changed", { detail: { tab: target } }));
      window._orgClearAllSelections?.();
      syncOrgFilterBarVisibility(tab.dataset.filterNs ?? null);
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape")
      return;
    const anyOpen = document.querySelector(".org-filter-bar.open");
    if (anyOpen) {
      document.querySelectorAll(".org-filter-bar.open").forEach((bar) => bar.classList.remove("open"));
      const activeTabEl = document.querySelector(".organize-tab.active[data-filter-ns]");
      const activeNs = activeTabEl?.dataset.filterNs ?? "tag";
      clearOrgFilter(activeNs);
      return;
    }
    window._orgBulk?.deselect?.();
  });
}
var ORG_SECTION_HERO = {
  labels: { icon: "tune", title: "Organize", subtitle: "Manage the tags, categories, statuses, and people labels used to organize your data." },
  lists: { icon: "bookmarks", title: "Lists", subtitle: "Group your pins into curated collections you can browse, share, and filter by." },
  filters: { icon: "filter_alt", title: "Filters", subtitle: "Save reusable filter criteria to quickly narrow down pins on the map and elsewhere." }
};
function updateOrgSectionHero(section) {
  const hero = ORG_SECTION_HERO[section];
  if (!hero)
    return;
  const titleEl = document.querySelector(".ul-page-hero__title");
  const iconEl = titleEl?.querySelector(".material-symbols-outlined");
  const subtitleEl = document.querySelector(".ul-page-hero__subtitle");
  if (iconEl)
    iconEl.textContent = hero.icon;
  if (titleEl) {
    const textNode = Array.from(titleEl.childNodes).find((n) => n.nodeType === Node.TEXT_NODE && !!n.textContent?.trim());
    if (textNode)
      textNode.textContent = ` ${hero.title} `;
  }
  if (subtitleEl)
    subtitleEl.textContent = hero.subtitle;
}
function installOrgSectionSwitching() {
  const tabs = document.querySelectorAll(".organize-section-tab");
  const panels = document.querySelectorAll(".organize-section-panel");
  if (!tabs.length)
    return;
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const section = tab.dataset.section;
      if (!section)
        return;
      tabs.forEach((t) => t.classList.toggle("is-active", t === tab));
      panels.forEach((p) => {
        p.hidden = p.id !== `panel-${section}`;
      });
      updateOrgSectionHero(section);
      const url = new URL(window.location.href);
      const tabParam = section === "labels" ? localStorage.getItem("organize_tab") ?? "tags" : section;
      url.searchParams.set("tab", tabParam);
      window.history.replaceState({}, "", url.toString());
    });
  });
}

// src/urbanlens/dashboard/frontend/ts/shared/tree-view.ts
var DEFAULT_TREE_ROOT_CLASS = "tag-tree-root";
function renderTreeView(rows, config) {
  const treeRootClass = config.treeRootClass ?? DEFAULT_TREE_ROOT_CLASS;
  rows.querySelector(`.${treeRootClass}`)?.remove();
  const cards = Array.from(rows.querySelectorAll(config.cardSelector));
  const cardMap = new Map;
  const parentMap = new Map;
  cards.forEach((card) => {
    const id = card.dataset[config.idKey];
    if (!id)
      return;
    cardMap.set(id, card);
    const parents = card.dataset[config.parentsKey] ?? "";
    parentMap.set(id, parents.split(",").map((s) => s.trim()).filter(Boolean));
    card.style.display = "none";
  });
  const childrenMap = new Map;
  parentMap.forEach((parents, id) => {
    parents.forEach((pid) => {
      const siblings = childrenMap.get(pid) ?? [];
      siblings.push(id);
      childrenMap.set(pid, siblings);
    });
  });
  const cardIds = new Set(cardMap.keys());
  const rootIds = Array.from(cardMap.keys()).filter((id) => {
    const parents = parentMap.get(id) ?? [];
    return parents.length === 0 || parents.every((pid) => !cardIds.has(pid));
  });
  rootIds.sort((a, b) => cards.indexOf(cardMap.get(a)) - cards.indexOf(cardMap.get(b)));
  const treeRoot = document.createElement("div");
  treeRoot.className = treeRootClass;
  const appearedInTree = new Set;
  function buildNode(id, depth, ancestorPath) {
    if (ancestorPath.has(id))
      return null;
    const card = cardMap.get(id);
    if (!card)
      return null;
    appearedInTree.add(id);
    const item = document.createElement("div");
    item.className = "tag-tree-item";
    item.dataset.depth = String(depth);
    item.style.setProperty("--tree-depth", String(depth));
    const clone = card.cloneNode(true);
    clone.style.display = "";
    clone.id = `tree-node-${id}-d${depth}-${Math.random().toString(36).slice(2, 6)}`;
    item.appendChild(clone);
    const newPath = new Set(ancestorPath);
    newPath.add(id);
    const children = childrenMap.get(id) ?? [];
    if (children.length > 0) {
      const childrenContainer = document.createElement("div");
      childrenContainer.className = "tag-tree-children";
      children.forEach((cid) => {
        const childNode = buildNode(cid, depth + 1, newPath);
        if (childNode)
          childrenContainer.appendChild(childNode);
      });
      item.appendChild(childrenContainer);
    }
    return item;
  }
  rootIds.forEach((id) => {
    const node = buildNode(id, 0, new Set);
    if (node)
      treeRoot.appendChild(node);
  });
  cardMap.forEach((_card, id) => {
    if (!appearedInTree.has(id)) {
      const node = buildNode(id, 0, new Set);
      if (node)
        treeRoot.appendChild(node);
    }
  });
  rows.appendChild(treeRoot);
  htmxProcess(treeRoot);
}

// src/urbanlens/dashboard/frontend/ts/shared/organize-tab-manager.ts
var MATERIAL_ICON_NAME = /^[a-z_]+$/;
function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

class OrgTabManager {
  cfg;
  selected = new Set;
  lastClickedIdx = -1;
  mergeTargetId = null;
  convertTarget = null;
  constructor(cfg) {
    this.cfg = cfg;
  }
  init() {
    this.wireSelection();
    this.wireRowEditIntercept();
    this.wireBulkEdit();
    this.wireMerge();
    this.wireHtmxHooks();
    registerBulkStateUpdater(this.cfg.ns, () => this.updateBulkState());
    const globalWindow = window;
    if (this.cfg.convertTargets.length > 0) {
      globalWindow[`_set${this.cfg.nsCapitalized}BulkConvert`] = (target) => this.setConvertTarget(target);
    }
    globalWindow[`_update${this.cfg.nsCapitalized}BulkState`] = () => this.updateBulkState();
    window._orgBulkEditByIds[this.cfg.ns] = (ids) => {
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
      onCreate: () => this.onCreate()
    });
    this.applyView();
  }
  tabKey() {
    return { tag: "tags", cat: "categories", status: "status", people: "people" }[this.cfg.ns] ?? this.cfg.ns;
  }
  get rows() {
    return document.getElementById(this.cfg.rowsId);
  }
  applyView() {
    const rows = this.rows;
    if (!rows)
      return;
    const view = orgHeader.getSharedView();
    rows.classList.remove("tag-view--list", "tag-view--gallery", "tag-view--tree");
    rows.classList.add(`tag-view--${view}`);
    orgHeader.syncViewButtons(view);
    if (view === "tree") {
      renderTreeView(rows, { cardSelector: this.cfg.cardSelector, idKey: this.cfg.idKey, parentsKey: this.cfg.parentsKey });
    } else {
      rows.querySelector(".tag-tree-root")?.remove();
      rows.querySelectorAll(".tag-card").forEach((c) => {
        c.style.display = "";
      });
    }
  }
  visibleCards() {
    return getOrgVisibleCards(this.rows, this.cfg.cardSelector);
  }
  getVisibleIds() {
    return this.visibleCards().filter((c) => !this.cfg.isProtected?.(c.dataset[this.cfg.idKey] ?? "")).map((c) => c.dataset[this.cfg.idKey] ?? "").filter(Boolean);
  }
  syncSelectionUi() {
    this.rows?.querySelectorAll(this.cfg.cardSelector).forEach((card) => {
      const id = card.dataset[this.cfg.idKey] ?? "";
      card.classList.toggle("tag-card--selected", this.selected.has(id));
      const cb = card.querySelector(this.cfg.checkboxSelector);
      if (cb)
        cb.checked = this.selected.has(id);
    });
    this.updateSelectionBar();
  }
  updateSelAllBtn() {
    const btn = document.getElementById("org-header-sel-all");
    if (!btn)
      return;
    const visIds = this.getVisibleIds();
    const allSel = visIds.length > 0 && visIds.every((id) => this.selected.has(id));
    btn.classList.toggle("deselect-mode", allSel);
    btn.title = allSel ? "Deselect all" : "Select all";
    btn.innerHTML = allSel ? '<i class="material-symbols-outlined">remove_done</i>' : '<i class="material-symbols-outlined">checklist</i>';
  }
  onSelectAll() {
    const visIds = this.getVisibleIds();
    const allSel = visIds.length > 0 && visIds.every((id) => this.selected.has(id));
    visIds.forEach((id) => {
      if (allSel)
        this.selected.delete(id);
      else
        this.selected.add(id);
    });
    this.lastClickedIdx = -1;
    this.syncSelectionUi();
  }
  updateSelectionBar() {
    const n = this.selected.size;
    window._orgBulk.deselect = () => {
      this.selected.clear();
      this.syncSelectionUi();
    };
    window._orgBulk.edit = () => {
      if (!this.selected.size)
        return;
      if (this.selected.size === 1 && window._orgOpenSingleEdit(`data-${this.datasetAttr(this.cfg.idKey)}`, Array.from(this.selected)[0]))
        return;
      this.openBulkEditDialog();
    };
    window._orgBulk.merge = () => {
      if (this.selected.size < 2)
        return;
      this.mergeTargetId = Array.from(this.selected)[0];
      this.renderMergeDialog();
      document.getElementById(this.cfg.mergeDialog.dialogId).showModal();
    };
    window._orgBulk.del = () => this.bulkDelete();
    window._orgBulkSync(n, { hasEdit: true, hasMerge: true, hasDel: true });
    this.updateSelAllBtn();
  }
  wireSelection() {
    this.rows?.addEventListener("click", (e) => {
      const target = e.target;
      const card = target.closest(this.cfg.cardSelector);
      if (!card)
        return;
      const cb = target.closest(this.cfg.checkboxSelector);
      if (cb) {
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
        for (let i = lo;i <= hi; i++) {
          const cid = cards[i]?.dataset[this.cfg.idKey];
          if (!cid)
            continue;
          if (this.cfg.isProtected?.(cid))
            continue;
          if (targetState)
            this.selected.add(cid);
          else
            this.selected.delete(cid);
        }
        if (isProtected) {
          if (targetState)
            this.selected.add(id);
          else
            this.selected.delete(id);
        }
      } else {
        if (this.selected.has(id))
          this.selected.delete(id);
        else
          this.selected.add(id);
        this.lastClickedIdx = idx;
      }
      this.syncSelectionUi();
    });
  }
  wireRowEditIntercept() {
    this.rows?.addEventListener("click", (e) => {
      if (this.selected.size <= 1)
        return;
      const btn = e.target.closest('.tag-card-actions .btn--icon[title="Edit"]');
      if (!btn)
        return;
      const card = btn.closest(this.cfg.cardSelector);
      const id = card?.dataset[this.cfg.idKey];
      if (!id || !this.selected.has(id))
        return;
      e.preventDefault();
      e.stopImmediatePropagation();
      this.openBulkEditDialog();
    }, true);
  }
  onRowsUpdated() {
    this.selected.clear();
    this.lastClickedIdx = -1;
    this.syncSelectionUi();
    this.applyView();
    applyOrgFilter(this.cfg.ns);
  }
  wireHtmxHooks() {
    this.rows?.addEventListener("htmx:afterSwap", () => this.onRowsUpdated());
    document.addEventListener("org:filter-applied", (e) => {
      if (e.detail.ns === this.cfg.ns)
        this.updateSelAllBtn();
    });
  }
  onCreate() {
    const f = document.getElementById(this.cfg.newForm?.dialogId ?? "");
    if (!f)
      return;
    if (this.cfg.newForm) {
      f.querySelector("form")?.reset();
      resetIconPicker(this.cfg.newForm.iconPickerId);
      resetColorPicker(this.cfg.newForm.colorPickerId, this.cfg.newForm.colorValueId);
      if (this.cfg.newForm.customPreviewId) {
        const preview = document.getElementById(this.cfg.newForm.customPreviewId);
        if (preview) {
          preview.src = "";
          preview.style.display = "none";
        }
      }
    } else {
      f.querySelector("form")?.reset();
    }
    if (!f.open)
      f.showModal();
  }
  async bulkDelete() {
    const n = this.selected.size;
    if (!n)
      return;
    const entity = n === 1 ? this.cfg.entitySingular.toLowerCase() : this.cfg.entityPluralLower;
    let message = `Delete ${n} ${entity}?`;
    if (this.cfg.deleteWarning)
      message += `
${this.cfg.deleteWarning}`;
    if (!await confirmAction({ title: `Delete ${this.cfg.entityPluralCap}`, message, confirmLabel: "Delete" }))
      return;
    const ids = Array.from(this.selected).map((id) => Number.parseInt(id, 10));
    try {
      const html = await this.postForHtml(this.cfg.endpoints.bulkDelete, { ids });
      this.replaceRows(html);
      this.onRowsUpdated();
      toast.success(n === 1 ? `1 ${this.cfg.entitySingular.toLowerCase()} deleted.` : `${n} ${this.cfg.entityPluralLower} deleted.`);
    } catch (err) {
      toast.error(`Delete failed: ${err.message}`);
    }
  }
  setConvertTarget(target) {
    const btns = document.querySelectorAll(`#${this.cfg.bulkEditDialog.dialogId} .kind-toggle-option`);
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
  updateBulkState() {
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
    const btn = document.getElementById(this.cfg.bulkEditDialog.confirmId);
    if (btn && !btn.disabled) {
      const targetLabel = this.cfg.convertTargets.find((t) => t.kind === this.convertTarget)?.label ?? "";
      btn.innerHTML = converting ? `<i class="material-icons" style="font-size:1rem;vertical-align:middle">swap_horiz</i> Convert to ${targetLabel}` : '<i class="material-icons" style="font-size:1rem;vertical-align:middle">edit</i> Apply Changes';
    }
  }
  openBulkEditDialog() {
    const d = this.cfg.bulkEditDialog;
    const ids = Array.from(this.selected);
    const iconSet = new Set;
    const colorSet = new Set;
    const customIconSet = new Set;
    ids.forEach((id) => {
      const card = document.querySelector(`[data-${this.datasetAttr(this.cfg.idKey)}="${id}"]`);
      if (!card)
        return;
      iconSet.add(card.dataset[this.cfg.iconKey] ?? "");
      colorSet.add(card.dataset[this.cfg.colorKey] ?? "");
      if (this.cfg.customIconKey)
        customIconSet.add(card.dataset[this.cfg.customIconKey] ?? "");
    });
    const sharedIcon = iconSet.size === 1 ? Array.from(iconSet)[0] : null;
    const sharedColor = colorSet.size === 1 ? Array.from(colorSet)[0] : null;
    const sharedCustomIcon = this.cfg.customIconKey && customIconSet.size === 1 ? Array.from(customIconSet)[0] : null;
    const iconNochange = document.getElementById(d.iconNochangeId);
    const iconValue = document.getElementById(`icon-value-${d.iconPickerId}`);
    const iconCurrent = document.getElementById(`icon-current-${d.iconPickerId}`);
    const iconGrid = document.getElementById(`icon-grid-${d.iconPickerId}`);
    iconGrid?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    if (sharedCustomIcon) {
      iconNochange.checked = true;
      if (iconValue)
        iconValue.value = "";
      if (iconCurrent)
        iconCurrent.innerHTML = `<img src="${sharedCustomIcon}" alt="" class="tag-icon-img"> <span class="icon-picker-none-label">Custom icon (kept unless you pick a new one)</span>`;
    } else if (sharedIcon !== null) {
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
    if (d.orderValueId && d.orderNochangeId) {
      const orderNochange = document.getElementById(d.orderNochangeId);
      const orderValue = document.getElementById(d.orderValueId);
      orderNochange.checked = true;
      if (orderValue) {
        orderValue.value = "0";
        orderValue.dataset.bulkOriginal = "0";
      }
    }
    if (d.descValueId && d.descNochangeId) {
      const descNochange = document.getElementById(d.descNochangeId);
      const descValue = document.getElementById(d.descValueId);
      descNochange.checked = true;
      if (descValue) {
        descValue.value = "";
        descValue.dataset.bulkOriginal = "";
      }
    }
    LabelRelPicker.reset(`${this.cfg.ns}-bulk`);
    this.convertTarget = null;
    document.querySelectorAll(`#${d.dialogId} .kind-toggle-option`).forEach((b) => b.classList.remove("is-active"));
    const titleEl = document.getElementById(d.titleId);
    if (titleEl)
      titleEl.textContent = `Edit ${ids.length} ${ids.length === 1 ? this.cfg.entitySingular : this.cfg.entityPluralCap}`;
    const confirmBtn = document.getElementById(d.confirmId);
    confirmBtn.disabled = false;
    confirmBtn.innerHTML = '<i class="material-icons" style="font-size:1rem;vertical-align:middle">edit</i> Apply Changes';
    this.updateBulkState();
    document.getElementById(d.dialogId).showModal();
  }
  wireBulkEdit() {
    const d = this.cfg.bulkEditDialog;
    document.getElementById(d.iconNochangeId)?.addEventListener("change", (e) => {
      if (e.target.checked)
        resetIconPicker(d.iconPickerId);
      this.updateBulkState();
    });
    document.getElementById(d.colorNochangeId)?.addEventListener("change", (e) => {
      if (e.target.checked)
        resetColorPicker(d.colorPickerId, d.colorValueId);
      this.updateBulkState();
    });
    document.getElementById(`icon-grid-${d.iconPickerId}`)?.addEventListener("click", (e) => {
      if (e.target.closest(".icon-picker-item")) {
        document.getElementById(d.iconNochangeId).checked = false;
        this.updateBulkState();
      }
    });
    if (d.orderValueId && d.orderNochangeId) {
      document.getElementById(d.orderValueId)?.addEventListener("input", () => {
        document.getElementById(d.orderNochangeId).checked = false;
      });
      document.getElementById(d.orderNochangeId)?.addEventListener("change", (e) => {
        if (e.target.checked) {
          const el = document.getElementById(d.orderValueId);
          if (el)
            el.value = el.dataset.bulkOriginal ?? "0";
        }
      });
    }
    if (d.descValueId && d.descNochangeId) {
      document.getElementById(d.descValueId)?.addEventListener("input", () => {
        document.getElementById(d.descNochangeId).checked = false;
      });
      document.getElementById(d.descNochangeId)?.addEventListener("change", (e) => {
        if (e.target.checked) {
          const el = document.getElementById(d.descValueId);
          if (el)
            el.value = el.dataset.bulkOriginal ?? "";
        }
      });
    }
    document.getElementById(d.confirmId)?.addEventListener("click", async () => {
      const ids = Array.from(this.selected).map((id) => Number.parseInt(id, 10));
      const converting = !!this.convertTarget;
      const btn = document.getElementById(d.confirmId);
      const saved = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `<span class="cat-merge-spinner"></span> ${converting ? "Converting…" : "Saving…"}`;
      const body = { ids };
      if (d.orderNochangeId && !document.getElementById(d.orderNochangeId).checked) {
        body.order = document.getElementById(d.orderValueId)?.value ?? "";
      }
      if (d.descNochangeId && !document.getElementById(d.descNochangeId).checked) {
        body.description = document.getElementById(d.descValueId)?.value ?? "";
      }
      if (!document.getElementById(d.iconNochangeId).checked) {
        body.icon = document.getElementById(`icon-value-${d.iconPickerId}`)?.value ?? "";
      }
      if (!document.getElementById(d.colorNochangeId).checked) {
        body.color = document.getElementById(d.colorValueId)?.value ?? "";
      }
      body.add_parent_ids = LabelRelPicker.getSelectedIds(`${this.cfg.ns}-bulk`, "parent");
      body.add_child_ids = LabelRelPicker.getSelectedIds(`${this.cfg.ns}-bulk`, "child");
      try {
        const target = converting ? this.cfg.convertTargets.find((t) => t.kind === this.convertTarget) : undefined;
        const url = converting ? target.endpoint : this.cfg.endpoints.bulkEdit;
        const html = await this.postForHtml(url, body);
        document.getElementById(d.dialogId).close();
        this.replaceRows(html);
        this.onRowsUpdated();
        if (converting) {
          toast.success(ids.length === 1 ? `1 ${this.cfg.entitySingular.toLowerCase()} converted.` : `${ids.length} ${this.cfg.entityPluralLower} converted.`);
          if (target?.rowsUrl && target.rowsTarget) {
            window.htmx?.ajax("GET", target.rowsUrl, { target: target.rowsTarget, swap: "innerHTML" });
          }
          if (target?.tabKey) {
            document.querySelector(`.organize-tab[data-tab="${target.tabKey}"]`)?.click();
          }
        } else {
          toast.success(`${this.cfg.entityPluralCap} updated.`);
        }
      } catch (err) {
        toast.error(`${converting ? "Convert" : "Edit"} failed: ${err.message}`);
        btn.disabled = false;
        btn.innerHTML = saved;
      }
    });
  }
  getCardData(id) {
    const card = document.querySelector(`[data-${this.datasetAttr(this.cfg.idKey)}="${id}"]`);
    if (!card)
      return { id, name: "?", color: "", icon: "", pinCount: "0" };
    const data = {
      id,
      name: card.dataset[this.cfg.nameKey] ?? "",
      color: card.dataset[this.cfg.colorKey] ?? "",
      icon: card.dataset[this.cfg.iconKey] ?? "",
      pinCount: card.dataset[this.cfg.pinCountKey] ?? "0"
    };
    if (this.cfg.customIconKey)
      data.customIcon = card.dataset[this.cfg.customIconKey] ?? "";
    if (this.cfg.locationCountKey)
      data.locationCount = card.dataset[this.cfg.locationCountKey] ?? "0";
    return data;
  }
  miniCardHtml(data, isTarget, hideSwap) {
    const colorStyle = data.color ? `background:${data.color}22;border-color:${data.color}44;` : "";
    const iconColorStyle = data.color ? `color:${data.color}` : "";
    let iconHtml;
    if (data.customIcon) {
      iconHtml = `<img src="${escHtml(data.customIcon)}" style="width:24px;height:24px;object-fit:cover;border-radius:4px;" alt="">`;
    } else if (data.icon) {
      iconHtml = MATERIAL_ICON_NAME.test(data.icon) ? `<i class="material-icons" style="${iconColorStyle}">${escHtml(data.icon)}</i>` : `<span class="tag-icon-emoji">${escHtml(data.icon)}</span>`;
    } else {
      iconHtml = `<i class="material-icons tag-icon-empty">${this.cfg.emptyIcon}</i>`;
    }
    const swapBtn = isTarget || hideSwap ? "" : `<button type="button" class="cat-merge-swap-btn" data-swap-id="${data.id}" title="Make this the surviving ${this.cfg.entitySingular.toLowerCase()}"><i class="material-symbols-outlined">swap_vert</i></button>`;
    const meta = data.locationCount !== undefined ? `${data.pinCount} pins &middot; ${data.locationCount} locations` : `${data.pinCount} pins`;
    return `<div class="cat-merge-mini-card${isTarget ? " cat-merge-mini-card--target" : ""}" data-merge-id="${data.id}">` + `<div class="tag-card-icon cat-merge-mini-icon" style="${colorStyle}">${iconHtml}</div>` + `<div class="cat-merge-mini-info"><div class="cat-merge-mini-name">${escHtml(data.name)}</div>` + `<div class="cat-merge-mini-meta">${meta}</div></div>${swapBtn}</div>`;
  }
  setMergeColorPicker(color) {
    const picker = document.getElementById(`${this.cfg.ns}-merge-color-picker`);
    const input = document.getElementById(`${this.cfg.ns}-merge-edit-color`);
    if (!picker || !input)
      return;
    picker.querySelectorAll(".color-swatch").forEach((s) => s.classList.remove("selected"));
    input.value = color;
    if (color)
      picker.querySelector(`[data-color="${color}"]`)?.classList.add("selected");
    else
      picker.querySelector(".color-clear")?.classList.add("selected");
  }
  setMergeIconPicker(icon) {
    const pickerId = this.cfg.mergeDialog.editIconId ?? `${this.cfg.ns}-merge-edit`;
    const iconValue = document.getElementById(`icon-value-${pickerId}`);
    const iconCurrent = document.getElementById(`icon-current-${pickerId}`);
    const iconGrid = document.getElementById(`icon-grid-${pickerId}`);
    iconGrid?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    if (iconValue)
      iconValue.value = icon;
    if (iconCurrent)
      iconCurrent.innerHTML = renderIconGlyphHtml(icon);
    if (icon && iconGrid)
      iconGrid.querySelector(`[data-icon="${icon}"]`)?.classList.add("selected");
    else
      iconGrid?.querySelector(".icon-picker-none")?.classList.add("selected");
  }
  renderMergeDialog() {
    const d = this.cfg.mergeDialog;
    const ids = Array.from(this.selected);
    const protectedId = this.cfg.isProtected ? ids.find((id) => this.cfg.isProtected(id)) : undefined;
    if (protectedId) {
      this.mergeTargetId = protectedId;
    } else if (!this.mergeTargetId || !this.selected.has(this.mergeTargetId)) {
      this.mergeTargetId = ids[0] ?? null;
    }
    const targetIsProtected = this.cfg.isProtected?.(this.mergeTargetId ?? "") ?? false;
    const sourceIds = ids.filter((id) => id !== this.mergeTargetId);
    const data = this.getCardData(this.mergeTargetId);
    const titleEl = document.getElementById(d.titleId);
    if (titleEl)
      titleEl.textContent = `Merge ${ids.length} ${this.cfg.entityPluralCap}`;
    const targetCard = document.getElementById(d.targetCardId);
    if (targetCard)
      targetCard.innerHTML = this.miniCardHtml(data, true, false);
    const sourcesList = document.getElementById(d.sourcesListId);
    if (sourcesList)
      sourcesList.innerHTML = sourceIds.map((id) => this.miniCardHtml(this.getCardData(id), false, targetIsProtected)).join("");
    if (this.cfg.supportsMergeEdit) {
      if (d.swapHintId) {
        const swapHint = document.getElementById(d.swapHintId);
        if (swapHint)
          swapHint.style.display = targetIsProtected ? "none" : "";
      }
      const nameEl = document.getElementById(d.editNameId ?? "");
      if (nameEl) {
        nameEl.value = data.name;
        nameEl.readOnly = targetIsProtected;
        nameEl.title = targetIsProtected ? "Protected status names cannot be changed" : "";
      }
      this.setMergeIconPicker(data.icon);
      this.setMergeColorPicker(data.color);
    }
    const confirmBtn = document.getElementById(d.confirmId);
    confirmBtn.innerHTML = `<i class="material-icons" style="font-size:1rem;vertical-align:middle">merge</i> Merge into ${escHtml(data.name)}`;
    confirmBtn.disabled = false;
  }
  wireMerge() {
    const d = this.cfg.mergeDialog;
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
      const saved = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="cat-merge-spinner"></span> Merging…';
      const capturedId = this.mergeTargetId;
      const origData = this.getCardData(capturedId);
      let editName = "";
      let editIcon = "";
      let editColor = "";
      let hasEdits = false;
      if (this.cfg.supportsMergeEdit) {
        editName = (document.getElementById(d.editNameId ?? "")?.value ?? "").trim() || origData.name;
        const iconPickerId = d.editIconId ?? `${this.cfg.ns}-merge-edit`;
        editIcon = document.getElementById(`icon-value-${iconPickerId}`)?.value ?? "";
        editColor = document.getElementById(`${this.cfg.ns}-merge-edit-color`)?.value ?? "";
        hasEdits = editName !== origData.name || editIcon !== origData.icon || editColor !== origData.color;
      }
      try {
        const mergeHtml = await this.postForHtml(this.cfg.endpoints.multiMerge, {
          target_id: Number.parseInt(capturedId, 10),
          source_ids: sourceIds.map((id) => Number.parseInt(id, 10))
        });
        let html = mergeHtml;
        if (hasEdits && this.cfg.endpoints.mergeEditTemplate) {
          const fd = new FormData;
          fd.append("name", editName);
          fd.append("icon", editIcon);
          fd.append("color", editColor);
          const editUrl = this.cfg.endpoints.mergeEditTemplate.replace("99999", capturedId);
          const editResponse = await fetch(editUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body: fd });
          if (!editResponse.ok)
            toast.warning("Merged, but could not save property changes.");
          else
            html = await editResponse.text();
        }
        document.getElementById(d.dialogId).close();
        this.replaceRows(html);
        this.mergeTargetId = null;
        this.onRowsUpdated();
        toast.success(`${this.cfg.entityPluralCap} merged successfully.`);
      } catch (err) {
        toast.error(`Merge failed: ${err.message}`);
        btn.disabled = false;
        btn.innerHTML = saved;
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
    window.htmx?.process(rows);
  }
  datasetAttr(camelKey) {
    return camelKey.replace(/([A-Z])/g, "-$1").toLowerCase();
  }
}

// src/urbanlens/dashboard/frontend/ts/shared/organize-priority.ts
function initOrganizePriority() {
  let prioritySortable = null;
  let priorityOrderEditing = null;
  let lastClickedIdx = -1;
  function priorityOrderBadge(item) {
    return item.querySelector(".priority-order-editor")?.querySelector(".priority-order-chip") ?? null;
  }
  function flashPriorityOrderSaved(item) {
    item.classList.remove("priority-item--order-saved");
    item.offsetWidth;
    item.classList.add("priority-item--order-saved");
    const badge = priorityOrderBadge(item);
    if (badge) {
      badge.classList.remove("priority-order-chip--flash");
      badge.offsetWidth;
      badge.classList.add("priority-order-chip--flash");
    }
    window.setTimeout(() => item.classList.remove("priority-item--order-saved"), 650);
  }
  function closeOrderEditor(restoreValue) {
    if (!priorityOrderEditing)
      return;
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
  async function savePriorityOrder(list, flashItem) {
    const items = Array.from(list.querySelectorAll(".priority-item[data-id]")).map((el, i) => {
      const badge = priorityOrderBadge(el);
      if (badge)
        badge.textContent = String(i + 1);
      return { id: Number.parseInt(el.dataset.id ?? "0", 10) };
    });
    try {
      const response = await fetch(list.dataset.saveUrl ?? "", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ items })
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
      if (flashItem)
        flashPriorityOrderSaved(flashItem);
      toast.success("Display order saved.");
    } catch (err) {
      toast.error(`Save failed: ${err.message}`);
    }
  }
  function commitOrderEditor() {
    if (!priorityOrderEditing)
      return;
    const edit = priorityOrderEditing;
    const list = edit.list;
    const total = list.querySelectorAll(".priority-item[data-id]").length;
    const newPos = Number.parseInt(edit.input.value, 10);
    if (Number.isNaN(newPos)) {
      closeOrderEditor(edit.originalValue);
      return;
    }
    const clampedPos = Math.max(1, Math.min(total, newPos));
    const items = Array.from(list.querySelectorAll(".priority-item[data-id]"));
    const currentIdx = items.indexOf(edit.item);
    const targetIdx = clampedPos - 1;
    closeOrderEditor(clampedPos);
    if (currentIdx === targetIdx)
      return;
    edit.item.remove();
    const remaining = Array.from(list.querySelectorAll(".priority-item[data-id]"));
    if (targetIdx >= remaining.length)
      list.appendChild(edit.item);
    else
      list.insertBefore(edit.item, remaining[targetIdx]);
    savePriorityOrder(list, edit.item);
  }
  function cancelOrderEditor() {
    if (priorityOrderEditing)
      closeOrderEditor(priorityOrderEditing.originalValue);
  }
  function beginPriorityOrderEdit(badge) {
    if (priorityOrderEditing) {
      if (priorityOrderEditing.badge === badge)
        return;
      cancelOrderEditor();
    }
    const editor = badge.closest(".priority-order-editor");
    const item = badge.closest(".priority-item");
    const list = document.getElementById("priority-list");
    if (!editor || !item || !list)
      return;
    const input = editor.querySelector(".priority-order-input");
    const saveBtn = editor.querySelector(".priority-order-save");
    if (!input || !saveBtn)
      return;
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
        if (priorityOrderEditing)
          priorityOrderEditing.cancelled = true;
        cancelOrderEditor();
      }
    };
    input.onblur = () => {
      window.setTimeout(() => {
        if (!priorityOrderEditing || priorityOrderEditing.input !== input)
          return;
        if (priorityOrderEditing.cancelled)
          return;
        const active = document.activeElement;
        if (active === saveBtn || saveBtn.contains(active))
          return;
        commitOrderEditor();
      }, 0);
    };
    saveBtn.onpointerdown = (e) => e.preventDefault();
    saveBtn.onclick = (e) => {
      e.preventDefault();
      commitOrderEditor();
    };
  }
  function priorityItems() {
    const list = document.getElementById("priority-list");
    return list ? Array.from(list.querySelectorAll(".priority-item[data-id]")) : [];
  }
  function setPrioritySelected(item, selected) {
    item.classList.toggle("priority-item--selected", selected);
  }
  function updatePrioritySelBar() {
    window._orgBulk.deselect = clearPrioritySelection;
    window._orgBulk.edit = () => {
      const items = document.querySelectorAll("#priority-list .priority-item--selected");
      if (!items.length)
        return;
      if (items.length === 1) {
        items[0].querySelector(".priority-edit-btn")?.click();
        return;
      }
      const kinds = new Set;
      const ids = [];
      items.forEach((item) => {
        if (item.dataset.kind)
          kinds.add(item.dataset.kind);
        if (item.dataset.id)
          ids.push(item.dataset.id);
      });
      if (kinds.size > 1) {
        toast.warning("Select only tags, only categories, or only statuses to bulk edit them together.");
        return;
      }
      const kind = Array.from(kinds)[0];
      const opener = kind ? window._orgBulkEditByIds[kind] : undefined;
      if (opener)
        opener(ids);
      else
        toast.error("Bulk edit is not available for this type.");
    };
    const n = document.querySelectorAll("#priority-list .priority-item--selected").length;
    window._orgBulkSync(n, { hasEdit: true, hasMerge: false, hasDel: false });
  }
  function clearPrioritySelection() {
    priorityItems().forEach((item) => setPrioritySelected(item, false));
    lastClickedIdx = -1;
    updatePrioritySelBar();
  }
  window._orgRegisterSelectionClearer(clearPrioritySelection);
  function initPrioritySortable() {
    const list = document.getElementById("priority-list");
    if (!list)
      return;
    prioritySortable?.destroy();
    prioritySortable = new sortable_esm_default(list, {
      animation: 150,
      handle: ".priority-drag-handle",
      ghostClass: "priority-item--ghost",
      fallbackTolerance: 3,
      onEnd: () => {
        savePriorityOrder(list, null);
      }
    });
  }
  document.getElementById("priority-list")?.addEventListener("click", (e) => {
    const target = e.target;
    const badge = target.closest(".priority-order-chip");
    if (badge) {
      e.preventDefault();
      beginPriorityOrderEdit(badge);
      return;
    }
    const jumpBtn = target.closest("[data-priority-jump]");
    if (jumpBtn) {
      const jumpItem = jumpBtn.closest(".priority-item");
      const list = document.getElementById("priority-list");
      if (!jumpItem || !list)
        return;
      if (jumpBtn.dataset.priorityJump === "top")
        list.insertBefore(jumpItem, list.firstElementChild);
      else
        list.appendChild(jumpItem);
      savePriorityOrder(list, jumpItem);
      return;
    }
    const item = target.closest(".priority-item");
    if (!item)
      return;
    if (target.closest(".priority-drag-handle,.priority-order-editor,a,button,input,select,textarea"))
      return;
    const items = priorityItems();
    const idx = items.indexOf(item);
    const isSelected = item.classList.contains("priority-item--selected");
    if (e.shiftKey && lastClickedIdx >= 0) {
      const lo = Math.min(idx, lastClickedIdx);
      const hi = Math.max(idx, lastClickedIdx);
      const targetState = !isSelected;
      for (let i = lo;i <= hi; i++) {
        const el = items[i];
        if (el)
          setPrioritySelected(el, targetState);
      }
    } else {
      setPrioritySelected(item, !isSelected);
      lastClickedIdx = idx;
    }
    updatePrioritySelBar();
  });
  window._initPrioritySortable = initPrioritySortable;
  document.getElementById("priority-list")?.addEventListener("htmx:afterSwap", () => {
    clearPrioritySelection();
    initPrioritySortable();
  });
  if (document.getElementById("panel-priority") && !document.getElementById("panel-priority").hidden) {
    initPrioritySortable();
  }
}

// src/urbanlens/dashboard/frontend/ts/shared/onboarding-tour.ts
function initOnboardingTour(config) {
  const sessionKey = `${config.prefix}_later`;
  let activeCard = null;
  function dismissed(id) {
    try {
      return localStorage.getItem(`${config.prefix}_${id}_dismissed`) === "1";
    } catch {
      return false;
    }
  }
  function dismiss(id) {
    try {
      localStorage.setItem(`${config.prefix}_${id}_dismissed`, "1");
    } catch {}
  }
  function later() {
    try {
      sessionStorage.setItem(sessionKey, "1");
    } catch {}
  }
  function laterSet() {
    try {
      return sessionStorage.getItem(sessionKey) === "1";
    } catch {
      return false;
    }
  }
  function isCardTargetVisible(card) {
    const el = document.querySelector(card.target);
    return !!el && el.offsetParent !== null;
  }
  function clear() {
    document.querySelector(config.hostSelector)?.replaceChildren();
    document.querySelectorAll(".onboarding-focus").forEach((el) => el.classList.remove("onboarding-focus"));
    activeCard = null;
  }
  function registerAutoDismiss(card) {
    if (dismissed(card.id) || !card.watchSelector)
      return;
    document.querySelectorAll(card.watchSelector).forEach((el) => {
      el.addEventListener(card.watchEvent ?? "click", () => dismiss(card.id), { once: true });
    });
  }
  function show(card) {
    const host = document.querySelector(config.hostSelector);
    if (!host)
      return;
    clear();
    activeCard = card;
    document.querySelector(card.target)?.classList.add("onboarding-focus");
    const el = document.createElement("section");
    el.className = "page-onboarding-card";
    el.innerHTML = `<div class="page-onboarding-card__icon"><i class="material-icons">${card.icon}</i></div>` + `<div class="page-onboarding-card__body"><div class="page-onboarding-card__eyebrow">${card.eyebrow}</div>` + `<h2>${card.title}</h2><p>${card.body}</p><div class="page-onboarding-card__actions">` + `<button type="button" class="btn btn--primary js-onboarding-action">${card.button}</button>` + `<button type="button" class="btn btn--ghost js-onboarding-later">Later</button>` + `<button type="button" class="page-onboarding-dismiss js-onboarding-dismiss">Don't show again</button></div></div>` + `<button type="button" class="page-onboarding-x js-onboarding-later" aria-label="Close"><i class="material-symbols-outlined">close</i></button>`;
    host.appendChild(el);
    el.querySelector(".js-onboarding-action")?.addEventListener("click", () => {
      dismiss(card.id);
      clear();
      card.action();
    });
    el.querySelectorAll(".js-onboarding-later").forEach((btn) => btn.addEventListener("click", () => {
      later();
      clear();
    }));
    el.querySelector(".js-onboarding-dismiss")?.addEventListener("click", () => {
      dismiss(card.id);
      clear();
    });
  }
  function tryShow() {
    if (laterSet())
      return;
    if (activeCard && (!activeCard.ready() || !isCardTargetVisible(activeCard)))
      clear();
    if (document.querySelector(".page-onboarding-card"))
      return;
    const card = config.cards.find((c) => c.ready() && isCardTargetVisible(c) && !dismissed(c.id));
    if (card)
      show(card);
  }
  config.cards.forEach(registerAutoDismiss);
  setTimeout(tryShow, config.initialDelayMs ?? 900);
  if (config.retryEvent) {
    document.addEventListener(config.retryEvent, () => setTimeout(tryShow, 250));
  } else {
    document.body.addEventListener("htmx:afterSettle", () => setTimeout(tryShow, 250));
  }
}

// src/urbanlens/dashboard/frontend/ts/entries/organize.ts
installGlobalOrganizeIconPicker();
installGlobalColorPicker();
installGlobalLabelRelPicker();
function showLabelCustomPreview(input, previewId) {
  const file = input.files?.[0];
  if (!file)
    return;
  const preview = document.getElementById(previewId);
  if (!preview)
    return;
  const reader = new FileReader;
  reader.onload = (e) => {
    preview.src = e.target?.result;
    preview.style.display = "block";
  };
  reader.readAsDataURL(file);
}
function showTagCustomPreview(input) {
  showLabelCustomPreview(input, "new-tag-custom-preview");
}
window.showLabelCustomPreview = showLabelCustomPreview;
window.showTagCustomPreview = showTagCustomPreview;
var KIND_ROWS_TARGET = { tag: "#tag-rows", category: "#category-rows", status: "#status-rows" };
var KIND_TAB_KEY = { tag: "tags", category: "categories", status: "status" };
function buildTabConfig(rows, overrides) {
  const page = document.querySelector(".organize-page");
  const rowsUrls = { tag: page?.dataset.rowsUrlTag, category: page?.dataset.rowsUrlCategory, status: page?.dataset.rowsUrlStatus };
  const convertTargets = [];
  if (rows.dataset.convertCategoryUrl)
    convertTargets.push({ kind: "category", label: "Categories", endpoint: rows.dataset.convertCategoryUrl, rowsUrl: rowsUrls.category, rowsTarget: KIND_ROWS_TARGET.category, tabKey: KIND_TAB_KEY.category });
  if (rows.dataset.convertTagUrl)
    convertTargets.push({ kind: "tag", label: "Tags", endpoint: rows.dataset.convertTagUrl, rowsUrl: rowsUrls.tag, rowsTarget: KIND_ROWS_TARGET.tag, tabKey: KIND_TAB_KEY.tag });
  if (rows.dataset.convertStatusUrl)
    convertTargets.push({ kind: "status", label: "Statuses", endpoint: rows.dataset.convertStatusUrl, rowsUrl: rowsUrls.status, rowsTarget: KIND_ROWS_TARGET.status, tabKey: KIND_TAB_KEY.status });
  const base = {
    ns: overrides.ns,
    nsCapitalized: overrides.nsCapitalized,
    rowsId: rows.id,
    cardSelector: `.tag-card[data-${overrides.ns}-id]`,
    idKey: `${overrides.ns}Id`,
    nameKey: `${overrides.ns}Name`,
    iconKey: `${overrides.ns}Icon`,
    colorKey: `${overrides.ns}Color`,
    parentsKey: `${overrides.ns}Parents`,
    pinCountKey: `${overrides.ns}PinCount`,
    checkboxSelector: `.${overrides.ns}-select-cb`,
    entitySingular: "",
    entityPluralLower: "",
    entityPluralCap: "",
    emptyIcon: "label",
    endpoints: {
      bulkDelete: rows.dataset.bulkDeleteUrl ?? "",
      bulkEdit: rows.dataset.bulkEditUrl ?? "",
      multiMerge: rows.dataset.mergeUrl ?? "",
      mergeEditTemplate: rows.dataset.mergeEditUrlTemplate
    },
    supportsMergeEdit: !!rows.dataset.mergeEditUrlTemplate,
    convertTargets,
    newForm: null,
    bulkEditDialog: {
      dialogId: `${overrides.ns}-bulk-edit-dialog`,
      titleId: `${overrides.ns}-bulk-edit-title`,
      confirmId: `${overrides.ns}-bulk-edit-confirm`,
      iconPickerId: `${overrides.ns}-bulk-edit`,
      iconNochangeId: `${overrides.ns}-bulk-icon-nochange`,
      colorPickerId: `${overrides.ns}-bulk-color-picker`,
      colorValueId: `${overrides.ns}-bulk-color-value`,
      colorNochangeId: `${overrides.ns}-bulk-color-nochange`,
      orderValueId: `${overrides.ns}-bulk-order-value`,
      orderNochangeId: `${overrides.ns}-bulk-order-nochange`,
      descValueId: `${overrides.ns}-bulk-description-value`,
      descNochangeId: `${overrides.ns}-bulk-description-nochange`,
      convertHintId: `${overrides.ns}-bulk-convert-hint`
    },
    mergeDialog: {
      dialogId: `${overrides.ns}-merge-dialog`,
      titleId: `${overrides.ns}-merge-dialog-title`,
      targetCardId: `${overrides.ns}-merge-target-card`,
      sourcesListId: `${overrides.ns}-merge-sources-list`,
      confirmId: `${overrides.ns}-merge-confirm-btn`,
      editNameId: `${overrides.ns}-merge-edit-name`,
      editIconId: `${overrides.ns}-merge-edit`,
      swapHintId: `${overrides.ns}-merge-swap-hint`
    }
  };
  return { ...base, ...overrides };
}
function initTabs() {
  const tagRows = document.getElementById("tag-rows");
  if (tagRows) {
    new OrgTabManager(buildTabConfig(tagRows, {
      ns: "tag",
      nsCapitalized: "Tag",
      entitySingular: "Tag",
      entityPluralLower: "tags",
      entityPluralCap: "Tags",
      emptyIcon: "label",
      customIconKey: "tagCustomIcon",
      deleteWarning: "Pins will NOT be deleted.",
      newForm: { dialogId: "new-tag-form", iconPickerId: "new-tag", colorPickerId: "new-tag-color-picker", colorValueId: "new-tag-color-value", customPreviewId: "new-tag-custom-preview" }
    })).init();
  }
  const catRows = document.getElementById("category-rows");
  if (catRows) {
    new OrgTabManager(buildTabConfig(catRows, {
      ns: "cat",
      nsCapitalized: "Cat",
      cardSelector: ".tag-card[data-category-id]",
      idKey: "categoryId",
      nameKey: "categoryName",
      iconKey: "categoryIcon",
      colorKey: "categoryColor",
      parentsKey: "categoryParents",
      pinCountKey: "categoryPinCount",
      locationCountKey: "categoryLocationCount",
      entitySingular: "Category",
      entityPluralLower: "categories",
      entityPluralCap: "Categories",
      emptyIcon: "category",
      deleteWarning: "Pins and locations will NOT be deleted.",
      newForm: { dialogId: "new-category-form", iconPickerId: "new-cat", colorPickerId: "new-cat-color-picker", colorValueId: "new-cat-color-value", customPreviewId: "new-cat-custom-preview" }
    })).init();
  }
  const statusRows = document.getElementById("status-rows");
  if (statusRows) {
    new OrgTabManager(buildTabConfig(statusRows, {
      ns: "status",
      nsCapitalized: "Status",
      entitySingular: "Status",
      entityPluralLower: "statuses",
      entityPluralCap: "Statuses",
      emptyIcon: "flag",
      isProtected: (id) => {
        const card = document.querySelector(`[data-status-id="${id}"]`);
        return card?.dataset.statusProtected === "true" || card?.dataset.statusProtected === "1";
      },
      newForm: { dialogId: "new-status-form", iconPickerId: "new-status", colorPickerId: "new-status-color-picker", colorValueId: "new-status-color-value", customPreviewId: "new-status-custom-preview" }
    })).init();
  }
  const peopleRows = document.getElementById("people-label-rows");
  if (peopleRows) {
    new OrgTabManager(buildTabConfig(peopleRows, {
      ns: "people",
      nsCapitalized: "People",
      cardSelector: ".tag-card[data-people-id]",
      idKey: "peopleId",
      nameKey: "peopleName",
      iconKey: "peopleIcon",
      colorKey: "peopleColor",
      parentsKey: "peopleParents",
      pinCountKey: "peoplePinCount",
      checkboxSelector: ".people-sel-cb",
      entitySingular: "Label",
      entityPluralLower: "labels",
      entityPluralCap: "Labels",
      emptyIcon: "person",
      bulkEditDialog: {
        dialogId: "people-bulk-edit-dialog",
        titleId: "people-bulk-edit-title",
        confirmId: "people-bulk-edit-confirm",
        iconPickerId: "people-bulk-edit",
        iconNochangeId: "people-bulk-icon-nochange",
        colorPickerId: "people-bulk-color-picker",
        colorValueId: "people-bulk-color-value",
        colorNochangeId: "people-bulk-color-nochange",
        descValueId: "people-bulk-description-value",
        descNochangeId: "people-bulk-description-nochange"
      },
      newForm: { dialogId: "new-people-form", iconPickerId: "new-people", colorPickerId: "new-people-color-picker", colorValueId: "new-people-color-value" }
    })).init();
  }
}
function initOnboarding() {
  const host = document.getElementById("organize-onboarding");
  if (!host)
    return;
  if (host.dataset.standaloneMode)
    return;
  if (!host.dataset.showOnboardingTips)
    return;
  initOnboardingTour({
    prefix: "ul_onboarding_v1_organize",
    hostSelector: "#organize-onboarding",
    retryEvent: "org:tab-changed",
    cards: [
      {
        id: "priority-order",
        icon: "low_priority",
        target: "#priority-explainer",
        eyebrow: "Map display",
        title: "Display order decides which label wins on the map",
        body: "If a pin has multiple tags, categories, or statuses, the highest item in this list provides the icon/color that appears on the map.",
        button: "Open display order",
        watchSelector: '[data-tab="priority"]',
        action: () => {
          document.querySelector('[data-tab="priority"]')?.click();
          document.getElementById("priority-explainer")?.scrollIntoView({ behavior: "smooth", block: "center" });
        },
        ready: () => !!document.getElementById("priority-explainer")
      },
      {
        id: "drag-priority",
        icon: "drag_indicator",
        target: "#priority-list .priority-drag-handle, #priority-list",
        eyebrow: "Reorder visually",
        title: "Drag important labels upward",
        body: "Put more specific labels near the top so the map shows the most meaningful icon when a pin has multiple tags.",
        button: "Go to display order",
        watchSelector: ".priority-drag-handle",
        watchEvent: "pointerdown",
        action: () => {
          document.querySelector('[data-tab="priority"]')?.click();
          document.getElementById("priority-list")?.scrollIntoView({ behavior: "smooth", block: "center" });
        },
        ready: () => !!document.getElementById("priority-list")
      },
      {
        id: "bulk-actions",
        icon: "checklist",
        target: "#org-header-sel-all",
        eyebrow: "Cleanup tools",
        title: "Select multiple labels to merge, edit, or delete in batches",
        body: "Bulk selection is useful when consolidating duplicate tags or applying the same icon/color to a group.",
        button: "Try bulk select",
        watchSelector: "#org-header-sel-all",
        action: () => {
          const btn = document.getElementById("org-header-sel-all");
          btn?.click();
          btn?.focus();
        },
        ready: () => !!document.getElementById("org-header-sel-all")
      }
    ]
  });
}
function initKindChangedListener() {
  const page = document.querySelector(".organize-page");
  const rowUrls = {
    tag: page?.dataset.rowsUrlTag,
    category: page?.dataset.rowsUrlCategory,
    status: page?.dataset.rowsUrlStatus
  };
  document.body.addEventListener("htmx:afterRequest", (e) => {
    const detail = e.detail;
    if (!detail.xhr || !detail.successful)
      return;
    const kindChanged = detail.xhr.getResponseHeader("X-Kind-Changed");
    if (!kindChanged)
      return;
    const url = rowUrls[kindChanged];
    const target = KIND_ROWS_TARGET[kindChanged];
    if (url && target)
      window.htmx?.ajax("GET", url, { target, swap: "innerHTML" });
    const tabKey = KIND_TAB_KEY[kindChanged];
    if (tabKey)
      document.querySelector(`.organize-tab[data-tab="${tabKey}"]`)?.click();
  });
}
function initPinCacheInvalidation() {
  document.body.addEventListener("htmx:afterRequest", (e) => {
    const detail = e.detail;
    if (!detail.xhr || !detail.successful)
      return;
    if (detail.requestConfig?.verb?.toLowerCase() === "get")
      return;
    try {
      localStorage.setItem("ul_pins_dirty", "1");
    } catch {}
    document.body.dispatchEvent(new Event("refreshPriority"));
  });
}
function initConsolidatedDialogOpener() {
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const detail = e.detail;
    const id = detail.target?.id;
    if (!id)
      return;
    if (id === "label-edit-dialog-body") {
      const body = detail.target;
      const titleEl = document.getElementById("label-edit-dialog-title");
      if (titleEl) {
        if (body.querySelector(".organize-label-merge-form")) {
          const mergeName = body.querySelector(".tag-merge-source-name");
          titleEl.textContent = mergeName ? `Merge ${mergeName.textContent?.trim()}` : "Merge";
        } else if (body.querySelector(".organize-label-customize-form")) {
          titleEl.textContent = "Customize Display";
        } else if (body.querySelector(".tag-global-edit-form")) {
          titleEl.textContent = "Edit Global Tag";
        } else {
          const kindInput = body.querySelector('input[name="kind"]:checked');
          const titles = { tag: "Tag", category: "Category", status: "Status" };
          titleEl.textContent = `Edit ${titles[kindInput?.value ?? ""] ?? "Label"}`;
        }
      }
      const dialog = document.getElementById("label-edit-dialog");
      if (dialog && !dialog.open)
        dialog.showModal();
    } else if (id === "people-label-edit-dialog-body") {
      const dialog = document.getElementById("people-label-edit-dialog");
      if (dialog && !dialog.open)
        dialog.showModal();
    }
  });
}
function init() {
  const page = document.querySelector(".organize-page");
  if (!page)
    return;
  installOrgFilterEngine();
  installOrgBulkToolbar();
  createOrganizeHeader(page.dataset.activeTab ?? "tags");
  installOrgTabSwitching();
  installOrgSectionSwitching();
  initConsolidatedDialogOpener();
  initKindChangedListener();
  initPinCacheInvalidation();
  initOnboarding();
  initTabs();
  initOrganizePriority();
  orgHeader.init();
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
