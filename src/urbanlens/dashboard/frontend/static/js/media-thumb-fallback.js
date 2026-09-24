// Served as a file so the browser keeps it, instead of re-downloading it inline on every
// navigation (P133). In <head>, and not deferred: an `onerror` for an image that 404s fires
// during parsing, as soon as the response comes back. Nine templates render
// `<img onerror="urbanlensMediaThumbFallback(...)">` server-side, and defining this after the
// page content meant every one of them threw ReferenceError instead of swapping in the icon tile -
// while `typeof window.urbanlensMediaThumbFallback` read "function" by the time anyone looked,
// which is what made it invisible.
//
// Only a handler that can fire during parse needs to be up here; an `onclick` cannot, which is
// why the edit-in-place sizer further down (themes/base.html) is fine where it is. This script
// tag must stay in <head>, loaded synchronously (no defer/async), for the same reason.
//
// Swaps a broken/missing thumbnail <img> for an icon tile instead of hiding its wrapper - keeps
// records with no (or a dead) preview image visible, and their row/tile the same size as ones
// that have an image, instead of collapsing and breaking the grid/row's consistent styling.
window.urbanlensMediaThumbFallback = function (img, icon, className) {
    // Server-rendered previews (a PDF/TIFF/HEIC tile) answer 503 until the
    // sandbox worker has decoded them - "not yet" rather than "never" - so
    // retry a couple of times before giving up. Everything else falls back
    // immediately.
    // A proxy that is out of upstream slots answers 503 the same way; its <img> says so with data-retry-busy.
    var src = img.getAttribute('src') || '';
    var isPreview = src.indexOf('/media-preview/') !== -1 || /[?&]preview=1(&|$)/.test(src);
    var retries = isPreview || img.hasAttribute('data-retry-busy');
    var attempt = parseInt(img.dataset.previewRetry || '0', 10);
    if (retries && attempt < 2) {
        img.dataset.previewRetry = String(attempt + 1);
        // A new query param, not the same URL again: the browser has
        // already negatively cached this exact one.
        var retryUrl = src.replace(/([?&])_r=\d+/, '$1_r=' + (attempt + 1));
        if (retryUrl === src) retryUrl = src + (src.indexOf('?') === -1 ? '?' : '&') + '_r=' + (attempt + 1);
        setTimeout(function () { img.setAttribute('src', retryUrl); }, 2000 * (attempt + 1));
        return;
    }
    var span = document.createElement('span');
    span.className = className || 'media-item-thumb media-item-thumb-fallback';
    span.setAttribute('aria-hidden', 'true');
    span.innerHTML = '<i class="material-icons">' + (icon || 'description') + '</i>';
    img.replaceWith(span);
};
