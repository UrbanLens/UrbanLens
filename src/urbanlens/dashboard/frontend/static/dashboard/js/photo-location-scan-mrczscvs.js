import {
  IconPicker
} from "./photo-location-scan-ee3vhq76.js";

// src/urbanlens/dashboard/frontend/ts/shared/organize-icon-picker.ts
var bulkStateUpdaters = new Map;
function registerBulkStateUpdater(nsPrefix, updater) {
  bulkStateUpdaters.set(nsPrefix, updater);
}
var OrganizeIconPicker = {
  ...IconPicker,
  pick(id, icon, btn) {
    IconPicker.pick(id, icon, btn);
    const clearFlag = document.getElementById(`edit-clear-custom-${id}`);
    if (clearFlag)
      clearFlag.value = "1";
    const uploadInput = document.getElementById(`icon-upload-input-${id}`);
    if (icon && uploadInput)
      uploadInput.value = "";
    if (id.endsWith("-bulk-edit")) {
      const ns = id.slice(0, -"-bulk-edit".length);
      const nochange = document.getElementById(`${ns}-bulk-icon-nochange`);
      if (nochange)
        nochange.checked = false;
      bulkStateUpdaters.get(ns)?.();
    }
  },
  _handleUpload(id, input) {
    const file = input.files?.[0];
    if (!file)
      return;
    const clearFlag = document.getElementById(`edit-clear-custom-${id}`);
    if (clearFlag)
      clearFlag.value = "";
    const iconVal = document.getElementById(`icon-value-${id}`);
    if (iconVal)
      iconVal.value = "";
    document.getElementById(`icon-grid-${id}`)?.querySelectorAll(".icon-picker-item").forEach((b) => b.classList.remove("selected"));
    const reader = new FileReader;
    reader.onload = (e) => {
      const current = document.getElementById(`icon-current-${id}`);
      if (current)
        current.innerHTML = `<img src="${e.target?.result}" class="icon-picker-custom-preview" alt="Custom icon">`;
    };
    reader.readAsDataURL(file);
    document.getElementById(`icon-panel-${id}`)?.setAttribute("hidden", "");
  }
};
function installGlobalOrganizeIconPicker() {
  window.IconPicker = OrganizeIconPicker;
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

export { registerBulkStateUpdater, installGlobalOrganizeIconPicker, resetColorPicker, installGlobalColorPicker };
