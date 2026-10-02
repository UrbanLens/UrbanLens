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
    var isPreview = isCopy || /[?&]preview=1(&|$)/.test(src);
    var retries = isPreview || img.hasAttribute('data-retry-busy');
    // Every handler that sees one failure asks; it is retried once.
    if (img.dataset.retryOf === src) return true;
    // A copy waits its turn behind every other one queued site-wide, so a page's later tiles can take minutes.
    var delays = isCopy ? [4000, 6000, 10000, 15000, 20000, 25000, 30000, 30000, 30000] : [2000, 4000];
    // The count belongs to one address: an element reused for another image starts again.
    var base = src.replace(/([?&])_r=\d+$/, '');
    var attempt = img.dataset.previewRetryFor === base ? parseInt(img.dataset.previewRetry || '0', 10) : 0;
    if (!retries || attempt >= delays.length) {
        img.removeAttribute('data-retry-pending');
        return false;
    }
    img.dataset.previewRetry = String(attempt + 1);
    img.dataset.previewRetryFor = base;
    img.dataset.retryOf = src;
    // Hidden until it loads: a broken-image glyph redrawn on every attempt is what makes a waiting grid flicker.
    img.setAttribute('data-retry-pending', '');
    // A new query param, not the same URL again: the browser has already negatively cached this exact one.
    var retryUrl = src.replace(/([?&])_r=\d+/, '$1_r=' + (attempt + 1));
    if (retryUrl === src) retryUrl = src + (src.indexOf('?') === -1 ? '?' : '&') + '_r=' + (attempt + 1);
    setTimeout(function () {
        if (img.dataset.retryOf === src) delete img.dataset.retryOf;
        // Something else put another image here meanwhile, so this retry is no longer wanted.
        if (img.getAttribute('src') === src) img.setAttribute('src', retryUrl);
    }, delays[attempt]);
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
// Any image still being made is retried; one with its own onerror handles that itself.
// `data-hide-on-fail="<selector>"` hides the image's closest match once it has given up.
document.addEventListener('error', function (event) {
    var img = event.target;
    if (!(img instanceof HTMLImageElement)) return;
    if (img.hasAttribute('data-thumb-fallback')) {
        window.urbanlensMediaThumbFallback(img, img.getAttribute('data-thumb-fallback') || undefined, img.getAttribute('data-thumb-fallback-class') || undefined);
    } else if (!img.onerror && !window.urbanlensRetryPendingImage(img) && img.hasAttribute('data-hide-on-fail')) {
        (img.closest(img.getAttribute('data-hide-on-fail') || '') || img).style.display = 'none';
    }
}, true);

// For thumbnails that fade in once decoded rather than pop in over their tile.
document.addEventListener('load', function (event) {
    var img = event.target;
    if (!(img instanceof HTMLImageElement)) return;
    img.removeAttribute('data-retry-pending');
    if (img.hasAttribute('data-fade-in')) img.classList.add('is-loaded');
}, true);
