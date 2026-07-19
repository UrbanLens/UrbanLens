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
      btn?.classList.add("selected");
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

export { IconPicker, renderIconGlyphHtml, resetIconPicker, installGlobalIconPicker };
