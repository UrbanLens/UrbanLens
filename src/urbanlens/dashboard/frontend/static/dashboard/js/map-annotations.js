import {
  confirmAction,
  getCsrfToken,
  toast
} from "./map-annotations-9jdqkrz7.js";

// src/urbanlens/dashboard/frontend/ts/shared/map-layers.ts
var TILE_DEFS = {
  street: {
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    options: {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxNativeZoom: 19,
      maxZoom: 21
    }
  },
  dark: {
    url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    options: {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
      maxNativeZoom: 20,
      maxZoom: 21
    }
  },
  topographic: {
    url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    options: {
      attribution: "&copy; OpenTopoMap contributors",
      maxNativeZoom: 17,
      maxZoom: 21
    }
  },
  satellite: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    options: {
      attribution: "Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community",
      maxNativeZoom: 19,
      maxZoom: 21
    }
  },
  borders: {
    url: "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    options: {
      attribution: "Boundaries &copy; Esri",
      maxNativeZoom: 19,
      maxZoom: 21,
      opacity: 0.6,
      pane: "overlayPane"
    }
  }
};
var BASE_ALIASES = {
  street: "street",
  standard: "street",
  osm: "street",
  topographic: "topographic",
  topo: "topographic",
  terrain: "topographic",
  satellite: "satellite"
};
function normalizeBase(key) {
  return BASE_ALIASES[(key || "").toLowerCase()] || "street";
}
function tileLayer(kind, extraOptions) {
  const def = TILE_DEFS[kind] || TILE_DEFS[normalizeBase(kind)] || TILE_DEFS.street;
  return L.tileLayer(def.url, { ...def.options, ...extraOptions });
}
function bordersOverlay() {
  return tileLayer("borders");
}
function weatherLayers(apiKey) {
  const attribution = 'Map data &copy; <a href="https://openweathermap.org">OpenWeatherMap</a>';
  const make = (layer, opacity) => L.tileLayer(`https://tile.openweathermap.org/map/${layer}/{z}/{x}/{y}.png?appid=${apiKey}`, { attribution, opacity, maxZoom: 21 });
  return { rain: make("precipitation_new", 0.7), clouds: make("clouds_new", 0.5) };
}
var PANEL_TRANSITION_MS = 220;
function createMapLayers(map, options = {}) {
  const opts = options;
  const root = typeof opts.root === "string" ? document.querySelector(opts.root) : opts.root ?? null;
  let darkMode = opts.darkMode || "light";
  const custom = { ...opts.custom || {} };
  const topoPaneName = opts.topoPane === undefined ? "topoPane" : opts.topoPane;
  if (topoPaneName && !map.getPane(topoPaneName)) {
    map.createPane(topoPaneName).style.zIndex = "401";
  }
  const streetLayer = tileLayer("street");
  const darkLayer = tileLayer("dark");
  const topographicLayer = tileLayer("topographic", topoPaneName ? { pane: topoPaneName } : undefined);
  const satelliteLayer = tileLayer("satellite");
  const bordersLayer = bordersOverlay();
  const weather = opts.apiKey ? weatherLayers(opts.apiKey) : null;
  function isDarkActive() {
    if (darkMode === "dark")
      return true;
    if (darkMode === "light")
      return false;
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function applyTopoFilter() {
    if (!topoPaneName)
      return;
    const pane = map.getPane(topoPaneName);
    if (!pane)
      return;
    pane.style.filter = isDarkActive() && map.hasLayer(topographicLayer) ? "invert(100%) hue-rotate(180deg) brightness(90%)" : "";
  }
  function syncStyleAttribute() {
    const target = opts.styleTarget ?? map.getContainer();
    target.dataset.mapStyle = isDarkActive() ? "dark" : "light";
  }
  function syncBaseLayer() {
    if (isDarkActive()) {
      if (map.hasLayer(streetLayer))
        map.removeLayer(streetLayer);
      if (!map.hasLayer(darkLayer))
        darkLayer.addTo(map);
    } else {
      if (map.hasLayer(darkLayer))
        map.removeLayer(darkLayer);
      if (!map.hasLayer(streetLayer))
        streetLayer.addTo(map);
    }
    applyTopoFilter();
    syncStyleAttribute();
  }
  map.on("layeradd layerremove", (e) => {
    if (e.layer === topographicLayer)
      applyTopoFilter();
  });
  if (darkMode === "system") {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      syncBaseLayer();
      syncButtons();
    });
  }
  function baseKey() {
    if (map.hasLayer(satelliteLayer))
      return "satellite";
    if (map.hasLayer(topographicLayer))
      return "topographic";
    return "street";
  }
  function getState() {
    return {
      base: baseKey(),
      weather: !!weather && (map.hasLayer(weather.rain) || map.hasLayer(weather.clouds)),
      borders: map.hasLayer(bordersLayer),
      darkMode
    };
  }
  const remember = opts.defaultBase === "remember" && !!opts.storageKey;
  function persistState() {
    if (remember) {
      try {
        const state = getState();
        localStorage.setItem(opts.storageKey, JSON.stringify({ base: state.base, weather: state.weather }));
      } catch {}
    }
    opts.onStateChange?.(getState());
  }
  function attributionText() {
    const parts = [];
    if (map.hasLayer(satelliteLayer)) {
      parts.push("© Esri");
    } else if (map.hasLayer(topographicLayer)) {
      parts.push("© OpenTopoMap");
    } else if (map.hasLayer(darkLayer)) {
      parts.push("© OSM · CARTO");
    } else {
      parts.push("© OpenStreetMap");
    }
    if (weather && (map.hasLayer(weather.rain) || map.hasLayer(weather.clouds))) {
      parts.push("© OpenWeatherMap");
    }
    if (map.hasLayer(bordersLayer) && !map.hasLayer(satelliteLayer)) {
      parts.push("© Esri");
    }
    parts.push("Leaflet");
    return parts.join(" · ");
  }
  if (opts.onAttribution) {
    map.on("layeradd layerremove", () => opts.onAttribution(attributionText()));
  }
  if (opts.loadingTarget) {
    const target = opts.loadingTarget;
    let loadingCount = 0;
    const onLoading = () => {
      loadingCount++;
      target.classList.add("tiles-loading");
    };
    const onLoad = () => {
      loadingCount = Math.max(0, loadingCount - 1);
      if (loadingCount === 0)
        target.classList.remove("tiles-loading");
    };
    for (const layer of [streetLayer, topographicLayer, satelliteLayer, darkLayer]) {
      layer.on("loading", onLoading);
      layer.on("load", onLoad);
      layer.on("error", onLoad);
    }
  }
  function layerButton(key) {
    return root?.querySelector(`[data-map-layer="${key}"]`) ?? null;
  }
  function syncButtons() {
    if (!root)
      return;
    const state = getState();
    layerButton("street")?.classList.toggle("active", state.base === "street");
    layerButton("terrain")?.classList.toggle("active", state.base === "topographic");
    layerButton("satellite")?.classList.toggle("active", state.base === "satellite");
    layerButton("weather")?.classList.toggle("active", state.weather);
    layerButton("borders")?.classList.toggle("active", state.borders);
    layerButton("dark")?.classList.toggle("active", isDarkActive());
    for (const [key, toggle] of Object.entries(custom)) {
      const active = toggle.activeWhenOff ? !toggle.isActive() : toggle.isActive();
      layerButton(key)?.classList.toggle("active", active);
    }
  }
  function setBase(rawKey) {
    const key = normalizeBase(rawKey);
    if (key !== "satellite" && map.hasLayer(satelliteLayer))
      map.removeLayer(satelliteLayer);
    if (key !== "topographic" && map.hasLayer(topographicLayer))
      map.removeLayer(topographicLayer);
    if (key === "satellite" && !map.hasLayer(satelliteLayer))
      satelliteLayer.addTo(map);
    if (key === "topographic" && !map.hasLayer(topographicLayer))
      topographicLayer.addTo(map);
    syncButtons();
    persistState();
  }
  function toggleBase(rawKey) {
    const key = normalizeBase(rawKey);
    if (key !== "street") {
      const layer = key === "satellite" ? satelliteLayer : topographicLayer;
      if (map.hasLayer(layer)) {
        setBase("street");
        return;
      }
    }
    setBase(key);
  }
  function toggleWeather() {
    if (!weather)
      return;
    if (map.hasLayer(weather.rain) || map.hasLayer(weather.clouds)) {
      map.removeLayer(weather.rain);
      map.removeLayer(weather.clouds);
    } else {
      weather.rain.addTo(map);
      weather.clouds.addTo(map);
    }
    syncButtons();
    persistState();
  }
  function toggleBorders() {
    if (map.hasLayer(bordersLayer))
      map.removeLayer(bordersLayer);
    else
      bordersLayer.addTo(map);
    syncButtons();
    persistState();
  }
  function setOverlay(key, on) {
    if (key === "weather") {
      if (!weather)
        return;
      const active = map.hasLayer(weather.rain) || map.hasLayer(weather.clouds);
      if (active !== on)
        toggleWeather();
    } else if (key === "borders") {
      if (map.hasLayer(bordersLayer) !== on)
        toggleBorders();
    }
  }
  function toggleCustom(key) {
    custom[key]?.toggle();
    syncButtons();
  }
  function registerToggle(key, toggle) {
    custom[key] = toggle;
    syncButtons();
  }
  function setDarkMode(mode) {
    darkMode = mode;
    syncBaseLayer();
    syncButtons();
  }
  function toggleDark() {
    const newMode = darkMode === "dark" ? "light" : "dark";
    setDarkMode(newMode);
    opts.onDarkModeChange?.(newMode);
    persistState();
  }
  const toggleBtn = root?.querySelector("[data-layers-toggle]") ?? null;
  const menu = root?.querySelector("[data-layers-menu]") ?? null;
  let panelCloseTimer = null;
  function isPanelOpen() {
    return root?.classList.contains("is-open") ?? false;
  }
  function closePanel() {
    if (!root || !root.classList.contains("is-open"))
      return;
    root.classList.remove("is-open");
    if (toggleBtn) {
      toggleBtn.classList.remove("active");
      toggleBtn.setAttribute("aria-expanded", "false");
    }
    if (menu) {
      menu.setAttribute("aria-hidden", "true");
      let closed = false;
      const finishClose = (e) => {
        if (e && e.target !== menu)
          return;
        if (closed || root.classList.contains("is-open"))
          return;
        closed = true;
        if (panelCloseTimer) {
          clearTimeout(panelCloseTimer);
          panelCloseTimer = null;
        }
        menu.hidden = true;
        menu.removeEventListener("transitionend", finishClose);
      };
      menu.addEventListener("transitionend", finishClose);
      panelCloseTimer = setTimeout(finishClose, PANEL_TRANSITION_MS + 40);
    }
  }
  function openPanel() {
    if (!root)
      return;
    if (panelCloseTimer) {
      clearTimeout(panelCloseTimer);
      panelCloseTimer = null;
    }
    if (menu) {
      menu.hidden = false;
      menu.setAttribute("aria-hidden", "false");
      menu.offsetWidth;
    }
    root.classList.add("is-open");
    if (toggleBtn) {
      toggleBtn.classList.add("active");
      toggleBtn.setAttribute("aria-expanded", "true");
    }
  }
  function togglePanel() {
    if (isPanelOpen())
      closePanel();
    else
      openPanel();
  }
  if (toggleBtn) {
    toggleBtn.addEventListener("click", togglePanel);
    document.addEventListener("click", (e) => {
      if (root && !root.contains(e.target))
        closePanel();
    });
  }
  if (root) {
    root.querySelectorAll("[data-map-layer]").forEach((btn) => {
      const key = btn.dataset.mapLayer;
      const kind = btn.dataset.layerKind || "custom";
      if (key === "weather" && !weather) {
        btn.hidden = true;
        return;
      }
      btn.addEventListener("click", () => {
        if (kind === "base")
          toggleBase(key === "terrain" ? "topographic" : key);
        else if (key === "weather")
          toggleWeather();
        else if (key === "borders")
          toggleBorders();
        else if (key === "dark")
          toggleDark();
        else
          toggleCustom(key);
      });
    });
  }
  syncBaseLayer();
  (function applyInitialLayers() {
    let base = opts.defaultBase || "street";
    let weatherOn = (opts.initialOverlays || []).includes("weather");
    const bordersOn = (opts.initialOverlays || []).includes("borders");
    if (base === "remember") {
      base = "street";
      try {
        const saved = JSON.parse(localStorage.getItem(opts.storageKey || "") || "null");
        if (saved) {
          base = saved.base || "street";
          weatherOn = !!saved.weather;
        }
      } catch {}
    }
    const key = normalizeBase(base);
    if (key === "satellite")
      satelliteLayer.addTo(map);
    else if (key === "topographic")
      topographicLayer.addTo(map);
    if (weatherOn && weather) {
      weather.rain.addTo(map);
      weather.clouds.addTo(map);
    }
    if (bordersOn)
      bordersLayer.addTo(map);
    syncButtons();
  })();
  return {
    setBase,
    toggleBase,
    toggleWeather,
    toggleBorders,
    setOverlay,
    toggleCustom,
    registerToggle,
    toggleDark,
    setDarkMode,
    isDarkActive,
    openPanel,
    closePanel,
    togglePanel,
    isPanelOpen,
    syncButtons,
    getState,
    baseKey
  };
}

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
    boundaryUrl: d.boundaryUrl || "",
    photoGalleryJsonUrl: d.photoGalleryJsonUrl || "",
    markupFillOpacity: d.markupFillOpacity ? Number.parseInt(d.markupFillOpacity, 10) : 87,
    markupBorderOpacity: d.markupBorderOpacity ? Number.parseInt(d.markupBorderOpacity, 10) : 100,
    showOnboardingTips: d.showOnboardingTips === "1"
  };
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
  map.createPane("markupPane").style.zIndex = "550";
  map.createPane("boundaryPane").style.zIndex = "540";
  let scrollEnableTimer;
  mapEl.addEventListener("mouseenter", () => {
    scrollEnableTimer = setTimeout(() => map.scrollWheelZoom.enable(), 750);
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
    mainMarker.on("dragend", () => {
      const pos = mainMarker.getLatLng();
      fetch(`/dashboard/rest/pins/${cfg.mainMarkerOwnerUuid}/`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ latitude: pos.lat.toFixed(6), longitude: pos.lng.toFixed(6) })
      }).then((r) => {
        if (!r.ok)
          throw new Error;
        return r.json();
      }).then(() => {
        mainMarkerLat = pos.lat;
        mainMarkerLng = pos.lng;
        toast.success("Pin moved.");
      }).catch(() => {
        toast.error("Failed to save new position.");
        mainMarker.setLatLng([mainMarkerLat, mainMarkerLng]);
      });
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
  const detailPinColors = { building: "#6b7280", entrance: "#16a34a", poi: "#d97706", danger: "#dc2626", other: "#7c3aed", location: "#2563eb" };
  const detailPinIcons = { building: "business", entrance: "door_front", poi: "star", danger: "warning", other: "info", location: "place" };
  const detailPinLayer = L.layerGroup();
  const markupLayer = L.layerGroup();
  const detailsLayer = L.layerGroup([detailPinLayer, markupLayer]).addTo(map);
  const photoLayer = L.layerGroup().addTo(map);
  createMapLayers(map, {
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
      }
    }
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
      countLabel.textContent = `${total} Layer${total === 1 ? "" : "s"}`;
    if (handle)
      handle.style.display = total ? "" : "none";
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
      li.innerHTML = `
                <span class="material-icons detail-pin-list-item-icon" style="color:${escHtml(item.color)}">${escHtml(markupIcon[item.markup_type] || "edit")}</span>
                <span class="detail-pin-list-item-name">${escHtml(displayName)}</span>
                ${ownerMeta}
                ${item.owner_name ? "" : `<button type="button" class="detail-pin-list-item-delete" title="Delete"><i class="material-symbols-outlined">close</i></button>`}`;
      li.addEventListener("click", (e) => {
        if (e.target.closest(".detail-pin-list-item-delete"))
          return;
        if (item.owner_name)
          return;
        toolbar.openMarkupEditDialog(item);
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
  function detailPinPopupContent(entry) {
    const el = document.createElement("div");
    el.className = "pin-popup child-pin-popup";
    const owner = entry.owner_name ? `<div class="popup-child-parent"><i class="material-symbols-outlined">subdirectory_arrow_right</i> Inside ${escHtml(entry.owner_name)}</div>` : "";
    el.innerHTML = `
            <div class="popup-title">${escHtml(entry.name || "Sub pin")}</div>
            ${owner}
            ${entry.description ? `<div class="popup-desc">${escHtml(entry.description)}</div>` : ""}
            <div class="popup-actions">
                ${entry.url ? `<a href="${escHtml(entry.url)}" class="view-full-pin">View Details</a>` : ""}
            </div>`;
    if (!entry.owner_name) {
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "edit-pin-button";
      editBtn.title = "Edit sub pin";
      editBtn.innerHTML = '<i class="material-symbols-outlined">edit</i>';
      editBtn.addEventListener("click", () => {
        map.closePopup();
        openDetailPinEditDialog(entry);
      });
      el.querySelector(".popup-actions").appendChild(editBtn);
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
        const tooltip = entry.owner_name && dp.name ? `${dp.name} — inside ${entry.owner_name}` : dp.name;
        if (tooltip)
          marker.bindTooltip(tooltip, { permanent: false, direction: "top", className: "detail-pin-tooltip" });
        if (entry.url) {
          marker.bindPopup(detailPinPopupContent(entry));
        } else {
          marker.on("click", () => openDetailPinEditDialog(entry));
        }
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
  const toolbar = window.createMarkupToolbar(map, markupLayer, {
    markupJsonUrl: cfg.markupJsonUrl,
    markupCreateUrl: cfg.markupCreateUrl,
    markupEditUrlTemplate: cfg.markupEditUrlTemplate,
    markupFillOpacity: cfg.markupFillOpacity,
    markupBorderOpacity: cfg.markupBorderOpacity,
    lineFinishTipDismissed: () => !cfg.showOnboardingTips,
    onBuildDetailList: () => buildDetailList(),
    onClearDetailPinHighlight: () => clearDetailPinHighlight(),
    onCloseDetailPinPanel: () => closeDetailPinPanel()
  });
  window.startMarkupDraw = toolbar.startMarkupDraw;
  window.startShapeDraw = toolbar.startShapeDraw;
  window.startTextPlacement = toolbar.startTextPlacement;
  window.closeMarkupPanel = toolbar.closeMarkupPanel;
  window._closeMarkupDraw = toolbar.closeOrFinishDraw;
  window.deleteMarkupEdit = toolbar.deleteMarkupEdit;
  window.openMarkupEditDialog = toolbar.openMarkupEditDialog;
  window.loadMarkup = toolbar.loadMarkup;
  loadDetailPins();
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
        addGeoJSONPolygons(detailBuildingItems, entry.polygon, DETAIL_BUILDING_STYLE, "Building boundary (from a sub pin)");
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
      if (data.pending && attempt < 30) {
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
      if (data.pending)
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
    return {
      name: document.getElementById("dp-name").value.trim(),
      description: document.getElementById("dp-description").value.trim(),
      pin_type: document.getElementById("dp-type").value,
      icon: document.getElementById("dp-icon").value || null,
      color: document.getElementById("dp-color").value || null,
      bg_color: document.getElementById("dp-bg-color").value || null,
      bg_opacity: Number.parseInt(document.getElementById("dp-bg-opacity").value, 10),
      border_color: document.getElementById("dp-border-color").value || null,
      border_opacity: Number.parseInt(document.getElementById("dp-border-opacity").value, 10),
      latitude: document.getElementById("dp-lat").value,
      longitude: document.getElementById("dp-lon").value
    };
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
  document.getElementById("dp-type")?.addEventListener("change", updateDpMarkerIcon);
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
