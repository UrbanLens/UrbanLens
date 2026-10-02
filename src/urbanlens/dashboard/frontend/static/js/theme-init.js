// Resolves the "system" theme from the OS preference; the server writes data-theme for any other choice.
// Synchronous in <head>, ahead of the stylesheet: anything later paints a frame in the wrong theme.
(function () {
    var root = document.getElementById('html-root');
    if (!root || root.getAttribute('data-theme')) return;
    var query = window.matchMedia('(prefers-color-scheme: dark)');
    root.setAttribute('data-theme', query.matches ? 'dark' : 'light');
    query.addEventListener('change', function (event) {
        root.setAttribute('data-theme', event.matches ? 'dark' : 'light');
    });
}());
