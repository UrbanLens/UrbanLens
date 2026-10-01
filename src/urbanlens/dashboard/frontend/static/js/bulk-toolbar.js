/**
 * Floating multi-select action bar, reusable across pages. Plain vanilla JS so non-TS-bundle pages can use it too.
 * Pair with dashboard/partials/ui/_bulk_toolbar.html for the markup.
 *
 * Usage:
 *   window.ulBulkToolbar.sync('media', selectedCount, {
 *     relevant: function () { ... },
 *     not_relevant: function () { ... },
 *     wiki: function () { ... },
 *     deselect: function () { ... },
 *   });
 *   window.ulBulkToolbar.clear('media');
 *
 * Each key matches a `data-bulk-action` value on a button inside the bar
 * markup for that namespace; a namespace may leave any action's callback out
 * to hide that button entirely for the current selection.
 */
(function () {
    var bars = {};

        function ensureBar(namespace) {
        var barEl = document.getElementById('ul-bulk-bar-' + namespace);
        if (bars[namespace] && bars[namespace].barEl === barEl && barEl) return bars[namespace];
        if (!barEl) return null;

        var entry = { barEl: barEl, countEl: barEl.querySelector('.ul-bulk-count'), buttons: {}, actions: {} };
        barEl.querySelectorAll('[data-bulk-action]').forEach(function (btn) {
            var action = btn.dataset.bulkAction;
            entry.buttons[action] = btn;
            btn.addEventListener('click', function () {
                if (entry.actions[action]) entry.actions[action]();
            });
        });
        bars[namespace] = entry;
        return entry;
    }

    window.ulBulkToolbar = {
        /**
         * Show/hide the bar for `namespace` for the current selection. Missing/falsy actions hide their button.
         */
        sync: function (namespace, count, actions) {
            var entry = ensureBar(namespace);
            if (!entry) return;
            entry.actions = actions || {};
            entry.barEl.classList.toggle('visible', count > 0);
            if (entry.countEl) entry.countEl.textContent = count === 1 ? '1 selected' : count + ' selected';
            Object.keys(entry.buttons).forEach(function (action) {
                entry.buttons[action].hidden = !entry.actions[action];
            });
        },

        /** Hide the bar for `namespace` without invoking any callback. */
        clear: function (namespace) {
            var entry = bars[namespace];
            if (entry) entry.barEl.classList.remove('visible');
        },
    };
}());
