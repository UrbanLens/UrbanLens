import {
  createMapLayers,
  tileLayer
} from "./achievements-rarq1vf2.js";
import {
  confirmAction,
  getCsrfToken,
  htmxProcess,
  toast
} from "./achievements-5jnnp4sj.js";
import"./achievements-2vd5xdaq.js";

// src/urbanlens/dashboard/frontend/ts/entries/map-annotations.ts
function escHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function readConfig(el) {
  const d = el.dataset;
  return {
    mapCenterLat: Number.parseFloat(d.mapCenterLat ?? "0"),
    mapCenterLng: Number.parseFloat(d.mapCenterLng ?? "0"),
    pinSlug: d.pinSlug || "",
    locationSlug: d.locationSlug || "",
    defaultMapView: d.defaultMapView || "satellite",
    openweathermapApiKey: d.openweathermapApiKey || "",
    mainMarkerOwnerUuid: d.mainMarkerOwnerUuid || "",
    markupJsonUrl: d.markupJsonUrl || "",
    markupCreateUrl: d.markupCreateUrl || "",
    markupEditUrlTemplate: d.markupEditUrlTemplate || "",
    detailPinsJsonUrl: d.detailPinsJsonUrl || "",
    detailPinCreateUrl: d.detailPinCreateUrl || "",
    detailPinEditUrlTemplate: d.detailPinEditUrlTemplate || "",
    detailPinsBulkEditUrl: d.detailPinsBulkEditUrl || "",
    pinShareDialogUrl: d.pinShareDialogUrl || "",
    detailPinsSendToWikiUrl: d.detailPinsSendToWikiUrl || "",
    boundaryUrl: d.boundaryUrl || "",
    photoGalleryJsonUrl: d.photoGalleryJsonUrl || "",
    nearbyPinsJsonUrl: d.nearbyPinsJsonUrl || "",
    mediaRelevanceUrl: d.mediaRelevanceUrl || "",
    markupFillOpacity: d.markupFillOpacity ? Number.parseInt(d.markupFillOpacity, 10) : 87,
    markupBorderOpacity: d.markupBorderOpacity ? Number.parseInt(d.markupBorderOpacity, 10) : 100,
    showOnboardingTips: d.showOnboardingTips === "1"
  };
}
function readCustomLayers() {
  try {
    return JSON.parse(document.getElementById("custom-layers-data")?.textContent || "[]");
  } catch {
    return [];
  }
}
function init() {
  const mapEl = document.getElementById("map");
  const configEl = document.getElementById("map-annotations-config");
  if (!mapEl || !configEl)
    return;
  const cfg = readConfig(configEl);
  const mapCenterLat = cfg.mapCenterLat;
  const mapCenterLng = cfg.mapCenterLng;
  window._commentMapDefaultLat = mapCenterLat;
  window._commentMapDefaultLng = mapCenterLng;
  window._openMapScreenshot = function() {
    const context = cfg.pinSlug ? { pinSlug: cfg.pinSlug } : cfg.locationSlug ? { locationSlug: cfg.locationSlug } : null;
    const center = map.getCenter();
    window._openCommentMapComposer({ context, initialView: { lat: center.lat, lng: center.lng, zoom: map.getZoom() } });
  };
  const map = L.map("map", { scrollWheelZoom: false, attributionControl: false }).setView([mapCenterLat, mapCenterLng], 15);
  window.map = map;
  let buildingImportMap = null;
  let buildingSelectMode = false;
  let applyBuildingSelectMode = null;
  window.toggleBuildingImportSelectMode = function() {
    buildingSelectMode = !buildingSelectMode;
    applyBuildingSelectMode?.(buildingSelectMode);
  };
  function initBuildingImportDialog() {
    const dialog = document.getElementById("building-import-dialog");
    const mapElement = document.getElementById("building-import-map");
    const dataElement = document.getElementById("building-import-map-data");
    const form = dialog?.querySelector(".building-import-form");
    if (!dialog || !mapElement || !dataElement || !form)
      return;
    let buildings;
    try {
      buildings = JSON.parse(dataElement.textContent || "[]");
    } catch {
      buildings = [];
    }
    buildingImportMap?.remove();
    const previewMap = L.map(mapElement, { scrollWheelZoom: false, attributionControl: false }).setView([mapCenterLat, mapCenterLng], 16);
    buildingImportMap = previewMap;
    tileLayer("street").addTo(previewMap);
    const checkboxes = Array.from(form.querySelectorAll('input[name="building_keys"]'));
    const checkboxByKey = new Map(checkboxes.map((checkbox) => [checkbox.value, checkbox]));
    const rowByKey = new Map;
    checkboxes.forEach((checkbox) => {
      const row = checkbox.closest(".building-import-item");
      if (row)
        rowByKey.set(checkbox.value, row);
    });
    const selectedCount = form.querySelector("[data-building-selected-count]");
    const selectAll = form.querySelector("[data-building-select-all]");
    const submit = form.querySelector("[data-building-import-submit]");
    const submitLabel = form.querySelector("[data-building-submit-label]");
    const isRestructure = form.dataset.restructure === "1";
    const canSubmitWithoutBuildings = Number.parseInt(form.dataset.nestableCount || "0", 10) > 0;
    const pathsByKey = new Map;
    const boundsByKey = new Map;
    const previewBounds = L.latLngBounds([]);
    const selectedStyle = { color: "#2563eb", weight: 3, fillColor: "#3b82f6", fillOpacity: 0.45, opacity: 1 };
    const unselectedStyle = { color: "#64748b", weight: 2, fillColor: "#94a3b8", fillOpacity: 0.12, opacity: 0.5 };
    const hoverStyle = { color: "#f97316", weight: 4 };
    const styleForKey = (key) => checkboxByKey.get(key)?.checked ? selectedStyle : unselectedStyle;
    let hoveredKey = null;
    const setBuildingHover = (key) => {
      if (hoveredKey === key)
        return;
      if (hoveredKey) {
        rowByKey.get(hoveredKey)?.classList.remove("is-hovered");
        pathsByKey.get(hoveredKey)?.forEach((path) => path.setStyle(styleForKey(hoveredKey)));
      }
      hoveredKey = key;
      if (key) {
        rowByKey.get(key)?.classList.add("is-hovered");
        pathsByKey.get(key)?.forEach((path) => {
          path.setStyle(hoverStyle);
          path.bringToFront();
        });
      }
    };
    const syncSelection = () => {
      let checked = 0;
      checkboxes.forEach((checkbox) => {
        if (checkbox.checked)
          checked += 1;
      });
      pathsByKey.forEach((paths, key) => {
        if (key === hoveredKey)
          return;
        paths.forEach((path) => path.setStyle(styleForKey(key)));
      });
      if (selectedCount)
        selectedCount.textContent = String(checked);
      if (selectAll)
        selectAll.textContent = checked === checkboxes.length ? "Uncheck all" : "Check all";
      if (submit)
        submit.disabled = checked === 0 && !canSubmitWithoutBuildings;
      if (submitLabel && !isRestructure)
        submitLabel.textContent = `Add ${checked} building${checked === 1 ? "" : "s"}`;
    };
    const toggleBuildingKey = (key) => {
      const checkbox = checkboxByKey.get(key);
      if (!checkbox)
        return;
      checkbox.checked = !checkbox.checked;
      syncSelection();
    };
    buildings.forEach((building) => {
      const paths = [];
      let preview = null;
      if (building.geometry) {
        const geoJson = L.geoJSON(building.geometry, {
          style: selectedStyle,
          onEachFeature: (_feature, layer) => {
            if (layer instanceof L.Path)
              paths.push(layer);
          }
        }).addTo(previewMap);
        preview = geoJson;
        const bounds = geoJson.getBounds();
        if (bounds.isValid()) {
          previewBounds.extend(bounds);
          boundsByKey.set(building.selection_key, bounds);
        }
      } else if (building.latitude != null && building.longitude != null) {
        const point = L.circleMarker([building.latitude, building.longitude], { ...selectedStyle, radius: 8 }).addTo(previewMap);
        preview = point;
        paths.push(point);
        previewBounds.extend(point.getLatLng());
        boundsByKey.set(building.selection_key, L.latLngBounds(point.getLatLng(), point.getLatLng()));
      }
      preview?.bindTooltip(building.name || (building.building_number ? `Building ${building.building_number}` : "Unnamed building"));
      preview?.on("mouseover", () => setBuildingHover(building.selection_key));
      preview?.on("mouseout", () => setBuildingHover(null));
      preview?.on("click", () => {
        if (buildingSelectMode)
          toggleBuildingKey(building.selection_key);
      });
      pathsByKey.set(building.selection_key, paths);
    });
    if (previewBounds.isValid())
      previewMap.fitBounds(previewBounds.pad(0.18), { maxZoom: 18 });
    rowByKey.forEach((row, key) => {
      row.addEventListener("mouseenter", () => setBuildingHover(key));
      row.addEventListener("mouseleave", () => setBuildingHover(null));
    });
    checkboxes.forEach((checkbox) => checkbox.addEventListener("change", syncSelection));
    selectAll?.addEventListener("click", () => {
      const shouldCheck = checkboxes.some((checkbox) => !checkbox.checked);
      checkboxes.forEach((checkbox) => {
        checkbox.checked = shouldCheck;
      });
      syncSelection();
    });
    syncSelection();
    buildingSelectMode = false;
    const selectBtn = document.getElementById("select-building-import-button");
    applyBuildingSelectMode = (active) => {
      selectBtn?.classList.toggle("active", active);
      mapElement.classList.toggle("select-mode", active);
      if (active)
        previewMap.dragging.disable();
      else
        previewMap.dragging.enable();
    };
    let dragRect = null;
    mapElement.addEventListener("mousedown", (e) => {
      if (!buildingSelectMode || e.button !== 0)
        return;
      const startLL = previewMap.mouseEventToLatLng(e);
      const startX = e.clientX;
      const startY = e.clientY;
      let dragging = false;
      function onMove(ev) {
        if (!dragging && Math.hypot(ev.clientX - startX, ev.clientY - startY) < 6)
          return;
        dragging = true;
        if (dragRect)
          previewMap.removeLayer(dragRect);
        dragRect = L.rectangle(L.latLngBounds(startLL, previewMap.mouseEventToLatLng(ev)), {
          color: "#1E88E5",
          weight: 2,
          fillOpacity: 0.08,
          dashArray: "4 4",
          interactive: false
        }).addTo(previewMap);
      }
      function onUp(ev) {
        document.removeEventListener("mousemove", onMove);
        if (dragRect) {
          previewMap.removeLayer(dragRect);
          dragRect = null;
        }
        if (!dragging)
          return;
        const bounds = L.latLngBounds(startLL, previewMap.mouseEventToLatLng(ev));
        boundsByKey.forEach((buildingBounds, key) => {
          if (bounds.intersects(buildingBounds))
            toggleBuildingKey(key);
        });
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp, { once: true });
    });
    requestAnimationFrame(() => buildingImportMap?.invalidateSize());
  }
  window.openBuildingImportDialog = function() {
    const dialog = document.getElementById("building-import-dialog");
    if (!dialog)
      return;
    dialog.showModal();
    requestAnimationFrame(initBuildingImportDialog);
  };
  map.createPane("markupPane").style.zIndex = "550";
  map.createPane("boundaryPane").style.zIndex = "540";
  const SCROLL_ZOOM_ENABLE_DELAY_MS = 350;
  let scrollEnableTimer;
  mapEl.addEventListener("mouseenter", () => {
    scrollEnableTimer = setTimeout(() => map.scrollWheelZoom.enable(), SCROLL_ZOOM_ENABLE_DELAY_MS);
  });
  mapEl.addEventListener("mouseleave", () => {
    clearTimeout(scrollEnableTimer);
    map.scrollWheelZoom.disable();
  });
  const markerIcon = L.icon({
    iconUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png",
    shadowUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png",
    iconSize: [25, 41],
    shadowSize: [41, 41],
    iconAnchor: [12, 41],
    shadowAnchor: [12, 41],
    popupAnchor: [1, -34]
  });
  L.Marker.prototype.options.icon = markerIcon;
  let mainMarkerLat = mapCenterLat;
  let mainMarkerLng = mapCenterLng;
  const mainMarker = L.marker([mapCenterLat, mapCenterLng], { draggable: !!cfg.mainMarkerOwnerUuid }).addTo(map);
  if (cfg.mainMarkerOwnerUuid) {
    const savePosition = (lat, lng, confirmWikiLoss) => fetch(`/dashboard/rest/pins/${cfg.mainMarkerOwnerUuid}/`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify({
        latitude: lat.toFixed(6),
        longitude: lng.toFixed(6),
        ...confirmWikiLoss ? { confirm_wiki_loss: true } : {}
      })
    });
    mainMarker.on("dragend", () => {
      const pos = mainMarker.getLatLng();
      (async () => {
        try {
          let response = await savePosition(pos.lat, pos.lng, false);
          if (response.status === 409) {
            const payload = await response.json();
            const names = (payload.wikis ?? []).map((w) => w.name).join(", ");
            const confirmed = await confirmAction({
              title: "Move this pin?",
              message: names ? `You'll no longer see the community page for ${names}. Moving the pin back inside will restore it.` : "You'll no longer see this place's community page. Moving the pin back inside will restore it.",
              confirmLabel: "Move anyway",
              cancelLabel: "Cancel"
            });
            if (!confirmed) {
              mainMarker.setLatLng([mainMarkerLat, mainMarkerLng]);
              return;
            }
            response = await savePosition(pos.lat, pos.lng, true);
          }
          if (!response.ok)
            throw new Error;
          mainMarkerLat = pos.lat;
          mainMarkerLng = pos.lng;
          toast.success("Pin moved.");
        } catch {
          toast.error("Failed to save new position.");
          mainMarker.setLatLng([mainMarkerLat, mainMarkerLng]);
        }
      })();
    });
  }
  setTimeout(() => map.invalidateSize(), 300);
  (() => {
    let resizeTimer;
    function onResize() {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => map.invalidateSize(), 150);
    }
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", () => setTimeout(() => map.invalidateSize(), 300));
  })();
  (() => {
    const wrapper = document.getElementById("pin-detail-map-wrapper");
    const handle = document.getElementById("pin-detail-map-resize-handle");
    if (!wrapper || !handle)
      return;
    const MIN_HEIGHT_PX = 320;
    const MAX_HEIGHT_PX = 1200;
    let startY = 0;
    let startHeight = 0;
    function onPointerMove(e) {
      const delta = e.clientY - startY;
      const newHeight = Math.max(MIN_HEIGHT_PX, Math.min(MAX_HEIGHT_PX, startHeight + delta));
      wrapper.style.height = `${newHeight}px`;
      map.invalidateSize();
    }
    function onPointerUp() {
      handle.classList.remove("is-dragging");
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerup", onPointerUp);
      const finalHeight = Math.round(wrapper.getBoundingClientRect().height);
      fetch("/dashboard/map/pin/map-height/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ height: finalHeight })
      }).catch(() => {
        toast.error("Failed to save map size.");
      });
    }
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      startY = e.clientY;
      startHeight = wrapper.getBoundingClientRect().height;
      handle.classList.add("is-dragging");
      document.addEventListener("pointermove", onPointerMove);
      document.addEventListener("pointerup", onPointerUp);
    });
  })();
  const detailPinColors = { parcel: "#0f766e", building: "#6b7280", entrance: "#16a34a", poi: "#d97706", danger: "#dc2626", other: "#7c3aed", location: "#2563eb" };
  const detailPinIcons = { parcel: "crop_free", building: "business", entrance: "door_front", poi: "star", danger: "warning", other: "info", location: "place" };
  const detailPinLayer = L.layerGroup();
  const markupLayer = L.layerGroup();
  const detailsLayer = L.layerGroup([detailPinLayer, markupLayer]).addTo(map);
  const photoLayer = L.layerGroup().addTo(map);
  const nearbyLayer = L.layerGroup();
  let nearbyActive = false;
  let nearbyFetchPromise = null;
  function buildNearbyMarker(pin) {
    if (pin.latitude == null || pin.longitude == null)
      return null;
    const iconName = pin.icon || "place";
    const inner = /^[a-z_]+$/.test(iconName) ? `<i class="material-icons nearby-pin-icon">${escHtml(iconName)}</i>` : `<span class="nearby-pin-emoji">${escHtml(iconName)}</span>`;
    const marker = L.marker([pin.latitude, pin.longitude], {
      icon: L.divIcon({ className: "nearby-pin-marker-wrap", html: `<span class="nearby-pin-marker">${inner}</span>`, iconSize: [26, 26], iconAnchor: [13, 13] })
    });
    marker.bindPopup(`
            <div class="pin-popup nearby-pin-popup">
                <div class="popup-title">${escHtml(pin.name || "Pin")}</div>
                <div class="popup-actions"><a href="${escHtml(pin.url || "#")}" class="view-full-pin">View Details</a></div>
            </div>`);
    return marker;
  }
  function loadNearbyPins() {
    if (!cfg.nearbyPinsJsonUrl)
      return Promise.resolve();
    nearbyFetchPromise = fetch(cfg.nearbyPinsJsonUrl, { headers: { "X-Requested-With": "XMLHttpRequest" } }).then((r) => r.ok ? r.json() : { pins: [] }).then((data) => {
      nearbyLayer.clearLayers();
      (data.pins || []).forEach((pin) => {
        const m = buildNearbyMarker(pin);
        if (m)
          nearbyLayer.addLayer(m);
      });
    }).catch(() => {});
    return nearbyFetchPromise;
  }
  function setNearbyActive(on) {
    if (on === nearbyActive)
      return;
    nearbyActive = on;
    if (on) {
      nearbyLayer.addTo(map);
      if (!nearbyFetchPromise)
        loadNearbyPins();
    } else {
      map.removeLayer(nearbyLayer);
    }
  }
  const customLayers = readCustomLayers();
  const customLayerGroups = new Map;
  const customLayerToggles = {};
  customLayers.forEach((layer) => {
    const group = L.layerGroup();
    if (layer.default_visible)
      group.addTo(map);
    customLayerGroups.set(layer.uuid, group);
    customLayerToggles[`layer-${layer.uuid}`] = {
      isActive: () => map.hasLayer(group),
      toggle: () => map.hasLayer(group) ? map.removeLayer(group) : group.addTo(map)
    };
  });
  const mapLayersInstance = createMapLayers(map, {
    root: document.getElementById("detail-map-layers"),
    apiKey: cfg.openweathermapApiKey || null,
    defaultBase: cfg.defaultMapView,
    onAttribution: (text) => {
      const el = document.getElementById("page-footer-attribution-text");
      if (el)
        el.textContent = text;
    },
    custom: {
      details: {
        isActive: () => map.hasLayer(detailsLayer),
        toggle: () => map.hasLayer(detailsLayer) ? map.removeLayer(detailsLayer) : detailsLayer.addTo(map)
      },
      photos: {
        isActive: () => map.hasLayer(photoLayer),
        toggle: () => map.hasLayer(photoLayer) ? map.removeLayer(photoLayer) : photoLayer.addTo(map)
      },
      nearby: {
        isActive: () => nearbyActive,
        toggle: () => setNearbyActive(!nearbyActive)
      },
      ...customLayerToggles
    }
  });
  const customLayersMenu = document.querySelector("#detail-map-layers [data-layers-menu]");
  const customLayersManageBtn = customLayersMenu?.querySelector(".map-layers-manage-btn") ?? null;
  function customLayerButtonLabel(layer) {
    return `<span class="map-layer-thumb map-layer-thumb--icon"><i class="material-symbols-outlined">${escHtml(layer.icon || "layers")}</i></span><span>${escHtml(layer.name)}</span>`;
  }
  function syncCustomLayers(fresh) {
    const freshUuids = new Set(fresh.map((layer) => layer.uuid));
    customLayers.filter((layer) => !freshUuids.has(layer.uuid)).forEach((layer) => {
      const group = customLayerGroups.get(layer.uuid);
      if (group && map.hasLayer(group))
        map.removeLayer(group);
      customLayerGroups.delete(layer.uuid);
      customLayersMenu?.querySelector(`[data-map-layer="layer-${layer.uuid}"]`)?.remove();
      customLayers.splice(customLayers.indexOf(layer), 1);
    });
    fresh.forEach((layer) => {
      const key = `layer-${layer.uuid}`;
      const existing = customLayers.find((l) => l.uuid === layer.uuid);
      if (existing) {
        Object.assign(existing, layer);
      } else {
        const group = L.layerGroup();
        if (layer.default_visible)
          group.addTo(map);
        customLayerGroups.set(layer.uuid, group);
        mapLayersInstance.registerToggle(key, {
          isActive: () => map.hasLayer(group),
          toggle: () => map.hasLayer(group) ? map.removeLayer(group) : group.addTo(map)
        });
        customLayers.push(layer);
      }
      if (!customLayersMenu)
        return;
      let btn = customLayersMenu.querySelector(`[data-map-layer="${key}"]`);
      if (!btn) {
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = "map-layer-btn";
        btn.dataset.mapLayer = key;
        btn.dataset.layerKind = "custom";
        btn.addEventListener("click", () => mapLayersInstance.toggleCustom(key));
      }
      btn.innerHTML = customLayerButtonLabel(layer);
      btn.setAttribute("aria-label", `Show or hide ${layer.name}`);
      btn.setAttribute("data-tooltip", layer.name);
      btn.setAttribute("data-tooltip-float", "true");
      btn.setAttribute("data-tooltip-pos", "top");
      customLayersMenu.insertBefore(btn, customLayersManageBtn);
    });
    mapLayersInstance.syncButtons();
  }
  document.body.addEventListener("ul:custom-layers-changed", (e) => {
    syncCustomLayers(e.detail?.layers || []);
  });
  const dpEditBase = cfg.detailPinEditUrlTemplate.replace("00000000-0000-0000-0000-000000000000/", "");
  let detailPins = [];
  let highlightedDpUuid = null;
  let photoPanelItems = [];
  const photoMarkers = {};
  function hexToRgb(hex) {
    const r = Number.parseInt(hex.slice(1, 3), 16);
    const g = Number.parseInt(hex.slice(3, 5), 16);
    const b = Number.parseInt(hex.slice(5, 7), 16);
    return `${r},${g},${b}`;
  }
  function detailIcon(dp, highlighted) {
    const pinType = dp.pin_type || "location";
    const color = dp.color || detailPinColors[pinType] || "#2563eb";
    const icon = dp.icon || detailPinIcons[pinType] || "place";
    const bgColor = dp.bg_color || null;
    const bgOp = bgColor ? (dp.bg_opacity != null ? dp.bg_opacity : 80) / 100 : 0;
    const bdColor = dp.border_color || null;
    const bdOp = bdColor ? (dp.border_opacity != null ? dp.border_opacity : 100) / 100 : 0;
    const hasCircle = !!(bgColor || bdColor);
    const size = highlighted ? 32 : 24;
    const pad = hasCircle ? 5 : 0;
    const total = size + pad * 2;
    const bgStyle = bgColor ? `background:rgba(${hexToRgb(bgColor)},${bgOp});` : "";
    const bdStyle = bdColor ? `border:2px solid rgba(${hexToRgb(bdColor)},${bdOp});` : "";
    const ring = highlighted ? `<span style="position:absolute;inset:-5px;border:2.5px solid ${color};border-radius:50%;opacity:.55;pointer-events:none;"></span>` : "";
    return L.divIcon({
      className: "",
      html: `<span style="position:relative;display:inline-flex;align-items:center;justify-content:center;border-radius:50%;${bgStyle}${bdStyle}padding:${pad}px;">${ring}<span class="material-icons detail-map-icon" style="color:${color};font-size:${size}px;line-height:1;">${icon}</span></span>`,
      iconSize: [total, total],
      iconAnchor: [total / 2, total],
      popupAnchor: [0, -total - 2],
      tooltipAnchor: [0, -total / 2]
    });
  }
  function highlightDetailPin(uuid) {
    clearDetailPinHighlight();
    highlightedDpUuid = uuid;
    const dp = detailPins.find((d) => d.uuid === uuid);
    if (!dp?.marker)
      return;
    dp.marker.setIcon(detailIcon(dp, true));
    map.panTo(dp.marker.getLatLng());
    document.querySelectorAll(".detail-pin-list-item").forEach((li) => {
      li.classList.toggle("is-highlighted", li.dataset.uuid === uuid);
    });
  }
  function clearDetailPinHighlight() {
    if (highlightedDpUuid) {
      const dp = detailPins.find((d) => d.uuid === highlightedDpUuid);
      if (dp?.marker)
        dp.marker.setIcon(detailIcon(dp, false));
      highlightedDpUuid = null;
    }
    document.querySelectorAll(".detail-pin-list-item").forEach((li) => li.classList.remove("is-highlighted"));
  }
  function refreshPanelHeader() {
    const handle = document.getElementById("detail-pin-list-handle");
    const countLabel = document.getElementById("detail-pin-count-label");
    const total = detailPins.length + toolbar.getMarkupItems().length + photoPanelItems.length;
    if (countLabel)
      countLabel.textContent = `${total} Item${total === 1 ? "" : "s"}`;
    if (handle)
      handle.style.display = total ? "" : "none";
    refreshDetailPinSelectButton();
  }
  function buildDetailList() {
    const ul = document.getElementById("detail-pin-list-ul");
    if (!ul)
      return;
    refreshPanelHeader();
    ul.innerHTML = "";
    detailPins.forEach((dp) => {
      const color = dp.color || detailPinColors[dp.pin_type] || "#2563eb";
      const icon = dp.icon || detailPinIcons[dp.pin_type] || "place";
      const li = document.createElement("li");
      li.className = "detail-pin-list-item";
      li.dataset.uuid = dp.uuid;
      li.dataset.kind = "pin";
      const meta = dp.owner_name ? `<span class="detail-pin-list-item-meta">in ${escHtml(dp.owner_name)}</span>` : dp.added_by ? `<span class="detail-pin-list-item-meta">by ${dp.is_mine ? "you" : escHtml(dp.added_by)}</span>` : "";
      li.innerHTML = `
                <span class="material-icons detail-pin-list-item-icon" style="color:${escHtml(color)}">${escHtml(icon)}</span>
                <span class="detail-pin-list-item-name">${escHtml(dp.name)}</span>
                ${meta}
                ${dp.owner_name ? "" : `<button type="button" class="detail-pin-list-item-delete" title="Delete pin"><i class="material-symbols-outlined">close</i></button>`}`;
      li.addEventListener("click", (e) => {
        if (e.target.closest(".detail-pin-list-item-delete"))
          return;
        highlightDetailPin(dp.uuid);
        if (!dp.owner_name)
          openDetailPinEditDialog(dp);
      });
      li.querySelector(".detail-pin-list-item-delete")?.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!await confirmAction({ title: "Delete Pin", message: `Delete "${dp.name}"?`, confirmLabel: "Delete" }))
          return;
        fetch(`${dpEditBase}${dp.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } }).then((r) => {
          if (!r.ok)
            throw new Error;
        }).then(() => {
          toast.success("Detail pin deleted.");
          loadDetailPins();
          fetchBoundaries(0);
        }).catch(() => toast.error("Failed to delete detail pin."));
      });
      ul.appendChild(li);
    });
    const markupIcon = { line: "show_chart", arrow: "arrow_forward", text: "title", square: "crop_square", circle: "circle", polygon: "format_shapes" };
    toolbar.getMarkupItems().forEach((item) => {
      const li = document.createElement("li");
      li.className = "detail-pin-list-item";
      li.dataset.uuid = item.uuid;
      li.dataset.kind = "markup";
      const displayName = item.label || item.markup_type.charAt(0).toUpperCase() + item.markup_type.slice(1);
      const ownerMeta = item.owner_name ? `<span class="detail-pin-list-item-meta">in ${escHtml(item.owner_name)}</span>` : "";
      const layerPicker = !item.owner_name && customLayers.length ? `<select class="detail-pin-list-item-layer" title="Layer" aria-label="Layer">
                        <option value=""${item.layer_uuid ? "" : " selected"}>No layer</option>
                        ${customLayers.map((layer) => `<option value="${escHtml(layer.uuid)}"${item.layer_uuid === layer.uuid ? " selected" : ""}>${escHtml(layer.name)}</option>`).join("")}
                       </select>` : "";
      li.innerHTML = `
                <span class="material-icons detail-pin-list-item-icon" style="color:${escHtml(item.color)}">${escHtml(markupIcon[item.markup_type] || "edit")}</span>
                <span class="detail-pin-list-item-name">${escHtml(displayName)}</span>
                ${ownerMeta}
                ${layerPicker}
                ${item.owner_name ? "" : `<button type="button" class="detail-pin-list-item-delete" title="Delete"><i class="material-symbols-outlined">close</i></button>`}`;
      li.addEventListener("click", (e) => {
        if (e.target.closest(".detail-pin-list-item-delete, .detail-pin-list-item-layer"))
          return;
        if (item.owner_name)
          return;
        toolbar.openMarkupEditDialog(item);
      });
      li.querySelector(".detail-pin-list-item-layer")?.addEventListener("click", (e) => e.stopPropagation());
      li.querySelector(".detail-pin-list-item-layer")?.addEventListener("change", (e) => {
        toolbar.setItemLayer(item.uuid, e.target.value || null);
      });
      li.querySelector(".detail-pin-list-item-delete")?.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!await confirmAction({ title: "Delete Item", message: `Delete this ${item.markup_type}?`, confirmLabel: "Delete" }))
          return;
        fetch(`${cfg.markupEditUrlTemplate.replace("00000000-0000-0000-0000-000000000000/", "")}${item.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } }).then((r) => {
          if (!r.ok)
            throw new Error;
        }).then(() => {
          toast.success("Markup deleted.");
          toolbar.loadMarkup();
        }).catch(() => toast.error("Failed to delete markup."));
      });
      ul.appendChild(li);
    });
  }
  function toggleDetailPinListPanel() {
    const panel = document.getElementById("detail-pin-list-panel");
    const handle = document.getElementById("detail-pin-list-handle");
    if (!panel)
      return;
    const isOpen = panel.classList.toggle("open");
    if (handle) {
      handle.classList.toggle("open", isOpen);
      handle.setAttribute("aria-expanded", String(isOpen));
      const icon = handle.querySelector(".material-symbols-outlined, .material-icons");
      if (icon)
        icon.textContent = isOpen ? "chevron_left" : "chevron_right";
    }
  }
  window._toggleDetailPinListPanel = toggleDetailPinListPanel;
  const SAT_LAST_SOURCE_KEY = "ul_sat_last_source";
  function _satRememberSource(source) {
    if (!source)
      return;
    try {
      localStorage.setItem(SAT_LAST_SOURCE_KEY, source);
    } catch {}
  }
  function _satLastSource() {
    try {
      return localStorage.getItem(SAT_LAST_SOURCE_KEY);
    } catch {
      return null;
    }
  }
  let _satIdx = 0;
  function _satSlides() {
    const c = document.getElementById("sat-carousel");
    return c ? Array.from(c.querySelectorAll(".sat-slide")) : [];
  }
  function _satShow(idx) {
    const slides = _satSlides();
    if (!slides.length)
      return;
    _satIdx = (idx % slides.length + slides.length) % slides.length;
    slides.forEach((s, i) => s.classList.toggle("is-active", i === _satIdx));
    const active = slides[_satIdx];
    if (!active)
      return;
    const source = document.querySelector("#sat-carousel .sat-source");
    const date = document.querySelector("#sat-carousel .sat-date");
    const detail = document.querySelector("#sat-carousel .sat-detail");
    if (source)
      source.textContent = active.dataset.source || "";
    if (date)
      date.textContent = active.dataset.date || "";
    if (detail)
      detail.textContent = active.dataset.detail || "";
    _satRememberSource(active.dataset.source || "");
    _satRebuildDots(slides.length);
  }
  function _satRebuildDots(count) {
    const prev = document.querySelector("#sat-carousel .sat-prev");
    const next = document.querySelector("#sat-carousel .sat-next");
    if (prev)
      prev.hidden = count <= 1;
    if (next)
      next.hidden = count <= 1;
    const el = document.getElementById("sat-dots");
    if (!el)
      return;
    el.innerHTML = "";
    for (let i = 0;i < count; i++) {
      const dot = document.createElement("button");
      dot.type = "button";
      dot.className = "sat-dot" + (i === _satIdx ? " is-active" : "");
      dot.setAttribute("aria-label", `Slide ${i + 1}`);
      dot.addEventListener("click", () => _satShow(i));
      el.appendChild(dot);
    }
  }
  window._satRemoveSlide = function(img) {
    const slide = img.closest(".sat-slide");
    if (!slide)
      return;
    const wasActive = slide.classList.contains("is-active");
    slide.remove();
    const slides = _satSlides();
    if (!slides.length) {
      const c = document.getElementById("sat-carousel");
      if (c) {
        c.innerHTML = '<div class="view-unavailable"><i class="material-symbols-outlined">broken_image</i>' + "<span>No satellite imagery available for this location.</span></div>";
      }
      return;
    }
    if (wasActive)
      _satIdx = Math.max(0, Math.min(_satIdx, slides.length - 1));
    _satShow(_satIdx);
  };
  window._satPrev = function() {
    _satShow(_satIdx - 1);
  };
  window._satNext = function() {
    _satShow(_satIdx + 1);
  };
  window._satShowRemembered = function() {
    const slides = _satSlides();
    if (!slides.length)
      return;
    const lastSource = _satLastSource();
    const idx = lastSource ? slides.findIndex((s) => s.dataset.source === lastSource) : -1;
    _satShow(idx >= 0 ? idx : 0);
  };
  window._satShow = _satShow;
  function _svSwapToStatic(slide) {
    const iframe = slide.querySelector(".sv-embed");
    const staticImg = slide.querySelector(".sv-img--fallback");
    const btn = slide.querySelector(".sv-embed-fallback-btn");
    if (iframe)
      iframe.hidden = true;
    if (staticImg)
      staticImg.hidden = false;
    if (btn)
      btn.hidden = true;
  }
  let _svIdx = 0;
  function _svSlides() {
    const c = document.getElementById("sv-carousel");
    return c ? Array.from(c.querySelectorAll(".sv-slide")) : [];
  }
  function _svShow(idx) {
    const slides = _svSlides();
    if (!slides.length)
      return;
    _svIdx = (idx % slides.length + slides.length) % slides.length;
    slides.forEach((s, i) => s.classList.toggle("is-active", i === _svIdx));
    const active = slides[_svIdx];
    if (!active)
      return;
    const source = document.querySelector("#sv-carousel .sv-source");
    const date = document.querySelector("#sv-carousel .sv-date");
    const heading = document.querySelector("#sv-carousel .sv-heading");
    if (source)
      source.textContent = active.dataset.source || "";
    if (date)
      date.textContent = active.dataset.date || "";
    if (heading)
      heading.textContent = active.dataset.heading !== undefined ? `⇨ ${active.dataset.heading}°` : "";
    _svRebuildDots(slides.length);
  }
  function _svRebuildDots(count) {
    const prev = document.querySelector("#sv-carousel .sv-prev");
    const next = document.querySelector("#sv-carousel .sv-next");
    if (prev)
      prev.hidden = count <= 1;
    if (next)
      next.hidden = count <= 1;
    const el = document.getElementById("sv-dots");
    if (!el)
      return;
    el.innerHTML = "";
    for (let i = 0;i < count; i++) {
      const dot = document.createElement("button");
      dot.type = "button";
      dot.className = "sv-dot" + (i === _svIdx ? " is-active" : "");
      dot.setAttribute("aria-label", `Slide ${i + 1}`);
      dot.addEventListener("click", () => _svShow(i));
      el.appendChild(dot);
    }
  }
  window._svShowStaticFallback = function(btn) {
    const slide = btn.closest(".sv-slide");
    if (!slide)
      return;
    _svSwapToStatic(slide);
  };
  window._svRemoveSlide = function(img) {
    const slide = img.closest(".sv-slide");
    if (!slide)
      return;
    const wasActive = slide.classList.contains("is-active");
    slide.remove();
    const slides = _svSlides();
    if (!slides.length) {
      const c = document.getElementById("sv-carousel");
      if (c) {
        c.innerHTML = '<div class="view-unavailable"><i class="material-symbols-outlined">broken_image</i>' + "<span>No street-level imagery available for this location.</span></div>";
      }
      return;
    }
    if (wasActive)
      _svIdx = Math.max(0, Math.min(_svIdx, slides.length - 1));
    _svShow(_svIdx);
  };
  window._svPrev = function() {
    _svShow(_svIdx - 1);
  };
  window._svNext = function() {
    _svShow(_svIdx + 1);
  };
  window._svShow = _svShow;
  async function promotePinToParent(entry) {
    if (!entry.slug || !entry.url)
      return;
    if (!await confirmAction({ title: "Make this the parent pin?", message: `"${entry.name || "This pin"}" will become the parent, and the current pin will become its child. Everything else - name, notes, reviews, photos, visit history - stays with each pin.`, confirmLabel: "Swap" })) {
      return;
    }
    fetch(`/dashboard/map/pin/${encodeURIComponent(entry.slug)}/swap-parent/`, {
      method: "POST",
      headers: { "X-CSRFToken": getCsrfToken() }
    }).then((r) => r.json().then((data) => ({ ok: r.ok, data }))).then(({ ok, data }) => {
      if (!ok) {
        toast.error(data.error || "Could not swap these pins.");
        return;
      }
      toast.success("Pins swapped - taking you to the new parent pin.");
      window.location.href = entry.url;
    }).catch(() => toast.error("Could not swap these pins."));
  }
  function detailPinPopupContent(entry) {
    const el = document.createElement("div");
    el.className = "pin-popup child-pin-popup";
    const owner = entry.owner_name ? `<div class="popup-child-parent"><i class="material-symbols-outlined">subdirectory_arrow_right</i> Inside ${escHtml(entry.owner_name)}</div>` : "";
    el.innerHTML = `
            <div class="popup-title">${escHtml(entry.name || "Child pin")}</div>
            ${owner}
            ${entry.description ? `<div class="popup-desc">${escHtml(entry.description)}</div>` : ""}
            <div class="popup-actions">
                ${entry.url ? `<a href="${escHtml(entry.url)}" class="view-full-pin">View Details</a>` : ""}
            </div>`;
    if (!entry.owner_name) {
      const actions = el.querySelector(".popup-actions");
      const promoteBtn = document.createElement("button");
      promoteBtn.type = "button";
      promoteBtn.className = "promote-pin-button";
      promoteBtn.title = "Make this the parent pin";
      promoteBtn.innerHTML = '<i class="material-symbols-outlined">swap_vert</i>';
      promoteBtn.addEventListener("click", () => {
        map.closePopup();
        promotePinToParent(entry);
      });
      actions.appendChild(promoteBtn);
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "edit-pin-button";
      editBtn.title = "Edit child pin";
      editBtn.innerHTML = '<i class="material-symbols-outlined">edit</i>';
      editBtn.addEventListener("click", () => {
        map.closePopup();
        openDetailPinEditDialog(entry);
      });
      actions.appendChild(editBtn);
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "delete-button";
      deleteBtn.title = "Delete child pin";
      deleteBtn.innerHTML = '<i class="material-symbols-outlined">delete</i>';
      deleteBtn.addEventListener("click", async () => {
        map.closePopup();
        if (!await confirmAction({ title: "Delete Pin", message: `Delete "${entry.name || "this pin"}"?`, confirmLabel: "Delete" }))
          return;
        fetch(`${dpEditBase}${entry.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } }).then((r) => {
          if (!r.ok)
            throw new Error;
        }).then(() => {
          toast.success("Detail pin deleted.");
          loadDetailPins();
          fetchBoundaries(0);
        }).catch(() => toast.error("Failed to delete detail pin."));
      });
      actions.appendChild(deleteBtn);
    }
    return el;
  }
  function loadDetailPins() {
    fetch(cfg.detailPinsJsonUrl).then((r) => r.json()).then((data) => {
      detailPinLayer.clearLayers();
      highlightedDpUuid = null;
      detailPins = [];
      (data.detail_pins || []).forEach((dp) => {
        if (!dp.latitude || !dp.longitude)
          return;
        const entry = {
          uuid: dp.uuid,
          slug: dp.slug,
          url: dp.url,
          owner_name: dp.owner_name,
          name: dp.name,
          pin_type: dp.pin_type,
          icon: dp.icon,
          color: dp.color,
          bg_color: dp.bg_color || "",
          bg_opacity: dp.bg_opacity,
          border_color: dp.border_color || "",
          border_opacity: dp.border_opacity,
          description: dp.description || "",
          added_by: dp.added_by || "",
          is_mine: !!dp.is_mine,
          latitude: dp.latitude,
          longitude: dp.longitude,
          marker: null
        };
        const marker = L.marker([dp.latitude, dp.longitude], { icon: detailIcon(entry), draggable: !entry.owner_name });
        if (entry.url) {
          marker.bindPopup(detailPinPopupContent(entry));
        } else {
          marker.on("click", () => openDetailPinEditDialog(entry));
        }
        marker.on("click", (e) => {
          if (!detailSelectMode || entry.owner_name)
            return;
          marker.closePopup();
          L.DomEvent.stop(e);
          toggleDpSelection(entry.uuid);
        });
        marker.on("dragend", () => {
          const pos = marker.getLatLng();
          fetch(`${dpEditBase}${dp.uuid}/`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify({ latitude: pos.lat.toFixed(6), longitude: pos.lng.toFixed(6) })
          }).then((r) => {
            if (!r.ok)
              throw new Error;
            return r.json();
          }).then(() => {
            entry.latitude = pos.lat;
            entry.longitude = pos.lng;
            toast.success("Pin moved.");
            fetchBoundaries(0);
          }).catch(() => {
            toast.error("Failed to save new position.");
            marker.setLatLng([entry.latitude, entry.longitude]);
          });
        });
        marker.addTo(detailPinLayer);
        entry.marker = marker;
        detailPins.push(entry);
      });
      buildDetailList();
    }).catch((err) => console.warn("Could not load detail pins:", err));
  }
  let detailSelectMode = false;
  const selectedDpUuids = new Set;
  let dpDragSelectRect = null;
  function detailSelectableEntries() {
    return detailPins.filter((d) => !d.owner_name);
  }
  function refreshDetailPinSelectButton() {
    const btn = document.getElementById("select-detail-pins-button");
    if (!btn)
      return;
    if (!cfg.pinSlug) {
      btn.remove();
      return;
    }
    const hasSelectable = detailSelectableEntries().length > 0;
    btn.disabled = !hasSelectable;
    btn.setAttribute("data-tooltip", hasSelectable ? "Select multiple child pins" : "This pin has no child pins to select");
    if (!hasSelectable && detailSelectMode)
      exitDetailPinSelectMode();
  }
  function toggleDetailPinSelectMode() {
    if (detailSelectMode)
      exitDetailPinSelectMode();
    else
      enterDetailPinSelectMode();
  }
  window.toggleDetailPinSelectMode = toggleDetailPinSelectMode;
  function enterDetailPinSelectMode() {
    if (detailSelectMode || !detailSelectableEntries().length)
      return;
    detailSelectMode = true;
    document.getElementById("select-detail-pins-button")?.classList.add("active");
    document.getElementById("map")?.classList.add("select-mode");
    map.dragging.disable();
  }
  function exitDetailPinSelectMode() {
    if (!detailSelectMode)
      return;
    detailSelectMode = false;
    document.getElementById("select-detail-pins-button")?.classList.remove("active");
    document.getElementById("map")?.classList.remove("select-mode");
    map.dragging.enable();
    clearDpSelection();
  }
  function toggleDpSelection(uuid) {
    if (selectedDpUuids.has(uuid))
      selectedDpUuids.delete(uuid);
    else
      selectedDpUuids.add(uuid);
    const dp = detailPins.find((d) => d.uuid === uuid);
    dp?.marker?.getElement()?.classList.toggle("is-selected", selectedDpUuids.has(uuid));
    renderDetailBulkToolbar();
  }
  function clearDpSelection() {
    selectedDpUuids.forEach((uuid) => {
      detailPins.find((d) => d.uuid === uuid)?.marker?.getElement()?.classList.remove("is-selected");
    });
    selectedDpUuids.clear();
    window.ulBulkToolbar?.clear("detailpins");
  }
  function renderDetailBulkToolbar() {
    const n = selectedDpUuids.size;
    window.ulBulkToolbar?.sync("detailpins", n, n ? {
      ...cfg.detailPinsBulkEditUrl ? { edit: openSelectedDpBulkEditDialog } : {},
      promote: doPromoteSelectedDp,
      ...cfg.pinShareDialogUrl ? { share: doShareSelectedDp } : {},
      ...cfg.detailPinsSendToWikiUrl ? { wiki: doSendSelectedDpToWiki } : {},
      delete: doDeleteSelectedDp,
      deselect: clearDpSelection
    } : {});
  }
  function resetSelectedDpBulkEditDialog() {
    document.querySelectorAll("[data-dp-bulk-picker]").forEach((picker) => {
      delete picker.dataset.dpBulkValue;
      picker.querySelectorAll(".dp-icon-btn--active, .dp-color-swatch--active").forEach((button) => {
        button.classList.remove("dp-icon-btn--active", "dp-color-swatch--active");
      });
    });
    for (const [kind, defaultValue] of [
      ["bg", "80"],
      ["border", "100"]
    ]) {
      const enabled = document.getElementById(`detail-pin-bulk-${kind}-opacity-enabled`);
      const range = document.getElementById(`detail-pin-bulk-${kind}-opacity`);
      if (enabled)
        enabled.checked = false;
      if (range) {
        range.value = defaultValue;
        range.disabled = true;
      }
      const value = document.getElementById(`detail-pin-bulk-${kind}-opacity-value`);
      if (value)
        value.textContent = defaultValue;
    }
  }
  function openSelectedDpBulkEditDialog() {
    const dialog = document.getElementById("detail-pin-bulk-edit-dialog");
    if (!dialog || !selectedDpUuids.size)
      return;
    resetSelectedDpBulkEditDialog();
    const title = document.getElementById("detail-pin-bulk-edit-title");
    if (title)
      title.textContent = `Edit ${selectedDpUuids.size} child pin${selectedDpUuids.size === 1 ? "" : "s"}`;
    dialog.showModal();
  }
  document.querySelectorAll("[data-dp-bulk-picker]").forEach((picker) => {
    picker.addEventListener("click", (event) => {
      const button = event.target.closest("[data-dp-bulk-value]");
      if (!button || !picker.contains(button))
        return;
      picker.dataset.dpBulkValue = button.dataset.dpBulkValue ?? "";
      picker.querySelectorAll(".dp-icon-btn, .dp-color-swatch").forEach((candidate) => {
        candidate.classList.toggle("dp-icon-btn--active", candidate === button && candidate.classList.contains("dp-icon-btn"));
        candidate.classList.toggle("dp-color-swatch--active", candidate === button && candidate.classList.contains("dp-color-swatch"));
      });
    });
  });
  for (const kind of ["bg", "border"]) {
    const enabled = document.getElementById(`detail-pin-bulk-${kind}-opacity-enabled`);
    const range = document.getElementById(`detail-pin-bulk-${kind}-opacity`);
    const value = document.getElementById(`detail-pin-bulk-${kind}-opacity-value`);
    enabled?.addEventListener("change", () => {
      if (range)
        range.disabled = !enabled.checked;
    });
    range?.addEventListener("input", () => {
      if (value)
        value.textContent = range.value;
    });
  }
  document.getElementById("detail-pin-bulk-edit-submit")?.addEventListener("click", async function() {
    const uuids = Array.from(selectedDpUuids);
    if (!uuids.length || !cfg.detailPinsBulkEditUrl)
      return;
    const payload = { uuids };
    document.querySelectorAll("[data-dp-bulk-picker]").forEach((picker) => {
      const field = picker.dataset.dpBulkField;
      if (field && Object.hasOwn(picker.dataset, "dpBulkValue"))
        payload[field] = picker.dataset.dpBulkValue || null;
    });
    for (const kind of ["bg", "border"]) {
      const enabled = document.getElementById(`detail-pin-bulk-${kind}-opacity-enabled`);
      const range = document.getElementById(`detail-pin-bulk-${kind}-opacity`);
      if (enabled?.checked && range)
        payload[`${kind}_opacity`] = Number.parseInt(range.value, 10);
    }
    if (Object.keys(payload).length === 1) {
      toast.info("Choose at least one style to change.");
      return;
    }
    const saved = this.innerHTML;
    this.disabled = true;
    this.innerHTML = '<i class="material-symbols-outlined spin">progress_activity</i> Saving...';
    try {
      const response = await fetch(cfg.detailPinsBulkEditUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify(payload)
      });
      if (!response.ok)
        throw new Error(await response.text() || "Update failed.");
      document.getElementById("detail-pin-bulk-edit-dialog")?.close();
      toast.success(`${uuids.length} child pin${uuids.length === 1 ? "" : "s"} updated.`);
      clearDpSelection();
      loadDetailPins();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to update child pins.");
    } finally {
      this.disabled = false;
      this.innerHTML = saved;
    }
  });
  async function doPromoteSelectedDp() {
    const uuids = Array.from(selectedDpUuids);
    if (!uuids.length)
      return;
    const n = uuids.length;
    if (!await confirmAction({ title: "Promote child pins?", message: `Promote ${n} child pin${n === 1 ? "" : "s"} to top-level pins on your main map?`, confirmLabel: "Promote" }))
      return;
    const results = await Promise.all(uuids.map((uuid) => {
      const slug = detailPins.find((d) => d.uuid === uuid)?.slug || uuid;
      return fetch(`/dashboard/map/pin/${encodeURIComponent(slug)}/detach-parent/`, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken() }
      }).then((r) => r.ok);
    }));
    const promoted = results.filter(Boolean).length;
    if (promoted)
      toast.success(`${promoted} pin${promoted === 1 ? "" : "s"} promoted.`);
    if (promoted < n)
      toast.warning(`${n - promoted} pin${n - promoted === 1 ? "" : "s"} could not be promoted (location conflict).`);
    clearDpSelection();
    loadDetailPins();
    fetchBoundaries(0);
  }
  async function doShareSelectedDp() {
    if (!cfg.pinShareDialogUrl)
      return;
    const uuids = Array.from(selectedDpUuids);
    if (!uuids.length)
      return;
    const dialog = document.getElementById("pin-share-dialog");
    if (!dialog)
      return;
    const url = `${cfg.pinShareDialogUrl}?children=${uuids.map(encodeURIComponent).join(",")}`;
    const html = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } }).then((r) => r.ok ? r.text() : "").catch(() => "");
    if (!html) {
      toast.error("Failed to open the share dialog.");
      return;
    }
    dialog.innerHTML = html;
    htmxProcess(dialog);
    dialog.showModal();
    clearDpSelection();
  }
  async function doSendSelectedDpToWiki() {
    if (!cfg.detailPinsSendToWikiUrl)
      return;
    const uuids = Array.from(selectedDpUuids);
    if (!uuids.length)
      return;
    const body = new URLSearchParams;
    uuids.forEach((uuid) => body.append("child_pin_uuids", uuid));
    const response = await fetch(cfg.detailPinsSendToWikiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": getCsrfToken() },
      body
    }).catch(() => null);
    if (response?.ok) {
      const trigger = response.headers.get("HX-Trigger");
      if (trigger) {
        try {
          const parsed = JSON.parse(trigger);
          if (parsed.showToast)
            toast[parsed.showToast.level]?.(parsed.showToast.message);
        } catch {}
      }
    } else {
      toast.error("Failed to send child pins to the wiki.");
    }
    clearDpSelection();
  }
  async function doDeleteSelectedDp() {
    const uuids = Array.from(selectedDpUuids);
    if (!uuids.length)
      return;
    const n = uuids.length;
    if (!await confirmAction({ title: "Delete child pins?", message: `Delete ${n} child pin${n === 1 ? "" : "s"}? This also removes reviews, visit history, and notes.`, confirmLabel: "Delete" }))
      return;
    const results = await Promise.all(uuids.map((uuid) => fetch(`${dpEditBase}${uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } }).then((r) => r.ok)));
    const deleted = results.filter(Boolean).length;
    if (deleted)
      toast.success(`${deleted} pin${deleted === 1 ? "" : "s"} deleted.`);
    if (deleted < n)
      toast.warning(`${n - deleted} pin${n - deleted === 1 ? "" : "s"} could not be deleted.`);
    clearDpSelection();
    loadDetailPins();
    fetchBoundaries(0);
  }
  (function initDetailPinDragSelect() {
    mapEl.addEventListener("mousedown", (e) => {
      if (!detailSelectMode || e.button !== 0)
        return;
      const startLL = map.mouseEventToLatLng(e);
      const startX = e.clientX;
      const startY = e.clientY;
      let dragging = false;
      function onMove(ev) {
        if (!dragging && Math.hypot(ev.clientX - startX, ev.clientY - startY) < 6)
          return;
        dragging = true;
        if (dpDragSelectRect)
          map.removeLayer(dpDragSelectRect);
        dpDragSelectRect = L.rectangle(L.latLngBounds(startLL, map.mouseEventToLatLng(ev)), {
          color: "#1E88E5",
          weight: 2,
          fillOpacity: 0.08,
          dashArray: "4 4",
          interactive: false
        }).addTo(map);
      }
      function onUp(ev) {
        document.removeEventListener("mousemove", onMove);
        if (dpDragSelectRect) {
          map.removeLayer(dpDragSelectRect);
          dpDragSelectRect = null;
        }
        if (!dragging)
          return;
        const bounds = L.latLngBounds(startLL, map.mouseEventToLatLng(ev));
        detailSelectableEntries().forEach((dp) => {
          if (dp.marker && !selectedDpUuids.has(dp.uuid) && bounds.contains(dp.marker.getLatLng()))
            toggleDpSelection(dp.uuid);
        });
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp, { once: true });
    });
  })();
  const toolbar = window.createMarkupToolbar(map, markupLayer, {
    markupJsonUrl: cfg.markupJsonUrl,
    markupCreateUrl: cfg.markupCreateUrl,
    markupEditUrlTemplate: cfg.markupEditUrlTemplate,
    markupFillOpacity: cfg.markupFillOpacity,
    markupBorderOpacity: cfg.markupBorderOpacity,
    lineFinishTipDismissed: () => !cfg.showOnboardingTips,
    onBuildDetailList: () => buildDetailList(),
    onClearDetailPinHighlight: () => clearDetailPinHighlight(),
    onCloseDetailPinPanel: () => closeDetailPinPanel(),
    layerGroupFor: (item) => item.layer_uuid && customLayerGroups.get(item.layer_uuid) || markupLayer
  });
  window.startMarkupDraw = toolbar.startMarkupDraw;
  window.startShapeDraw = toolbar.startShapeDraw;
  window.startTextPlacement = toolbar.startTextPlacement;
  window.closeMarkupPanel = toolbar.closeMarkupPanel;
  window._liveApplyMarkupEdit = toolbar.liveApplyMarkupEdit;
  window._closeMarkupDraw = toolbar.closeOrFinishDraw;
  window.deleteMarkupEdit = toolbar.deleteMarkupEdit;
  window.openMarkupEditDialog = toolbar.openMarkupEditDialog;
  window.loadMarkup = toolbar.loadMarkup;
  loadDetailPins();
  document.body.addEventListener("pinDetailPinsChanged", () => {
    loadDetailPins();
    fetchBoundaries(0);
  });
  function makePhotoIcon(url, size, highlighted) {
    const shadow = highlighted ? "0 0 0 3px #2563eb, 0 3px 10px rgba(0,0,0,.45)" : "0 2px 6px rgba(0,0,0,.35)";
    return L.divIcon({
      className: "",
      html: `<img src="${url}" class="photo-marker-img" style="width:${size}px;height:${size}px;object-fit:cover;border-radius:5px;border:2px solid #fff;box-shadow:${shadow};display:block;transition:transform .15s,box-shadow .15s;">`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2]
    });
  }
  function addPhotoMarker(imgId, url, lat, lng, ownerName) {
    if (photoMarkers[imgId])
      photoLayer.removeLayer(photoMarkers[imgId].marker);
    const marker = L.marker([lat, lng], { icon: makePhotoIcon(url, 44, false), draggable: !ownerName });
    if (ownerName)
      marker.bindTooltip(`Photo from ${ownerName}`, { permanent: false, direction: "top", className: "detail-pin-tooltip" });
    marker.on("dragend", () => {
      const pos = marker.getLatLng();
      const prevLat = photoMarkers[imgId].lat;
      const prevLng = photoMarkers[imgId].lng;
      photoMarkers[imgId].lat = pos.lat;
      photoMarkers[imgId].lng = pos.lng;
      const item = photoPanelItems.find((p) => p.id === imgId);
      if (item) {
        item.lat = pos.lat;
        item.lng = pos.lng;
      }
      if (window.galleryRepositionImage) {
        window.galleryRepositionImage(imgId, pos.lat, pos.lng, () => {
          marker.setLatLng([prevLat, prevLng]);
          photoMarkers[imgId].lat = prevLat;
          photoMarkers[imgId].lng = prevLng;
          if (item) {
            item.lat = prevLat;
            item.lng = prevLng;
          }
          buildPhotoPanel();
        });
      }
      buildPhotoPanel();
    });
    marker.on("mouseover", () => window._galleryHighlightMarker?.(imgId, true));
    marker.on("mouseout", () => window._galleryHighlightMarker?.(imgId, false));
    marker.on("click", () => window.galleryOpenLightbox?.(imgId, { url }));
    marker.addTo(photoLayer);
    photoMarkers[imgId] = { marker, url, lat, lng };
  }
  window._galleryAddMarker = (img) => {
    if (!photoPanelItems.find((p) => p.id === img.id))
      photoPanelItems.push({ id: img.id, url: img.url, lat: img.latitude, lng: img.longitude, mine: true });
    if (img.latitude != null && img.longitude != null)
      addPhotoMarker(img.id, img.url, img.latitude, img.longitude);
    buildPhotoPanel();
    refreshPanelHeader();
  };
  window._galleryRemoveMarker = (imgId) => {
    photoPanelItems = photoPanelItems.filter((p) => p.id !== imgId);
    if (photoMarkers[imgId]) {
      photoLayer.removeLayer(photoMarkers[imgId].marker);
      delete photoMarkers[imgId];
    }
    buildPhotoPanel();
    refreshPanelHeader();
  };
  window._galleryHighlightMarker = (imgId, on) => {
    const entry = photoMarkers[imgId];
    if (entry) {
      const sz = on ? 56 : 44;
      entry.marker.setIcon(makePhotoIcon(entry.url, sz, on));
      if (on)
        map.panTo([entry.lat, entry.lng]);
    }
    document.querySelectorAll(".photo-panel-item").forEach((li) => {
      li.classList.toggle("is-highlighted", +(li.dataset.id ?? "") === imgId && !!on);
    });
  };
  function buildPhotoPanel() {
    const ul = document.getElementById("photo-panel-list");
    if (!ul)
      return;
    ul.innerHTML = "";
    const photoTab = document.getElementById("map-panel-tab-photos");
    if (photoTab) {
      let badge = photoTab.querySelector(".map-panel-tab-badge");
      if (photoPanelItems.length) {
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "map-panel-tab-badge";
          photoTab.appendChild(badge);
        }
        badge.textContent = String(photoPanelItems.length);
      } else if (badge) {
        badge.remove();
      }
    }
    if (!photoPanelItems.length) {
      const empty = document.createElement("li");
      empty.className = "photo-panel-empty";
      empty.innerHTML = '<i class="material-symbols-outlined">photo_camera</i><span>No photos yet</span>';
      ul.appendChild(empty);
      return;
    }
    photoPanelItems.forEach((img) => {
      const hasCoords = img.lat != null && img.lng != null;
      const li = document.createElement("li");
      li.className = "photo-panel-item";
      li.dataset.id = String(img.id);
      li.draggable = true;
      li.title = "Click to view";
      li.innerHTML = `
                <div class="photo-panel-thumb-wrap">
                    <img src="${img.url}" class="photo-panel-thumb" alt="" draggable="false">
                    <span class="photo-panel-coord-badge ${hasCoords ? "has-gps" : "no-gps"}" title="${hasCoords ? "Has GPS" : "No GPS"}">
                        <i class="material-icons">${hasCoords ? "place" : "location_off"}</i>
                    </span>
                </div>`;
      li.addEventListener("mouseenter", () => window._galleryHighlightMarker?.(img.id, true));
      li.addEventListener("mouseleave", () => window._galleryHighlightMarker?.(img.id, false));
      li.addEventListener("dragstart", (e) => {
        e.dataTransfer?.setData("text/photoid", String(img.id));
        if (e.dataTransfer)
          e.dataTransfer.effectAllowed = "move";
        li.classList.add("is-dragging");
      });
      li.addEventListener("dragend", () => li.classList.remove("is-dragging"));
      li.addEventListener("click", () => {
        if (hasCoords)
          map.panTo([img.lat, img.lng]);
        window.galleryOpenLightbox?.(img.id, { url: img.url });
      });
      ul.appendChild(li);
    });
  }
  mapEl.addEventListener("dragover", (e) => {
    if (!e.dataTransfer?.types.includes("text/photoid"))
      return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    mapEl.classList.add("photo-drop-target");
  });
  mapEl.addEventListener("dragleave", () => mapEl.classList.remove("photo-drop-target"));
  mapEl.addEventListener("drop", (e) => {
    mapEl.classList.remove("photo-drop-target");
    const idStr = e.dataTransfer?.getData("text/photoid");
    if (!idStr)
      return;
    e.preventDefault();
    const imgId = Number.parseInt(idStr, 10);
    const rect = mapEl.getBoundingClientRect();
    const latlng = map.containerPointToLatLng([e.clientX - rect.left, e.clientY - rect.top]);
    const item = photoPanelItems.find((p) => p.id === imgId);
    if (!item)
      return;
    const prevLat = item.lat;
    const prevLng = item.lng;
    item.lat = latlng.lat;
    item.lng = latlng.lng;
    addPhotoMarker(imgId, item.url, latlng.lat, latlng.lng);
    if (window.galleryRepositionImage) {
      window.galleryRepositionImage(imgId, latlng.lat, latlng.lng, () => {
        item.lat = prevLat;
        item.lng = prevLng;
        if (prevLat != null && prevLng != null) {
          addPhotoMarker(imgId, item.url, prevLat, prevLng);
        } else if (photoMarkers[imgId]) {
          photoLayer.removeLayer(photoMarkers[imgId].marker);
          delete photoMarkers[imgId];
        }
        buildPhotoPanel();
      });
    }
    buildPhotoPanel();
    refreshPanelHeader();
  });
  mapEl.addEventListener("dragover", (e) => {
    if (!cfg.mediaRelevanceUrl || !e.dataTransfer?.types.includes("text/media-item"))
      return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    mapEl.classList.add("photo-drop-target");
  });
  mapEl.addEventListener("dragleave", () => mapEl.classList.remove("photo-drop-target"));
  mapEl.addEventListener("drop", (e) => {
    const raw = e.dataTransfer?.getData("text/media-item");
    if (!cfg.mediaRelevanceUrl || !raw)
      return;
    e.preventDefault();
    mapEl.classList.remove("photo-drop-target");
    const itemEl = window._mediaDragItemEl;
    window._mediaDragItemEl = undefined;
    let item;
    try {
      item = JSON.parse(raw);
    } catch {
      return;
    }
    const rect = mapEl.getBoundingClientRect();
    const latlng = map.containerPointToLatLng([e.clientX - rect.left, e.clientY - rect.top]);
    fetch(cfg.mediaRelevanceUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify({
        source: item.source,
        item_key: item.key,
        url: item.url,
        is_relevant: true,
        page_url: item.pageUrl,
        caption: item.caption,
        latitude: latlng.lat,
        longitude: latlng.lng
      })
    }).then((r) => r.json()).then((data) => {
      window.mediaApplyMaterializedDrop?.(itemEl, data);
      if (data.image_id && data.latitude != null && data.longitude != null) {
        window._galleryAddMarker({ id: data.image_id, url: data.image_url, latitude: data.latitude, longitude: data.longitude });
      } else if (data.materialize_error) {
        toast.warning(`Couldn't save a local copy: ${data.materialize_error}`);
      }
    }).catch(() => toast.error("Failed to save photo location."));
  });
  document.querySelectorAll(".map-panel-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".map-panel-tab").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      const tab = btn.dataset.tab;
      document.getElementById("map-panel-details").hidden = tab !== "details";
      document.getElementById("map-panel-photos").hidden = tab !== "photos";
    });
  });
  fetch(cfg.photoGalleryJsonUrl).then((r) => r.json()).then((data) => {
    photoPanelItems = [];
    (data.images || []).forEach((img) => {
      photoPanelItems.push({ id: img.id, url: img.url, lat: img.latitude, lng: img.longitude, mine: img.is_mine });
      if (img.latitude != null && img.longitude != null)
        addPhotoMarker(img.id, img.url, img.latitude, img.longitude, img.child_pin_name);
    });
    buildPhotoPanel();
    refreshPanelHeader();
  }).catch((err) => console.warn("Could not load gallery photos for panel:", err));
  const boundaryApiUrl = cfg.boundaryUrl;
  const BOUNDARY_STYLES = {
    property: { pane: "boundaryPane", color: "#cc2200", fillColor: "#ff4422", fillOpacity: 0.2, weight: 2 },
    building: { pane: "boundaryPane", color: "#1d4ed8", fillColor: "#3b82f6", fillOpacity: 0.22, weight: 2 }
  };
  const CIRCLE_STYLE = { ...BOUNDARY_STYLES.property, dashArray: "6 6", fillOpacity: 0.06 };
  const DETAIL_BUILDING_STYLE = { ...BOUNDARY_STYLES.building, dashArray: "4 4", fillOpacity: 0.12 };
  const boundaryGroups = {
    property: new L.FeatureGroup().addTo(map),
    building: new L.FeatureGroup().addTo(map)
  };
  const detailBuildingItems = new L.FeatureGroup().addTo(map);
  let boundaryDrawControl = null;
  let editingBoundaryType = null;
  const savedBoundaries = { property: null, building: null };
  const boundarySources = { property: null, building: null };
  let boundaryBoundsFitted = false;
  if (!window._boundaryDrawToggleWired) {
    window._boundaryDrawToggleWired = true;
    [L.Draw.Polygon, L.EditToolbar.Edit].forEach((Ctor) => {
      const origEnable = Ctor.prototype.enable;
      Ctor.prototype.enable = function() {
        if (this._enabled) {
          this.disable();
          return this;
        }
        return origEnable.call(this);
      };
    });
  }
  function setMainMarkerVisible(visible) {
    if (visible && !map.hasLayer(mainMarker)) {
      mainMarker.addTo(map);
    } else if (!visible && map.hasLayer(mainMarker)) {
      map.removeLayer(mainMarker);
    }
  }
  function addGeoJSONPolygons(group, geojson, style, label) {
    const rings = geojson.type === "MultiPolygon" ? geojson.coordinates : geojson.type === "Polygon" ? [geojson.coordinates] : null;
    const bindLabel = (layer) => {
      if (label)
        layer.bindTooltip(label, { sticky: true, direction: "top", className: "boundary-tooltip" });
      return layer;
    };
    if (rings) {
      rings.forEach((ringSet) => {
        group.addLayer(bindLabel(L.polygon(ringSet.map((ring) => ring.map((c) => [c[1], c[0]])), style)));
      });
    } else {
      L.geoJSON(geojson, { style }).eachLayer((l) => group.addLayer(bindLabel(l)));
    }
  }
  function loadBoundary(type, geojson, source) {
    const group = boundaryGroups[type];
    group.clearLayers();
    savedBoundaries[type] = geojson || null;
    boundarySources[type] = geojson ? source || null : null;
    if (!geojson)
      return;
    const isCircle = type === "property" && source === "circle";
    const style = isCircle ? CIRCLE_STYLE : BOUNDARY_STYLES[type];
    const label = type === "property" ? isCircle ? "Approximate property area" : "Property boundary" : "Building boundary";
    addGeoJSONPolygons(group, geojson, style, label);
  }
  function boundaryHasRealPolygon(type) {
    return Boolean(savedBoundaries[type]) && boundarySources[type] !== "circle";
  }
  function applyBoundaryPayload(data) {
    const boundaries = data.boundaries || {};
    ["property", "building"].forEach((type) => {
      const entry = boundaries[type] || {};
      loadBoundary(type, entry.polygon || null, entry.source || null);
    });
    detailBuildingItems.clearLayers();
    (data.detail_buildings || []).forEach((entry) => {
      if (entry.polygon)
        addGeoJSONPolygons(detailBuildingItems, entry.polygon, DETAIL_BUILDING_STYLE, "Building boundary (from a child pin)");
    });
    setMainMarkerVisible(!boundaryHasRealPolygon("property"));
    if (!boundaryBoundsFitted) {
      const fitGroup = boundaryHasRealPolygon("property") ? boundaryGroups.property : boundaryHasRealPolygon("building") ? boundaryGroups.building : null;
      if (fitGroup && fitGroup.getLayers().length) {
        map.fitBounds(fitGroup.getBounds().pad(0.25));
        boundaryBoundsFitted = true;
      }
    }
    map.invalidateSize();
    attachBoundaryClickHandlers();
  }
  function fetchBoundaries(attempt) {
    fetch(boundaryApiUrl).then((r) => r.json()).then((data) => {
      applyBoundaryPayload(data);
      if ((data.pending || data.refreshing) && attempt < 30) {
        setTimeout(() => fetchBoundaries(attempt + 1), 2000);
      }
    }).catch((err) => console.warn("Could not load boundaries:", err));
  }
  fetchBoundaries(0);
  function attachEditRightClickDelete() {
    setTimeout(() => {
      if (!editingBoundaryType)
        return;
      boundaryGroups[editingBoundaryType].eachLayer((layer) => {
        const editableLayer = layer;
        if (editableLayer.editing?._markerGroup) {
          editableLayer.editing._markerGroup.eachLayer((m) => {
            m.off("contextmenu.rcdelete");
            m.on("contextmenu.rcdelete", (e) => {
              L.DomEvent.stopPropagation(e);
              m.fire("click");
            });
          });
        }
      });
    }, 100);
  }
  function setBoundaryEditButtonsVisible(visible) {
    const controls = document.getElementById("boundary-save-controls");
    if (controls)
      controls.style.display = visible ? "none" : "";
  }
  function startEditBoundary(type) {
    if (boundaryDrawControl || !boundaryGroups[type])
      return;
    editingBoundaryType = type;
    toolbar.closeMarkupPanel();
    closeDetailPinPanel();
    map.getPane("boundaryPane").style.zIndex = "560";
    const group = boundaryGroups[type];
    if (type === "property" && boundarySources.property === "circle")
      group.clearLayers();
    boundaryDrawControl = new L.Control.Draw({
      draw: {
        polygon: { allowIntersection: false, drawError: { color: "#ffcc00", message: "Boundaries cannot intersect!" }, shapeOptions: BOUNDARY_STYLES[type], showArea: true },
        marker: false,
        circle: false,
        rectangle: false,
        polyline: false,
        circlemarker: false
      },
      edit: { featureGroup: group, remove: false }
    });
    map.addControl(boundaryDrawControl);
    map.on(L.Draw.Event.CREATED, (e) => {
      group.addLayer(e.layer);
      saveBoundary({ exitEdit: false });
    });
    map.on(L.Draw.Event.EDITSTART, attachEditRightClickDelete);
    map.on(L.Draw.Event.EDITED, () => saveBoundary({ exitEdit: false }));
    map.on(L.Draw.Event.DELETED, () => saveBoundary({ exitEdit: false }));
    group.eachLayer((layer) => layer.on("edit", scheduleBoundaryAutoSave));
    setTimeout(() => {
      const control = boundaryDrawControl;
      if (group.getLayers().length > 0) {
        control._toolbars.edit._modes.edit.handler.enable();
        attachEditRightClickDelete();
        toast.info("Drag vertices to reshape, click a vertex to delete it, or right-click to delete.");
      } else {
        control._toolbars.draw._modes.polygon.handler.enable();
      }
    }, 50);
    setBoundaryEditButtonsVisible(false);
  }
  let boundaryAutoSaveTimer;
  function scheduleBoundaryAutoSave() {
    if (!boundaryDrawControl)
      return;
    clearTimeout(boundaryAutoSaveTimer);
    boundaryAutoSaveTimer = setTimeout(() => saveBoundary({ exitEdit: false, quiet: true }), 600);
  }
  function boundaryTypeOfLayer(layer) {
    if (boundaryGroups.property.hasLayer(layer))
      return "property";
    if (boundaryGroups.building.hasLayer(layer))
      return "building";
    return null;
  }
  function saveBoundary(options = {}) {
    const type = options.type || editingBoundaryType;
    if (!type)
      return;
    const layers = boundaryGroups[type].getLayers();
    const geometry = layers.length === 0 ? null : { type: "MultiPolygon", coordinates: layers.map((l) => l.toGeoJSON().geometry.coordinates) };
    fetch(boundaryApiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify({ boundary_type: type, polygon: geometry })
    }).then(async (r) => {
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try {
          msg = (await r.json()).error || msg;
        } catch {}
        throw new Error(msg);
      }
      return r.json();
    }).then((data) => {
      const exiting = options.exitEdit !== false;
      if (exiting)
        exitBoundaryEdit();
      if (exiting || !boundaryDrawControl)
        applyBoundaryPayload(data);
      if (data.pending || data.refreshing)
        fetchBoundaries(0);
      if (!options.quiet)
        toast.success(geometry ? "Boundary saved." : "Boundary reset to the default.");
    }).catch((err) => toast.error(`Failed to save boundary: ${err.message}`));
  }
  async function clearBoundary() {
    if (!editingBoundaryType)
      return;
    if (!await confirmAction({ title: "Clear Boundary", message: "Reset this boundary to its default?", confirmLabel: "Clear" }))
      return;
    boundaryGroups[editingBoundaryType].clearLayers();
    saveBoundary();
  }
  function exitBoundaryEdit() {
    if (boundaryDrawControl) {
      map.removeControl(boundaryDrawControl);
      boundaryDrawControl = null;
    }
    map.off(L.Draw.Event.CREATED);
    map.off(L.Draw.Event.EDITED);
    map.off(L.Draw.Event.DELETED);
    if (editingBoundaryType) {
      boundaryGroups[editingBoundaryType].eachLayer((layer) => layer.off("edit", scheduleBoundaryAutoSave));
    }
    editingBoundaryType = null;
    map.getPane("boundaryPane").style.zIndex = "540";
    setBoundaryEditButtonsVisible(true);
    attachBoundaryClickHandlers();
  }
  function cancelBoundaryEdit() {
    const type = editingBoundaryType;
    exitBoundaryEdit();
    if (type)
      loadBoundary(type, savedBoundaries[type], boundarySources[type]);
  }
  function finishBoundaryEdit() {
    clearTimeout(boundaryAutoSaveTimer);
    saveBoundary();
  }
  window.startEditBoundary = startEditBoundary;
  window.saveBoundary = saveBoundary;
  window.clearBoundary = clearBoundary;
  window.cancelBoundaryEdit = cancelBoundaryEdit;
  window.finishBoundaryEdit = finishBoundaryEdit;
  const circlePalette = ["#e53e3e", "#1d4ed8", "#16a34a", "#d97706", "#7c3aed", "#0f172a", "#f8fafc", "#ffffff"];
  function buildCircleSwatches(containerId, inputId, currentVal, onChange) {
    const container = document.getElementById(containerId);
    if (!container)
      return;
    container.innerHTML = "";
    const nb = document.createElement("button");
    nb.type = "button";
    nb.title = "None";
    nb.className = `dp-color-swatch markup-color-swatch--none${!currentVal ? " dp-color-swatch--active" : ""}`;
    nb.style.cssText = "background:transparent;border:1px dashed #cbd5e1;position:relative;";
    nb.innerHTML = '<span style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.65rem;color:#9ca3af">∅</span>';
    nb.addEventListener("click", () => {
      container.querySelectorAll(".dp-color-swatch").forEach((b) => b.classList.remove("dp-color-swatch--active"));
      nb.classList.add("dp-color-swatch--active");
      document.getElementById(inputId).value = "";
      onChange?.("");
    });
    container.appendChild(nb);
    circlePalette.forEach((color) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `dp-color-swatch${color === currentVal ? " dp-color-swatch--active" : ""}`;
      btn.style.cssText = `background:${color};${color === "#f8fafc" || color === "#ffffff" ? "border:1px solid #cbd5e1;" : ""}`;
      btn.addEventListener("click", () => {
        container.querySelectorAll(".dp-color-swatch").forEach((b) => b.classList.remove("dp-color-swatch--active"));
        btn.classList.add("dp-color-swatch--active");
        document.getElementById(inputId).value = color;
        onChange?.(color);
      });
      container.appendChild(btn);
    });
  }
  let editingDp = null;
  let dpMode = null;
  let dpActiveMarker = null;
  let dpCreatedUuid = null;
  let dpAutoSaveTimer;
  let dpAutoSaveUuid = null;
  let dpTypeTouched = false;
  function currentDpIcon() {
    return detailIcon({
      pin_type: document.getElementById("dp-type").value,
      icon: document.getElementById("dp-icon").value || null,
      color: document.getElementById("dp-color").value || null,
      bg_color: document.getElementById("dp-bg-color").value || "",
      bg_opacity: Number.parseInt(document.getElementById("dp-bg-opacity").value || "80", 10),
      border_color: document.getElementById("dp-border-color").value || "",
      border_opacity: Number.parseInt(document.getElementById("dp-border-opacity").value || "100", 10)
    });
  }
  function updateDpMarkerIcon() {
    dpActiveMarker?.setIcon(currentDpIcon());
    scheduleDpAutoSave();
  }
  function collectDpFormData() {
    const data = {
      name: document.getElementById("dp-name").value.trim(),
      description: document.getElementById("dp-description").value.trim(),
      icon: document.getElementById("dp-icon").value || null,
      color: document.getElementById("dp-color").value || null,
      bg_color: document.getElementById("dp-bg-color").value || null,
      bg_opacity: Number.parseInt(document.getElementById("dp-bg-opacity").value, 10),
      border_color: document.getElementById("dp-border-color").value || null,
      border_opacity: Number.parseInt(document.getElementById("dp-border-opacity").value, 10),
      latitude: document.getElementById("dp-lat").value,
      longitude: document.getElementById("dp-lon").value
    };
    if (dpTypeTouched)
      data.pin_type = document.getElementById("dp-type").value;
    return data;
  }
  function createDpImmediately(lat, lng) {
    const data = collectDpFormData();
    data.latitude = lat.toFixed(6);
    data.longitude = lng.toFixed(6);
    fetch(cfg.detailPinCreateUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify(data)
    }).then((r) => r.json().then((resp) => {
      if (!r.ok || resp.ok === false)
        throw resp;
      return resp;
    })).then((resp) => {
      dpCreatedUuid = resp.uuid;
      fetchBoundaries(0);
    }).catch((resp) => toast.error(resp && resp.error || "Failed to save detail pin."));
  }
  function scheduleDpAutoSave() {
    if (dpMode !== "add" || !dpCreatedUuid)
      return;
    dpAutoSaveUuid = dpCreatedUuid;
    clearTimeout(dpAutoSaveTimer);
    dpAutoSaveTimer = setTimeout(flushDpAutoSave, 500);
  }
  function flushDpAutoSave() {
    clearTimeout(dpAutoSaveTimer);
    const uuid = dpAutoSaveUuid;
    dpAutoSaveUuid = null;
    if (!uuid)
      return Promise.resolve();
    return fetch(`${dpEditBase}${uuid}/`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify(collectDpFormData())
    }).then(() => {
      return;
    }).catch(() => toast.error("Failed to save detail pin changes."));
  }
  function setDpLocation(lat, lng) {
    document.getElementById("dp-lat").value = lat.toFixed(6);
    document.getElementById("dp-lon").value = lng.toFixed(6);
    document.getElementById("detail-pin-submit-btn").disabled = false;
    document.getElementById("detail-pin-place-hint")?.classList.add("is-placed");
    document.getElementById("detail-pin-place-hint-text").textContent = dpMode === "edit" ? "Drag the pin to move it." : "Drag the pin, or click elsewhere to move it.";
  }
  function onDpMarkerDragEnd() {
    const pos = dpActiveMarker.getLatLng();
    setDpLocation(pos.lat, pos.lng);
    scheduleDpAutoSave();
  }
  function onMainMapClickForDp(e) {
    if (dpMode === "edit")
      return;
    const { lat, lng } = e.latlng;
    if (dpActiveMarker) {
      dpActiveMarker.setLatLng([lat, lng]);
      setDpLocation(lat, lng);
      scheduleDpAutoSave();
    } else {
      dpActiveMarker = L.marker([lat, lng], { icon: currentDpIcon(), draggable: true }).addTo(map);
      dpActiveMarker.on("dragend", onDpMarkerDragEnd);
      setDpLocation(lat, lng);
      createDpImmediately(lat, lng);
    }
  }
  function resetDpForm() {
    document.getElementById("detail-pin-form").reset();
    dpTypeTouched = false;
    document.getElementById("dp-lat").value = "";
    document.getElementById("dp-lon").value = "";
    document.getElementById("dp-icon").value = "";
    document.getElementById("dp-color").value = "";
    document.getElementById("dp-bg-color").value = "";
    document.getElementById("dp-border-color").value = "";
    document.getElementById("dp-bg-opacity").value = "80";
    document.getElementById("dp-border-opacity").value = "100";
    document.getElementById("dp-bg-opacity-val").textContent = "80";
    document.getElementById("dp-border-opacity-val").textContent = "100";
    document.querySelectorAll("#dp-icon-picker .dp-icon-btn").forEach((b) => b.classList.remove("dp-icon-btn--active"));
    document.querySelectorAll("#dp-color-picker .dp-color-swatch").forEach((s) => s.classList.remove("dp-color-swatch--active"));
    buildCircleSwatches("dp-bg-swatches", "dp-bg-color", "", updateDpMarkerIcon);
    buildCircleSwatches("dp-border-swatches", "dp-border-color", "", updateDpMarkerIcon);
  }
  function openAddPinDialog() {
    toolbar.closeMarkupPanel();
    dpMode = "add";
    editingDp = null;
    dpCreatedUuid = null;
    resetDpForm();
    document.getElementById("detail-pin-panel-title").textContent = "Add Detail Pin";
    document.getElementById("detail-pin-submit-btn").textContent = "Close";
    document.getElementById("detail-pin-submit-btn").disabled = true;
    document.getElementById("detail-pin-delete-btn").hidden = true;
    document.getElementById("detail-pin-place-hint")?.classList.remove("is-placed");
    document.getElementById("detail-pin-place-hint-text").textContent = "Click anywhere on the map to place the pin.";
    document.getElementById("detail-pin-panel").style.display = "";
    map.on("click", onMainMapClickForDp);
  }
  function openDetailPinEditDialog(dp) {
    toolbar.closeMarkupPanel();
    dpMode = "edit";
    editingDp = dp;
    resetDpForm();
    document.getElementById("detail-pin-panel-title").textContent = "Edit Detail Pin";
    document.getElementById("detail-pin-submit-btn").textContent = "Save Changes";
    document.getElementById("detail-pin-submit-btn").disabled = false;
    document.getElementById("detail-pin-delete-btn").hidden = false;
    document.getElementById("detail-pin-place-hint")?.classList.add("is-placed");
    document.getElementById("detail-pin-place-hint-text").textContent = "Drag the pin to move it.";
    document.getElementById("dp-name").value = dp.name || "";
    document.getElementById("dp-description").value = dp.description || "";
    document.getElementById("dp-type").value = dp.pin_type || "poi";
    document.getElementById("dp-icon").value = dp.icon || "";
    document.getElementById("dp-color").value = dp.color || "";
    document.getElementById("dp-lat").value = String(dp.latitude);
    document.getElementById("dp-lon").value = String(dp.longitude);
    document.querySelectorAll("#dp-icon-picker .dp-icon-btn").forEach((b) => {
      b.classList.toggle("dp-icon-btn--active", b.dataset.icon === dp.icon);
    });
    document.querySelectorAll("#dp-color-picker .dp-color-swatch").forEach((s) => {
      s.classList.toggle("dp-color-swatch--active", s.dataset.color === dp.color);
    });
    const bgOpacity = dp.bg_opacity != null ? dp.bg_opacity : 80;
    document.getElementById("dp-bg-color").value = dp.bg_color || "";
    document.getElementById("dp-bg-opacity").value = String(bgOpacity);
    document.getElementById("dp-bg-opacity-val").textContent = String(bgOpacity);
    buildCircleSwatches("dp-bg-swatches", "dp-bg-color", dp.bg_color || "", updateDpMarkerIcon);
    const bdOpacity = dp.border_opacity != null ? dp.border_opacity : 100;
    document.getElementById("dp-border-color").value = dp.border_color || "";
    document.getElementById("dp-border-opacity").value = String(bdOpacity);
    document.getElementById("dp-border-opacity-val").textContent = String(bdOpacity);
    buildCircleSwatches("dp-border-swatches", "dp-border-color", dp.border_color || "", updateDpMarkerIcon);
    document.getElementById("detail-pin-panel").style.display = "";
    dpActiveMarker = dp.marker;
    dp.marker?.on("dragend", onDpMarkerDragEnd);
    map.on("click", onMainMapClickForDp);
  }
  function closeDetailPinPanel() {
    document.getElementById("detail-pin-panel").style.display = "none";
    map.off("click", onMainMapClickForDp);
    const wasAdding = dpMode === "add" && dpCreatedUuid;
    if (dpActiveMarker) {
      dpActiveMarker.off("dragend", onDpMarkerDragEnd);
      if (dpMode === "add")
        map.removeLayer(dpActiveMarker);
    }
    dpActiveMarker = null;
    dpMode = null;
    editingDp = null;
    dpCreatedUuid = null;
    if (wasAdding)
      Promise.resolve(flushDpAutoSave()).finally(loadDetailPins);
  }
  window.openAddPinDialog = openAddPinDialog;
  window.closeDetailPinPanel = closeDetailPinPanel;
  document.getElementById("dp-icon-picker")?.addEventListener("click", function(e) {
    const btn = e.target.closest(".dp-icon-btn");
    if (!btn)
      return;
    this.querySelectorAll(".dp-icon-btn").forEach((b) => b.classList.remove("dp-icon-btn--active"));
    btn.classList.add("dp-icon-btn--active");
    document.getElementById("dp-icon").value = btn.dataset.icon ?? "";
    updateDpMarkerIcon();
  });
  document.getElementById("dp-color-picker")?.addEventListener("click", function(e) {
    const sw = e.target.closest(".dp-color-swatch");
    if (!sw)
      return;
    this.querySelectorAll(".dp-color-swatch").forEach((s) => s.classList.remove("dp-color-swatch--active"));
    sw.classList.add("dp-color-swatch--active");
    document.getElementById("dp-color").value = sw.dataset.color ?? "";
    updateDpMarkerIcon();
  });
  document.getElementById("dp-bg-opacity")?.addEventListener("input", function() {
    document.getElementById("dp-bg-opacity-val").textContent = this.value;
    updateDpMarkerIcon();
  });
  document.getElementById("dp-border-opacity")?.addEventListener("input", function() {
    document.getElementById("dp-border-opacity-val").textContent = this.value;
    updateDpMarkerIcon();
  });
  document.getElementById("dp-type")?.addEventListener("change", () => {
    dpTypeTouched = true;
    updateDpMarkerIcon();
  });
  document.getElementById("dp-name")?.addEventListener("input", scheduleDpAutoSave);
  document.getElementById("dp-description")?.addEventListener("input", scheduleDpAutoSave);
  document.getElementById("detail-pin-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    if (dpMode === "add") {
      closeDetailPinPanel();
      return;
    }
    const lat = document.getElementById("dp-lat").value;
    const lon = document.getElementById("dp-lon").value;
    if (!lat || !lon) {
      toast.warning("Click a point on the map to set the location first.");
      return;
    }
    const submitBtn = document.getElementById("detail-pin-submit-btn");
    submitBtn.disabled = true;
    const data = collectDpFormData();
    fetch(`${dpEditBase}${editingDp.uuid}/`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify(data)
    }).then((r) => r.json().then((resp) => {
      if (!r.ok || resp.ok === false)
        throw resp;
      return resp;
    })).then(() => {
      toast.success("Detail pin updated.");
      closeDetailPinPanel();
      loadDetailPins();
      fetchBoundaries(0);
    }).catch((resp) => {
      toast.error(resp && resp.error || "Failed to save detail pin.");
      submitBtn.disabled = false;
    });
  });
  document.getElementById("detail-pin-delete-btn")?.addEventListener("click", async () => {
    if (!editingDp)
      return;
    if (!await confirmAction({ title: "Delete Pin", message: `Delete "${editingDp.name}"?`, confirmLabel: "Delete" }))
      return;
    fetch(`${dpEditBase}${editingDp.uuid}/`, { method: "DELETE", headers: { "X-CSRFToken": getCsrfToken() } }).then((r) => {
      if (!r.ok)
        throw new Error;
      closeDetailPinPanel();
      loadDetailPins();
      fetchBoundaries(0);
      toast.success("Detail pin deleted.");
    }).catch(() => toast.error("Failed to delete detail pin."));
  });
  function onBoundaryLayerClick(e) {
    if (boundaryDrawControl)
      return;
    if (toolbar.isDrawBusy() || dpMode === "add")
      return;
    L.DomEvent.stopPropagation(e);
    openBoundaryCtxMenu(e.target, e.latlng);
  }
  function attachBoundaryClickHandlers() {
    ["property", "building"].forEach((type) => {
      boundaryGroups[type].eachLayer((layer) => {
        layer.off("click", onBoundaryLayerClick);
        layer.on("click", onBoundaryLayerClick);
      });
    });
  }
  let boundaryCtxOutsideHandler = null;
  function openBoundaryCtxMenu(layer, latlng) {
    if (boundaryCtxOutsideHandler) {
      document.removeEventListener("click", boundaryCtxOutsideHandler, true);
      boundaryCtxOutsideHandler = null;
    }
    const content = document.createElement("div");
    content.className = "boundary-ctx-menu";
    const layerType = boundaryTypeOfLayer(layer);
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "boundary-ctx-menu__item";
    editBtn.innerHTML = '<i class="material-symbols-outlined">edit</i> Edit';
    editBtn.addEventListener("click", () => {
      map.closePopup();
      if (layerType)
        startEditBoundary(layerType);
    });
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "boundary-ctx-menu__item boundary-ctx-menu__item--danger";
    delBtn.innerHTML = '<i class="material-symbols-outlined">delete_outline</i> Delete';
    delBtn.addEventListener("click", async () => {
      map.closePopup();
      if (!layerType)
        return;
      if (!await confirmAction({ title: "Delete Boundary", message: "Delete this boundary polygon?", confirmLabel: "Delete" }))
        return;
      boundaryGroups[layerType].removeLayer(layer);
      if (layerType === "property" && boundaryGroups.property.getLayers().length === 0)
        setMainMarkerVisible(true);
      saveBoundary({ exitEdit: false, type: layerType });
    });
    content.append(editBtn, delBtn);
    L.popup({ closeButton: false, className: "boundary-ctx-menu-popup", offset: [0, -2] }).setLatLng(latlng).setContent(content).openOn(map);
    boundaryCtxOutsideHandler = (e) => {
      document.removeEventListener("click", boundaryCtxOutsideHandler, true);
      boundaryCtxOutsideHandler = null;
      if (content.contains(e.target))
        return;
      map.closePopup();
    };
    document.addEventListener("click", boundaryCtxOutsideHandler, true);
  }
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
