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
 * `candidates` is every OTHER eligible photo ({id, url}); cycling wraps through the true cover without storing anything.
 */
(function () {
    var state = {};

    function ensure(key) {
        if (state[key]) return state[key];
        // Pin detail reuses _page_hero.html under its own id; mirror _photo_lightbox.html's key-"pin" special case.
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

    // Drop the memoized entry so the next cycle re-reads the current cover (called after live cover changes).
    window._coverHeroInvalidate = function (key) {
        delete state[key];
    };
}());
