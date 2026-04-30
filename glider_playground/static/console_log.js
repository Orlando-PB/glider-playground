// Shared console + fetch logging used across the index/plot/map/3d pages.
(function () {
    // Suppress the well-known Tailwind play-CDN production warning.
    // console_log.js must be loaded BEFORE the Tailwind CDN script for this to fire in time.
    const _origWarn = console.warn;
    console.warn = function (...args) {
        const first = args[0];
        if (typeof first === 'string' && first.indexOf('cdn.tailwindcss.com should not be used') !== -1) return;
        return _origWarn.apply(console, args);
    };

    // --- API call batching ---
    // Calls within a 150ms window are grouped and deduplicated.
    // /api/files is very high-frequency polling — demoted to console.debug (hidden unless Verbose).
    const NOISY_PATHS = ['/api/files'];

    const _pendingApi = {};
    let _flushTimer = null;

    function _flushApiLogs() {
        _flushTimer = null;
        const entries = Object.entries(_pendingApi);
        if (!entries.length) return;
        for (const key of Object.keys(_pendingApi)) delete _pendingApi[key];

        const total = entries.reduce((s, [, v]) => s + v.count, 0);
        const isSingle = total === 1;

        const styles = [
            'background:#1e3a5f;color:#7ec8f7;font-weight:bold;border-radius:3px 0 0 3px;padding:1px 4px',
            'color:#aac8e8;font-weight:normal',
        ];
        let label;
        if (isSingle) {
            label = `%c API %c ${entries[0][0]}  %c${entries[0][1].lastTime}`;
            styles.push('color:#556;font-weight:normal');
        } else {
            label = `%c API %c ${total} requests`;
        }

        console.groupCollapsed(label, ...styles);
        for (const [path, { count, lastTime }] of entries) {
            const countStr = count > 1 ? `  ×${count}` : '';
            console.log(`%c  ${path}%c  ${lastTime}${countStr}`, 'color:#7ec8f7', 'color:#556');
        }
        console.groupEnd();
    }

    const _origFetch = window.fetch;
    window.fetch = async function (...args) {
        const response = await _origFetch.apply(this, args);
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        if (url.indexOf('/api/') !== -1) {
            const t = response.headers && response.headers.get('X-Process-Time');
            if (t) {
                try {
                    const path = new URL(url, window.location.origin).pathname;
                    const timeStr = `${parseFloat(t).toFixed(3)}s`;

                    if (NOISY_PATHS.includes(path)) {
                        console.debug(`[API] ${path} ${timeStr}`);
                    } else {
                        if (_pendingApi[path]) {
                            _pendingApi[path].count++;
                            _pendingApi[path].lastTime = timeStr;
                        } else {
                            _pendingApi[path] = { count: 1, lastTime: timeStr };
                        }
                        clearTimeout(_flushTimer);
                        _flushTimer = setTimeout(_flushApiLogs, 150);
                    }
                } catch (_) {}
            }
        }
        return response;
    };

    window.logRender = function (label, ms) {
        const seconds = (ms / 1000).toFixed(3);
        console.log(
            `%c RENDER %c ${label}  %c${seconds}s`,
            'background:#14532d;color:#86efac;font-weight:bold;border-radius:3px 0 0 3px;padding:1px 4px',
            'color:#86efac;font-weight:normal',
            'color:#556'
        );
    };

    window.logVersion = function (version, isServer) {
        const modeLabel = isServer ? 'server' : 'local';
        const modeBg = isServer ? '#3b1f6e' : '#1a3a1a';
        const modeColor = isServer ? '#c4a8f5' : '#86efac';
        console.log(
            `%c Glider Playground %c v${version} %c ${modeLabel} `,
            'background:#1e3a5f;color:#7ec8f7;font-weight:bold;padding:2px 6px;border-radius:3px 0 0 3px',
            'background:#0f2540;color:#aac8e8;padding:2px 6px',
            `background:${modeBg};color:${modeColor};padding:2px 6px;border-radius:0 3px 3px 0`
        );
    };
})();
