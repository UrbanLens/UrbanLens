import {
  htmxProcess
} from "./categories-cm6bs6jx.js";

// src/urbanlens/dashboard/frontend/ts/shared/icon-picker.ts
var MATERIAL_ICON_NAME = /^[a-z_]+$/;
var IconPicker = {
  toggle(id) {
    const panel = document.getElementById(`icon-panel-${id}`);
    if (!panel)
      return;
    const isHidden = panel.hasAttribute("hidden");
    document.querySelectorAll(".icon-picker-panel").forEach((p) => p.setAttribute("hidden", ""));
    if (isHidden) {
      panel.removeAttribute("hidden");
      const search = panel.querySelector(".icon-picker-search-input");
      if (search) {
        search.value = "";
        search.focus();
      }
      IconPicker.setTabSilent(id, "");
    }
  },
  setTabSilent(id, cat) {
    const panel = document.getElementById(`icon-panel-${id}`);
    if (!panel)
      return;
    panel.querySelectorAll(".icon-tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === cat));
    const grid = document.getElementById(`icon-grid-${id}`);
    if (!grid)
      return;
    grid.querySelectorAll(".icon-picker-item").forEach((item) => {
      item.style.display = !cat || item.dataset.cat === cat || !item.dataset.cat ? "" : "none";
    });
  },
  setTab(id, cat, btn) {
    const panel = document.getElementById(`icon-panel-${id}`);
    if (!panel)
      return;
    panel.querySelectorAll(".icon-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const search = panel.querySelector(".icon-picker-search-input");
    if (search)
      search.value = "";
    const grid = document.getElementById(`icon-grid-${id}`);
    if (!grid)
      return;
    grid.querySelectorAll(".icon-picker-item").forEach((item) => {
      item.style.display = !cat || item.dataset.cat === cat || !item.dataset.cat ? "" : "none";
    });
  },
  search(id, query) {
    const q = query.toLowerCase().trim();
    const panel = document.getElementById(`icon-panel-${id}`);
    if (!panel)
      return;
    panel.querySelectorAll(".icon-tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === ""));
    const grid = document.getElementById(`icon-grid-${id}`);
    if (!grid)
      return;
    grid.querySelectorAll(".icon-picker-item").forEach((item) => {
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
  pick(id, icon, btn) {
    const input = document.getElementById(`icon-value-${id}`);
    if (input)
      input.value = icon;
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
    if (panel)
      panel.setAttribute("hidden", "");
  }
};
function renderIconGlyphHtml(icon) {
  if (!icon)
    return '<span class="icon-picker-none-label">No icon</span>';
  return MATERIAL_ICON_NAME.test(icon) ? `<i class="material-icons icon-picker-current-mi">${icon}</i>` : `<span class="icon-picker-current-glyph">${icon}</span>`;
}
function resetIconPicker(pickerId) {
  const input = document.getElementById(`icon-value-${pickerId}`);
  if (input)
    input.value = "";
  const current = document.getElementById(`icon-current-${pickerId}`);
  if (current)
    current.innerHTML = '<span class="icon-picker-none-label">No icon</span>';
  const grid = document.getElementById(`icon-grid-${pickerId}`);
  if (grid) {
    grid.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    grid.querySelector(".icon-picker-none")?.classList.add("selected");
  }
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".icon-picker-dropdown")) {
    document.querySelectorAll(".icon-picker-panel").forEach((p) => p.setAttribute("hidden", ""));
  }
});
function installGlobalIconPicker() {
  window.IconPicker = IconPicker;
}

// src/urbanlens/dashboard/frontend/ts/shared/color-picker.ts
function pickColor(pickerId, valueId, colorHex, btn) {
  const picker = document.getElementById(pickerId);
  picker?.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
  btn.classList.add("selected");
  const value = document.getElementById(valueId);
  if (value)
    value.value = colorHex;
}
function resetColorPicker(pickerId, valueId) {
  document.getElementById(pickerId)?.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
  const value = document.getElementById(valueId);
  if (value)
    value.value = "";
}
function installGlobalColorPicker() {
  window.pickColor = pickColor;
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

export { IconPicker, renderIconGlyphHtml, resetIconPicker, installGlobalIconPicker, resetColorPicker, installGlobalColorPicker, renderTreeView };
