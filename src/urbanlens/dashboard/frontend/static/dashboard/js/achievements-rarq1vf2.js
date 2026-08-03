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
    let attributionFrame = null;
    map.on("layeradd layerremove", () => {
      if (attributionFrame !== null)
        return;
      attributionFrame = window.requestAnimationFrame(() => {
        attributionFrame = null;
        opts.onAttribution(attributionText());
      });
    });
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
    const layer = custom[key];
    if (!layer)
      return;
    const wasActive = layer.isActive();
    layer.toggle();
    if (key === "details" && wasActive)
      setOverlay("borders", false);
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

export { tileLayer, createMapLayers };
