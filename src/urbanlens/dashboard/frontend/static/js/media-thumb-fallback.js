// Served as a file so the browser keeps it, instead of re-downloading it inline on every
// navigation (P133). In <head>, and not deferred: an image that 404s fails during parsing, as
// soon as the response comes back, so the listener below has to be up before the body is.
// Templates mark an image with `data-thumb-fallback="<icon>"` and, optionally,
// `data-thumb-fallback-class`; scripts that build an <img> can call the function directly.
//
// Swaps a broken/missing thumbnail <img> for an icon tile instead of hiding its wrapper - keeps
// records with no (or a dead) preview image visible, and their row/tile the same size as ones
// that have an image, instead of collapsing and breaking the grid/row's consistent styling.
// Server-rendered previews (a PDF/TIFF/HEIC tile) answer 503 until the sandbox worker has decoded them - "not yet"
// rather than "never". A proxy that is out of upstream slots answers 503 the same way; its <img> says so with
// data-retry-busy. A copy of a third-party image does too while it is first downloaded and re-encoded.
// Returns true when a retry was scheduled, so the caller should not give up on the image yet.
window.urbanlensRetryPendingImage = function (img) {
    var src = img.getAttribute('src') || '';
    var isCopy = src.indexOf('/media-copy/') !== -1;
    var isPreview = isCopy || src.indexOf('/media-preview/') !== -1 || /[?&]preview=1(&|$)/.test(src);
    var retries = isPreview || img.hasAttribute('data-retry-busy');
    var attempt = parseInt(img.dataset.previewRetry || '0', 10);
    if (!retries || attempt >= (isCopy ? 6 : 2)) return false;
    img.dataset.previewRetry = String(attempt + 1);
    // A new query param, not the same URL again: the browser has already negatively cached this exact one.
    var retryUrl = src.replace(/([?&])_r=\d+/, '$1_r=' + (attempt + 1));
    if (retryUrl === src) retryUrl = src + (src.indexOf('?') === -1 ? '?' : '&') + '_r=' + (attempt + 1);
    setTimeout(function () { img.setAttribute('src', retryUrl); }, 2000 * (attempt + 1));
    return true;
};

window.urbanlensMediaThumbFallback = function (img, icon, className) {
    if (window.urbanlensRetryPendingImage(img)) return;
    var span = document.createElement('span');
    span.className = className || 'media-item-thumb media-item-thumb-fallback';
    span.setAttribute('aria-hidden', 'true');
    var glyph = document.createElement('i');
    glyph.className = 'material-icons';
    glyph.textContent = icon || 'description';
    span.appendChild(glyph);
    img.replaceWith(span);
};

// An image's error doesn't bubble, but it does pass through the document on its way down.
document.addEventListener('error', function (event) {
    var img = event.target;
    if (!(img instanceof HTMLImageElement) || !img.hasAttribute('data-thumb-fallback')) return;
    window.urbanlensMediaThumbFallback(img, img.getAttribute('data-thumb-fallback') || undefined, img.getAttribute('data-thumb-fallback-class') || undefined);
}, true);

// For thumbnails that fade in once decoded rather than pop in over their tile.
document.addEventListener('load', function (event) {
    var img = event.target;
    if (img instanceof HTMLImageElement && img.hasAttribute('data-fade-in')) img.classList.add('is-loaded');
}, true);
