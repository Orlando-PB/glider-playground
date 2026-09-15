/* Research-ship layer for map_view.html — experimental, self-contained.
 *
 * Shows the latest reported position of RRS Discovery, RRS James Cook and
 * RRS Sir David Attenborough as MARS map icons (vendored in static/icons/),
 * rendered through map_view's own HTML marker layer so they look exactly like
 * the glider end markers, plus a sidebar toggle and a detail card. No tracks:
 * the feeds are only good to a few km. map_view.html only calls:
 *
 *   ShipsLayer.attach(getGlobe, layerBody, refreshDots)   once the globe exists
 *   ShipsLayer.dots()                        inside buildPositionDots() (marker items)
 *   ShipsLayer.handleClick(x, y)             at the top of its click handler
 *   ShipsLayer.hoverInfo(x, y, tol)          at the top of its hover pass
 *
 * Remove the <script> tag + those four calls and nothing else changes.
 * Backend: ships_logic.py (/api/ships).
 */
(function () {
    'use strict';

    const COL = '#fb923c';            // sidebar swatch
    const REFRESH_MS = 10 * 60 * 1000;

    let getGlobe = null, refreshDots = null;
    let enabled = false;
    let ships = [];                   // [{id, name, lat, lon, time, expedition, status, ...}]
    let selected = null;              // ship id
    let fetchSeq = 0, timer = null;
    let ui = {};
    const _v = (typeof THREE !== 'undefined') ? new THREE.Vector3() : null;

    const css = `
        #shipsRow { display:flex; flex-direction:column; gap:3px; }
        #shipsCard {
            display:none; position:absolute; top:10px; left:10px; z-index:62; width:250px;
            max-width: calc(100vw - 20px); max-height: calc(100vh - 20px); overflow-y:auto;
            box-sizing:border-box; padding:8px 10px; border-radius:10px; font-size:11px; line-height:1.4;
            color: var(--text-primary);
            background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
            box-shadow: 0 1px 8px rgba(0,0,0,0.3); backdrop-filter: blur(4px);
        }
        #shipsCard.visible { display:block; }
        #shipsCard h3 { margin:0 0 4px; font-size:12px; font-weight:700; display:flex; align-items:center; gap:6px; cursor:pointer; user-select:none; }
        #shipsCard h3 .sp { flex:1; }
        #shipsCard h3 button { border:none; background:transparent; color:var(--text-muted); cursor:pointer; padding:0; display:flex; }
        #shipsCard h3 button:hover { color:var(--text-primary); }
        #shipsCard .chev { font-size:16px; color: var(--text-muted); transition: transform .15s; }
        #shipsCard.collapsed .chev { transform: rotate(-90deg); }
        #shipsCard.collapsed > :not(h3) { display:none; }
        #shipsCard.collapsed h3 { margin-bottom:0; }
        #shipsCard dl { display:grid; grid-template-columns: 70px 1fr; gap:1px 8px; margin:0; }
        #shipsCard dt { color: var(--text-muted); }
        #shipsCard dd { margin:0; word-break: break-word; white-space: pre-line; }
        #shipsCard a { color: var(--accent); text-decoration:none; }
        #shipsCard a:hover { text-decoration:underline; }
        #shipsCard .muted { color: var(--text-muted); }
        #shipsCard .shipSpin { animation: chlaspin 0.9s linear infinite; display:inline-block; }
        @media (max-width: 520px) {
            #shipsCard { width:210px; max-height:60vh; padding:6px 8px; font-size:10px; }
            #shipsCard dl { grid-template-columns: 60px 1fr; }
        }
    `;

    function esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    // Backend strings carry an explicit Z — never a bare new Date(str) here.
    function parseUTC(s) {
        if (!s) return NaN;
        s = String(s).replace(' ', 'T');
        if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
        return Date.parse(s);
    }
    function fmtDate(s) {
        const t = parseUTC(s);
        if (isNaN(t)) return '–';
        return new Date(t).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
    }
    function ago(s) {
        const t = parseUTC(s);
        if (isNaN(t)) return '';
        const h = Math.max(0, Date.now() - t) / 3600000;
        if (h < 1) return `${Math.floor(h * 60)} min ago`;
        if (h < 48) return `${Math.floor(h)} h ago`;
        return `${Math.floor(h / 24)} d ago`;
    }
    function fmtLatLon(lat, lon) {
        if (lat == null || lon == null) return '–';
        return `${Number(lat).toFixed(3)}°, ${Number(lon).toFixed(3)}°`;
    }

    // ---------- sidebar ----------
    function buildUI(layerBody) {
        const style = document.createElement('style');
        style.textContent = css;
        document.head.appendChild(style);

        const row = document.createElement('div');
        row.id = 'shipsRow';
        row.innerHTML = `
            <button id="shipsToggle" class="ovBtn" title="Show UK research ship positions">
                <span class="material-symbols-outlined ovSwatch" style="font-size:14px;color:${COL};">directions_boat</span>
                <span class="ovLabel">Research ships</span>
            </button>`;
        // Below the Argo row if it's there, otherwise at the top.
        const argo = layerBody.querySelector('#argoRow');
        if (argo && argo.nextSibling) layerBody.insertBefore(row, argo.nextSibling);
        else if (argo) layerBody.appendChild(row);
        else layerBody.insertBefore(row, layerBody.firstChild);

        const card = document.createElement('div');
        card.id = 'shipsCard';
        document.body.appendChild(card);

        ui = { toggle: row.querySelector('#shipsToggle'), card };
        ui.toggle.addEventListener('click', () => setEnabled(!enabled));
    }

    function setEnabled(on) {
        enabled = on;
        ui.toggle.classList.toggle('on', on);
        ui.toggle.classList.remove('loading');
        ui.toggle.title = (on ? 'Hide' : 'Show') + ' UK research ship positions';
        if (on) {
            fetchShips();
            timer = setInterval(fetchShips, REFRESH_MS);
        } else {
            clearInterval(timer); timer = null;
            ships = [];
            closeCard();
            applyAll();
        }
    }

    // ---------- data ----------
    async function fetchShips() {
        const seq = ++fetchSeq;
        if (!ships.length) ui.toggle.classList.add('loading');
        try {
            const res = await fetch('/api/ships');
            if (!res.ok) throw new Error(`HTTP ${res.status} (server out of date? restart it)`);
            const j = await res.json();
            if (seq !== fetchSeq || !enabled) return;
            ships = j.ships || [];
            ui.toggle.classList.remove('loading');
            // Source failures only surface in the button's tooltip.
            ui.toggle.title = 'Hide UK research ship positions' +
                ((j.errors && j.errors.length) ? '\nSource down: ' + j.errors.join('; ') : '');
            applyAll();
            if (selected) {
                const s = ships.find(x => x.id === selected);
                if (s) renderCard(s);
            }
        } catch (err) {
            if (seq !== fetchSeq) return;
            ui.toggle.classList.remove('loading');
            ui.toggle.title = 'Research ships: ' + String(err && err.message || err);
            console.warn('[ships] fetch failed', err);
        }
    }

    // ---------- drawing ----------
    // Marker items for map_view's htmlElementsData layer (same renderer as the
    // glider end markers; dotEl handles `ship: true`).
    function dots() {
        if (!enabled) return [];
        return ships.map(s => ({
            lat: s.lat, lng: s.lon, ship: true, kind: s.icon || 'ship',
            name: s.name, active: s.id === selected, lastTime: s.time, id: s.id,
        }));
    }

    function applyAll() {
        if (refreshDots) { try { refreshDots(); } catch (_) {} }
    }

    // ---------- picking ----------
    function nearestAtScreen(clientX, clientY, tolPx) {
        const g = getGlobe && getGlobe();
        if (!g || !enabled || !ships.length || !_v) return null;
        const rect = document.getElementById('globe-container').getBoundingClientRect();
        const px = clientX - rect.left, py = clientY - rect.top;
        const cam = g.camera();
        const ex = cam.position.x, ey = cam.position.y, ez = cam.position.z;
        let best = null, bestD = tolPx * tolPx;
        for (const s of ships) {
            const p = g.getCoords(s.lat, s.lon, 0);
            if (p.x * (ex - p.x) + p.y * (ey - p.y) + p.z * (ez - p.z) <= 0) continue;
            _v.set(p.x, p.y, p.z).project(cam);
            if (_v.z > 1) continue;
            const sx = (_v.x * 0.5 + 0.5) * rect.width;
            const sy = (-_v.y * 0.5 + 0.5) * rect.height;
            const dx = sx - px, dy = sy - py, d = dx * dx + dy * dy;
            if (d < bestD) { bestD = d; best = s; }
        }
        return best;
    }

    function hoverInfo(clientX, clientY, tolPx) {
        const s = nearestAtScreen(clientX, clientY, Math.max(tolPx || 0, 16));
        if (!s) return null;
        return { title: s.name, subtitle: ago(s.time), detail: s.expedition || s.operator };
    }

    function handleClick(clientX, clientY) {
        const s = nearestAtScreen(clientX, clientY, 20);
        if (!s) return false;
        selected = s.id;
        applyAll();
        renderCard(s);
        return true;
    }

    // ---------- card ----------
    function closeCard() {
        selected = null;
        ui.card.classList.remove('visible');
        applyAll();
    }

    function renderCard(s) {
        const row = (k, v) => (v == null || v === '' || v === '–') ? '' : `<dt>${esc(k)}</dt><dd>${v}</dd>`;
        ui.card.innerHTML = `
            <h3 id="shipsHead"><span class="material-symbols-outlined chev">expand_more</span><span>${esc(s.name)}</span>
                <span class="sp"></span><button id="shipsClose" title="Close"><span class="material-symbols-outlined" style="font-size:16px;">close</span></button></h3>
            <dl>
                ${row('Reported', esc(fmtDate(s.time)) + `<br><span class="muted">${esc(ago(s.time))}</span>`)}
                ${row('Position', esc(fmtLatLon(s.lat, s.lon)))}
                ${row('Speed', esc(s.speed))}
                ${row('Expedition', esc(s.expedition))}
                ${row('Status', esc(s.status))}
                ${row('Intentions', esc(s.intentions))}
                ${row('Weather', esc(s.wx))}
                ${row('Operator', esc(s.operator))}
                ${row('Source', esc(s.source))}
                ${row('Precision', esc(s.precision))}
                ${row('MMSI', esc(s.mmsi))}
            </dl>
            <div style="margin-top:6px;"><a href="${esc(s.link)}" target="_blank" rel="noopener">Position source ↗</a>
            &nbsp;·&nbsp; <a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:${esc(s.mmsi)}" target="_blank" rel="noopener">AIS ↗</a></div>`;
        ui.card.classList.add('visible');
        ui.card.querySelector('#shipsClose').addEventListener('click', (e) => { e.stopPropagation(); closeCard(); });
        ui.card.querySelector('#shipsHead').addEventListener('click', () => ui.card.classList.toggle('collapsed'));
    }

    window.ShipsLayer = {
        attach(globeGetter, layerBody, refresh) {
            getGlobe = globeGetter;
            refreshDots = refresh || null;
            if (!ui.toggle) { buildUI(layerBody); setEnabled(true); }   // on by default
        },
        dots,
        handleClick,
        hoverInfo,
        isEnabled: () => enabled,
    };
})();
