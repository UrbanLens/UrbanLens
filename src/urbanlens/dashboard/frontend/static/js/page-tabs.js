/*
 * Wikipedia-style page tabs (Overview / Article / Comments / Edit History).
 *
 * Markup contract (attribute-driven - the classnames are cosmetic; pin
 * details and wiki both use the shared .ul-subnav-tabs/.ul-subnav-tab look):
 *   <nav data-page-tabs>
 *     <a href="#tab-overview" data-tab="overview">...</a>
 *     ...
 *   </nav>
 *   <section data-tab-panel="overview">...</section>
 *
 * Behavior:
 *   - Real <a href="#tab-..."> elements, so middle-click/ctrl-click open the
 *     tab in a new tab and right-click gets the browser's normal link
 *     context menu; only a plain left-click is intercepted for the in-page
 *     swap below.
 *   - Clicking a tab shows its panel and hides the rest.
 *   - The active tab is deep-linkable via the URL hash (#tab-article,
 *     #tab-comments, #tab-history); "overview" is the default and keeps the
 *     hash clean. The "tab-" prefix keeps tab hashes from colliding with
 *     in-article heading anchors (an article section named "History" owns
 *     the bare #history anchor).
 *   - Showing a tab dispatches a window resize (so Leaflet maps and other
 *     measure-on-show widgets recover from having been display:none) and a
 *     "ul:tabShown" event for anything else that wants to react.
 */
(function () {
    'use strict';

    function tabsEl() { return document.querySelector('[data-page-tabs]'); }

    function tabNames(nav) {
        return Array.prototype.map.call(nav.querySelectorAll('[data-tab]'), function (btn) { return btn.dataset.tab; });
    }

    function activate(name, options) {
        var nav = tabsEl();
        if (!nav) return;
        var names = tabNames(nav);
        if (names.indexOf(name) === -1) name = names[0] || 'overview';

        nav.querySelectorAll('[data-tab]').forEach(function (btn) {
            var active = btn.dataset.tab === name;
            btn.classList.toggle('is-active', active);
            btn.setAttribute('aria-selected', active ? 'true' : 'false');
            btn.setAttribute('tabindex', active ? '0' : '-1');
        });
        document.querySelectorAll('[data-tab-panel]').forEach(function (panel) {
            panel.hidden = panel.dataset.tabPanel !== name;
        });

        if (!options || !options.skipHash) {
            var hash = name === names[0] ? '' : '#tab-' + name;
            if (window.history && window.history.replaceState) {
                window.history.replaceState(null, '', window.location.pathname + window.location.search + hash);
            }
        }

        // Leaflet (and the adaptive pagination system) both re-measure on
        // window resize; fire one so content hidden at init renders correctly.
        window.setTimeout(function () {
            window.dispatchEvent(new Event('resize'));
        }, 30);
        document.body.dispatchEvent(new CustomEvent('ul:tabShown', { detail: { tab: name } }));
    }

    function currentFromHash() {
        var raw = (window.location.hash || '').replace('#', '');
        return raw.indexOf('tab-') === 0 ? raw.slice(4) : null;
    }

    function init() {
        var nav = tabsEl();
        if (!nav) return;
        nav.setAttribute('role', 'tablist');
        nav.querySelectorAll('[data-tab]').forEach(function (btn) {
            btn.setAttribute('role', 'tab');
            btn.addEventListener('click', function (event) {
                // Modifier-clicks (ctrl/cmd/shift) are the browser's own
                // "open in new tab/window" gestures - leave those (and
                // middle-click/right-click, which never reach a 'click'
                // handler at all) to the native <a href> behavior.
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                activate(btn.dataset.tab);
            });
        });
        // Arrow-key navigation between tabs, per the WAI-ARIA tabs pattern.
        nav.addEventListener('keydown', function (event) {
            if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
            var buttons = Array.prototype.slice.call(nav.querySelectorAll('[data-tab]'));
            var index = buttons.indexOf(document.activeElement);
            if (index === -1) return;
            event.preventDefault();
            var next = event.key === 'ArrowRight' ? (index + 1) % buttons.length : (index - 1 + buttons.length) % buttons.length;
            buttons[next].focus();
            activate(buttons[next].dataset.tab);
        });

        var fromHash = currentFromHash();
        activate(fromHash || tabNames(nav)[0] || 'overview', { skipHash: !fromHash });

        window.addEventListener('hashchange', function () {
            var name = currentFromHash();
            if (name) activate(name, { skipHash: true });
        });
    }

    // Expose for anything that wants to switch tabs programmatically.
    window.ulActivatePageTab = activate;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
