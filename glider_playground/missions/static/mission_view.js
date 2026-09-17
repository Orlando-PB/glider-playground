// Mission view: one 3D scene (same look as 3d_view.html: seabed mesh, water panes, scenery, low-poly models)
// holding several platforms on a shared time bar, with the mission's labels pinned to 3D points.
// Data: /api/missions/<id> (the mission JSON + file status), /scene (bathymetry, floats), /track/<key>.
// Runs in an iframe over the shell's workspace (missions_shell.js): no id = the mission list. Picking a mission
// posts `missionOpen`; clicking a platform posts the shell's `requestActivate`.
(() => {
    const qs = new URLSearchParams(location.search);
    // /missions = the list, /missions/<id> = one mission (?id= still works, e.g. inside an iframe).
    const fromPath = (location.pathname.match(/^\/missions\/([^/]+)\/?$/) || [])[1];
    const missionId = qs.get('id') || (fromPath && fromPath !== 'static' ? decodeURIComponent(fromPath) : null);
    const $ = id => document.getElementById(id);
    const setTheme = t => document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light');
    const cookieTheme = () => { const c = document.cookie.split('; ').find(v => v.startsWith('gp_theme=')); return c ? decodeURIComponent(c.slice(9)) : 'light'; };   // the shell's theme cookie
    setTheme(qs.get('theme') || cookieTheme());
    const embedded = window.parent !== window;       // inside the shell: it owns the URL and the way back
    if (embedded) document.documentElement.classList.add('embedded');
    window.addEventListener('message', e => { if (e.data && e.data.type === 'setTheme') setTheme(e.data.theme); });

    // Naive-UTC strings only (see CLAUDE.md timezone gotcha): force 'Z' before parsing.
    const parseUTC = s => { const v = String(s).trim().replace(' ', 'T'); return Date.parse((v.length <= 10 ? v + 'T00:00:00' : v) + 'Z'); };
    const fmtDate = ms => new Date(ms).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
    const getJSON = url => fetch(url).then(r => { if (!r.ok) throw new Error(`${url} → ${r.status}`); return r.json(); });
    const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

    // ── Look: the 3D view's palette and rules ──
    const SK = window.SceneKit;          // palette, seabed/water meshing, models, Plotly fast path: shared with 3d_view.html
    const boxAspectTarget = 0.22;        // box height as a fraction of its width (sets the vertical exaggeration)
    const SIZES = SK.SIZES;              // scene units; × mission.model_scale
    // Aft-deck cargo slots as fractions of the ship's hull length [x from midships (+ = bow), y to port]; DECK_LEN = cargo length (floats: height).
    const DECK_SLOTS = { alr: [[-0.37, 0.046], [-0.37, -0.046]], glider: [[-0.415, 0.014], [-0.415, -0.014], [-0.325, 0.014], [-0.325, -0.014]],
                         float: [[-0.262, 0.064], [-0.262, 0.022], [-0.262, -0.022], [-0.262, -0.064]] };
    const DECK_LEN = { alr: 0.17, glider: 0.08, float: 0.07 };
    const DIVE_SECONDS = 3;              // at the faster speeds a model takes this long (wall clock) for one down-and-up
    const alrWindowH = 1, alrTurnH = 1.5;   // ALRs are propelled, so they follow their own track closely: ± hours averaged, hours to swing onto a new heading
    const LINE_MS = 250;                 // while playing, a travelled line is redrawn at most this often (wall clock); its tip trace bridges the gap every frame
    const pitchTauS = 0.6;               // wall-clock seconds for a glider's nose to swing between diving and climbing
    const poseWindowH = 9;               // models' HORIZONTAL position rides a ±9 h average of the track (depth is real: see depthAt)
    const MODEL_LIGHT = SK.LIGHT.model;

    // ── Mission list: a hero strip + one card per mission with a live map thumbnail (same ocean tiles as the globe) ──
    const TILE = (z, x, y) => `https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/${z}/${y}/${x}`;
    const iconFor = kind => `/static/icons/${kind === 'rrs_discovery' ? 'rrs-discovery' : kind === 'rrs_james_cook' ? 'rrs-james-cook' : /^rrs|ship/.test(kind) ? 'ship' : kind}-mapicon.svg`;
    const KIND_NAMES = { alr: 'ALR', slocum: 'Slocum', seaglider: 'Seaglider' };
    function drawThumb(box, pv) {
        if (!pv || !pv.bounds) return;
        const W = box.clientWidth || 340, H = box.clientHeight || 210, b = pv.bounds;
        const mx = lon => (lon + 180) / 360, my = lat => { const s = Math.sin(lat * Math.PI / 180); return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI); };
        const z = Math.max(1, Math.min(8, Math.ceil(Math.log2(W / ((mx(b.max_lon) - mx(b.min_lon)) * 256)))));
        const n = 256 * 2 ** z, x0 = mx(b.min_lon) * n, x1 = mx(b.max_lon) * n, y0 = my(b.max_lat) * n, y1 = my(b.min_lat) * n;
        const k = Math.max(W / (x1 - x0), H / (y1 - y0)), ox = (W - (x1 - x0) * k) / 2 - x0 * k, oy = (H - (y1 - y0) * k) / 2 - y0 * k;   // cover + centre
        const px = (lat, lon) => [mx(lon) * n * k + ox, my(lat) * n * k + oy];
        const tiles = document.createElement('div'); tiles.className = 'tiles'; tiles.style.transform = `translate(${ox}px,${oy}px) scale(${k})`;
        for (let tx = Math.floor((-ox / k) / 256); tx <= Math.floor(((W - ox) / k) / 256); tx++) for (let ty = Math.floor((-oy / k) / 256); ty <= Math.floor(((H - oy) / k) / 256); ty++) {
            if (ty < 0 || ty >= 2 ** z) continue;
            const im = document.createElement('img'); im.alt = ''; im.loading = 'lazy'; im.src = TILE(z, ((tx % 2 ** z) + 2 ** z) % 2 ** z, ty);
            im.style.left = tx * 256 + 'px'; im.style.top = ty * 256 + 'px'; tiles.appendChild(im);
        }
        const path = (pts, colour, width, dash) => {
            const d = pts.map((q, i) => (i ? 'L' : 'M') + px(q[0], q[1]).map(v => v.toFixed(1)).join(',')).join('');
            let len = 0; for (let i = 1; i < pts.length; i++) { const A = px(pts[i - 1][0], pts[i - 1][1]), B = px(pts[i][0], pts[i][1]); len += Math.hypot(B[0] - A[0], B[1] - A[1]); }
            return `<path d="${d}" stroke="#fff" stroke-opacity=".7" stroke-width="${width + 2}" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`
                + `<path class="trk" d="${d}" stroke="${esc(colour)}" stroke-width="${width}" ${dash ? 'stroke-dasharray="2 5" style="animation:none;stroke-dashoffset:0"' : `style="--len:${Math.ceil(len)}"`}/>`;
        };
        let svg = '';
        for (const st of pv.stations || []) { const c = px(st.lat, st.lon), e = px(st.lat + (st.radius_km || 50) / 111.2, st.lon); svg += `<circle cx="${c[0]}" cy="${c[1]}" r="${Math.max(4, Math.abs(c[1] - e[1]))}" fill="${esc(st.colour || '#0f8b8d')}" fill-opacity=".18" stroke="${esc(st.colour || '#0f8b8d')}" stroke-width="1.5" stroke-dasharray="4 3"/>`; }
        for (const sh of pv.ships || []) for (const leg of sh.legs) svg += path(leg, sh.colour, 1.6, true);
        for (const l of pv.lines || []) svg += path(l.points, l.colour, l.width);
        box.appendChild(tiles); box.insertAdjacentHTML('beforeend', `<svg>${svg}</svg>`);
    }
    function importMission(file, msg) {
        const fd = new FormData(); fd.append('file', file);
        msg.textContent = 'Importing…';
        return fetch('/api/missions/import', { method: 'POST', body: fd }).then(r => r.json().catch(() => ({ message: 'HTTP ' + r.status })).then(j => {
            if (j.status !== 'success') throw new Error(j.message || j.detail || 'import failed');
            showPicker();
            setTimeout(() => { const m = document.querySelector('#picker .tools .msg'); if (m) m.textContent = `Imported “${j.mission}”` + (j.files || []).map(f => ` · ${f.name}: ${f.action}`).join(''); }, 0);
        })).catch(e => { msg.textContent = 'Import failed: ' + e.message; });
    }
    function wireTools(tools) {
        const input = tools.querySelector('input'), msg = tools.querySelector('.msg');
        tools.querySelector('[data-act=import]').onclick = () => input.click();
        input.onchange = () => { if (input.files[0]) importMission(input.files[0], msg); };
        tools.querySelector('[data-act=new]').onclick = async () => {
            const tpl = await fetch('/api/missions/template').then(r => r.text());
            const dlg = document.createElement('div'); dlg.className = 'newdlg';
            dlg.innerHTML = '<div class="box"><b>New mission</b><p>A mission is one JSON file. Edit this template (its <code>file</code> names must match .nc files you have added), or copy the AI prompt, let an AI write it from your data, and paste the result back here.</p>'
                + '<textarea spellcheck="false"></textarea><div class="btns"><span class="msg"></span><button data-a="prompt">Copy AI prompt</button><button data-a="cancel">Cancel</button><button data-a="save" class="primary">Save mission</button></div></div>';
            const ta = dlg.querySelector('textarea'), m2 = dlg.querySelector('.msg'); ta.value = tpl;
            dlg.addEventListener('click', e => { if (e.target === dlg || e.target.dataset.a === 'cancel') dlg.remove(); });
            dlg.querySelector('[data-a=prompt]').onclick = async () => {
                const guide = await fetch('/api/missions/guide').then(r => r.text());
                const text = 'Write a Glider Playground mission JSON for my deployment. Ask me for the data file names, their date ranges and the story of the mission first.\n\n--- FIELD GUIDE ---\n' + guide + '\n\n--- TEMPLATE ---\n' + tpl;
                navigator.clipboard.writeText(text).then(() => { m2.textContent = 'Prompt copied'; }, () => { m2.textContent = 'Copy blocked by the browser'; });
            };
            dlg.querySelector('[data-a=save]').onclick = () => {
                let j; try { j = JSON.parse(ta.value); } catch (e) { m2.textContent = 'Not valid JSON: ' + e.message; return; }
                const id = String(j.id || j.title || 'mission');
                importMission(new File([ta.value], id + '.json', { type: 'application/json' }), m2).then(() => { if (!m2.textContent.startsWith('Import failed')) dlg.remove(); });
            };
            document.body.appendChild(dlg);
        };
    }
    function showPicker() {
        $('status').classList.add('hidden'); document.body.classList.add('picking');
        const el = $('picker'); el.classList.remove('hidden');
        // A different little fleet each visit: one ship on the surface, a few vehicles below, a float bobbing.
        const pick = (arr, n) => arr.slice().sort(() => Math.random() - 0.5).slice(0, n), rnd = (a, b) => a + Math.random() * (b - a);
        const swimmer = (n, w, top) => { const dur = rnd(26, 44); return `<img src="/static/icons/${n}-mapicon.svg" alt="" style="width:${w}px;top:${top}px;left:0;animation-duration:${dur.toFixed(0)}s;animation-delay:-${rnd(0, dur).toFixed(0)}s">`; };
        const fleet = swimmer(pick(['rrs-discovery', 'rrs-james-cook', 'ship'], 1)[0], 130, 14)
            + pick(['alr', 'slocum', 'seaglider'], 2 + Math.round(Math.random())).map((n, i) => swimmer(n, n === 'alr' ? 84 : 66, 70 + i * 34)).join('')
            + `<img src="/static/icons/argo-mapicon.svg" alt="" style="width:13px;top:${rnd(52, 78).toFixed(0)}px;left:${rnd(15, 80).toFixed(0)}%;animation:bob 5s ease-in-out infinite">`;
        const hero = `<div class="hero"><div class="fleet">${fleet}</div><h2>Missions</h2><p>Whole missions in one scene: Press play and watch it unfold, or click a platform to open its data.</p>`
            + '<svg class="wave" viewBox="0 0 1200 46" preserveAspectRatio="none"><path d="M0 30 Q 75 10 150 30 T 300 30 T 450 30 T 600 30 T 750 30 T 900 30 T 1050 30 T 1200 30 V46 H0Z" fill="#fff"/></svg></div>';
        const dates = t => { if (!t || !t.start) return ''; const f = d => new Date(parseUTC(d)).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }); return `${f(t.start)} – ${f(t.end)}`; };
        getJSON('/api/missions').then(({ missions, local }) => {
            const EXPORTS = m => !local ? '' : `<span class="exports"><a class="xbtn" href="/api/missions/${encodeURIComponent(m.id)}/export" download title="Just the mission JSON: whoever opens it needs the data files themselves"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>Export mission</a>`
                    + (m.platforms_available ? `<a class="xbtn" href="/api/missions/${encodeURIComponent(m.id)}/export?data=1" download title="One .zip with the mission and its full data files, ready to import elsewhere"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>Export mission + data</a>` : '') + `</span>`;
            el.innerHTML = `<div class="wrap">${hero}` + (missions.length ? '' : '<p>No missions yet.</p>')
                + missions.map(m => `<div class="mcard" role="button" tabindex="0" data-id="${esc(m.id)}"><div class="thumb"></div><div class="body"><b>${esc(m.title)}</b><span class="when"></span>`
                    + `<span class="sum">${esc(m.summary)}</span><div class="chips"><span class="chip ${m.platforms_available < m.platforms ? 'part' : ''}">${m.platforms_available} / ${m.platforms} data files</span></div>` + EXPORTS(m) + `</div></div>`).join('')
                + '<div class="hint">Missions are JSON files; a bundle (.zip) carries one together with its data. See missions/README.md.'
                + (local ? '' : ' To add one to this server, copy its bundle into the missions inbox.') + '</div></div>';
            const tools = document.createElement('div'); tools.className = 'tools';
            tools.innerHTML = local ? '<button data-act="new">New mission</button><button data-act="import">Import mission (.zip / .json)</button><input type="file" accept=".zip,.json" hidden><span class="msg"></span>' : '';
            el.querySelector('.hero').insertAdjacentElement('afterend', tools);
            if (local) wireTools(tools);
            const open = c => { if (embedded) window.parent.postMessage({ type: 'missionOpen', id: c.dataset.id }, '*'); else location.href = '/missions/static/mission_view.html?id=' + encodeURIComponent(c.dataset.id); };
            el.querySelectorAll('.mcard').forEach(c => {
                c.addEventListener('click', e => { if (!e.target.closest('.exports')) open(c); }); c.addEventListener('keydown', e => { if (e.target === c && (e.key === 'Enter' || e.key === ' ')) open(c); });
                getJSON(`/api/missions/${encodeURIComponent(c.dataset.id)}/preview`).then(pv => {
                    drawThumb(c.querySelector('.thumb'), pv);
                    c.querySelector('.when').textContent = dates(pv.time);
                    const chips = Object.entries(pv.kinds || {}).map(([k, n]) => `<span class="chip"><img src="${iconFor(k)}" alt="">${n} × ${esc(KIND_NAMES[k] || k)}</span>`);
                    if (pv.floats) chips.push(`<span class="chip"><img src="${iconFor('argo')}" alt="">Argo floats</span>`);
                    (pv.ships || []).forEach(sh => chips.push(`<span class="chip"><img src="${iconFor(sh.model)}" alt="">${esc(sh.label)}</span>`));
                    c.querySelector('.chips').insertAdjacentHTML('afterbegin', chips.join(''));
                }).catch(() => { c.querySelector('.thumb').classList.add('empty'); });
            });
        }).catch(e => { el.textContent = 'Could not list missions: ' + e.message; });
    }

    if (!missionId) { showPicker(); return; }      // after the list's constants above: they are `const`, not hoisted

    const mergeParts = SK.mergeParts, loadModel = name => SK.loadModel(name).catch(() => null);

    // ── Timed paths: {t, lon, lat, z} with ascending t; NaN lon = a gap in the drawn line ──
    const lowerBound = (arr, v) => { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < v) lo = m + 1; else hi = m; } return lo; };
    function sampleAt(P, t) {
        const n = P.t.length;
        if (!n || t < P.t[0]) return null;
        if (t >= P.t[n - 1]) return { lon: P.lon[n - 1], lat: P.lat[n - 1], z: P.z[n - 1], i: n - 1, ended: true };
        const i = Math.max(1, lowerBound(P.t, t)), f = (t - P.t[i - 1]) / Math.max(P.t[i] - P.t[i - 1], 1);
        const mix = a => (isNaN(a[i - 1]) || isNaN(a[i])) ? a[i - 1] : a[i - 1] + (a[i] - a[i - 1]) * f;
        return { lon: mix(P.lon), lat: mix(P.lat), z: mix(P.z), i: i - 1 };
    }
    // Windowed mean of a track on a regular grid: what the models ride.
    const shipSmoothH = 9, shipTurnH = 10;      // ship pose: ± hours averaged, and hours of mission time to swing onto a new heading
    const shipRollRad = 0.03, shipRollS = 9, shipPitchRad = 0.01, shipPitchS = 13;      // sway: amplitude (rad) and wall-clock period (s)
    // Even time steps along a path. smoothPath averages samples, so sparse points (ship legs) must be filled in first
    // or the mean only moves when a point enters or leaves the window.
    // Times a track turns between diving and climbing (hysteresis ignores depth-holding wobble): gives the local dive period.
    function diveTurns(P, minM = 15) {
        const out = []; let dir = 0, ext = NaN, extT = 0;
        for (let q = 0; q < P.t.length; q++) {
            const z = P.z[q]; if (isNaN(z)) continue;
            if (isNaN(ext)) { ext = z; extT = P.t[q]; continue; }
            if (dir >= 0 && z > ext || dir <= 0 && z < ext) { if (dir) { ext = z; extT = P.t[q]; } else if (Math.abs(z - ext) >= minM) { dir = z > ext ? 1 : -1; ext = z; extT = P.t[q]; } }
            else if (dir && Math.abs(z - ext) >= minM) { out.push(extT); dir = -dir; ext = z; extT = P.t[q]; }
        }
        return out;
    }
    function resample(P, stepH, extra) {
        const out = { t: [], lon: [], lat: [], z: [] }, n = P.t.length, step = stepH * 3600e3;
        for (let t = P.t[0]; n; t += step) {
            const tt = Math.min(t, P.t[n - 1]), p = sampleAt(P, tt);
            out.t.push(tt); out.lon.push(p.lon); out.lat.push(p.lat); out.z.push(p.z);
            if (tt === P.t[n - 1]) break;
        }
        for (const e of extra || []) {       // keep these instants as samples of their own
            const q = lowerBound(out.t, e), p = sampleAt(P, e);
            if (p && out.t[q] !== e) { out.t.splice(q, 0, e); out.lon.splice(q, 0, p.lon); out.lat.splice(q, 0, p.lat); out.z.splice(q, 0, p.z); }
        }
        return out;
    }
    // `pins`: times the smoothed path passes through exactly (the window shrinks to nothing towards each).
    function smoothPath(P, stepH, halfH, pins) {
        const out = { t: [], lon: [], lat: [], z: [] }, n = P.t.length;
        if (!n) return out;
        const step = stepH * 3600e3, fullHalf = halfH * 3600e3, inside = (pins || []).filter(v => v > P.t[0] && v < P.t[n - 1]);
        const times = [];
        for (let t = P.t[0]; ; t += step) { times.push(Math.min(t, P.t[n - 1])); if (t >= P.t[n - 1]) break; }
        const grid = [...new Set(times.concat(inside))].sort((x, y) => x - y);
        for (const tt of grid) {
            const half = inside.reduce((h, pt) => Math.min(h, Math.abs(tt - pt)), fullHalf);
            const a = lowerBound(P.t, tt - half), b = Math.max(a + 1, lowerBound(P.t, tt + half + 1));
            let sx = 0, sy = 0, sz = 0, c = 0;
            for (let q = a; q < b; q++) { if (isNaN(P.lon[q])) continue; sx += P.lon[q]; sy += P.lat[q]; sz += P.z[q]; c++; }
            if (c) { out.t.push(tt); out.lon.push(sx / c); out.lat.push(sy / c); out.z.push(sz / c); }
        }
        // Pin both ends to the real first / last fix so launch and recovery sit where they happened.
        const first = P.lon.findIndex(v => !isNaN(v)); let last = n - 1; while (last > 0 && isNaN(P.lon[last])) last--;
        if (out.t.length) { out.lon[0] = P.lon[first]; out.lat[0] = P.lat[first]; const e = out.t.length - 1; out.lon[e] = P.lon[last]; out.lat[e] = P.lat[last]; }
        return out;
    }
    // Synthetic Argo depth between known surfacings (SceneKit's standard cycle, as in 3d_view.html).
    function argoDepth(P, t) {
        const H = 3600e3, n = P.t.length, i = Math.max(0, lowerBound(P.t, t + 1) - 1), since = (t - P.t[i]) / H;
        return i >= n - 1 ? SK.argoParkDepth(since) : SK.argoCycleDepth(since, (P.t[i + 1] - P.t[i]) / H);
    }
    const catmull = (pts, n = 14) => {
        const p = [pts[0], ...pts, pts[pts.length - 1]], out = [];
        for (let i = 1; i < p.length - 2; i++) for (let j = 0; j < n; j++) {
            const t = j / n, [a, b, c, d] = [p[i - 1], p[i], p[i + 1], p[i + 2]];
            out.push([0, 1].map(k => 0.5 * (2 * b[k] + (c[k] - a[k]) * t + (2 * a[k] - 5 * b[k] + 4 * c[k] - d[k]) * t * t + (3 * b[k] - 3 * c[k] + d[k] - a[k]) * t * t * t)));
        }
        out.push(pts[pts.length - 1]);
        return out;
    };

    // ── Load everything ──
    // Loading card: mission name, a thin bar that fills as each piece arrives, and what is being fetched. No spinner.
    const status = (step, frac) => {
        const el = $('status');
        if (!step) { el.classList.add('hidden'); return; }
        el.querySelector('.step').textContent = step;
        if (frac != null) el.querySelector('.fill').style.width = Math.round(frac * 100) + '%';
    };
    const fail = msg => { $('status').classList.remove('hidden'); $('status').classList.add('error'); $('status').querySelector('.step').textContent = msg; };
    let mission, scene, tracks = {};
    status('Fetching the seabed', 0.12);
    const slowNote = setTimeout(() => { if (!scene) status('Fetching the seabed · first time only, it is cached afterwards'); }, 5000);
    const missionJSON = getJSON(`/api/missions/${encodeURIComponent(missionId)}`);
    missionJSON.then(m => { $('status').querySelector('.name').textContent = String(m.title || '').replace(/\s+/g, ' '); }).catch(() => {});
    Promise.all([missionJSON, getJSON(`/api/missions/${encodeURIComponent(missionId)}/scene`)])
        .then(([m, s]) => {
            mission = m; scene = s; clearTimeout(slowNote);
            let done = 0; const total = m.platforms.length;
            status(`Loading tracks · 0 of ${total}`, 0.35);
            document.title = `${String(m.title || m.id).replace(/\s+/g, ' ')} · Glider Playground`;
            $('title').textContent = m.title || m.id;
            const kinds = new Set(['argo', ...m.platforms.map(p => p.kind), ...(m.ships || []).map(sh => sh.model || 'rrs_discovery')]);
            return Promise.all([
                Promise.all(m.platforms.map(p => getJSON(`/api/missions/${encodeURIComponent(missionId)}/track/${encodeURIComponent(p.key)}`).catch(() => ({ status: 'error' }))
                    .then(r => { done++; status(`Loading tracks · ${done} of ${total}`, 0.35 + 0.45 * done / Math.max(total, 1)); return r; }))),
                Promise.all([...kinds].map(loadModel)),
                Scenery.load(mergeParts).catch(() => null),
            ]);
        })
        .then(([trackList]) => {
            mission.platforms.forEach((p, i) => { tracks[p.key] = trackList[i]; });
            status('Building the scene', 0.9);
            return new Promise(r => setTimeout(r, 30)).then(start);      // let the bar paint before the heavy meshing
        })
        .catch(e => fail('Could not load this mission: ' + e.message));

    async function start() {
        const T0 = scene.time_ms[0], T1 = scene.time_ms[1];
        const plotDiv = $('plot3d');

        // ── Scene box: SceneKit's seabed, coastline and water, with a mission-sized exaggeration ──
        const grid = SK.upsampleGrid(scene.bathy_lon, scene.bathy_lat, scene.bathy_z, SK.LOOK.bathyUpsampleTo);
        const bLon = grid.lon, bLat = grid.lat, xLen = bLon.length, yLen = bLat.length;
        const lon0 = bLon[0], lon1 = bLon[xLen - 1], lat0 = bLat[0], lat1 = bLat[yLen - 1];
        const fp = SK.footprint(lon0, lon1, lat0, lat1), cosLat = fp.cosLat, maxHoriz = fp.maxHoriz;
        const trueMinZ = Math.min(...scene.bathy_z.flat(), -10);
        const baseZ = trueMinZ - Math.abs(trueMinZ) * 0.15;
        const vExag = Math.max(1, Math.min(100, mission.vertical_exaggeration || boxAspectTarget * maxHoriz / Math.abs(baseZ)));
        $('scaleX').textContent = vExag > 1.05 ? `Vertical scale ×${vExag >= 10 ? Math.round(vExag) : vExag.toFixed(1)} · ` : ''; $('scaleNote').classList.remove('hidden');
        const seabed = SK.buildSeabed(grid, { landScale: SK.landScale(vExag), coastStep: Math.abs(trueMinZ) * SK.LOOK.coastStepFrac, baseZ, basemap: await SK.loadCoast(grid) });
        const bathyZ = seabed.bathyZ;
        const zTop0 = bathyZ.flat().reduce((a, b) => Math.max(a, b), 0);
        const box = SK.sceneBox(fp, lon0, lon1, lat0, lat1, baseZ, vExag, zTop0, Math.max(SIZES.ship * 0.6, Scenery.maxHeight()));
        const S = box.scale;      // data units per scene unit
        const hidePoint = [lon0, lat0, baseZ];
        const surfaceLift = 0.0025 * S.kz;     // surface lines sit a hair above the water plane so it doesn't swallow them

        const traces = [...seabed.traces(), SK.waterTrace(bLon, bLat, bathyZ, baseZ)];
        const snowAt = SK.snowAt;
        let sceneryIdx = -1;             // the moving part: what the sway patches
        const sceneryAll = [];
        {
            const sc = Scenery.build({ bathy_lon: bLon, bathy_lat: bLat }, bathyZ, S, 'mission:' + missionId, snowAt, baseZ);
            for (const part of sc ? [sc.still, sc.moving] : []) { if (!part) continue; if (part === sc.moving) sceneryIdx = traces.length; sceneryAll.push(traces.length); traces.push(part); }
        }

        // ── Things that move: every entry has a timed path, a drawn line (faint whole + bright travelled) and a model ──
        const movers = [];
        const modelScale = +mission.model_scale || 1;
        const addMover = (o) => {
            const line = { mode: 'lines', type: 'scatter3d', hoverinfo: 'skip', x: o.path.lon.map(v => isNaN(v) ? null : v), y: o.path.lat.map(v => isNaN(v) ? null : v),
                z: o.path.z.map((v, q) => isNaN(o.path.lon[q]) ? null : v) };
            // Still to come: the same line, thinner (same colour, variable colouring included).
            o.aheadIdx = traces.length; traces.push({ ...line, line: { color: o.colour, width: Math.max(1, o.width * 0.45), dash: o.dash } });
            o.pastIdx = traces.length; traces.push({ ...line, line: { color: o.colour, width: o.width, dash: o.dash } });
            // Tip: from where the travelled line was last drawn to where the vehicle is now.
            o.tipIdx = traces.length; traces.push({ ...line, x: [null, null], y: [null, null], z: [null, null], line: { color: o.colour, width: o.width, dash: o.dash } });
            o.full = line;
            o.size *= modelScale;
            if (o.model) {
                const M = o.model;
                o.modelIdx = traces.length;
                traces.push(SK.modelTrace(M, SK.collapsed(M.x.length, hidePoint), MODEL_LIGHT));
            }
            movers.push(o);
        };

        for (const p of mission.platforms) {
            const tr = tracks[p.key];
            if (!tr || tr.status !== 'ready') continue;
            const path = { t: [], lon: [], lat: [], z: [], src: [], pitch: tr.pitch ? [] : null };      // src: index into the served track (colour values use it)
            for (let q = 0; q < tr.time_ms.length; q++) {
                if (tr.lon[q] == null || tr.lat[q] == null) continue;
                path.src.push(q); path.t.push(tr.time_ms[q]); path.lon.push(tr.lon[q]); path.lat.push(tr.lat[q]); path.z.push(Math.min(0, tr.z[q] == null ? 0 : tr.z[q]));
                if (path.pitch) path.pitch.push(tr.pitch[q]);
            }
            if (path.t.length < 2) continue;
            addMover({ key: p.key, label: p.label || p.key, fileId: tr.file_id, colour: p.colour || '#d9c45c', width: p.width || 2.4, path, turns: diveTurns(path), follow: p.kind === 'alr', pose: p.kind === 'alr' ? smoothPath(path, 0.5, alrWindowH) : smoothPath(path, 3, poseWindowH),
                model: await loadModel(p.kind), size: p.size || SIZES[p.kind] || SIZES.slocum, showLabel: !!p.show_label, kindName: p.kind });
        }
        const byKey = Object.fromEntries(movers.map(o => [o.key, o]));

        for (const f of scene.floats || []) {
            if (!f.wmo || f.time_ms.length < 1) continue;
            const path = { t: f.time_ms, lon: f.lon, lat: f.lat, z: f.lat.map(() => surfaceLift) };
            addMover({ key: 'float' + f.wmo, label: 'Argo ' + f.wmo, wmo: f.wmo, colour: (mission.floats || {}).colour || '#0f8b8d', width: 3, dash: 'dot', path, pose: path,
                model: await loadModel('argo'), size: SIZES.argo, upright: true, quiet: true });
        }

        for (const sh of mission.ships || []) {
            const path = { t: [], lon: [], lat: [], z: [] };
            const push = (t, lat, lon) => { path.t.push(t); path.lat.push(lat); path.lon.push(lon); path.z.push(surfaceLift); };
            sh._legLabels = [];
            const legEnds = [];      // the ship is exactly where a leg starts and ends (deployments, recoveries)
            for (const leg of sh.legs || []) {
                const [a, b] = leg.time.map(parseUTC);
                let pts;
                if (leg.follow) {
                    const who = leg.follow.map(k => byKey[k]).filter(Boolean), dLat = -(leg.offset_km || 0) / 111.2;
                    pts = [];
                    for (let t = a; t <= b; t += 24 * 3600e3) {
                        const ps = who.map(o => sampleAt(o.pose, t)).filter(Boolean);
                        if (ps.length) pts.push([t, ps.reduce((s, v) => s + v.lat, 0) / ps.length + dLat, ps.reduce((s, v) => s + v.lon, 0) / ps.length]);
                    }
                    if (pts.length < 2) continue;
                } else {
                    const c = catmull(leg.points), len = [0];
                    for (let q = 1; q < c.length; q++) len.push(len[q - 1] + Math.hypot((c[q][1] - c[q - 1][1]) * cosLat, c[q][0] - c[q - 1][0]));
                    pts = c.map((v, q) => [a + (b - a) * len[q] / Math.max(len[len.length - 1], 1e-9), v[0], v[1]]);
                }
                if (path.t.length) push(Math.max(path.t[path.t.length - 1], pts[0][0] - 1), NaN, NaN);   // gap: don't join legs with a line
                pts.forEach(v => push(v[0], v[1], v[2]));
                legEnds.push(pts[0][0], pts[pts.length - 1][0]);
                if (leg.label) { const mid = pts[Math.floor(pts.length / 2)]; sh._legLabels.push({ text: leg.label, lat: mid[1], lon: mid[2], time: a, colour: sh.colour }); }
            }
            if (path.t.length < 2) continue;
            // The model holds station across gaps: a pose path without the NaN separators.
            const pose = { t: [], lon: [], lat: [], z: [] };
            path.t.forEach((t, q) => { if (!isNaN(path.lon[q])) { pose.t.push(t); pose.lon.push(path.lon[q]); pose.lat.push(path.lat[q]); pose.z.push(0); } });
            // Legs are schematic, so the model rides a windowed mean of them: no corner kinks, no sidestep between legs.
            const rawPose = pose;
            addMover({ key: sh.key || 'ship', label: sh.label || 'Ship', colour: sh.colour || '#e8702a', width: 5, path, pose: smoothPath(resample(rawPose, 1, legEnds), 1, shipSmoothH, legEnds), holdHeading: true,
                model: await loadModel(sh.model || 'rrs_discovery'), size: sh.size || SIZES.ship, isShip: true, legLabels: sh._legLabels,
                // A last leg that ends at the edge of the box sails out of the scene instead of parking there.
                leavesAt: (() => {
                    const e = pose.t.length - 1, edge = Math.min(pose.lon[e] - lon0, lon1 - pose.lon[e], (pose.lat[e] - lat0) / cosLat, (lat1 - pose.lat[e]) / cosLat);
                    return (sh.leaves_scene != null ? sh.leaves_scene : edge < 0.8) ? pose.t[e] : Infinity;
                })() });
            // Deck cargo: platforms ride the aft deck until their first fix (`deploys`) or from their last one (`recovers`).
            const ship = movers[movers.length - 1], used = { glider: 0, float: 0, alr: 0 };
            const resolve = k => (k === 'floats' ? movers.filter(o => o.wmo) : [byKey[k]].filter(Boolean));
            for (const [list, mode] of [[sh.deploys, 'deploy'], [sh.recovers, 'recover']]) for (const k of list || []) for (const o of resolve(k)) {
                const kind = o.wmo ? 'float' : o.kindName === 'alr' ? 'alr' : 'glider';
                if (!o.carry) { const slot = (DECK_SLOTS[kind] || [])[used[kind]++]; if (!slot) continue; o.carry = { ship, slot, len: DECK_LEN[kind] }; }
                o.carry[mode] = true;
            }
        }

        const cell = (lat, lon) => SK.gridAt(bLon, bLat, bathyZ, lon, lat);
        const seabedAt = (lat, lon) => Math.max(0, -cell(lat, lon));      // m, positive down
        const stationIdx = [];       // per station: its trace indices (buoy, circle), for the legend's show/hide
        for (const st of mission.stations || []) {
            const mine = []; stationIdx.push(mine);
            const B = ['green', 'red', 'blue'].includes(st.buoy) ? await loadModel('buoy_' + st.buoy) : null;
            if (B) {             // a buoy marks the station's centre
                const k = SIZES.buoy * modelScale / B.height;
                mine.push(traces.length);
                traces.push(SK.modelTrace(B, { x: B.x.map(v => st.lon + v * k * S.kx), y: B.y.map(v => st.lat + v * k * S.ky), z: B.z.map(v => v * k * S.kz) }, MODEL_LIGHT));
            }
            const n = 48, rLat = (st.radius_km || 50) / 111.2, rLon = rLat / cosLat;
            mine.push(traces.length);
            traces.push({ type: 'scatter3d', mode: 'lines', hoverinfo: 'skip', x: Array.from({ length: n + 1 }, (_, q) => st.lon + rLon * Math.cos(q / n * 2 * Math.PI)),
                y: Array.from({ length: n + 1 }, (_, q) => st.lat + rLat * Math.sin(q / n * 2 * Math.PI)), z: new Array(n + 1).fill(surfaceLift), line: { color: st.colour || '#0f8b8d', width: 5, dash: 'dash' } });
        }

        // ── Plot ──
        const cam = mission.camera || {};
        const v3 = (a, d) => (Array.isArray(a) && a.length === 3 ? { x: a[0], y: a[1], z: a[2] } : d);
        await Plotly.newPlot(plotDiv, traces, SK.layout(box, { eye: v3(cam.eye, { x: -0.1, y: -1.15, z: 1.0 }), center: v3(cam.center, { x: 0, y: 0.05, z: -0.2 }), up: { x: 0, y: 0, z: 1 } }, { dragmode: 'turntable' }),
            { displayModeBar: false, responsive: true, scrollZoom: true });
        status('');

        // Trackpad: a sideways two-finger swipe would spin the whole scene (gl-plot3d rotates on horizontal wheel).
        // Only vertical scroll (zoom) gets through, and zoom stops at sensible distances.
        const eyeDist = () => { const c = plotDiv._fullLayout.scene.camera, e = c.eye, o = c.center; return Math.hypot(e.x - o.x, e.y - o.y, e.z - o.z); };
        plotDiv.addEventListener('wheel', e => {
            const sideways = Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey;
            let capped = false;
            try { const d = eyeDist(); capped = (e.deltaY > 0 && d >= 3.2) || (e.deltaY < 0 && d <= 0.3); } catch (_) {}
            if (sideways || capped) { e.preventDefault(); e.stopPropagation(); }
        }, { capture: true, passive: false });

        // Fast trace updates straight into the WebGL wrappers, tight clip planes and no pick pass: all SceneKit, as in 3d_view.html.
        const glScene = () => SK.glScene(plotDiv);
        const noPicking = () => SK.tuneGl(plotDiv);
        noPicking();
        // A fallback restyle redraws from the stored camera, which a drag only writes on release: skip it mid-drag
        // (the next frame patches again) and hand it the live camera otherwise.
        const fastPatch = (ti, patch) => SK.fastPatch(plotDiv, ti, patch, () => {
            if (rotating) return false;
            try { plotDiv.layout.scene.camera = glScene().getCamera(); } catch (_) {}
        });
        async function setTracesVisible(idx, on) {
            if (!idx.length) return;
            try { plotDiv.layout.scene.camera = glScene().getCamera(); } catch (_) {}
            await Plotly.restyle(plotDiv, { visible: on }, idx);
            noPicking();             // the redraw made fresh WebGL objects
        }
        // Legend show/hide. Hidden movers are not placed at all (a fast patch has no WebGL object to write to).
        async function setHidden(who, hide) {
            who = who.filter(o => !!o.hidden !== hide);
            if (!who.length) return;
            who.forEach(o => { o.hidden = hide; o._upto = null; o._shown = null; });
            const idx = who.flatMap(o => [o.aheadIdx, o.pastIdx, o.tipIdx, o.modelIdx]).filter(v => v != null);
            await setTracesVisible(idx, !hide);
            setTime(now);
        }
        let rotating = false;      // pointer down on the plot
        plotDiv.addEventListener('pointerdown', () => { rotating = true; }, true);
        ['pointerup', 'pointercancel', 'blur'].forEach(ev => window.addEventListener(ev, () => { rotating = false; }, true));

        // ── Pose + travelled line at mission time t ──
        function headingAt(o, t) {
            const rate = diveWindowMs() / 0.15, span = o.follow ? Math.min(8 * 3600e3, Math.max(1.5 * 3600e3, rate * 0.3)) : 8 * 3600e3, a = sampleAt(o.pose, Math.max(o.pose.t[0], t - span)), b = sampleAt(o.pose, Math.min(o.pose.t[o.pose.t.length - 1], t + span));
            if (!a || !b) return o._h || 0;
            const dx = (b.lon - a.lon) / S.kx, dy = (b.lat - a.lat) / S.ky;
            const moving = Math.hypot(dx, dy) > (o.follow ? 0.0006 : 0.004);             // hold the last heading while loitering
            const turnH = o.isShip ? shipTurnH : o.follow ? Math.max(alrTurnH, rate * 0.5 / 3600e3) : 0;      // ALRs: never quicker than ~half a second of wall clock
            if (o._h == null || (moving && !turnH)) o._h = Math.atan2(dy, dx);
            else if (moving) {       // ships and ALRs turn, never snap: ease towards the new heading (a scrub jump goes straight there)
                const want = Math.atan2(dy, dx), d = ((want - o._h + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
                const near = o._ht != null && Math.abs(t - o._ht) < 6 * 3600e3;
                o._h += near ? d * Math.min(1, Math.abs(t - o._ht) / (turnH * 3600e3)) : d;
            }
            o._ht = t;
            return o._h;
        }
        // Real dive depth for the model. While playing, depth is averaged over what flies past in ~0.3 s of wall clock,
        // so Slow shows every yo-yo and the faster speeds settle towards mid-water instead of strobing. Paused = exact.
        const diveWindowMs = () => (playing ? (T1 - T0) / (+$('speedSel').value || 90) * 0.15 : 0);
        function depthAt(P, t, half) {
            // Always through the time-interpolated track: files holding only downcasts (or only upcasts) have hours with
            // no samples, and the vehicle must glide across them rather than snap to the next point.
            if (half < 5 * 60e3) { const p = sampleAt(P, t); return p ? p.z : 0; }
            let sum = 0, c = 0;
            for (let q = -3; q <= 3; q++) { const p = sampleAt(P, t + half * q / 3); if (p && !isNaN(p.z)) { sum += p.z; c++; } }
            return c ? sum / c : 0;
        }
        // Measured pitch (rad, nose up +) averaged over the same window as the depth; null where the file has none.
        function pitchAt(P, t, half) {
            if (!P.pitch) return null;
            const a = Math.max(0, lowerBound(P.t, t - half) - 1), b = Math.min(P.t.length, lowerBound(P.t, t + half) + 1);
            let sum = 0, c = 0;
            for (let q = a; q < b; q++) if (P.pitch[q] != null) { sum += P.pitch[q]; c++; }
            return c ? sum / c * Math.PI / 180 : null;
        }
        // True when the pose differs visibly from the last one patched (positions in scene units, angles in rad).
        const POSE_TOL = [2e-4, 2e-4, 2e-4, 0.004, 0.004, 0.004];
        function poseChanged(o, key) {
            const k = o._key;
            if (o._shown && k && key.every((v, q) => Math.abs(v - k[q]) < POSE_TOL[q])) return false;
            o._key = key; return true;
        }
        // Gliders / ALRs: smooth horizontal pose + a dive you can actually follow. Sets p.z, returns the pitch (rad).
        function divePose(o, p, t) {
            // Gliders: what the data is doing decides the animation: where real dive cycles would flash past
            // faster than the eye can follow, glide slowly between the depths really being worked; otherwise the real track.
            const half = diveWindowMs(), rate = half / 0.15;      // rate: mission ms per wall second (0 when paused)
            const win = Math.max(rate * DIVE_SECONDS, 12 * 3600e3);
            const a = lowerBound(o.turns, t - win), b = lowerBound(o.turns, t + win);
            const cycleS = rate && b - a >= 2 ? 2 * (o.turns[b - 1] - o.turns[a]) / (b - a - 1) / rate : Infinity;      // wall seconds per real dive cycle here
            const k = Math.max(0, Math.min(1, (DIVE_SECONDS * 1.5 - cycleS) / DIVE_SECONDS)), i = lowerBound(o.turns, t);
            let synth = o.follow ? 0 : k * k * (3 - 2 * k);   // 0 = real … 1 = slow glide. ALRs (propelled) always stay on their real track
            if (synth > 0) {     // holding a depth (a flat run, a surface drift) is not a dive cycle: the gap between turns around `t` dwarfs its neighbours'
                const gaps = []; for (let q = a + 1; q < b; q++) gaps.push(o.turns[q] - o.turns[q - 1]);
                gaps.sort((x, y) => x - y);
                const g = i > 0 && i < o.turns.length ? o.turns[i] - o.turns[i - 1] : Infinity, f = Math.max(0, Math.min(1, (g / gaps[gaps.length >> 1] - 2) / 2));
                synth *= 1 - f * f * (3 - 2 * f);
            }
            {                    // ease between real and glide (wall clock) so a hold starting or ending never snaps the depth
                const w = performance.now(), dtw = (w - (o._sw || 0)) / 1000; o._sw = w;
                synth = o._s = playing && o._s != null && dtw < 0.5 ? o._s + (synth - o._s) * (1 - Math.exp(-dtw / 1.2)) : synth;
            }
            let zr = 0, pr = 0, zs = 0, ps = 0;
            if (synth < 1) {
                const dt = Math.max(half, 12 * 60e3);
                zr = depthAt(o.path, t, half);
                const measured = pitchAt(o.path, t, half);      // the vehicle's own pitch sensor, as the 3D view uses; else the slope of the dive
                pr = measured != null ? measured : 0.45 * Math.tanh((depthAt(o.path, t + dt, half) - depthAt(o.path, t - dt, half)) / 60);     // ±26° at most
            }
            if (synth > 0) {
                const period = rate * DIVE_SECONDS;
                const i0 = lowerBound(o.path.t, t - win), i1 = Math.max(i0 + 1, lowerBound(o.path.t, t + win));
                let lo = 0, hi = -Infinity;      // z is negative down: hi = shallowest, lo = deepest
                for (let q = i0; q < i1 && q < o.path.t.length; q++) { const z = o.path.z[q]; if (isNaN(z)) continue; if (z < lo) lo = z; if (z > hi) hi = z; }
                if (hi === -Infinity) hi = 0;
                const ph = ((t / period + (o._phase || (o._phase = Math.random()))) % 1 + 1) % 1, tri = ph < 0.5 ? ph * 2 : 2 - ph * 2;   // 0 → 1 → 0
                zs = hi + (lo - hi) * (1 - Math.cos(Math.PI * tri)) / 2;      // eased at the top and bottom, so the nose swings round
                ps = -(hi - lo > 40 ? 0.42 : 0) * Math.sin(2 * Math.PI * ph);
            }
            p.z = zr + (zs - zr) * synth;
            return pr + (ps - pr) * synth;
        }
        // Model vertices → data coordinates: scaled by k, pitched and rolled about the body's centre (zm), shifted by `off`
        // (scene units, body frame), turned onto heading h and placed at c (scene units).
        function transformModel(M, k, zm, pitch, roll, off, h, c) {
            const n = M.x.length, xs = new Array(n), ys = new Array(n), zs = new Array(n);
            const cp = Math.cos(pitch), sp = Math.sin(pitch), cr = Math.cos(roll), sr = Math.sin(roll), ch = Math.cos(h), sh = Math.sin(h);
            for (let q = 0; q < n; q++) {
                const x0 = M.x[q] * k, z0 = (M.z[q] - zm) * k, y0 = M.y[q] * k, z1 = x0 * sp + z0 * cp;
                const mx = x0 * cp - z0 * sp + off[0], my = y0 * cr - z1 * sr + off[1], mz = y0 * sr + z1 * cr + zm * k + off[2];
                xs[q] = (c[0] + mx * ch - my * sh) * S.kx; ys[q] = (c[1] + mx * sh + my * ch) * S.ky; zs[q] = (c[2] + mz) * S.kz;
            }
            return { x: xs, y: ys, z: zs };
        }
        function placeModel(o, t) {
            if (o.modelIdx == null || o.hidden) return;
            const M = o.model, n = M.x.length;
            if (o.carry) {      // on the ship's deck: drawn in the ship's frame, at deck scale
                const c = o.carry, P = o.path.t, aboard = (c.deploy && t < P[0]) || (c.recover && t >= P[P.length - 1]);
                if (aboard && t >= c.ship.leavesAt) { o.now = null; if (o._shown !== false) { o._shown = false; fastPatch(o.modelIdx, SK.collapsed(n, hidePoint)); } return; }
                const sp = aboard ? sampleAt(c.ship.pose, t) : null;
                if (sp) {
                    const SM = c.ship.model, L = SM.hullLen, ks = c.ship.size / SM.extent, h = headingAt(c.ship, t);
                    const kc = c.len * L / (o.upright ? M.height : M.length || M.extent), cx = sp.lon / S.kx, cy = sp.lat / S.ky;
                    o.now = null;       // no name tag while aboard
                    if (!poseChanged(o, [cx, cy, 0, h, -9, 0])) return;
                    o._shown = true;
                    fastPatch(o.modelIdx, transformModel(M, kc * ks, 0, 0, 0, [c.slot[0] * L * ks, c.slot[1] * L * ks, (SM.deckZ - M.zMin * kc) * ks], h, [cx, cy, 0]));
                    return;
                }
            }
            const p = t >= (o.leavesAt || Infinity) ? null : sampleAt(o.pose, t);
            let pitch = 0;
            if (p && o.fileId) pitch = divePose(o, p, t);
            if (p && o.wmo) {        // floats dive between surfacings, never through the seabed
                const clearance = o.size * 0.6 * S.kz;
                p.z = -Math.min(argoDepth(o.path, t), Math.max(0, seabedAt(p.lat, p.lon) - clearance));
            }
            o.now = p;
            if (!p) { if (o._shown !== false) { o._shown = false; fastPatch(o.modelIdx, SK.collapsed(n, hidePoint)); } return; }
            const h = o.upright ? 0 : headingAt(o, t), k = o.size / M.extent;
            const cx = p.lon / S.kx, cy = p.lat / S.ky, cz = o.isShip ? 0 : p.z / S.kz - (o.upright ? 0 : M.zMid * k);   // ship models have z = 0 at the waterline
            if (!o.isShip) {         // the nose eases onto a new pitch (wall clock); paused or scrubbed = exact
                const w = performance.now(), dtw = (w - (o._pw || 0)) / 1000; o._pw = w;
                o._p = playing && o._p != null && dtw < 0.5 ? o._p + (pitch - o._p) * (1 - Math.exp(-dtw / pitchTauS)) : pitch;
                pitch = o._p;
            }
            let roll = 0;
            if (o.isShip) { const w = performance.now() / 1000; roll = shipRollRad * Math.sin(2 * Math.PI * w / shipRollS); pitch = shipPitchRad * Math.sin(2 * Math.PI * w / shipPitchS + 1); }   // a gentle swell
            if (!poseChanged(o, [cx, cy, cz, h, pitch, roll])) return;
            const zm = o.upright || o.isShip ? 0 : M.zMid;
            const top = (M.zMax == null ? (M.zMax = Math.max(...M.z)) : M.zMax) * k;
            o._shown = true; o.anchor = [p.lon, p.lat, (cz + top) * S.kz];
            o.labelAnchor = o.isShip ? o.anchor : [p.lon, p.lat, Math.max(o.anchor[2], (top - zm * k) * S.kz)];   // tag rides at surface height, straight above the vehicle
            fastPatch(o.modelIdx, transformModel(M, k, zm, pitch, roll, [0, 0, 0], h, [cx, cy, cz]));
        }
        // The tip takes the colour at the line's end when coloured by a variable.
        function setTip(o, a, p, upto) {
            if (!a && !o._tip) return;
            o._tip = !!a;
            const patch = a ? { x: [a[0], p.lon], y: [a[1], p.lat], z: [a[2], p.z] } : { x: [null, null], y: [null, null], z: [null, null] };
            if (a && o.cvals) { const c = o.cvals[Math.min(o.cvals.length - 1, Math.max(0, upto - 1))]; patch.line = { ...plotDiv._fullData[o.tipIdx].line, color: [c, c] }; }
            fastPatch(o.tipIdx, patch);
        }
        function placeLine(o, t) {
            if (o.hidden) return;
            const P = o.path, p = sampleAt(P, t), upto = p ? p.i + 1 : 0;
            if (upto === o._upto && (!p || p.ended)) return;
            // Rebuilding a scatter3d line costs a few ms and thousands of array slots: while playing, LINE_MS apart is plenty.
            const w = performance.now(), live = p && !p.ended && !isNaN(p.lon);
            if (playing && upto !== 0 && !(p && p.ended) && o._upto != null && w - (o._lw || 0) < LINE_MS) { if (live && o._from) setTip(o, o._from, p, upto); return; }
            o._lw = w; o._upto = upto;
            o._from = live ? [p.lon, p.lat, p.z] : null; setTip(o, null);
            const x = o.full.x.slice(0, upto), y = o.full.y.slice(0, upto), z = o.full.z.slice(0, upto);
            if (p && !p.ended && !isNaN(p.lon)) { x.push(p.lon); y.push(p.lat); z.push(p.z); }
            if (!x.length) { x.push(null); y.push(null); z.push(null); }
            if (o.cvals) {       // coloured by a variable: the colour array is re-sliced with the line
                const c = o.cvals.slice(0, x.length); while (c.length < x.length) c.push(o.cvals[Math.min(o.cvals.length - 1, Math.max(0, upto - 1))]);
                fastPatch(o.pastIdx, { x, y, z, line: { ...plotDiv._fullData[o.pastIdx].line, color: c } });
                return;
            }
            fastPatch(o.pastIdx, { x, y, z });
        }

        // ── Labels: HTML pinned to 3D points through the scene's own camera matrices ──
        const mul = (m, v) => [0, 1, 2, 3].map(r => m[r] * v[0] + m[4 + r] * v[1] + m[8 + r] * v[2] + m[12 + r] * v[3]);
        function project(pt) {
            const sc = glScene(), cp = sc.glplot.cameraParams, ds = sc.dataScale;
            let v = mul(cp.model, [pt[0] * ds[0], pt[1] * ds[1], pt[2] * ds[2], 1]); v = mul(cp.view, v); v = mul(cp.projection, v);
            if (v[3] <= 0) return null;
            return [(0.5 + 0.5 * v[0] / v[3]) * plotDiv.clientWidth, (0.5 - 0.5 * v[1] / v[3]) * plotDiv.clientHeight];
        }
        const groundZ = (lat, lon) => Math.max(0, cell(lat, lon));
        const anchorOf = at => {
            if (at && at.platform) { const o = byKey[at.platform] || movers.find(m => m.key === at.platform); if (!o) return null; const p = sampleAt(o.path, parseUTC(at.time)); return p ? [p.lon, p.lat, p.z] : null; }
            return at && at.lat != null ? [at.lon, at.lat, groundZ(at.lat, at.lon) + surfaceLift] : null;
        };
        const labels = $('labels'), leaders = $('leaders'), items = [];
        const add = (html, anchor, o = {}) => {
            if (!anchor && !o.mover) return;
            const el = document.createElement('div'); el.innerHTML = html;
            const node = el.firstElementChild; labels.appendChild(node); items.push({ node, anchor, ...o });
            if (o.jump && o.time != null) { node.classList.add('jumpable'); node.title = 'Jump to this date'; node.addEventListener('click', () => jumpTo(o.time)); }
            return node;
        };
        let jumpTo = () => {};       // set once the time bar exists
        (mission.places || []).forEach(p => add(`<div class="place ${esc(p.style || '')}">${esc(p.text)}</div>`, anchorOf(p), { layer: 'places' }));
        (mission.stations || []).forEach((s, q) => add(`<div class="place station" style="color:${esc(s.colour || '#0f8b8d')}">${esc(s.label || '')}</div>`, [s.lon, s.lat + (s.radius_km || 50) / 111.2 * 1.25, surfaceLift], { layer: 'station:' + q }));
        movers.filter(o => o.legLabels).forEach(o => o.legLabels.forEach(l => add(`<div class="place" style="color:${esc(l.colour)};font-weight:700">${esc(l.text)}</div>`, [l.lon, l.lat, surfaceLift], { time: l.time, dy: -14, owner: o })));
        const stageIcon = `/static/icons/${encodeURIComponent(mission.stage_icon || 'alr')}-mapicon.svg`;
        (mission.stages || []).forEach(s => {
            const a = anchorOf(s.at); if (!a) return;
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line'); line.setAttribute('stroke', '#12295c'); line.setAttribute('stroke-width', '2'); leaders.appendChild(line);
            add('<div class="dot"></div>', a, { layer: 'stages', time: s.at.time ? parseUTC(s.at.time) : null });
            add(`<div class="pill"><span class="logo"><img src="${stageIcon}" alt=""></span><span><b>${esc(s.title)}</b><small>${esc(s.detail || '')}</small></span></div>`, a,
                { layer: 'stages', dx: (s.offset || [0, -60])[0], dy: (s.offset || [0, -60])[1], line, time: s.at.time ? parseUTC(s.at.time) : null, jump: true });
        });
        (mission.events || []).forEach((e, q) => add(`<span class="badge pin">${q + 1}</span>`, anchorOf(e.at), { layer: 'events', dx: (e.offset || [0, 0])[0], dy: (e.offset || [0, 0])[1], time: e.time ? parseUTC(e.time) : null, jump: true }));
        const openMover = o => {
            if (o.fileId) { try { window.parent.postMessage({ type: 'requestActivate', id: o.fileId, from: 'mission' }, '*'); } catch (_) {} }
            else if (o.wmo) window.open(`https://fleetmonitoring.euro-argo.eu/float/${encodeURIComponent(o.wmo)}`, '_blank', 'noopener');
        };
        movers.filter(o => o.fileId || o.wmo || o.isShip).forEach(o => {
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line'); line.setAttribute('stroke', o.colour); line.setAttribute('stroke-width', '1'); line.setAttribute('stroke-dasharray', '2 3'); leaders.appendChild(line);
            const node = add(`<div class="tag ${o.showLabel || o.isShip ? '' : 'quiet'}" title="${o.fileId ? 'Open this platform’s data' : o.wmo ? 'Open on Euro-Argo fleet monitoring' : ''}">${esc(o.label)}</div>`, null, { mover: o, drop: line });
            if (o.fileId || o.wmo) node.addEventListener('click', () => openMover(o)); else node.style.cursor = 'default';
        });

        const hiddenLayers = new Set();      // legend: annotation layers switched off
        let now = T1;
        function layoutLabels() {
            for (const it of items) {
                const a = hiddenLayers.has(it.layer) || (it.owner && it.owner.hidden) ? null : it.mover ? (!it.mover.hidden && it.mover.now && it.mover.labelAnchor) : it.anchor, p = a && project(a);
                if (it.drop) {       // dotted drop line from the surface-height tag down to the vehicle
                    const v = p && it.mover.anchor[2] < a[2] && project(it.mover.anchor);
                    it.drop.style.display = v ? '' : 'none';
                    if (v) { it.drop.setAttribute('x1', p[0]); it.drop.setAttribute('y1', p[1]); it.drop.setAttribute('x2', v[0]); it.drop.setAttribute('y2', v[1]); }
                }
                if (!p) { if (it._hid !== true) { it._hid = true; it.node.style.display = 'none'; } if (it.line) it.line.style.display = 'none'; continue; }
                const x = p[0] + (it.dx || 0), y = p[1] + (it.dy || 0);
                const tr = x.toFixed(1) + 'px ' + y.toFixed(1) + 'px';
                if (it._hid !== false) { it._hid = false; it.node.style.display = ''; }
                if (tr !== it._tr) { it._tr = tr; it.node.style.translate = tr; }
                const future = it.time != null && it.time > now;
                it.node.classList.toggle('future', future);
                if (it.line) { it.line.style.display = ''; it.line.style.opacity = future ? 0.3 : 1; it.line.setAttribute('x1', p[0]); it.line.setAttribute('y1', p[1]); it.line.setAttribute('x2', x); it.line.setAttribute('y2', y); }
            }
        }

        // ── Side cards ──
        $('timelineRows').innerHTML = (mission.events || []).map((e, q) => `<div class="row ${e.time ? 'jump' : ''}" data-t="${e.time ? parseUTC(e.time) : ''}" title="${e.time ? 'Jump to this date' : ''}"><span class="badge sm">${q + 1}</span><b>${esc(e.date)}</b><span>${esc(e.text)}</span></div>`).join('');
        $('timeline').classList.toggle('hidden', !(mission.events || []).length);
        {
            const rows = [], seenModel = new Set(), seenGroup = new Set();
            const names = { alr: 'Autosub Long Range (ALR)', slocum: 'Slocum glider', seaglider: 'Seaglider', argo: 'Argo float' };
            // One section per kind of vehicle (icon + name), its tracks listed beneath; every row has a show/hide eye.
            const eye = '<button class="eye" title="Show / hide"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/><path class="slash" d="M4 4l16 16"/></svg></button>';
            const section = (kind, text, group) => { if (seenModel.has(group)) return; seenModel.add(group); rows.push(`<div class="krow head" data-group="${esc(group)}"><span class="sw"><img src="${iconFor(kind)}" alt=""></span><span>${esc(text)}</span>${eye}</div>`); };
            mission.platforms.forEach(p => {
                const group = 'kind:' + p.kind;
                if (!seenModel.has(group)) {
                    section(p.kind, p.group || names[p.kind] || p.kind, group);
                    mission.platforms.filter(q => q.kind === p.kind).forEach(q => {
                        const tr = tracks[q.key] || {}, ok = tr.status === 'ready';
                        const note = ok ? '' : `<small>${tr.status === 'processing' ? 'processing…' : 'no data loaded'}</small>`;
                        rows.push(`<div class="krow sub ${ok ? 'open' : 'off'}" data-in="${esc(group)}" data-key="${esc(q.key)}" title="${ok ? 'Open this platform’s data' : ''}"><span class="sw"><i style="background:${esc(q.colour || '#d9c45c')};height:${q.width ? 5 : 3}px"></i></span><span>${esc(q.key_label || q.label || q.key)}</span>${note}${ok ? eye : ''}</div>`);
                    });
                }
            });
            (mission.ships || []).forEach(sh => {
                const key = sh.key || 'ship', group = 'ship:' + key;
                section(sh.model || 'rrs_discovery', sh.label || 'Ship', group);
                rows.push(`<div class="krow sub" data-in="${esc(group)}" data-key="${esc(key)}"><span class="sw"><i style="background:${esc(sh.colour || '#e8702a')};height:4px"></i></span><span>Track (approximate)</span></div>`);
            });
            if ((scene.floats || []).some(f => f.wmo)) {
                section('argo', names.argo + 's', 'floats');
                rows.push(`<div class="krow sub" data-in="floats"><span class="sw"><i style="border-top:3px dotted ${esc((mission.floats || {}).colour || '#0f8b8d')}"></i></span><span>Drift between surfacings</span></div>`);
            }
            // Annotations: built from whatever the mission file has, nothing to declare.
            const layers = [];
            (mission.stations || []).forEach((st, q) => layers.push(['station:' + q, st.label || 'Station', `<i style="border-top:3px dashed ${esc(st.colour || '#0f8b8d')}"></i>`]));
            if ((mission.stages || []).length) layers.push(['stages', 'Stage labels', `<span class="klogo"><img src="${stageIcon}" alt=""></span>`]);
            if ((mission.events || []).length) layers.push(['events', 'Timeline markers', '<span class="badge sm">1</span>']);
            if ((mission.places || []).length) layers.push(['places', 'Place names', '<b style="font-size:11px;color:var(--soft)">Abc</b>']);
            if (sceneryAll.length) layers.push(['scenery', 'Scenery', '<svg viewBox="0 0 24 24" width="20" height="20" fill="#2f9e6a"><path d="M12 2l5 7h-2.6l4.1 6h-3l3.5 5H13v2h-2v-2H5l3.5-5h-3l4.1-6H7z"/></svg>']);
            if (layers.length) rows.push('<div class="krow head plain"><span>Scene</span></div>');
            layers.forEach(([id, text, sw]) => rows.push(`<div class="krow sub" data-layer="${esc(id)}"><span class="sw">${sw}</span><span>${esc(text)}</span>${eye}</div>`));
            $('keyRows').innerHTML = rows.join(''); $('key').classList.remove('hidden');
            $('keyRows').querySelectorAll('.krow.open').forEach(r => r.addEventListener('click', () => { const o = byKey[r.dataset.key]; if (o && !o.hidden) openMover(o); }));
            // Show / hide: a section's eye switches everything in it, a row's eye just that platform.
            const membersOf = group => (group === 'floats' ? movers.filter(o => o.wmo) : group.startsWith('ship:') ? movers.filter(o => o.isShip && o.key === group.slice(5)) : movers.filter(o => o.fileId && 'kind:' + o.kindName === group)).filter(Boolean);
            const paint = () => $('keyRows').querySelectorAll('.krow:not([data-layer]):not(.plain)').forEach(r => {
                const who = r.classList.contains('head') ? membersOf(r.dataset.group) : r.dataset.key ? [byKey[r.dataset.key]].filter(Boolean) : membersOf(r.dataset.in);
                const off = who.length > 0 && who.every(o => o.hidden);
                r.classList.toggle('hiddenRow', off);
            });
            $('keyRows').querySelectorAll('.eye').forEach(b => b.addEventListener('click', e => {
                e.stopPropagation();
                const r = b.closest('.krow');
                if (r.dataset.layer) {
                    const id = r.dataset.layer, hide = !hiddenLayers.has(id);
                    hiddenLayers[hide ? 'add' : 'delete'](id); r.classList.toggle('hiddenRow', hide);
                    if (id === 'scenery') setTracesVisible(sceneryAll, !hide);
                    if (id === 'events') $('timeline').classList.toggle('hidden', hide);      // the timeline card goes with its markers
                    if (id.startsWith('station:')) setTracesVisible(stationIdx[+id.slice(8)] || [], !hide);
                    layoutLabels(); return;
                }
                const who = r.classList.contains('head') ? membersOf(r.dataset.group) : [byKey[r.dataset.key]].filter(Boolean);
                setHidden(who, !who.every(o => o.hidden)); paint();
            }));
        }

        // ── Track colour: presets any loaded platform has (the rest draw grey), on ONE scale across the mission ──
        const NO_DATA = SK.LOOK.noDataColour;
        let refresh = () => {};      // set once the time bar exists
        getJSON(`/api/missions/${encodeURIComponent(missionId)}/colours`).then(({ options }) => {
            const plat = movers.filter(o => o.fileId);
            if (!options || !options.length || !plat.length) return;
            const sel = $('colourSel'), bar = $('colourBar');
            sel.innerHTML = '<option value="">By platform</option>' + options.map(o => `<option value="${esc(o.key)}">${esc(o.label)}</option>`).join('');
            $('colourRow').classList.remove('hidden');
            const fmt = SK.fmtValue;
            sel.addEventListener('change', async () => {
                const opt = options.find(o => o.key === sel.value);
                if (!opt) {
                    plat.forEach(o => { o.cvals = null; o._upto = null; });
                    await Plotly.restyle(plotDiv, { 'line.color': plat.map(o => o.colour), 'line.width': plat.map(o => o.width) }, plat.map(o => o.pastIdx));
                    await Plotly.restyle(plotDiv, { 'line.color': plat.map(o => o.colour), 'line.width': plat.map(o => Math.max(1, o.width * 0.45)) }, plat.map(o => o.aheadIdx));
                    await Plotly.restyle(plotDiv, { 'line.color': plat.map(o => o.colour), 'line.width': plat.map(o => o.width) }, plat.map(o => o.tipIdx));
                    bar.classList.add('hidden'); refresh(); return;
                }
                sel.disabled = true;
                const got = await Promise.all(plat.map(o => getJSON(`/api/missions/${encodeURIComponent(missionId)}/track/${encodeURIComponent(o.key)}?colour=${encodeURIComponent(opt.key)}`).catch(() => ({}))));
                sel.disabled = false;
                const raw = plat.map((o, i) => { const v = (got[i].colour || {}).values || []; return o.path.src.map(q => (v[q] == null ? null : v[q])); });
                const all = raw.flat().filter(v => v != null).sort((a, b) => a - b);
                if (!all.length) { sel.value = ''; return; }
                const cmin = all[Math.floor(all.length * 0.02)], cmax = Math.max(all[Math.floor(all.length * 0.98)], cmin + 1e-9);
                const pal = SK.paletteFor(opt.cmap), { plotMin, scale } = SK.colourScale(pal, cmin, cmax);      // missing values: a grey band under the scale
                plat.forEach((o, i) => { o.cvals = SK.clampValues(raw[i], cmin, cmax, plotMin); o._upto = null; });
                await Plotly.restyle(plotDiv, { 'line.color': plat.map(o => o.cvals), 'line.colorscale': plat.map(() => scale), 'line.cmin': plotMin, 'line.cmax': cmax, 'line.width': 4.5 }, plat.map(o => o.pastIdx));
                await Plotly.restyle(plotDiv, { 'line.color': plat.map(o => o.cvals), 'line.colorscale': plat.map(() => scale), 'line.cmin': plotMin, 'line.cmax': cmax, 'line.width': 2 }, plat.map(o => o.aheadIdx));
                await Plotly.restyle(plotDiv, { 'line.color': plat.map(() => [plotMin, plotMin]), 'line.colorscale': plat.map(() => scale), 'line.cmin': plotMin, 'line.cmax': cmax, 'line.width': 4.5 }, plat.map(o => o.tipIdx));
                const units = ((got.find(g => g.colour) || {}).colour || {}).units || '';
                const grey = raw.some(r => !r.some(v => v != null));      // a platform without it: grey gets its own square
                bar.querySelector('i').style.background = grey
                    ? `linear-gradient(${NO_DATA},${NO_DATA}) left / 9px 100% no-repeat, linear-gradient(90deg, ${pal.join(',')}) right / calc(100% - 14px) 100% no-repeat`
                    : `linear-gradient(90deg, ${pal.join(',')})`;
                bar.querySelector('div').style.paddingLeft = grey ? '14px' : '';
                bar.querySelector('div').innerHTML = `<span>${fmt(cmin)}</span><span>${esc(units)}</span><span>${fmt(cmax)}</span>`;
                bar.classList.remove('hidden'); refresh();
            });
        }).catch(() => {});

        // ── Time bar ──
        const slider = $('timeSlider'), clock = $('clock'), playBtn = $('playBtn');
        $('timebar').classList.remove('hidden');
        function setTime(t, fromSlider) {
            now = Math.max(T0, Math.min(T1, t));
            if (!fromSlider) slider.value = Math.round((now - T0) / (T1 - T0) * 1000);
            clock.textContent = fmtDate(now);
            for (const o of movers) placeLine(o, now);
            for (const o of movers) if (o.isShip) placeModel(o, now);      // ships first: deck cargo uses their heading
            for (const o of movers) if (!o.isShip) placeModel(o, now);
            let cur = null;
            for (const r of timelineRows) { const f = r.t > now; if (f !== r.future) { r.future = f; r.node.classList.toggle('future', f); } if (!f && (!cur || r.t >= cur.t)) cur = r; }
            if (cur !== currentRow) {       // the latest event passed: marked, and kept in view if the card scrolls
                if (currentRow) currentRow.node.classList.remove('current');
                currentRow = cur;
                if (cur) { cur.node.classList.add('current'); cur.node.scrollIntoView({ block: 'nearest' }); }
            }
            layoutLabels();
        }
        const timelineRows = [...document.querySelectorAll('#timelineRows .row')].filter(r => r.dataset.t !== '').map(node => ({ node, t: +node.dataset.t, future: null }));
        let playing = false, lastFrame = 0, currentRow = null;
        const ICON = { play: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M8 5.5v13a1 1 0 0 0 1.53.85l10.2-6.5a1 1 0 0 0 0-1.7L9.53 4.65A1 1 0 0 0 8 5.5z"/></svg>',
                       pause: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><rect x="6.5" y="5" width="4" height="14" rx="1.2"/><rect x="13.5" y="5" width="4" height="14" rx="1.2"/></svg>' };
        const play = on => { const was = playing; playing = on; playBtn.innerHTML = on ? ICON.pause : ICON.play; playBtn.setAttribute('aria-label', on ? 'Pause' : 'Play'); if (on && now >= T1) setTime(T0); else if (was && !on) setTime(now, true); lastFrame = 0; };      // pausing: lines catch up to the exact instant
        playBtn.addEventListener('click', () => play(!playing));
        slider.addEventListener('input', () => { play(false); setTime(T0 + (T1 - T0) * slider.value / 1000, true); });
        jumpTo = t => { play(false); setTime(t); };
        const speedSel = $('speedSel');
        try { const v = localStorage.getItem('gp_mission_speed'); if (v && [...speedSel.options].some(o => o.value === v)) speedSel.value = v; } catch (_) {}
        speedSel.addEventListener('change', () => { try { localStorage.setItem('gp_mission_speed', speedSel.value); } catch (_) {} });
        const homeCamera = JSON.parse(JSON.stringify(plotDiv.layout.scene.camera));
        $('resetBtn').addEventListener('click', async () => { await Plotly.relayout(plotDiv, { 'scene.camera': JSON.parse(JSON.stringify(homeCamera)) }); noPicking(); });
        // Keys: Space = play / pause, ←/→ = step (Shift: bigger), Home / End. Form controls keep their own keys.
        window.addEventListener('keydown', e => {
            if (e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(e.target.tagName) || document.querySelector('.newdlg')) return;
            const step = (T1 - T0) / (e.shiftKey ? 20 : 200);
            if (e.key === ' ') play(!playing);
            else if (e.key === 'ArrowLeft') jumpTo(now - step);
            else if (e.key === 'ArrowRight') jumpTo(now + step);
            else if (e.key === 'Home') jumpTo(T0);
            else if (e.key === 'End') jumpTo(T1);
            else return;
            e.preventDefault();
        });
        refresh = () => setTime(now);
        document.querySelectorAll('#timelineRows .row.jump').forEach(r => r.addEventListener('click', () => jumpTo(+r.dataset.t)));

        const stillScene = matchMedia('(prefers-reduced-motion: reduce)').matches;      // no autoplay, sway or swell
        // One rAF loop: playback, scenery sway, and re-pinning labels whenever the camera moves.
        let lastSway = 0, lastShipSway = 0, shown = true, lastTs = 0, frameMs = 16, lastInput = performance.now();
        const SWAY_MS = [50, 90], SWELL_REST_MS = 30e3;      // sway every 50 ms, 90 when frames run long; a paused scene's swell stops this long after the last input
        ['pointerdown', 'pointermove', 'wheel', 'keydown'].forEach(ev => window.addEventListener(ev, () => { lastInput = performance.now(); }, { capture: true, passive: true }));
        const camKey = new Float64Array(18);
        const camMoved = () => {
            const v = glScene().glplot.cameraParams.view; let moved = false;
            for (let q = 0; q < 18; q++) { const c = q < 16 ? v[q] : q === 16 ? plotDiv.clientWidth : plotDiv.clientHeight; if (camKey[q] !== c) { camKey[q] = c; moved = true; } }
            return moved;
        };
        // The shell hides this iframe rather than unloading it ("Back to mission"): stand still until it is shown again.
        window.addEventListener('message', e => { if (e.data && e.data.type === 'missionVisible') { shown = !!e.data.on; lastFrame = 0; } });
        const frame = ts => {
            if (!shown || document.hidden) { lastFrame = lastTs = 0; requestAnimationFrame(frame); return; }
            if (playing) {
                if (lastFrame) { const t = now + (T1 - T0) * (ts - lastFrame) / 1000 / (+$('speedSel').value || 60); if (t >= T1) { setTime(T1); play(false); } else setTime(t); }
                lastFrame = ts;
            }
            if (lastTs) frameMs += (Math.min(ts - lastTs, 100) - frameMs) * 0.05;
            lastTs = ts;
            if (!playing && !stillScene && ts - lastShipSway > 60 && performance.now() - lastInput < SWELL_REST_MS) { lastShipSway = ts; for (const o of movers) if (o.isShip) placeModel(o, now); }   // the swell keeps going while paused
            // Not mid-drag: a sway re-uploads the whole scenery mesh, the view's costliest patch.
            if (sceneryIdx >= 0 && !rotating && !stillScene && !hiddenLayers.has('scenery') && ts - lastSway > SWAY_MS[frameMs > 22 ? 1 : 0]) { lastSway = ts; const s = Scenery.sway(ts / 1000); if (s) fastPatch(sceneryIdx, { x: s.x, y: s.y, z: s.z }); }
            try { if (camMoved()) layoutLabels(); } catch (_) {}
            requestAnimationFrame(frame);
        };
        // Opens playing from the start; "open_at" in the mission's time block opens paused on that date instead.
        if (mission.time && mission.time.open_at) { setTime(parseUTC(mission.time.open_at)); play(false); }
        else if (stillScene) { setTime(T1); play(false); }      // reduced motion: the whole mission, standing still
        else { setTime(T0); play(true); }
        requestAnimationFrame(frame);

        // Files still processing: reload once they're ready.
        if (mission.platforms.some(p => (tracks[p.key] || {}).status === 'processing')) {
            const poll = setInterval(() => getJSON(`/api/missions/${encodeURIComponent(missionId)}`).then(m => { if (!m.platforms.some(p => p.status !== 'ready' && p.status !== 'missing')) { clearInterval(poll); location.reload(); } }).catch(() => {}), 8000);
        }
    }
})();
