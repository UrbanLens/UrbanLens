// The active-checkin banner's close button (partials/safety/_active_checkin_banner.html). Served
// as a file so the browser caches it across navigations instead of re-downloading it inline on
// every page (P133).
(function () {
    // Delegated on document so this keeps working no matter how many banners
    // render (0-N) and survives this partial being reloaded/swapped by HTMX
    // polling - re-registering the listener each time would be harmless but
    // wasteful, so guard with a flag on window instead.
    if (window.__safetyActiveBannerDelegated) return;
    window.__safetyActiveBannerDelegated = true;

    document.addEventListener('click', function (event) {
        var btn = event.target.closest('.safety-active-banner-close-btn');
        if (!btn) return;
        var url = btn.dataset.checkinUrl;
        var bannerId = btn.dataset.bannerId;
        var proceed = window.confirmDialog ? window.confirmDialog({
            title: "Check in now?",
            message: "This marks you as checked in and closes the active check-in.",
            confirmLabel: "I'm safe - check in",
            danger: false,
        }) : Promise.resolve(window.confirm('Mark yourself checked in and close this check-in?'));
        proceed.then(function (ok) {
            if (!ok) return;
            fetch(url, {
                method: 'POST',
                headers: { 'X-CSRFToken': btn.dataset.csrfToken },
            }).then(function (r) {
                if (!r.ok) throw new Error();
                var banner = document.getElementById(bannerId);
                if (banner) banner.remove();
                if (window.toastr) toastr.success('Checked in - check-in closed.');
            }).catch(function () {
                if (window.toastr) toastr.error('Could not check in. Please try again.');
            });
        });
    });
}());
