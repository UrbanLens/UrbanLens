import {
  sortable_esm_default
} from "./photo-location-scan-351ta66q.js";
import {
  getCsrfToken,
  toast
} from "./photo-location-scan-5jnnp4sj.js";
import"./photo-location-scan-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/shared/album-items.ts
var QUEUED_REFRESH_DELAY_MS = 4000;
var albumSortable = null;
function albumPanel() {
  return document.getElementById("albums-panel");
}
async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    throw new Error(await response.text() || response.statusText);
  }
  return await response.json();
}
function refreshPanel() {
  const panel = albumPanel();
  const url = panel?.dataset.refreshUrl;
  if (!url)
    return;
  window.htmx?.ajax("GET", url, { target: "#albums-panel", swap: "outerHTML" });
}
async function saveAlbumOrder(grid) {
  const panel = albumPanel();
  const url = panel?.dataset.reorderUrl;
  if (!url)
    return;
  const items = Array.from(grid.querySelectorAll(".album-item[data-item-id]")).map((el) => Number.parseInt(el.dataset.itemId ?? "0", 10));
  try {
    await postJson(url, { items });
    toast.success("Photo order saved.");
  } catch (err) {
    toast.error(`Could not save order: ${err.message}`);
  }
}
function initAlbumSortable() {
  albumSortable?.destroy();
  albumSortable = null;
  const grid = document.getElementById("album-items-grid");
  if (!grid || grid.dataset.albumSortable !== "1")
    return;
  albumSortable = new sortable_esm_default(grid, {
    animation: 150,
    ghostClass: "album-item--ghost",
    fallbackTolerance: 3,
    onEnd: () => {
      saveAlbumOrder(grid);
    }
  });
}
document.addEventListener("click", (event) => {
  const target = event.target;
  const removeBtn = target.closest(".album-item-remove");
  if (removeBtn) {
    event.preventDefault();
    const url = albumPanel()?.dataset.removeUrl;
    const imageId = Number.parseInt(removeBtn.dataset.imageId ?? "0", 10);
    if (!url || !imageId)
      return;
    postJson(url, { image_ids: [imageId] }).then(() => {
      toast.success("Removed from album.");
      refreshPanel();
    }).catch((err) => toast.error(`Could not remove: ${err.message}`));
    return;
  }
  const addBtn = target.closest(".album-item-add");
  if (addBtn) {
    event.preventDefault();
    const url = albumPanel()?.dataset.addUrl;
    const imageId = Number.parseInt(addBtn.dataset.imageId ?? "0", 10);
    if (!url || !imageId)
      return;
    postJson(url, { image_ids: [imageId] }).then(() => {
      toast.success("Added to album.");
      refreshPanel();
    }).catch((err) => toast.error(`Could not add: ${err.message}`));
  }
});
window.albumAddExternalMedia = async (addUrl, media) => {
  toast.info("Saving photo...");
  try {
    const result = await postJson(addUrl, { media });
    if (result.declined) {
      toast.warning(result.message || "You already marked this photo as not relevant.");
      return;
    }
    if (result.error) {
      toast.error(result.error);
      return;
    }
    if (result.queued) {
      toast.success(result.message || "Saving this photo - it'll appear shortly.");
      window.setTimeout(refreshPanel, QUEUED_REFRESH_DELAY_MS);
      return;
    }
    toast.success("Added to album.");
    refreshPanel();
  } catch (err) {
    toast.error(`Could not add: ${err.message}`);
  }
};
document.body.addEventListener("htmx:afterSwap", () => initAlbumSortable());
initAlbumSortable();
