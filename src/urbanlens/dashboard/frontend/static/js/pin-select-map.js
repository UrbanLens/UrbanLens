/**
 * Reusable Leaflet map + click/drag-select UX for a page that reviews a list
 * of geolocated items (pin suggestions, unlogged visits, etc.) alongside a
 * card grid, with a bulk-action toolbar for whatever is currently selected.
 *
 * Originally built for Memories > Locations (the batch-scan pin-suggestion
 * review queue) and generalized so Memories > Visits can share the exact
 * same map/selection behavior instead of re-implementing it - see
 * dashboard/partials/ui/_bulk_toolbar.html + bulk-toolbar.js for the toolbar
 * half of this pairing.
 *
 * Usage:
 *   window.PinSelectMap.create(document.getElementById('my-map'), {
 *     dataUrl: '/some/map-data/',
 *     itemsKey: 'suggestions',        // key in the JSON response holding the array
 *     idKey: 'id',                    // (default 'id') item field used as its identity
 *     icon: function (item, selected) { return L.divIcon({...}); },
 *     tooltip: function (item) { return item.name; },  // optional
 *     cardEl: function (id) { return document.getElementById('card-' + id); },
 *     wrapEl: document.getElementById('cards-wrap'),   // delegated checkbox listener root
 *     checkboxSelector: '.my-select-cb',
 *     checkboxIdAttr: 'itemId',       // dataset key on the checkbox holding the id
 *     layersPanelId: 'my-map-layers',
 *     selectToggleBtnId: 'my-select-toggle',
 *     namespace: 'my_items',          // bulk-toolbar namespace
 *     bulkActions: {
 *       accept: function (ids) { return fetch(...).then(...); },
 *       reject: function (ids) { return fetch(...).then(...); },
 *     },
 *     onMarkerClick: function (item) { ... },   // optional, called after toggling selection
 *     refreshEvent: 'refreshQueue',   // (default 'refreshQueue') body event that reloads markers
 *   });
 *
 * Returns { toggleSelection, clearSelection, reload, getSelected }.
 */
(function () {
    function create(mapEl, opts) {
        if (!mapEl) return null;

        var idKey = opts.idKey || 'id';
        var itemsKey = opts.itemsKey || 'items';
        var refreshEvent = opts.refreshEvent || 'refreshQueue';

        var map = L.map(mapEl).setView([20, 0], 2);
        window.MapLayers.create(map, { root: document.getElementById(opts.layersPanelId) });

        var selectedIds = new Set();
        var markerMap = new Map();  // id -> L.Marker
        var itemById = new Map();   // id -> raw item, for re-deriving icons on select toggle

        function syncMarker(id) {
            var marker = markerMap.get(id);
            var item = itemById.get(id);
            if (!marker || !item) return;
            marker.setIcon(opts.icon(item, selectedIds.has(id)));
        }

        function syncCard(id) {
            var card = opts.cardEl(id);
            if (!card) return;
            card.classList.toggle('is-selected', selectedIds.has(id));
            var cb = opts.checkboxSelector ? card.querySelector(opts.checkboxSelector) : null;
            if (cb) cb.checked = selectedIds.has(id);
        }

        function syncToolbar() {
            var n = selectedIds.size;
            var actions = {};
            if (n > 0) {
                Object.keys(opts.bulkActions || {}).forEach(function (action) {
                    actions[action] = function () { runBulkAction(action); };
                });
                actions.deselect = clearSelection;
            }
            window.ulBulkToolbar.sync(opts.namespace, n, actions);
        }

        function toggleSelection(id) {
            if (selectedIds.has(id)) selectedIds.delete(id); else selectedIds.add(id);
            syncMarker(id);
            syncCard(id);
            syncToolbar();
        }

        function clearSelection() {
            var ids = Array.from(selectedIds);
            selectedIds.clear();
            ids.forEach(function (id) { syncMarker(id); syncCard(id); });
            syncToolbar();
        }

        function runBulkAction(action) {
            var ids = Array.from(selectedIds);
            if (!ids.length || !opts.bulkActions[action]) return;
            Promise.resolve(opts.bulkActions[action](ids)).then(function () {
                clearSelection();
                document.body.dispatchEvent(new CustomEvent(refreshEvent));
            }).catch(function () {
                if (window.toastr) toastr.error('Something went wrong. Please try again.');
            });
        }

        function reload() {
            fetch(opts.dataUrl).then(function (r) { return r.json(); }).then(function (data) {
                markerMap.forEach(function (marker) { map.removeLayer(marker); });
                markerMap.clear();
                itemById.clear();

                var items = data[itemsKey] || [];
                var bounds = [];
                items.forEach(function (item) {
                    var id = item[idKey];
                    itemById.set(id, item);
                    var marker = L.marker([item.latitude, item.longitude], { icon: opts.icon(item, selectedIds.has(id)) }).addTo(map);
                    if (opts.tooltip) marker.bindTooltip(opts.tooltip(item));
                    marker.on('click', function () {
                        toggleSelection(id);
                        if (opts.onMarkerClick) opts.onMarkerClick(item);
                    });
                    markerMap.set(id, marker);
                    bounds.push([item.latitude, item.longitude]);
                });
                // Drop any selection for items no longer on the map (handled/off-page).
                Array.from(selectedIds).forEach(function (id) { if (!markerMap.has(id)) selectedIds.delete(id); });
                syncToolbar();
                if (bounds.length === 1) map.setView(bounds[0], 14);
                else if (bounds.length > 1) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
            });
        }

        // -- Select-multiple mode: while active, map dragging (panning) is
        // disabled in favor of rectangle drag-select, exactly like the main
        // map's select tool - toggled up front rather than reactively per
        // gesture, so there's no race with Leaflet's own drag handling.
        var selectMode = false;
        var selectToggleBtn = opts.selectToggleBtnId ? document.getElementById(opts.selectToggleBtnId) : null;

        function setSelectMode(on) {
            selectMode = on;
            map.dragging[on ? 'disable' : 'enable']();
            if (selectToggleBtn) selectToggleBtn.classList.toggle('active', on);
            mapEl.classList.toggle('select-mode', on);
        }
        if (selectToggleBtn) selectToggleBtn.addEventListener('click', function () { setSelectMode(!selectMode); });

        // -- Rectangle drag-select: mousedown-move-up on the map container while
        // select-multiple mode is active. A 6px move threshold keeps a plain
        // click on empty map space from being treated as a (zero-size) drag-select.
        (function initDragSelect() {
            var dragRect = null;
            map.getContainer().addEventListener('mousedown', function (e) {
                if (!selectMode || e.button !== 0) return;
                var startLL = map.mouseEventToLatLng(e);
                var startX = e.clientX;
                var startY = e.clientY;
                var dragging = false;

                function onMove(ev) {
                    if (!dragging && Math.hypot(ev.clientX - startX, ev.clientY - startY) < 6) return;
                    dragging = true;
                    if (dragRect) map.removeLayer(dragRect);
                    dragRect = L.rectangle(L.latLngBounds(startLL, map.mouseEventToLatLng(ev)), {
                        color: '#1E88E5', weight: 2, fillOpacity: 0.08, dashArray: '4 4', interactive: false,
                    }).addTo(map);
                }
                function onUp(ev) {
                    document.removeEventListener('mousemove', onMove);
                    if (dragRect) { map.removeLayer(dragRect); dragRect = null; }
                    if (!dragging) return;
                    var bounds = L.latLngBounds(startLL, map.mouseEventToLatLng(ev));
                    markerMap.forEach(function (marker, id) {
                        if (!selectedIds.has(id) && bounds.contains(marker.getLatLng())) toggleSelection(id);
                    });
                }
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp, { once: true });
            });
        }());

        // Re-fetch markers whenever the caller's list refreshes (single item
        // action via HX-Trigger, or our own bulk actions above).
        document.body.addEventListener(refreshEvent, reload);
        reload();

        // -- Card-level selection checkbox -----------------------------------
        // Delegated on the wrap so it keeps working across HTMX swaps/pagination.
        if (opts.wrapEl && opts.checkboxSelector) {
            opts.wrapEl.addEventListener('change', function (e) {
                var target = e.target;
                if (!target.matches(opts.checkboxSelector)) return;
                var raw = target.dataset[opts.checkboxIdAttr || 'id'];
                // Ids are opaque (numeric PKs for some pages, string slugs for
                // others) - only coerce to a number when it actually looks like
                // one, so it matches whatever type the map-data JSON used.
                var id = /^-?\d+$/.test(raw || '') ? parseInt(raw, 10) : raw;
                if (id !== undefined && id !== null && id !== '') toggleSelection(id);
            });
        }

        return { toggleSelection: toggleSelection, clearSelection: clearSelection, reload: reload, getSelected: function () { return Array.from(selectedIds); } };
    }

    window.PinSelectMap = { create: create };
}());
