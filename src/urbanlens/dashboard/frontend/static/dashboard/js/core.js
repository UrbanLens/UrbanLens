(() => {
  // src/urbanlens/dashboard/frontend/ts/shared/location-search-engine.ts
  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }
  var PLUS_CODE_RE = /^([23456789CFGHJMPQRVWXcfghjmpqrvwx]{4,8}\+[23456789CFGHJMPQRVWXcfghjmpqrvwx]{0,2})([\s,].*)?$/;
  function isPlusCode(q) {
    return PLUS_CODE_RE.test((q || "").trim());
  }
  async function resolvePlusCode(q) {
    const googleMaps = window.google?.maps;
    if (googleMaps?.Geocoder) {
      return new Promise((resolve) => {
        new googleMaps.Geocoder().geocode({ address: q.trim() }, (results, status) => {
          if (status === "OK" && results && results.length > 0) {
            const loc = results[0].geometry.location;
            resolve({ lat: loc.lat(), lng: loc.lng() });
          } else {
            resolve(null);
          }
        });
      });
    }
    try {
      const r = await fetch(`https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(q.trim())}&format=json&limit=1`, {
        headers: { Accept: "application/json", "Accept-Language": "en" }
      });
      const data = await r.json();
      if (data && data.length > 0)
        return { lat: Number.parseFloat(data[0].lat), lng: Number.parseFloat(data[0].lon) };
    } catch {}
    return null;
  }
  function parseCoordinates(q) {
    const m1 = q.trim().match(/^(-?\d{1,3}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)$/);
    if (m1) {
      const a = Number.parseFloat(m1[1]);
      const b = Number.parseFloat(m1[2]);
      if (Number.isFinite(a) && Number.isFinite(b)) {
        if (Math.abs(a) <= 90 && Math.abs(b) <= 180)
          return { lat: a, lng: b };
        if (Math.abs(b) <= 90 && Math.abs(a) <= 180)
          return { lat: b, lng: a };
      }
    }
    const dmsRe = /(\d{1,3})[°\s]+(\d{1,2})['\s]+(\d{1,2}(?:\.\d+)?)["\s]*([NSns])\s+(\d{1,3})[°\s]+(\d{1,2})['\s]+(\d{1,2}(?:\.\d+)?)["\s]*([EWew])/;
    const m2 = q.match(dmsRe);
    if (m2) {
      const lat = (Number.parseFloat(m2[1]) + Number.parseFloat(m2[2]) / 60 + Number.parseFloat(m2[3]) / 3600) * (/[Ss]/.test(m2[4]) ? -1 : 1);
      const lng = (Number.parseFloat(m2[5]) + Number.parseFloat(m2[6]) / 60 + Number.parseFloat(m2[7]) / 3600) * (/[Ww]/.test(m2[8]) ? -1 : 1);
      if (Math.abs(lat) <= 90 && Math.abs(lng) <= 180)
        return { lat, lng };
    }
    return null;
  }
  function sectionKey(label) {
    const l = label.toLowerCase();
    if (l.includes("pin") || l.includes("location"))
      return "pins";
    if (l.includes("google"))
      return "places";
    if (l.includes("place") || l.includes("address"))
      return "suggestions";
    if (l.includes("cit"))
      return "cities";
    if (l.includes("navigation") || l.includes("quick"))
      return "navigation";
    if (l.includes("recent") || l.includes("history"))
      return "history";
    if (l.includes("coord"))
      return "coordinates";
    return "suggestions";
  }
  async function nominatimSearch(query, { limit = 5, viewbox = null } = {}) {
    const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=${limit}&addressdetails=1` + (viewbox ? `&viewbox=${viewbox}&bounded=0` : "");
    const r = await fetch(url, { headers: { Accept: "application/json", "Accept-Language": "en" } });
    return r.json();
  }
  function generateDerivedSuggestions(query) {
    const results = [];
    const words = query.trim().split(/\s+/).filter((w) => w.length > 0);
    if (words.length >= 2) {
      const last = words[words.length - 1];
      const rest = words.slice(0, -1).join(" ");
      results.push({
        type: "derived",
        icon: "search",
        title: `Search for "${rest}" near ${last}`,
        subtitle: "Jump to result",
        geocodeQuery: `${rest} near ${last}`
      });
      if (words.length === 2) {
        results.push({
          type: "derived",
          icon: "search",
          title: `Search for "${words[1]} ${words[0]}"`,
          subtitle: "Jump to result",
          geocodeQuery: `${words[1]} ${words[0]}`
        });
      }
    }
    results.push({
      type: "external",
      icon: "open_in_new",
      title: `Search Google Maps for "${query}"`,
      subtitle: "Opens in a new tab",
      externalUrl: `https://maps.google.com/maps?q=${encodeURIComponent(query)}`
    });
    return results;
  }
  function create(options) {
    const {
      input,
      suggestions,
      bar,
      clearBtn = null,
      historyBtn = null,
      historyKey = null,
      recentPinsKey = null,
      sources = {},
      resolvePlaceUrl = null,
      home = null,
      enableMyLocation = true,
      getUserLocationCache = null,
      setUserLocationCache = null,
      onGeolocationVisit = null,
      defaultZoom = 15,
      onSelect,
      onMultiResult = null,
      onSearchStart = null,
      onFetchingChange = null,
      onToast = null
    } = options;
    const barEl = bar || input.parentElement;
    function toast(level, message) {
      if (onToast) {
        onToast(level, message);
        return;
      }
      if (typeof window.toastr !== "undefined") {
        (window.toastr[level] ?? window.toastr.info)(message);
        return;
      }
      if (level === "error")
        console.error(message);
      else
        console.warn(message);
    }
    function setFetching(on, message) {
      onFetchingChange?.(on, message);
    }
    function getHistory() {
      if (!historyKey)
        return [];
      try {
        const raw = localStorage.getItem(historyKey);
        const parsed = JSON.parse(raw ?? "[]");
        return Array.isArray(parsed) ? parsed : [];
      } catch {
        return [];
      }
    }
    function addToHistory(q) {
      if (!historyKey || !q || !q.trim())
        return;
      try {
        const deduped = getHistory().filter((x) => x !== q);
        deduped.unshift(q);
        localStorage.setItem(historyKey, JSON.stringify(deduped.slice(0, 20)));
      } catch {}
    }
    function getRecentPins(limit) {
      if (!recentPinsKey)
        return [];
      try {
        const raw = localStorage.getItem(recentPinsKey);
        const list = raw ? JSON.parse(raw) : [];
        return list.slice(0, limit);
      } catch {
        return [];
      }
    }
    function trackRecentPin(entry) {
      if (!recentPinsKey)
        return;
      try {
        const raw = localStorage.getItem(recentPinsKey);
        const list = raw ? JSON.parse(raw) : [];
        const filtered = list.filter((p) => p.slug !== entry.slug);
        filtered.unshift(entry);
        localStorage.setItem(recentPinsKey, JSON.stringify(filtered.slice(0, 10)));
      } catch {}
    }
    function getCachedUserLocation() {
      return getUserLocationCache ? getUserLocationCache() : null;
    }
    function cacheUserLocation(lat, lng) {
      setUserLocationCache?.(lat, lng);
    }
    let addrBarTimer;
    let historyNavIdx = -1;
    let activeIdx = -1;
    let searchSeq = 0;
    let updateHistoryBtn = () => {};
    function makeCollapsibleHdr(label, key, slot) {
      const hdr = document.createElement("div");
      hdr.className = "addr-suggestion-group-hdr";
      hdr.dataset.collapsible = "1";
      hdr.dataset.section = key;
      hdr.textContent = label;
      const hint = document.createElement("span");
      hint.className = "addr-section-collapsed-hint";
      hdr.appendChild(hint);
      hdr.addEventListener("click", () => {
        slot.classList.toggle("is-collapsed");
        const collapsed = slot.classList.contains("is-collapsed");
        hdr.dataset.collapsed = collapsed ? "1" : "";
        const count = slot.querySelectorAll(".addr-suggestion").length;
        hint.textContent = collapsed ? `${count} suggestion${count !== 1 ? "s" : ""}` : "";
      });
      return hdr;
    }
    function makeSlot(container) {
      const div = document.createElement("div");
      div.className = "addr-source-slot";
      container.appendChild(div);
      return div;
    }
    function highlight(idx) {
      const items = [...suggestions.querySelectorAll(".addr-suggestion")];
      activeIdx = Math.max(-1, Math.min(items.length - 1, idx));
      items.forEach((el, i) => el.classList.toggle("addr-suggestion--active", i === activeIdx));
      if (activeIdx >= 0)
        items[activeIdx]?.scrollIntoView({ block: "nearest" });
    }
    function clearSearch() {
      const wasEmpty = !input.value.trim();
      input.value = "";
      clearBtn?.classList.remove("addr-search-clear--visible");
      suggestions.hidden = true;
      searchSeq++;
      activeIdx = -1;
      historyNavIdx = -1;
      if (!wasEmpty)
        input.focus();
      updateHistoryBtn();
    }
    async function runNearQuery(geocodeQuery, fallbackTitle) {
      onSearchStart?.();
      setFetching(true, "Searching…");
      try {
        const nearMatch = geocodeQuery.match(/^(.+?) near (.+)$/i);
        if (!nearMatch) {
          const data = await nominatimSearch(geocodeQuery, { limit: 1 });
          if (data?.length) {
            const lat = Number.parseFloat(data[0].lat);
            const lng = Number.parseFloat(data[0].lon);
            onSelect({ lat, lng, zoom: 14, title: data[0].display_name || fallbackTitle, type: "address", raw: data[0] });
          } else {
            toast("warning", `No results for "${geocodeQuery}"`);
          }
          return;
        }
        const searchTerm = nearMatch[1].trim();
        const anchorText = nearMatch[2].trim();
        const anchorData = await nominatimSearch(anchorText, { limit: 1 });
        const anchorLat = anchorData[0] ? Number.parseFloat(anchorData[0].lat) : null;
        const anchorLng = anchorData[0] ? Number.parseFloat(anchorData[0].lon) : null;
        if (anchorLat == null) {
          toast("warning", `Couldn't find "${anchorText}" - try a more specific location`);
          return;
        }
        const pad = 0.5;
        const searchData = await nominatimSearch(searchTerm, {
          limit: 5,
          viewbox: `${anchorLng - pad},${anchorLat - pad},${anchorLng + pad},${anchorLat + pad}`
        });
        if (!searchData?.length) {
          onSelect({ lat: anchorLat, lng: anchorLng, zoom: 13, title: anchorText, type: "address" });
          toast("warning", `No "${searchTerm}" found near ${anchorText}`);
          return;
        }
        const results = searchData.map((r) => ({
          lat: Number.parseFloat(r.lat),
          lng: Number.parseFloat(r.lon),
          title: r.display_name || searchTerm,
          raw: r
        }));
        if (onMultiResult) {
          onMultiResult({ searchTerm, anchorText, anchorLat, anchorLng, results });
        } else {
          const first = results[0];
          onSelect({ lat: first.lat, lng: first.lng, zoom: 14, title: first.title, type: "address" });
          if (results.length > 1)
            toast("info", `Found ${results.length} results for "${searchTerm}"`);
        }
      } catch {
        toast("error", "Search failed - check your connection.");
      } finally {
        setFetching(false);
      }
    }
    function buildSuggestionItem(result) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `addr-suggestion addr-suggestion--${result.type}`;
      const subtitle = result.subtitle ? `<span class="addr-suggestion-sub">${escHtml(result.subtitle)}</span>` : "";
      btn.innerHTML = `
            <i class="material-icons addr-suggestion-icon">${escHtml(result.icon || "place")}</i>
            <span class="addr-suggestion-content">
                <span class="addr-suggestion-title">${escHtml(result.title)}</span>
                ${subtitle}
            </span>`;
      btn.addEventListener("mousedown", async (e) => {
        e.preventDefault();
        suggestions.hidden = true;
        activeIdx = -1;
        if (result.externalUrl) {
          window.open(result.externalUrl, "_blank", "noopener");
          return;
        }
        if (result.geocodeQuery) {
          input.value = result.geocodeQuery;
          clearBtn?.classList.add("addr-search-clear--visible");
          updateHistoryBtn();
          addToHistory(result.geocodeQuery);
          await runNearQuery(result.geocodeQuery, result.title);
          return;
        }
        if (result.searchQuery) {
          input.value = result.searchQuery;
          clearBtn?.classList.add("addr-search-clear--visible");
          updateHistoryBtn();
          addToHistory(result.searchQuery);
          startMultiSearch(result.searchQuery);
          suggestions.hidden = false;
          return;
        }
        if (result.action === "gps") {
          input.value = "My Location";
          clearBtn?.classList.add("addr-search-clear--visible");
          updateHistoryBtn();
          recenterToUserLocation();
          return;
        }
        input.value = result.title;
        clearBtn?.classList.add("addr-search-clear--visible");
        updateHistoryBtn();
        addToHistory(result.title);
        if (result.lat != null && result.lng != null) {
          onSelect({
            lat: result.lat,
            lng: result.lng,
            zoom: result.zoom || defaultZoom,
            title: result.title,
            type: result.type,
            pinSlug: result.pin_slug,
            raw: result
          });
        } else if (result.place_id) {
          if (!resolvePlaceUrl)
            return;
          try {
            const r = await fetch(`${resolvePlaceUrl}?place_id=${encodeURIComponent(result.place_id)}`, {
              headers: { "X-Requested-With": "XMLHttpRequest" }
            });
            if (r.ok) {
              const d = await r.json();
              if (d.lat != null) {
                if (d.name)
                  input.value = d.name;
                onSelect({ lat: d.lat, lng: d.lng, zoom: result.zoom || defaultZoom, title: d.name || result.title, type: result.type, raw: d });
              }
            } else {
              toast("error", "Could not resolve location - try searching again.");
            }
          } catch {
            toast("error", "Could not resolve location - check your connection.");
          }
        }
      });
      return btn;
    }
    async function fetchSourceIntoSlot(seq, label, url, parser, slot, onDone, fetchOpts) {
      slot.innerHTML = `<div class="addr-source-loading" data-seq="${seq}">
            <span class="addr-spinner"></span><span class="addr-source-loading-label">${escHtml(label)}...</span>
        </div>`;
      suggestions.hidden = false;
      try {
        const resp = await fetch(url, fetchOpts ?? { headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (seq !== searchSeq)
          return;
        slot.innerHTML = "";
        if (!resp.ok) {
          onDone?.(false);
          return;
        }
        const raw = await resp.json();
        if (seq !== searchSeq)
          return;
        const results = parser(raw);
        if (!results?.length) {
          onDone?.(false);
          return;
        }
        slot.appendChild(makeCollapsibleHdr(label, sectionKey(label), slot));
        for (const r of results)
          slot.appendChild(buildSuggestionItem(r));
        suggestions.hidden = false;
        highlight(-1);
        onDone?.(true);
      } catch {
        if (seq === searchSeq) {
          slot.innerHTML = "";
          onDone?.(false);
        }
      }
    }
    function recenterToUserLocation() {
      if (!navigator.geolocation) {
        toast("warning", "Geolocation is not supported by your browser.");
        return;
      }
      suggestions.hidden = true;
      const cached = getCachedUserLocation();
      if (cached) {
        onSelect({ lat: cached.lat, lng: cached.lng, zoom: defaultZoom, title: "My Location", type: "mylocation" });
      } else {
        setFetching(true, "Getting your location…");
      }
      navigator.geolocation.getCurrentPosition((pos) => {
        cacheUserLocation(pos.coords.latitude, pos.coords.longitude);
        onGeolocationVisit?.(pos.coords.latitude, pos.coords.longitude);
        onSelect({ lat: pos.coords.latitude, lng: pos.coords.longitude, zoom: defaultZoom, title: "My Location", type: "mylocation" });
        if (!cached)
          setFetching(false);
      }, () => {
        if (!cached) {
          setFetching(false);
          toast("warning", "Could not get your location. Check permissions.");
        }
      }, { timeout: 8000, maximumAge: 300000 });
    }
    function geocodeAddress() {
      const q = (input.value || "").trim();
      if (!q)
        return;
      const items = [...suggestions.querySelectorAll(".addr-suggestion")];
      if (activeIdx >= 0 && activeIdx < items.length) {
        items[activeIdx].dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        return;
      }
      suggestions.hidden = true;
      const coords = parseCoordinates(q);
      if (coords) {
        onSelect({ lat: coords.lat, lng: coords.lng, zoom: 16, title: q, type: "coordinates" });
        return;
      }
      if (isPlusCode(q)) {
        setFetching(true, "Resolving Plus Code…");
        resolvePlusCode(q).then((resolved) => {
          setFetching(false);
          if (resolved) {
            onSelect({ lat: resolved.lat, lng: resolved.lng, zoom: 16, title: q, type: "plus_code" });
          } else {
            toast("warning", "Could not resolve Plus Code - try adding a city name.");
          }
        }).catch(() => {
          setFetching(false);
          toast("error", "Plus Code resolution failed.");
        });
        return;
      }
      addToHistory(q);
      nominatimSearch(q, { limit: 1 }).then((results) => {
        if (!results.length) {
          toast("warning", "Address not found.");
          return;
        }
        onSelect({ lat: Number.parseFloat(results[0].lat), lng: Number.parseFloat(results[0].lon), zoom: 16, title: results[0].display_name || q, type: "address", raw: results[0] });
      }).catch(() => toast("error", "Geocoding failed - check your connection."));
    }
    function startMultiSearch(query) {
      onSearchStart?.();
      const seq = ++searchSeq;
      const box = suggestions;
      box.innerHTML = "";
      box.hidden = true;
      activeIdx = -1;
      const coordSlot = makeSlot(box);
      const localSlot = sources.localPins ? makeSlot(box) : null;
      const osmSlot = sources.osmNominatim !== false ? makeSlot(box) : null;
      const placesSlot = sources.googlePlaces ? makeSlot(box) : null;
      const noMsgSlot = makeSlot(box);
      const derivedSlot = makeSlot(box);
      let pendingSources = [localSlot, osmSlot, placesSlot].filter(Boolean).length;
      let primaryHits = 0;
      function onPrimaryDone(hasResults) {
        if (hasResults)
          primaryHits++;
        if (--pendingSources === 0 && primaryHits === 0 && !parseCoordinates(query) && !isPlusCode(query)) {
          noMsgSlot.innerHTML = '<div class="addr-no-results">No exact matches found</div>';
        }
      }
      const coords = parseCoordinates(query);
      if (coords) {
        coordSlot.appendChild(makeCollapsibleHdr("Coordinates", "coordinates", coordSlot));
        coordSlot.appendChild(buildSuggestionItem({
          type: "coordinates",
          title: `${coords.lat.toFixed(6)}, ${coords.lng.toFixed(6)}`,
          subtitle: "Jump to these exact coordinates",
          lat: coords.lat,
          lng: coords.lng,
          zoom: 16,
          icon: "my_location"
        }));
        box.hidden = false;
      } else if (isPlusCode(query)) {
        coordSlot.appendChild(makeCollapsibleHdr("Plus Code", "coordinates", coordSlot));
        const pcBtn = buildSuggestionItem({ type: "plus_code", title: query.trim(), subtitle: "Jump to this Plus Code location", icon: "pin_drop" });
        const freshBtn = pcBtn.cloneNode(true);
        freshBtn.addEventListener("mousedown", async (e) => {
          e.preventDefault();
          e.stopImmediatePropagation();
          box.hidden = true;
          activeIdx = -1;
          setFetching(true, "Resolving Plus Code…");
          try {
            const resolved = await resolvePlusCode(query);
            if (resolved) {
              onSelect({ lat: resolved.lat, lng: resolved.lng, zoom: 16, title: query.trim(), type: "plus_code" });
            } else {
              toast("warning", "Could not resolve Plus Code - try adding a city name.");
            }
          } catch {
            toast("error", "Plus Code resolution failed.");
          } finally {
            setFetching(false);
          }
        }, true);
        coordSlot.appendChild(freshBtn);
        box.hidden = false;
      }
      if (localSlot) {
        fetchSourceIntoSlot(seq, "Your Pins & Locations", `${sources.localPins.url}?q=${encodeURIComponent(query)}`, (data) => data.results || [], localSlot, onPrimaryDone);
      }
      if (osmSlot) {
        fetchSourceIntoSlot(seq, "Places & Addresses", `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=5&addressdetails=1`, (data) => (data || []).map((r) => ({
          type: "address",
          title: r.name || (r.display_name || "").split(",")[0].trim(),
          subtitle: r.display_name || "",
          lat: Number.parseFloat(r.lat),
          lng: Number.parseFloat(r.lon),
          zoom: 15,
          icon: "place"
        })), osmSlot, onPrimaryDone, { headers: { Accept: "application/json", "Accept-Language": "en" } });
      }
      if (placesSlot) {
        fetchSourceIntoSlot(seq, "Google Places", `${sources.googlePlaces.url}?q=${encodeURIComponent(query)}`, (data) => data.disabled ? [] : data.results || [], placesSlot, (hasResults) => {
          onPrimaryDone(hasResults);
          if (hasResults) {
            derivedSlot.querySelectorAll(".addr-suggestion--external").forEach((el) => el.remove());
            if (!derivedSlot.querySelector(".addr-suggestion")) {
              derivedSlot.querySelector(".addr-suggestion-group-hdr")?.remove();
            }
          }
        });
      }
      const derived = generateDerivedSuggestions(query);
      derivedSlot.appendChild(makeCollapsibleHdr("Search Suggestions", "suggestions", derivedSlot));
      for (const r of derived)
        derivedSlot.appendChild(buildSuggestionItem(r));
      box.hidden = false;
    }
    function showEmptySuggestions() {
      const seq = ++searchSeq;
      const box = suggestions;
      box.innerHTML = "";
      activeIdx = -1;
      function emptySection(label, key) {
        const slot = document.createElement("div");
        slot.className = "addr-source-slot";
        slot.appendChild(makeCollapsibleHdr(label, key, slot));
        box.appendChild(slot);
        return slot;
      }
      const history = getHistory();
      if (history.length) {
        const histSlot = emptySection("Recent Searches", "history");
        for (const q of history.slice(0, 3)) {
          histSlot.appendChild(buildSuggestionItem({ type: "history", icon: "history", title: q, subtitle: "Recent search", searchQuery: q }));
        }
      }
      const hasGeo = enableMyLocation && !!navigator.geolocation;
      const hasHome = !!home;
      if (hasGeo || hasHome) {
        const navSlot = emptySection("Quick Navigation", "navigation");
        if (hasGeo) {
          navSlot.appendChild(buildSuggestionItem({ type: "mylocation", icon: "my_location", title: "My Location", subtitle: "Jump to your current GPS position", action: "gps" }));
        }
        if (hasHome) {
          navSlot.appendChild(buildSuggestionItem({
            type: "home",
            icon: "home",
            title: home.title || "Home",
            subtitle: home.subtitle || "Default map center",
            lat: home.lat,
            lng: home.lng,
            zoom: home.zoom || defaultZoom
          }));
        }
      }
      const recentPins = getRecentPins(2);
      if (recentPins.length) {
        const recentSlot = emptySection("Recently Viewed", "recent");
        for (const pin of recentPins) {
          recentSlot.appendChild(buildSuggestionItem({ type: "pin", icon: "push_pin", title: pin.name || "Unnamed", subtitle: "Recently viewed", lat: pin.lat, lng: pin.lng, zoom: 16, pin_slug: pin.slug }));
        }
      }
      let citySlot = null;
      if (sources.topCities) {
        citySlot = makeSlot(box);
        fetchSourceIntoSlot(seq, "Your Top Cities", sources.topCities.url, (data) => data.results || [], citySlot, (hasResults) => {
          if (!hasResults && !box.querySelectorAll(".addr-suggestion").length)
            box.hidden = true;
        });
      }
      const hasStatic = box.querySelectorAll(".addr-suggestion").length > 0;
      box.hidden = !hasStatic && !(citySlot && citySlot.firstChild);
      if (citySlot?.firstChild)
        box.hidden = false;
    }
    let mouseOverSuggestions = false;
    let blurTimer;
    updateHistoryBtn = () => {
      const isEmpty = !input.value.trim();
      const hasHistory = getHistory().length > 0;
      historyBtn?.classList.toggle("addr-search-history--visible", isEmpty && hasHistory);
    };
    suggestions.addEventListener("mouseenter", () => {
      mouseOverSuggestions = true;
    });
    suggestions.addEventListener("mouseleave", () => {
      mouseOverSuggestions = false;
    });
    suggestions.addEventListener("mouseup", () => {
      if (mouseOverSuggestions && document.activeElement !== input)
        input.focus();
    });
    function hideSuggestionsSoon() {
      clearTimeout(blurTimer);
      blurTimer = setTimeout(() => {
        if (!barEl.contains(document.activeElement) && !mouseOverSuggestions) {
          suggestions.hidden = true;
          activeIdx = -1;
        }
      }, 200);
    }
    barEl.addEventListener("mousedown", (e) => {
      const target = e.target;
      if (target.closest(".addr-search-history, .addr-search-clear, .addr-suggestion"))
        return;
      if (target !== input) {
        e.preventDefault();
        input.focus();
      }
    });
    suggestions.addEventListener("mousedown", (e) => e.preventDefault());
    suggestions.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
    historyBtn?.addEventListener("click", () => {
      const hist = getHistory();
      if (!hist.length)
        return;
      historyNavIdx = (historyNavIdx + 1) % hist.length;
      input.value = hist[historyNavIdx];
      clearBtn?.classList.add("addr-search-clear--visible");
      updateHistoryBtn();
      startMultiSearch(hist[historyNavIdx]);
    });
    clearBtn?.addEventListener("click", clearSearch);
    input.addEventListener("input", function() {
      const q = this.value.trim();
      clearBtn?.classList.toggle("addr-search-clear--visible", !!q);
      updateHistoryBtn();
      clearTimeout(addrBarTimer);
      if (!q) {
        searchSeq++;
        activeIdx = -1;
        historyNavIdx = -1;
        showEmptySuggestions();
        return;
      }
      suggestions.hidden = true;
      addrBarTimer = setTimeout(() => startMultiSearch(q), 250);
    });
    input.addEventListener("keydown", function(e) {
      const items = [...suggestions.querySelectorAll(".addr-suggestion")];
      if (e.key === "ArrowDown") {
        e.preventDefault();
        highlight(activeIdx + 1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (!this.value.trim() && activeIdx < 0) {
          const hist = getHistory().slice(0, 10);
          if (!hist.length) {
            if (suggestions.hidden)
              showEmptySuggestions();
            return;
          }
          historyNavIdx = historyNavIdx < 0 ? 0 : (historyNavIdx + 1) % hist.length;
          const chosen = hist[historyNavIdx];
          this.value = chosen;
          clearBtn?.classList.add("addr-search-clear--visible");
          updateHistoryBtn();
          suggestions.hidden = true;
          activeIdx = -1;
          clearTimeout(addrBarTimer);
          addrBarTimer = setTimeout(() => startMultiSearch(chosen), 200);
        } else {
          highlight(activeIdx - 1);
        }
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (activeIdx >= 0 && activeIdx < items.length) {
          items[activeIdx].dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        } else {
          geocodeAddress();
        }
      } else if (e.key === "Escape") {
        clearSearch();
      }
    });
    input.addEventListener("blur", hideSuggestionsSoon);
    input.addEventListener("focus", function() {
      clearTimeout(blurTimer);
      updateHistoryBtn();
      if (!this.value.trim()) {
        showEmptySuggestions();
      } else if (suggestions.children.length) {
        suggestions.hidden = false;
      }
    });
    updateHistoryBtn();
    return {
      search: startMultiSearch,
      showEmptySuggestions,
      clear: clearSearch,
      recenterToUserLocation,
      trackRecentPin
    };
  }
  var LocationSearchEngine = { create };
  function installGlobalLocationSearchEngine() {
    window.LocationSearchEngine = LocationSearchEngine;
  }

  // src/urbanlens/dashboard/frontend/ts/shared/markup-engine.ts
  var HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;
  function safeColor(v, fallback = "#e74c3c") {
    return typeof v === "string" && HEX_COLOR_RE.test(v) ? v : fallback;
  }
  function safeNumber(v, lo, hi, def) {
    const n = Number.parseFloat(v);
    if (Number.isNaN(n))
      return def;
    return Math.max(lo, Math.min(hi, n));
  }
  function bearing(from, to) {
    const flat = Array.isArray(from) ? from[0] : from.lat;
    const flng = Array.isArray(from) ? from[1] : from.lng;
    const tlat = Array.isArray(to) ? to[0] : to.lat;
    const tlng = Array.isArray(to) ? to[1] : to.lng;
    return Math.atan2(tlng - flng, tlat - flat) * (180 / Math.PI);
  }
  function arrowheadSvg(color, deg, sz = 28, opacity = 1) {
    const op = opacity == null ? 1 : +opacity;
    const h = sz / 2;
    const tip = -(sz * 0.43);
    const bx = sz * 0.36;
    const by = sz * 0.29;
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${sz}" height="${sz}"` + ` viewBox="${-h} ${-h} ${sz} ${sz}"` + ` style="transform:rotate(${deg.toFixed(1)}deg);opacity:${op.toFixed(2)}">` + `<polygon points="0,${tip.toFixed(1)} ${bx.toFixed(1)},${by.toFixed(1)} ${(-bx).toFixed(1)},${by.toFixed(1)}"` + ` fill="${color}" stroke="white" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
  }
  function arrowheadSize(zoom) {
    if (zoom == null || zoom >= 16)
      return 28;
    if (zoom >= 13)
      return 20;
    if (zoom >= 10)
      return 14;
    return 8;
  }
  function textLabelHtml(s) {
    const color = safeColor(s.color, "#e53e3e");
    const sz = safeNumber(s.stroke_width, 8, 96, 16);
    const bg = s.border_color;
    const bgVal = !bg || bg === "none" ? "rgba(255,255,255,0.92)" : safeColor(bg, "rgba(255,255,255,0.92)");
    const lbl = (s.label ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");
    return `<span class="markup-text-label" style="color:${color}` + `;font-size:${sz}px;background:${bgVal}` + ";display:inline-block;padding:.15em .45em;border-radius:3px" + ";white-space:nowrap;line-height:1.3;font-weight:600" + `;box-shadow:0 1px 3px rgba(0,0,0,.2)">${lbl || "&nbsp;"}</span>`;
  }
  function renderShape(s, group, zoom) {
    if (typeof L === "undefined")
      return;
    const color = safeColor(s.color, "#e74c3c");
    const weight = safeNumber(s.stroke_width != null ? s.stroke_width : s.weight, 1, 50, 3);
    const fillOp = safeNumber(s.fill_opacity != null ? s.fill_opacity : 87, 0, 100, 87) / 100;
    const borderOp = safeNumber(s.border_opacity != null ? s.border_opacity : 100, 0, 100, 100) / 100;
    const bc = s.border_color && s.border_color !== "none" ? safeColor(s.border_color, color) : null;
    const hasBorder = !!bc;
    const strokeC = hasBorder ? bc : color;
    function shapeOpts() {
      return { color: strokeC, weight: hasBorder ? weight : 2, fillColor: color, fillOpacity: fillOp, opacity: borderOp };
    }
    switch (s.type) {
      case "line":
        L.polyline(s.latlngs, { color, weight, opacity: fillOp }).addTo(group);
        break;
      case "arrow": {
        L.polyline(s.latlngs, { color, weight, opacity: fillOp }).addTo(group);
        if (s.latlngs.length >= 2) {
          const n = s.latlngs.length;
          const deg = bearing(s.latlngs[n - 2], s.latlngs[n - 1]);
          const sz2 = arrowheadSize(zoom);
          L.marker(L.latLng(s.latlngs[n - 1][0], s.latlngs[n - 1][1]), {
            icon: L.divIcon({ className: "", html: arrowheadSvg(color, deg, sz2, fillOp), iconSize: [sz2, sz2], iconAnchor: [sz2 / 2, sz2 / 2] }),
            interactive: false
          }).addTo(group);
        }
        break;
      }
      case "circle": {
        const p1 = L.latLng(s.latlngs[0]);
        const p2 = L.latLng(s.latlngs[1]);
        L.circle(p1, { ...shapeOpts(), radius: p1.distanceTo(p2) }).addTo(group);
        break;
      }
      case "rect":
        L.rectangle(L.latLngBounds(s.latlngs[0], s.latlngs[1]), shapeOpts()).addTo(group);
        break;
      case "polygon":
        L.polygon(s.latlngs, shapeOpts()).addTo(group);
        break;
      case "text":
        L.marker(L.latLng(s.latlngs[0][0], s.latlngs[0][1]), {
          icon: L.divIcon({ className: "", html: textLabelHtml(s), iconSize: undefined, iconAnchor: [0, 0] }),
          interactive: false
        }).addTo(group);
        break;
    }
  }
  function createDrawSession(map, opts) {
    let tool = null;
    let state = null;
    const prevLayer = L.layerGroup().addTo(map);
    let lastCursorLL = null;
    let suppressClickUntil = 0;
    const getColor = () => opts.getColor?.() ?? "#e74c3c";
    const getLabel = () => opts.getTextLabel?.() ?? "";
    function hint() {
      if (opts.onHintChange) {
        if (!tool) {
          opts.onHintChange("");
        } else {
          const n = state ? state.points.length : 0;
          const msgs = {
            arrow: n >= 2 ? "Click near last point (or Enter) to finish, or drag" : n ? `${n} pt - click to add another point` : "Click to start, drag for a quick arrow",
            line: n >= 2 ? "Click near last point (or Enter) to finish, or drag" : n ? `${n} pt - click to add another point` : "Click to start, drag for a quick line",
            polygon: n >= 3 ? "Click near start (or Enter) to close" : n ? "Click to add vertices" : "Click to place first vertex",
            circle: n ? "Click to set radius, or drag" : "Click to place center, or drag",
            rect: n ? "Click second corner, or drag" : "Click first corner, or drag",
            text: "Click to place, or drag to size a text box"
          };
          opts.onHintChange(msgs[tool] ?? "");
        }
      }
      opts.onPointCountChange?.(tool, state ? state.points.length : 0);
    }
    function clearPrev() {
      prevLayer.clearLayers();
    }
    function preview(cursorLL) {
      clearPrev();
      const c = getColor();
      if ((tool === "line" || tool === "arrow") && state) {
        const pts = [...state.points, [cursorLL.lat, cursorLL.lng]];
        L.polyline(pts, { color: c, dashArray: "5 7", weight: 2, opacity: 0.7, interactive: false }).addTo(prevLayer);
        if (tool === "arrow" && pts.length >= 2) {
          const n = pts.length;
          const deg = bearing(pts[n - 2], pts[n - 1]);
          const sz = 20;
          L.marker(L.latLng(pts[n - 1][0], pts[n - 1][1]), {
            icon: L.divIcon({ className: "", html: arrowheadSvg(c, deg, sz), iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2] }),
            interactive: false
          }).addTo(prevLayer);
        }
      } else if (tool === "polygon" && state) {
        const ppts = [...state.points, [cursorLL.lat, cursorLL.lng]];
        L.polygon(ppts, { color: c, dashArray: "5 7", weight: 2, fillOpacity: 0.07, interactive: false }).addTo(prevLayer);
        if (state.points.length >= 3) {
          L.circleMarker(L.latLng(state.points[0][0], state.points[0][1]), { radius: 8, color: c, fillColor: c, fillOpacity: 0.35, weight: 2, interactive: false }).addTo(prevLayer);
        }
      } else if (tool === "rect" && state && state.points.length >= 1) {
        L.rectangle(L.latLngBounds(state.points[0], [cursorLL.lat, cursorLL.lng]), { color: c, weight: 2, fillOpacity: 0.08, dashArray: "4 4", interactive: false }).addTo(prevLayer);
      } else if (tool === "circle" && state && state.points.length >= 1) {
        const center = L.latLng(state.points[0]);
        L.circle(center, { radius: center.distanceTo(cursorLL), color: c, weight: 2, fillOpacity: 0.08, dashArray: "5 7", interactive: false }).addTo(prevLayer);
      }
    }
    function commit(type, latlngs, extras) {
      clearPrev();
      state = null;
      hint();
      opts.onCommit?.(type, latlngs, extras ?? {});
    }
    function cancelShape() {
      state = null;
      clearPrev();
      hint();
    }
    function deactivate() {
      cancelShape();
      tool = null;
      map.doubleClickZoom.enable();
      map.dragging.enable();
      map.getContainer().style.cursor = "";
      opts.onToolChange?.(null);
    }
    function startTool(type) {
      cancelShape();
      tool = type;
      map.doubleClickZoom.disable();
      map.dragging.disable();
      map.getContainer().style.cursor = "crosshair";
      opts.onToolChange?.(type);
      hint();
    }
    function getCurrentTool() {
      return tool;
    }
    function isBusy() {
      return !!tool || Date.now() < suppressClickUntil;
    }
    function canFinish() {
      if (!state)
        return false;
      const n = state.points.length;
      if (tool === "polygon")
        return n >= 3;
      if (tool === "line" || tool === "arrow")
        return n >= 2;
      if (tool === "rect" || tool === "circle")
        return n >= 1 && !!lastCursorLL;
      return false;
    }
    function finishCurrent() {
      if (!state)
        return;
      const n = state.points.length;
      if (tool === "polygon" && n >= 3) {
        commit("polygon", state.points.slice());
        return;
      }
      if ((tool === "line" || tool === "arrow") && n >= 2) {
        commit(tool, state.points.slice());
        return;
      }
      if (tool === "rect" && n >= 1 && lastCursorLL) {
        commit("rect", [state.points[0], [lastCursorLL.lat, lastCursorLL.lng]]);
        return;
      }
      if (tool === "circle" && n >= 1 && lastCursorLL) {
        commit("circle", [state.points[0], [lastCursorLL.lat, lastCursorLL.lng]]);
      }
    }
    function onClick(e) {
      if (!tool || e.originalEvent.detail > 1)
        return;
      if (Date.now() < suppressClickUntil)
        return;
      const ll = e.latlng;
      if (tool === "text") {
        commit("text", [[ll.lat, ll.lng]], { label: getLabel() });
        return;
      }
      if (tool === "line" || tool === "arrow") {
        if (!state) {
          state = { points: [[ll.lat, ll.lng]] };
        } else {
          const n = state.points.length;
          if (n >= 2) {
            const lp = map.latLngToContainerPoint(L.latLng(state.points[n - 1][0], state.points[n - 1][1]));
            const cp = map.latLngToContainerPoint(ll);
            if (Math.hypot(lp.x - cp.x, lp.y - cp.y) <= 20) {
              commit(tool, state.points.slice());
              return;
            }
          }
          state.points.push([ll.lat, ll.lng]);
        }
        preview(ll);
        hint();
        return;
      }
      if (tool === "polygon") {
        if (!state) {
          state = { points: [[ll.lat, ll.lng]] };
        } else {
          if (state.points.length >= 3) {
            const fp = map.latLngToContainerPoint(L.latLng(state.points[0][0], state.points[0][1]));
            const cp = map.latLngToContainerPoint(ll);
            if (Math.hypot(fp.x - cp.x, fp.y - cp.y) <= 20) {
              commit("polygon", state.points.slice());
              return;
            }
          }
          state.points.push([ll.lat, ll.lng]);
        }
        preview(ll);
        hint();
        return;
      }
      if (tool === "rect") {
        if (!state) {
          state = { points: [[ll.lat, ll.lng]] };
          hint();
        } else {
          commit("rect", [state.points[0], [ll.lat, ll.lng]]);
        }
        return;
      }
      if (tool === "circle") {
        if (!state) {
          state = { points: [[ll.lat, ll.lng]] };
          hint();
        } else {
          commit("circle", [state.points[0], [ll.lat, ll.lng]]);
        }
      }
    }
    function onDblClick(e) {
      if (!tool || !state)
        return;
      L.DomEvent.stop(e);
      if (tool === "line" || tool === "arrow") {
        const pts = state.points.length > 2 ? state.points.slice(0, -1) : state.points.slice();
        if (pts.length >= 2)
          commit(tool, pts);
        return;
      }
      if (tool === "polygon") {
        const ppts = state.points.length > 3 ? state.points.slice(0, -1) : state.points.slice();
        if (ppts.length >= 3)
          commit("polygon", ppts);
      }
    }
    function onMouseMove(e) {
      lastCursorLL = e.latlng;
      if (state || tool === "rect" || tool === "circle")
        preview(e.latlng);
    }
    function onMouseDown(e) {
      if (!tool || e.button !== 0)
        return;
      const eligible = ["circle", "rect", "arrow", "line", "text"].includes(tool);
      if (!eligible)
        return;
      const startLL = map.mouseEventToLatLng(e);
      const startX = e.clientX;
      const startY = e.clientY;
      let isDragging = false;
      const hasPoints = !!(state?.points.length && (tool === "arrow" || tool === "line"));
      function onMove(ev) {
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;
        if (!isDragging && Math.hypot(dx, dy) < 6)
          return;
        isDragging = true;
        const endLL = map.mouseEventToLatLng(ev);
        const c = getColor();
        clearPrev();
        if (tool === "circle") {
          L.circle(startLL, { radius: startLL.distanceTo(endLL), color: c, weight: 2, fillOpacity: 0.1, interactive: false }).addTo(prevLayer);
        } else if (tool === "rect") {
          const rs = state && state.points.length >= 1 ? L.latLng(state.points[0]) : startLL;
          L.rectangle(L.latLngBounds(rs, endLL), { color: c, weight: 2, fillOpacity: 0.08, dashArray: "4 4", interactive: false }).addTo(prevLayer);
        } else if (tool === "arrow" || tool === "line") {
          const pts = hasPoints ? [...state.points, [endLL.lat, endLL.lng]] : [[startLL.lat, startLL.lng], [endLL.lat, endLL.lng]];
          L.polyline(pts, { color: c, weight: 2, opacity: 0.85, interactive: false }).addTo(prevLayer);
          if (tool === "arrow") {
            const n = pts.length;
            const deg = bearing(pts[n - 2], pts[n - 1]);
            const sz = 20;
            L.marker(L.latLng(endLL.lat, endLL.lng), {
              icon: L.divIcon({ className: "", html: arrowheadSvg(c, deg, sz), iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2] }),
              interactive: false
            }).addTo(prevLayer);
          }
        } else if (tool === "text") {
          L.rectangle(L.latLngBounds(startLL, endLL), { color: c, weight: 1, dashArray: "3 4", fillOpacity: 0.04, interactive: false }).addTo(prevLayer);
        }
      }
      function onUp(ev) {
        document.removeEventListener("mousemove", onMove);
        clearPrev();
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;
        if (!isDragging || Math.hypot(dx, dy) < 6)
          return;
        const endLL = map.mouseEventToLatLng(ev);
        suppressClickUntil = Date.now() + 350;
        if (tool === "circle") {
          commit("circle", [[startLL.lat, startLL.lng], [endLL.lat, endLL.lng]]);
        } else if (tool === "rect") {
          const rectStart = state && state.points.length >= 1 ? state.points[0] : [startLL.lat, startLL.lng];
          commit("rect", [rectStart, [endLL.lat, endLL.lng]]);
        } else if (tool === "arrow" || tool === "line") {
          const finalPts = hasPoints ? [...state.points, [endLL.lat, endLL.lng]] : [[startLL.lat, startLL.lng], [endLL.lat, endLL.lng]];
          commit(tool, finalPts);
        } else if (tool === "text") {
          commit("text", [[startLL.lat, startLL.lng], [endLL.lat, endLL.lng]], { label: getLabel() });
        }
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp, { once: true });
    }
    function onKeyDown(e) {
      if (!tool)
        return;
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        e.preventDefault();
        if (state)
          cancelShape();
        else
          deactivate();
        return;
      }
      if (!state)
        return;
      if (e.key === "Enter") {
        e.stopImmediatePropagation();
        e.preventDefault();
        finishCurrent();
      }
    }
    map.on("click", onClick);
    map.on("dblclick", onDblClick);
    map.on("mousemove", onMouseMove);
    map.getContainer().addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown, true);
    function destroy() {
      map.off("click", onClick);
      map.off("dblclick", onDblClick);
      map.off("mousemove", onMouseMove);
      map.getContainer().removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown, true);
      if (map.hasLayer(prevLayer))
        map.removeLayer(prevLayer);
    }
    return { startTool, deactivate, cancelShape, getCurrentTool, isBusy, canFinish, finishCurrent, destroy };
  }
  var MarkupEngine = {
    bearing,
    arrowheadSvg,
    arrowheadSize,
    textLabelHtml,
    renderShape,
    createDrawSession
  };
  function installGlobalMarkupEngine() {
    window.MarkupEngine = MarkupEngine;
  }

  // src/urbanlens/dashboard/frontend/ts/entries-classic/core.ts
  installGlobalLocationSearchEngine();
  installGlobalMarkupEngine();
})();
