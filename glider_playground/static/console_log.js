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

    // The main plot fetch is folded into the unified PLOT timing log (logPlotTiming),
    // so we skip its standalone "API /api/plot_data" line to avoid a duplicate log.
    // initPlot sets this flag synchronously right before issuing that one fetch.
    let _skipNextApiLog = false;
    window.gpSkipNextApiLog = function () { _skipNextApiLog = true; };

    const _origFetch = window.fetch;
    window.fetch = async function (...args) {
        // Capture (and clear) the suppress flag synchronously at call time, before
        // any await — otherwise a concurrent fetch could consume it.
        const skipLog = _skipNextApiLog;
        _skipNextApiLog = false;
        const response = await _origFetch.apply(this, args);
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        if (!skipLog && url.indexOf('/api/') !== -1) {
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

    // Lightweight one-liner for plot re-draws that don't go through the full PLOT
    // timing pipeline — zoom-in high-res swaps and zoom-out/reset. `points` is the
    // count now on the plot, so you can watch it change as you zoom in and back out.
    window.logRedraw = function (action, points, ms) {
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
        const seconds = (ms / 1000).toFixed(3);
        console.log(
            `%c RENDER %c ${label}  %c${seconds}s`,
            'background:#14532d;color:#86efac;font-weight:bold;border-radius:3px 0 0 3px;padding:1px 4px',
            'color:#86efac;font-weight:normal',
            'color:#556'
        );
    };

    // High-resolution timestamp comparable ACROSS documents (parent <-> iframe).
    // performance.now() is per-document; adding timeOrigin lifts it to a shared
    // epoch-ms clock so a timestamp taken in index.html lines up with one taken
    // inside the plot iframe.
    window.gpNow = function () {
        return performance.timeOrigin + performance.now();
    };

    // One collapsed log for a full plot render: header shows the total click->painted
    // time, the expanded view breaks it into phases plus any unaccounted remainder.
    //   label   — e.g. 'WebGL Plot'
    //   phases  — ordered [{ name, ms, color?, children? }]; deltas between pipeline
    //             marks. A phase may carry `children` (same shape) to break it down
    //             further — they render indented, with an auto "·other" remainder so
    //             the children always reconcile to the parent.
    //   totalMs — overall click -> fully-painted time
    // Unaccounted = total - sum(phases); it surfaces time we didn't attribute to a
    // named phase (queueing, idle waiting, anything we forgot to measure).
    //   note    — optional string shown in the collapsed header (e.g. point count),
    //             kept out of the aligned columns so it never shifts the bars.
    window.logPlotTiming = function (label, phases, totalMs, note) {
        const total = Math.max(0, totalMs);
        const sum = phases.reduce((s, p) => s + Math.max(0, p.ms), 0);
        const rows = phases.slice();
        rows.push({ name: 'unaccounted', ms: total - sum, dim: true });

        const fmt = (ms) => (Math.abs(ms) >= 100 ? ms.toFixed(0) : ms.toFixed(1)) + 'ms';
        const headerSecs = (total / 1000).toFixed(3);

        console.groupCollapsed(
            `%c PLOT %c ${label}  %c${headerSecs}s${note ? '  ·  ' + note : ''}`,
            'background:#14532d;color:#86efac;font-weight:bold;border-radius:3px 0 0 3px;padding:1px 4px',
            'color:#86efac;font-weight:normal',
            'color:#556'
        );

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
            // A `header` phase is a pure grouping label — its children carry the
            // numbers, so showing the parent's own bar/total would read as a
            // double count. Print just the label; children still reconcile to p.ms.
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

    window.logVersion = function (version, isServer, throttle, lowMemory) {
        const modeLabel = isServer ? 'server' : 'local';
        const modeBg = isServer ? '#3b1f6e' : '#1a3a1a';
        const modeColor = isServer ? '#c4a8f5' : '#86efac';
        const throttleOn = !!throttle;
        const lowMemOn = !!lowMemory;
        const modeBorder = (throttleOn || lowMemOn) ? '0' : '0 3px 3px 0';
        const parts = [
            `%c Glider Playground %c v${version} %c ${modeLabel} `,
            'background:#1e3a5f;color:#7ec8f7;font-weight:bold;padding:2px 6px;border-radius:3px 0 0 3px',
            'background:#0f2540;color:#aac8e8;padding:2px 6px',
            `background:${modeBg};color:${modeColor};padding:2px 6px;border-radius:${modeBorder}`,
        ];
        if (throttleOn) {
            const isLast = !lowMemOn;
            parts[0] += `%c throttle ON `;
            parts.push(`background:#5a3a0a;color:#fbbf24;padding:2px 6px;border-radius:${isLast ? '0 3px 3px 0' : '0'}`);
        }
        if (lowMemOn) {
            parts[0] += `%c low-mem `;
            parts.push('background:#3a1a1a;color:#fca5a5;padding:2px 6px;border-radius:0 3px 3px 0');
        }
        console.log(...parts);
    };
})();
