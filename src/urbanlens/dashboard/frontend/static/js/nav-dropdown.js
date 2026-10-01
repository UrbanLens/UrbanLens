// The navbar's user/notification/messages dropdowns and the mobile hamburger drawer
// (partials/layout/header.html). Served as a file so the browser caches it across navigations
// instead of re-downloading it inline on every page (P133).
(function () {
    var menu      = document.getElementById('nav-user');
    var btn       = document.getElementById('nav-user-btn');
    var notifWrap = document.getElementById('nav-notif');
    var notifBtn  = document.getElementById('nav-notif-btn');
    var msgWrap   = document.getElementById('nav-msg');
    var msgBtn    = document.getElementById('nav-msg-btn');

    function openMenu() {
        if (!menu) return;
        closeNotif();
        closeMsg();
        menu.classList.add('is-open');
        btn.setAttribute('aria-expanded', 'true');
    }
    function closeMenu() {
        if (!menu) return;
        menu.classList.remove('is-open');
        btn.setAttribute('aria-expanded', 'false');
    }
    window.closeNavDropdown = closeMenu;
    function openNotif() {
        if (!notifWrap) return;
        closeMenu();
        closeMsg();
        notifWrap.classList.add('is-open');
        notifBtn.setAttribute('aria-expanded', 'true');
        // Re-fetch on every open (matches openMsg() below) so actions taken
        // elsewhere - accepting a friend request from the profile page, a pin
        // share, etc. - are reflected instead of showing whatever was cached
        // from the first time the dropdown was opened this page load.
        document.body.dispatchEvent(new Event('notifOpen'));
    }
    function closeNotif() {
        if (!notifWrap) return;
        notifWrap.classList.remove('is-open');
        notifBtn.setAttribute('aria-expanded', 'false');
    }
    function openMsg() {
        if (!msgWrap) return;
        closeMenu();
        closeNotif();
        msgWrap.classList.add('is-open');
        msgBtn.setAttribute('aria-expanded', 'true');
        // Re-fetch on every open so previews and unread pills are always current.
        document.body.dispatchEvent(new Event('msgOpen'));
    }
    function closeMsg() {
        if (!msgWrap) return;
        msgWrap.classList.remove('is-open');
        msgBtn.setAttribute('aria-expanded', 'false');
    }

    if (msgBtn) {
        msgBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            msgWrap.classList.contains('is-open') ? closeMsg() : openMsg();
        });
    }

    if (btn) {
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            menu.classList.contains('is-open') ? closeMenu() : openMenu();
        });
    }

    if (notifBtn) {
        notifBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            notifWrap.classList.contains('is-open') ? closeNotif() : openNotif();
        });

        document.body.addEventListener('htmx:afterSwap', function (ev) {
            var t = ev.detail.target;
            if (!t) return;
            if (t.id === 'notif-dropdown-wrap' || t.id === 'notif-dropdown' ||
                (t.classList && t.classList.contains('notif-item'))) {
                htmx.trigger(document.body, 'notifCountRefresh');
            }
            // After an actionable row animates out of the bell, refresh the
            // dropdown if the list is now empty so the empty-state copy appears.
            if (t.classList && t.classList.contains('notif-item')) {
                var list = document.querySelector('#notif-dropdown .notif-list');
                if (list && !list.querySelector('.notif-item')) {
                    var wrap = document.getElementById('notif-dropdown-wrap');
                    if (wrap) {
                        htmx.ajax('GET', wrap.getAttribute('hx-get') || notifBtn.dataset.dropdownUrl, {
                            target: '#notif-dropdown-wrap',
                            swap: 'innerHTML'
                        });
                    }
                }
            }
        });

        // data-notif-url carries NotificationLog.url, the same unvalidated
        // CharField _notification_push.html guards before navigating. These two
        // sinks had no guard at all, so they were strictly less protected than
        // the ones a scanner had already flagged. Same single-code-path check:
        // let the browser's own parser decide, never a hand-rolled prefix test.
        function notifUrlIfSameOrigin(url) {
            if (typeof url !== 'string' || !url) return null;
            try {
                var parsed = new URL(url, window.location.origin);
                var ok = (parsed.protocol === 'http:' || parsed.protocol === 'https:') && parsed.origin === window.location.origin;
                return ok ? parsed.href : null;
            } catch (err) {
                return null;
            }
        }

        document.body.addEventListener('htmx:afterRequest', function (ev) {
            var elt = ev.detail.elt;
            if (!elt || !elt.classList || !elt.classList.contains('notif-item')) return;
            if (!ev.detail.successful) return;
            var url = notifUrlIfSameOrigin(elt.dataset.notifUrl);
            if (url && elt.classList.contains('notif-item--unread')) {
                window.location = url;
            }
        });

        document.getElementById('notif-dropdown-wrap').addEventListener('click', function (ev) {
            var item = ev.target.closest('.notif-item--link:not(.notif-item--unread)');
            if (!item) return;
            var target = notifUrlIfSameOrigin(item.dataset.notifUrl);
            if (target) window.location = target;
        });
    }

    // Single document click handler - closes whichever dropdown was not clicked in
    document.addEventListener('click', function (e) {
        if (menu      && !menu.contains(e.target))      closeMenu();
        if (notifWrap && !notifWrap.contains(e.target)) closeNotif();
        if (msgWrap   && !msgWrap.contains(e.target))   closeMsg();
    });

    // Escape closes all of them
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { closeMenu(); closeNotif(); closeMsg(); closeDrawer(); }
    });

    // -- Mobile hamburger drawer -----------------------------------------------
    var hamburgerBtn  = document.getElementById('nav-hamburger-btn');
    var hamburgerIcon = document.getElementById('nav-hamburger-icon');
    var navDrawer     = document.getElementById('app-nav-drawer');

    function openDrawer() {
        if (!navDrawer) return;
        navDrawer.classList.add('is-open');
        navDrawer.setAttribute('aria-hidden', 'false');
        if (hamburgerBtn) hamburgerBtn.setAttribute('aria-expanded', 'true');
        if (hamburgerIcon) hamburgerIcon.textContent = 'close';
    }
    function closeDrawer() {
        if (!navDrawer) return;
        navDrawer.classList.remove('is-open');
        navDrawer.setAttribute('aria-hidden', 'true');
        if (hamburgerBtn) hamburgerBtn.setAttribute('aria-expanded', 'false');
        if (hamburgerIcon) hamburgerIcon.textContent = 'menu';
    }

    if (hamburgerBtn) {
        hamburgerBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            navDrawer.classList.contains('is-open') ? closeDrawer() : openDrawer();
        });
    }

    // Close drawer on outside click
    document.addEventListener('click', function (e) {
        if (navDrawer && navDrawer.classList.contains('is-open') &&
            !navDrawer.contains(e.target) && hamburgerBtn && !hamburgerBtn.contains(e.target)) {
            closeDrawer();
        }
    });
}());
