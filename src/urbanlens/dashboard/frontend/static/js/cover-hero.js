/**
 * Hover-revealed prev/next controls for a cover-photo hero banner.
 *
 * Usage:
 *   {{ candidates|json_script:"KEY-cover-candidates" }}
 *   <div class="cover-hero" id="KEY-cover-hero" style="background-image:url('...')">
 *     <button onclick="window._coverHeroNav('KEY', -1)">...</button>
 *     <button onclick="window._coverHeroNav('KEY', 1)">...</button>
 *   </div>
 *
 * `candidates` is a list of {id, url} dicts for every OTHER eligible photo
 * (the cover photo itself is excluded - it's already the banner's initial
 * background-image). Cycling wraps back to the true cover photo between
 * runs through the candidate list; this never changes what's actually
 * stored as the cover photo, it's just a browsing preview.
 */
(function () {
    var state = {};

    function ensure(key) {
        if (state[key]) return state[key];
        // Wiki's cover hero is its own standalone div (id="<key>-cover-hero").
        // The pin detail page reuses the shared _page_hero.html hero section
        // instead, under its own pre-existing id - _photo_lightbox.html's
        // _applyCoverHeroUpdate already special-cases key "pin" the same way
        // for its live-update path, so this mirrors that convention.
        var heroEl = key === 'pin' ? document.getElementById('pin-detail-hero') : document.getElementById(key + '-cover-hero');
        if (!heroEl) return null;
        var dataEl = document.getElementById(key + '-cover-candidates');
        var candidates = [];
        if (dataEl) {
            try { candidates = JSON.parse(dataEl.textContent) || []; } catch (e) { candidates = []; }
        }
        var entry = { heroEl: heroEl, originalBackground: heroEl.style.backgroundImage, candidates: candidates, index: -1 };
        state[key] = entry;
        return entry;
    }

    window._coverHeroNav = function (key, dir) {
        var entry = ensure(key);
        if (!entry || !entry.candidates.length) return;
        var total = entry.candidates.length + 1; // +1 slot for the true cover photo
        var current = ((entry.index + 1) + dir + total) % total;
        entry.index = current - 1;
        entry.heroEl.style.backgroundImage = entry.index < 0 ? entry.originalBackground : "url('" + entry.candidates[entry.index].url + "')";
    };

    // Drops the memoized entry for `key` so the next hover-cycle re-reads the
    // hero element's current background as the "true" cover to return to.
    // Called after the cover photo changes live (see _photo_lightbox.html's
    // _applyCoverHeroUpdate) - the cached originalBackground/candidates would
    // otherwise still reflect the pre-change cover photo.
    window._coverHeroInvalidate = function (key) {
        delete state[key];
    };
}());
