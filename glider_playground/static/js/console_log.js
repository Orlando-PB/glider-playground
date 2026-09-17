// Shared console + fetch logging used across the index/plot/map/3d pages.
(function () {
    // Suppress the Tailwind play-CDN warning; this file must load BEFORE the Tailwind CDN script.
    const _origWarn = console.warn;
    console.warn = function (...args) {
        const first = args[0];
        if (typeof first === 'string' && first.indexOf('cdn.tailwindcss.com should not be used') !== -1) return;
        return _origWarn.apply(console, args);
    };

    // --- Diagnostics toggle: off by default; gpSetDebug(true/false) from any panel (shared via localStorage) ---
    function _readDebugFlag() {
        try { return localStorage.getItem('gp_debug') === '1'; } catch (_) { return false; }
    }
    window.GP_DEBUG = _readDebugFlag();
    window.gpSetDebug = function (on) {
        window.GP_DEBUG = !!on;
        try { localStorage.setItem('gp_debug', on ? '1' : '0'); } catch (_) {}
        console.log(
            `%c Diagnostics ${on ? 'ON' : 'OFF'} `,
            `background:${on ? '#14532d' : '#3a1a1a'};color:${on ? '#86efac' : '#fca5a5'};padding:2px 6px;border-radius:3px`
        );
    };

    // --- API call batching: 150ms windows, deduplicated; NOISY_PATHS go to console.debug ---
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

    // The main plot fetch is logged by logPlotTiming, so initPlot sets this flag to skip its API line.
    let _skipNextApiLog = false;
    window.gpSkipNextApiLog = function () { _skipNextApiLog = true; };

    const _origFetch = window.fetch;
    window.fetch = async function (...args) {
        // Capture (and clear) the flag synchronously, before any await.
        const skipLog = _skipNextApiLog;
        _skipNextApiLog = false;
        const response = await _origFetch.apply(this, args);
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        if (window.GP_DEBUG && !skipLog && url.indexOf('/api/') !== -1) {
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

    // One-liner for plot re-draws outside the full PLOT timing pipeline; `points` is the count now on the plot.
    window.logRedraw = function (action, points, ms) {
        if (!window.GP_DEBUG) return;
        const pts = (typeof points === 'number') ? points.toLocaleString() + ' pts' : '';
        const t = (typeof ms === 'number') ? `  %c${(ms).toFixed(0)}ms` : '';
        const styles = [
            'background:#1e3a5f;color:#7ec8f7;font-weight:bold;border-radius:3px 0 0 3px;padding:1px 4px',
            'color:#7ec8f7;font-weight:normal',
            'color:#aac8e8',
        ];
        if (t) styles.push('color:#556');
        console.log(`%c REDRAW %c ${action}  %c${pts}${t}`, ...styles);
    };

    window.logRender = function (label, ms) {
        if (!window.GP_DEBUG) return;
        const seconds = (ms / 1000).toFixed(3);
        console.log(
            `%c RENDER %c ${label}  %c${seconds}s`,
            'background:#14532d;color:#86efac;font-weight:bold;border-radius:3px 0 0 3px;padding:1px 4px',
            'color:#86efac;font-weight:normal',
            'color:#556'
        );
    };

    // Timestamp comparable ACROSS documents (parent <-> iframe): performance.now() + timeOrigin.
    window.gpNow = function () {
        return performance.timeOrigin + performance.now();
    };

    // One collapsed log for a full render. phases: ordered [{ name, ms, color?, children? }] (children get an
    // auto "·other" remainder); unaccounted = totalMs - sum(phases). note: header text; detail: dim line inside;
    // opts: { badge, badgeBg, badgeColor } to relabel the header chip.
    window.logPlotTiming = function (label, phases, totalMs, note, detail, opts) {
        if (!window.GP_DEBUG) return;
        opts = opts || {};
        const badge = opts.badge || 'PLOT';
        const badgeBg = opts.badgeBg || '#14532d';
        const badgeColor = opts.badgeColor || '#86efac';
        const total = Math.max(0, totalMs);
        const sum = phases.reduce((s, p) => s + Math.max(0, p.ms), 0);
        const rows = phases.slice();
        rows.push({ name: 'unaccounted', ms: total - sum, dim: true });

        const fmt = (ms) => (Math.abs(ms) >= 100 ? ms.toFixed(0) : ms.toFixed(1)) + 'ms';
        const headerSecs = (total / 1000).toFixed(3);

        console.groupCollapsed(
            `%c ${badge} %c ${label}  %c${headerSecs}s${note ? '  ·  ' + note : ''}`,
            `background:${badgeBg};color:${badgeColor};font-weight:bold;border-radius:3px 0 0 3px;padding:1px 4px`,
            `color:${badgeColor};font-weight:normal`,
            'color:#556'
        );
        if (detail) console.log(`%c${detail}`, 'color:#9aa6b8');

        const BAR = 22;
        // Pad names to a shared width INCLUDING the indent of nested rows so bars line up.
        const widthOf = (list, indent) => list.reduce((w, p) => {
            let cur = Math.max(w, indent + p.name.length);
            if (p.children && p.children.length) cur = Math.max(cur, widthOf(p.children, indent + 2));
            return cur;
        }, 0);
        const nameW = widthOf(rows, 0);

        const printRow = (p, indent) => {
            const name = (' '.repeat(indent) + p.name).padEnd(nameW);
            // A `header` phase is a pure grouping label: print just the label, not its own bar.
            if (p.header) {
                console.log(`%c${name}`, 'color:#aac8e8;font-weight:600');
            } else {
                const frac = total > 0 ? Math.max(0, p.ms) / total : 0;
                const filled = Math.min(BAR, Math.round(frac * BAR));
                const bar = '█'.repeat(filled) + '·'.repeat(BAR - filled);
                const pct = (frac * 100).toFixed(0).padStart(3);
                const color = p.dim ? '#5b6472' : (p.color || '#7ec8f7');
                console.log(
                    `%c${name} %c${bar} %c${pct}%%  %c${fmt(p.ms).padStart(8)}`,
                    p.dim ? 'color:#5b6472' : 'color:#aac8e8',
                    `color:${color}`,
                    'color:#667',
                    p.dim ? 'color:#5b6472' : 'color:#cdd3de'
                );
            }
            if (p.children && p.children.length) {
                const childSum = p.children.reduce((s, c) => s + Math.max(0, c.ms), 0);
                for (const c of p.children) printRow(c, indent + 2);
                const other = p.ms - childSum;
                if (Math.abs(other) >= 1) printRow({ name: 'other', ms: other, dim: true }, indent + 2);
            }
        };
        for (const p of rows) printRow(p, 0);
        console.groupEnd();
    };

    // One-line log for expected user-facing failures (no stack trace); always visible, not gated by GP_DEBUG.
    window.logNote = function (message) {
        console.log(
            `%c NOTE %c ${message}`,
            'background:#5a3a0a;color:#fbbf24;font-weight:bold;border-radius:3px 0 0 3px;padding:1px 4px',
            'color:#fbbf24;font-weight:normal'
        );
    };

    window.logVersion = function (version, isServer) {
        const modeLabel = isServer ? 'server' : 'local';
        const modeBg = isServer ? '#3b1f6e' : '#1a3a1a';
        const modeColor = isServer ? '#c4a8f5' : '#86efac';
        const parts = [
            `%c Glider Playground %c v${version} %c ${modeLabel} `,
            'background:#1e3a5f;color:#7ec8f7;font-weight:bold;padding:2px 6px;border-radius:3px 0 0 3px',
            'background:#0f2540;color:#aac8e8;padding:2px 6px',
            `background:${modeBg};color:${modeColor};padding:2px 6px;border-radius:0 3px 3px 0`,
        ];
        console.log(...parts);
        if (!window.GP_DEBUG) {
            console.log('%cdiagnostics off — run gpSetDebug(true) for API/PLOT/RENDER timing logs', 'color:#5b6472');
        }
    };
})();
