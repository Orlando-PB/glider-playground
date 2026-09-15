/* Argo float layer for map_view.html — experimental, self-contained.
 *
 * Draws the fleet as one THREE.Points sprite cloud (constant pixel size, one
 * draw call, depth-tested against the globe so the far side hides) added to
 * the globe scene, draws the selected float's trajectory with the map's own
 * surface ribbon builder (passed in via attach, so it sits exactly on the
 * sphere like the glider tracks — no parallax), and adds its
 * own row in the Layers sidebar and a detail card. map_view.html only calls:
 *
 *   ArgoLayer.attach(getGlobe, layerBody, ribbonMesh)   once the globe exists
 *   ArgoLayer.handleClick(x, y)             at the top of its click handler
 *   ArgoLayer.hoverInfo(x, y, tol)          at the top of its hover pass
 *
 * Remove the <script> tag + those three calls and nothing else changes.
 * Backend: argo_logic.py (/api/argo/floats, /api/argo/float/<wmo>).
 */
(function () {
    'use strict';

    // Slider stops: "active" = last profile within this many days (0 = all).
    const RANGES = [
        { days: 1 / 24, label: '1 hour' },
        { days: 1,    label: '1 day' },
        { days: 5,    label: '5 days' },
        { days: 10,   label: '10 days' },
        { days: 30,   label: '30 days' },
        { days: 365,  label: '1 year' },
        { days: 3650, label: '10 years' },
        { days: 0,    label: 'all time' },
    ];
    const DEFAULT_RANGE = 4;          // 30 days
    const COL_RECENT = '#7dd3fc';     // last profile ≤ 30 d (light blue, pops on the ocean tiles)
    const COL_OLD = '#cbd5e1';        // older (light grey)
    const DOT_PX = 7, DOT_PX_SEL = 13; // sprite size in screen pixels (no size attenuation)
    const COL_SEL = '#FFD700';        // selected (matches the app accent)

    let getGlobe = null;
    let enabled = false;
    let rangeIdx = DEFAULT_RANGE;
    let allFloats = [];               // every float in the current time range
    let floats = [];                  // ...after the BODC filter: [{wmo, lat, lng, last, n, first, dac}]
    let bodcOnly = false;
    let selected = null;              // wmo string
    let fetchSeq = 0;
    let ui = {};
    let cloud = null;                 // THREE.Points for the fleet
    let selCloud = null;              // THREE.Points for the selected float (drawn on top)
    let trackMesh = null;             // ribbon for the selected float's trajectory
    let ribbonMesh = null;            // map_view's surface ribbon builder (polylines, renderOrder)
    const _v = (typeof THREE !== 'undefined') ? new THREE.Vector3() : null;

    // ---------- styles ----------
    const css = `
        #argoRow { display:flex; flex-direction:column; gap:3px; }
        #argoSlider { display:none; flex-direction:column; gap:2px; padding:0 8px 4px; }
        #argoSlider.on { display:flex; }
        #argoSlider input { width:100%; margin:0; accent-color: var(--accent); }
        #argoSlider .argoLbl { font-size:10px; color: var(--text-muted); display:flex; justify-content:space-between; }
        /* All | BODC segmented switch, full width of the sidebar row. */
        #argoSeg { display:flex; margin-top:3px; border-radius:6px; overflow:hidden;
            background: color-mix(in srgb, var(--text-primary) 10%, transparent); }
        #argoSeg button { flex:1; border:none; background:transparent; color: var(--text-muted);
            font-size:10px; font-weight:600; padding:3px 0; cursor:pointer; }
        #argoSeg button.on { background: color-mix(in srgb, var(--accent) 30%, transparent); color: var(--text-primary); }
        #argoCard {
            display:none; position:absolute; top:10px; left:10px; z-index:62; width:250px;
            max-width: calc(100vw - 20px);
            max-height: calc(100vh - 20px); overflow-y:auto; box-sizing:border-box;
            padding:8px 10px; border-radius:10px; font-size:11px; line-height:1.4;
            color: var(--text-primary);
            background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
            box-shadow: 0 1px 8px rgba(0,0,0,0.3); backdrop-filter: blur(4px);
        }
        #argoCard.visible { display:block; }
        #argoCard h3 { margin:0 0 4px; font-size:12px; font-weight:700; display:flex; align-items:center; gap:6px; }
        #argoCard h3 .sp { flex:1; }
        #argoCard h3 button { border:none; background:transparent; color:var(--text-muted); cursor:pointer; padding:0; display:flex; }
        #argoCard h3 button:hover { color:var(--text-primary); }
        #argoCard .st { font-size:10px; font-weight:700; padding:1px 6px; border-radius:99px; }
        #argoCard .st.on { background: rgba(34,197,94,0.25); color:#22c55e; }
        #argoCard .st.off { background: rgba(156,163,175,0.25); color: var(--text-muted); }
        #argoCard dl { display:grid; grid-template-columns: 78px 1fr; gap:1px 8px; margin:0; }
        #argoCard dt { color: var(--text-muted); }
        #argoCard dd { margin:0; word-break: break-word; }
        #argoCard .sec { margin:6px 0 2px; font-size:9px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color: var(--text-muted); }
        #argoCard a { color: var(--accent); text-decoration:none; }
        #argoCard a:hover { text-decoration:underline; }
        #argoCard .muted { color: var(--text-muted); }
        #argoCard .argoSpin { animation: chlaspin 0.9s linear infinite; display:inline-block; }
        #argoCard h3 { cursor:pointer; user-select:none; }
        #argoCard .chev { font-size:16px; color: var(--text-muted); transition: transform .15s; }
        #argoCard.collapsed .chev { transform: rotate(-90deg); }
        #argoCard.collapsed > :not(h3) { display:none; }
        #argoCard.collapsed h3 { margin-bottom:0; }
        /* Narrow frames: stay top-left (the bottom belongs to the Layers
           button), just a bit narrower and shorter; the header folds it. */
        @media (max-width: 520px) {
            #argoCard { width:210px; max-height:60vh; padding:6px 8px; font-size:10px; }
            #argoCard dl { grid-template-columns: 66px 1fr; }
        }
    `;

    function esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    // Backend ISO strings carry an explicit Z; fleet-monitoring ones are
    // truncated to seconds with no zone (they're UTC) — force UTC either way.
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
        const d = Math.max(0, Date.now() - t) / 86400000;
        if (d < 1) return `${Math.floor(d * 24)} h ago`;
        if (d < 60) return `${Math.floor(d)} d ago`;
        if (d < 730) return `${(d / 30.44).toFixed(1)} months ago`;
        return `${(d / 365.25).toFixed(1)} years ago`;
    }
    function ageYears(s) {
        const t = parseUTC(s);
        if (isNaN(t)) return '';
        return ((Date.now() - t) / (365.25 * 86400000)).toFixed(2) + ' years';
    }
    function fmtLatLon(lat, lon) {
        if (lat == null || lon == null) return '–';
        return `${Number(lat).toFixed(3)}°, ${Number(lon).toFixed(3)}°`;
    }

    // ---------- sidebar UI ----------
    function buildUI(layerBody) {
        const style = document.createElement('style');
        style.textContent = css;
        document.head.appendChild(style);

        const row = document.createElement('div');
        row.id = 'argoRow';
        row.innerHTML = `
            <button id="argoToggle" class="ovBtn" title="Show Argo float positions">
                <span class="material-symbols-outlined ovSwatch" style="font-size:14px;">scatter_plot</span>
                <span class="ovLabel">Argo floats</span>
            </button>
            <div id="argoSlider">
                <input type="range" min="0" max="${RANGES.length - 1}" value="${DEFAULT_RANGE}" step="1">
                <div class="argoLbl"><span id="argoRangeLbl"></span><span id="argoCount"></span></div>
                <div id="argoSeg" title="Filter by data centre">
                    <button data-v="all" class="on">All</button><button data-v="bodc">BODC</button>
                </div>
            </div>`;
        // Insert above the Smooth/DAC toggles so it sits with the data layers.
        layerBody.insertBefore(row, layerBody.firstChild);

        const card = document.createElement('div');
        card.id = 'argoCard';
        document.body.appendChild(card);

        ui = {
            toggle: row.querySelector('#argoToggle'),
            slider: row.querySelector('#argoSlider'),
            range: row.querySelector('input'),
            rangeLbl: row.querySelector('#argoRangeLbl'),
            count: row.querySelector('#argoCount'),
            seg: row.querySelectorAll('#argoSeg button'),
            card,
        };
        ui.seg.forEach(b => b.addEventListener('click', () => {
            bodcOnly = b.dataset.v === 'bodc';
            ui.seg.forEach(x => x.classList.toggle('on', x === b));
            applyFilter();
        }));
        ui.toggle.addEventListener('click', () => setEnabled(!enabled));
        ui.range.addEventListener('input', () => {
            rangeIdx = Number(ui.range.value);
            updateRangeLabel();
            if (enabled) fetchFloats();
        });
        updateRangeLabel();
    }

    function updateRangeLabel() {
        ui.rangeLbl.textContent = 'Active: ' + RANGES[rangeIdx].label;
    }

    function setEnabled(on) {
        enabled = on;
        ui.toggle.classList.toggle('on', on);
        ui.slider.classList.toggle('on', on);
        ui.toggle.title = (on ? 'Hide' : 'Show') + ' Argo float positions';
        if (on) fetchFloats();
        else {
            allFloats = []; floats = [];
            closeCard();
            applyPoints();
            ui.count.textContent = '';
        }
    }

    // ---------- data ----------
    async function fetchFloats() {
        const seq = ++fetchSeq;
        const days = RANGES[rangeIdx].days;
        ui.count.innerHTML = '<span class="material-symbols-outlined argoSpin" style="font-size:11px;">progress_activity</span>';
        try {
            const res = await fetch(`/api/argo/floats?days=${days}`);
            if (!res.ok) throw new Error(`HTTP ${res.status} (server out of date? restart it)`);
            const j = await res.json();
            if (seq !== fetchSeq || !enabled) return;
            if (j.status === 'building') {
                ui.count.textContent = 'building index…';
                setTimeout(() => { if (enabled && seq === fetchSeq) fetchFloats(); }, 4000);
                return;
            }
            if (j.status === 'error') {
                ui.count.textContent = 'unavailable';
                console.warn('[argo]', j.error);
                return;
            }
            allFloats = j.floats.map(r => ({ wmo: r[0], lat: r[1], lng: r[2], last: r[3], n: r[4], first: r[5], dac: r[6] }));
            applyFilter();
        } catch (err) {
            if (seq !== fetchSeq) return;
            ui.count.textContent = 'failed';
            ui.count.title = String(err && err.message || err);
            console.warn('[argo] fetch failed', err);
        }
    }

    // Apply the BODC-only checkbox to the fetched range and redraw. A selected
    // float that drops out of the filter keeps its card but loses its dot.
    function applyFilter() {
        floats = bodcOnly ? allFloats.filter(f => f.dac === 'bodc') : allFloats;
        ui.count.textContent = floats.length.toLocaleString() + ' floats';
        applyPoints();
    }

    function isRecent(f) {
        const t = parseUTC(f.last);
        return !isNaN(t) && (Date.now() - t) < 30 * 86400000;
    }

    // Fleet dots: a GL point cloud with its own tiny shader, matching how the
    // glider map draws every surface layer — depth test OFF (the ocean tiles
    // make the depth buffer useless for surface things), the far hemisphere
    // discarded by the "outward normal faces the camera" test, stacking by
    // renderOrder only. Each point is a fixed-pixel disc with a white rim.
    const DOT_VERT = `
        uniform float uSize;
        attribute vec3 color;
        varying vec3 vColor; varying vec3 vWorld;
        void main() {
            vColor = color;
            vWorld = (modelMatrix * vec4(position, 1.0)).xyz;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
            gl_PointSize = uSize;
        }`;
    const DOT_FRAG = `
        varying vec3 vColor; varying vec3 vWorld;
        void main() {
            if (dot(vWorld, cameraPosition - vWorld) < 0.0) discard;   // far side of the globe
            vec2 d = gl_PointCoord - vec2(0.5);
            float r = length(d) * 2.0;                                 // 0 centre → 1 edge
            if (r > 1.0) discard;
            float rim = smoothstep(0.62, 0.74, r);                     // inner fill → white rim
            float edge = 1.0 - smoothstep(0.86, 1.0, r);               // soft outer edge
            gl_FragColor = vec4(mix(vColor, vec3(1.0), rim), edge);
        }`;

    function buildCloud(list, px) {
        const n = list.length;
        const pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
        const g = getGlobe();
        const c = new THREE.Color();
        list.forEach((f, i) => {
            const p = g.getCoords(f.lat, f.lng, 0);
            pos[i * 3] = p.x; pos[i * 3 + 1] = p.y; pos[i * 3 + 2] = p.z;
            c.set(f.wmo === selected ? COL_SEL : (isRecent(f) ? COL_RECENT : COL_OLD));
            col[i * 3] = c.r; col[i * 3 + 1] = c.g; col[i * 3 + 2] = c.b;
        });
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
        let dpr = 1;
        try { dpr = g.renderer().getPixelRatio() || 1; } catch (_) {}
        const mat = new THREE.ShaderMaterial({
            uniforms: { uSize: { value: px * dpr } },   // gl_PointSize is in device pixels
            vertexShader: DOT_VERT, fragmentShader: DOT_FRAG,
            transparent: true, depthTest: false, depthWrite: false,
        });
        const pts = new THREE.Points(geo, mat);
        pts.renderOrder = 15;        // above tracks/DAC (≤14), below the ruler (20)
        pts.frustumCulled = false;
        return pts;
    }

    function disposeCloud(pts) {
        if (!pts) return;
        try { getGlobe().scene().remove(pts); pts.geometry.dispose(); pts.material.dispose(); } catch (_) {}
    }

    function applyPoints() {
        const g = getGlobe && getGlobe();
        if (!g || typeof THREE === 'undefined') return;
        disposeCloud(cloud); disposeCloud(selCloud);
        cloud = selCloud = null;
        if (!floats.length) return;
        cloud = buildCloud(floats.filter(f => f.wmo !== selected), DOT_PX);
        g.scene().add(cloud);
        const sel = floats.filter(f => f.wmo === selected);
        if (sel.length) { selCloud = buildCloud(sel, DOT_PX_SEL); g.scene().add(selCloud); }
    }

    // ---------- picking (same screen-space projection as the glider markers) ----------
    function nearestAtScreen(clientX, clientY, tolPx) {
        const g = getGlobe && getGlobe();
        if (!g || !enabled || !floats.length || !_v) return null;
        const rect = document.getElementById('globe-container').getBoundingClientRect();
        const px = clientX - rect.left, py = clientY - rect.top;
        const cam = g.camera();
        const ex = cam.position.x, ey = cam.position.y, ez = cam.position.z;
        let best = null, bestD = tolPx * tolPx;
        for (const f of floats) {
            const p = g.getCoords(f.lat, f.lng, 0);
            if (p.x * (ex - p.x) + p.y * (ey - p.y) + p.z * (ez - p.z) <= 0) continue;   // far side
            _v.set(p.x, p.y, p.z).project(cam);
            if (_v.z > 1) continue;
            const sx = (_v.x * 0.5 + 0.5) * rect.width;
            const sy = (-_v.y * 0.5 + 0.5) * rect.height;
            const dx = sx - px, dy = sy - py, d = dx * dx + dy * dy;
            if (d < bestD) { bestD = d; best = f; }
        }
        return best;
    }

    function hoverInfo(clientX, clientY, tolPx) {
        const f = nearestAtScreen(clientX, clientY, tolPx || 10);
        if (!f) return null;
        return { title: 'Argo ' + f.wmo, subtitle: ago(f.last), detail: `${f.n} profiles · ${f.dac}` };
    }

    function handleClick(clientX, clientY) {
        const f = nearestAtScreen(clientX, clientY, 14);
        if (!f) return false;
        select(f);
        return true;
    }

    // ---------- selection + detail card ----------
    function select(f) {
        selected = f.wmo;
        applyPoints();
        showCard(f);
        loadDetail(f);
    }

    function closeCard() {
        selected = null;
        ui.card.classList.remove('visible');
        clearTrajectory();
        applyPoints();
    }

    function cardHeader(wmo, status) {
        const cls = status === 'Active' ? 'on' : 'off';
        return `<h3 id="argoHead"><span class="material-symbols-outlined chev">expand_more</span><span>Argo ${esc(wmo)}</span>` +
            (status ? `<span class="st ${cls}">${esc(status)}</span>` : '') +
            `<span class="sp"></span><button id="argoClose" title="Close"><span class="material-symbols-outlined" style="font-size:16px;">close</span></button></h3>`;
    }
    function wireCard() {
        ui.card.querySelector('#argoClose').addEventListener('click', (e) => { e.stopPropagation(); closeCard(); });
        ui.card.querySelector('#argoHead').addEventListener('click', () => ui.card.classList.toggle('collapsed'));
    }

    function showCard(f) {
        ui.card.innerHTML = cardHeader(f.wmo, null) + `
            <dl>
                <dt>Last profile</dt><dd>${esc(fmtDate(f.last))}<br><span class="muted">${esc(ago(f.last))}</span></dd>
                <dt>Position</dt><dd>${esc(fmtLatLon(f.lat, f.lng))}</dd>
                <dt>Profiles</dt><dd>${esc(f.n)}</dd>
                <dt>First profile</dt><dd>${esc(fmtDate(f.first))}</dd>
                <dt>DAC</dt><dd>${esc(f.dac)}</dd>
            </dl>
            <div class="muted" style="margin-top:6px;"><span class="material-symbols-outlined argoSpin" style="font-size:11px;vertical-align:-2px;">progress_activity</span> Loading details…</div>`;
        ui.card.classList.add('visible');
        wireCard();
    }

    async function loadDetail(f) {
        let d;
        try {
            const res = await fetch(`/api/argo/float/${encodeURIComponent(f.wmo)}`);
            d = await res.json();
        } catch (err) {
            d = { error: String(err) };
        }
        if (selected !== f.wmo) return;
        renderCard(f, d);
        drawTrajectory(d);
    }

    function measure(m) {
        if (!m || m.pres == null) return '–';
        const parts = [`${Number(m.pres).toFixed(1)} dbar`];
        if (m.temp != null) parts.push(`${Number(m.temp).toFixed(3)} °C`);
        if (m.psal != null) parts.push(`${Number(m.psal).toFixed(3)} PSU`);
        return parts.join(' · ');
    }

    function renderCard(f, d) {
        const row = (k, v) => (v == null || v === '' || v === '–') ? '' : `<dt>${esc(k)}</dt><dd>${v}</dd>`;
        const dep = d.deployment || {}, lc = d.last_cycle || {};
        let html = cardHeader(f.wmo, d.status);
        if (d.error) {
            html += `<div class="muted">${esc(d.error)}</div>`;
        }
        html += `<div class="sec">Activity</div><dl>
            ${row('Last profile', esc(fmtDate(lc.date || f.last)) + `<br><span class="muted">${esc(ago(lc.date || f.last))}</span>`)}
            ${row('Cycle', lc.cycle)}
            ${row('Profiles', d.n_cycles || f.n)}
            ${row('Position', esc(fmtLatLon(lc.lat ?? f.lat, lc.lon ?? f.lng)))}
            ${row('Surface', esc(measure(lc.surface)))}
            ${row('Bottom', esc(measure(lc.bottom)))}
            ${d.grey_list && d.grey_list.length ? row('Grey list', esc(d.grey_list.join(', '))) : ''}
        </dl>`;
        if (!d.error) {
            html += `<div class="sec">Deployment</div><dl>
                ${row('Launched', dep.date ? esc(fmtDate(dep.date)) + `<br><span class="muted">${esc(ageYears(dep.date))} old</span>` : null)}
                ${row('Where', dep.lat != null ? esc(fmtLatLon(dep.lat, dep.lon)) : null)}
                ${row('Ship', esc(dep.ship))}
                ${row('Cruise', esc(dep.cruise))}
                ${row('PI', esc(dep.pi))}
            </dl>
            <div class="sec">About</div><dl>
                ${row('Project', esc((d.projects || []).join(', ')))}
                ${row('Owner', esc(d.owner))}
                ${row('Data centre', esc(d.data_centre))}
                ${row('Platform', esc([d.platform_type, d.maker].filter(Boolean).join(' · ')))}
                ${row('Network', esc((d.networks || []).join(', ')))}
                ${row('Telemetry', esc(d.transmission))}
                ${row('Sensors', esc((d.sensors || []).join(', ')))}
            </dl>
            <div style="margin-top:6px;"><a href="${esc(d.link)}" target="_blank" rel="noopener">Euro-Argo fleet monitoring ↗</a></div>`;
        }
        ui.card.innerHTML = html;
        wireCard();
    }

    function clearTrajectory() {
        if (!trackMesh) return;
        try { getGlobe().scene().remove(trackMesh); trackMesh.geometry.dispose(); trackMesh.material.dispose(); } catch (_) {}
        trackMesh = null;
    }

    // Trajectory as a surface ribbon (same shader/altitude as glider tracks):
    // yellow, 2px, fading in from the oldest fix to the newest.
    function drawTrajectory(d) {
        clearTrajectory();
        const g = getGlobe && getGlobe();
        const locs = (d && d.locations) || [];
        if (!g || !ribbonMesh || locs.length < 2) return;
        const n = locs.length;
        const pl = locs.map((L, i) => ({ lat: L[0], lng: L[1], t: i / (n - 1) }));
        trackMesh = ribbonMesh([pl], 14.5);   // just under the float dots (15)
        const c = new THREE.Color(COL_SEL);
        trackMesh.material.uniforms.uColorA.value.set(c.r, c.g, c.b, 0.35);
        trackMesh.material.uniforms.uColorB.value.set(c.r, c.g, c.b, 1.0);
        trackMesh.material.uniforms.uWidthA.value = 1.5;
        trackMesh.material.uniforms.uWidthB.value = 2.5;
        g.scene().add(trackMesh);
    }

    // ---------- public ----------
    window.ArgoLayer = {
        attach(globeGetter, layerBody, ribbonBuilder) {
            getGlobe = globeGetter;
            ribbonMesh = ribbonBuilder || null;
            if (!ui.toggle) buildUI(layerBody);
        },
        handleClick,
        hoverInfo,
        isEnabled: () => enabled,
    };
})();
