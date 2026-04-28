// Shared console + fetch logging used across the index/plot/map/3d pages.
// Replaces the previous duplicated styled-console blocks. Plain-text output
// stays readable when copied out of the dev console.
(function () {
    // Suppress the well-known Tailwind play-CDN production warning. We use the
    // CDN intentionally so the package stays pip-installable without a build
    // step; the warning is just noise.
    const _origWarn = console.warn;
    console.warn = function (...args) {
        const first = args[0];
        if (typeof first === 'string' && first.indexOf('cdn.tailwindcss.com should not be used') !== -1) return;
        return _origWarn.apply(console, args);
    };

    const _origFetch = window.fetch;
    window.fetch = async function (...args) {
        const response = await _origFetch.apply(this, args);
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        if (url.indexOf('/api/') !== -1) {
            const t = response.headers && response.headers.get('X-Process-Time');
            if (t) {
                try {
                    const path = new URL(url, window.location.origin).pathname;
                    console.log(`[API] ${path} ${parseFloat(t).toFixed(3)}s`);
                } catch (_) {}
            }
        }
        return response;
    };

    window.logRender = function (label, ms) {
        const seconds = (ms / 1000).toFixed(3);
        console.log(`[RENDER] ${label} ${seconds}s`);
    };
})();
