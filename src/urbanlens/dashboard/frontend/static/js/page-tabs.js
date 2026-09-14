/*
 * Wikipedia-style page tabs. Attribute-driven markup (classnames are cosmetic):
 *   <nav data-page-tabs>
 *     <a href="#tab-overview" data-tab="overview">...</a>
 *     ...
 *   </nav>
 *   <section data-tab-panel="overview">...</section>
 *
 * Behavior: real links (middle-click works; plain left-click swaps in-page); active tab deep-linkable via
 * #tab-<name> ("overview" keeps the hash clean); showing a tab fires resize + "ul:tabShown".
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

        // Fire one resize so content hidden at init renders correctly.
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
                // Leave modifier-clicks to native <a href> behavior.
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                activate(btn.dataset.tab);
            });
        });
        // Arrow-key navigation, per the WAI-ARIA tabs pattern.
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

    // Public: switch tabs programmatically.
    window.ulActivatePageTab = activate;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
