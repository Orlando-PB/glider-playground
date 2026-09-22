// The mission list: a hero strip + one card per mission with a live map thumbnail. Runs in an iframe over the shell's
// workspace (missions_shell.js); picking a mission posts `missionOpen`. A mission itself is mission_three.html
// (static/ocean3d/mission.js): opened here with an id, this page hands over to it.
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
    const liveBusy = st => !!st && (st.scanning || st.downloading > 0 || st.processing > 0);
    function liveText(st) {
        if (!st) return '';
        const bits = [];
        if (st.scanning) bits.push('checking BODC for live platforms');
        if (st.downloading) bits.push(`downloading ${st.downloading} file${st.downloading > 1 ? 's' : ''}`);
        if (st.processing) bits.push(`processing ${st.processing} file${st.processing > 1 ? 's' : ''}`);
        if (bits.length) return '<i class="spin"></i>Live missions: ' + bits.join(', ') + '…';
        return st.error ? 'Live missions: BODC could not be reached, showing what is already here.' : '';
    }
    function showPicker() {
        document.body.classList.add('picking');
        const el = $('picker');
        const hero = `<div class="hero"><h2>Missions</h2><p>Whole missions in one scene: Press play and watch it unfold, or click a platform to open its data.</p>`
            + '<svg class="wave" viewBox="0 0 1200 46" preserveAspectRatio="none"><path d="M0 30 Q 75 10 150 30 T 300 30 T 450 30 T 600 30 T 750 30 T 900 30 T 1050 30 T 1200 30 V46 H0Z" fill="#fff"/></svg></div>';
        const dates = t => { if (!t || !t.start) return ''; const f = d => new Date(parseUTC(d)).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }); return `${f(t.start)} – ${f(t.end)}`; };
        getJSON('/api/missions').then(({ missions, local, live_status }) => {
            const EXPORTS = m => !local ? '' : `<span class="exports"><a class="xbtn" href="/api/missions/${encodeURIComponent(m.id)}/export" download title="Just the mission JSON: whoever opens it needs the data files themselves"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>Export mission</a>`
                    + (m.platforms_available ? `<a class="xbtn" href="/api/missions/${encodeURIComponent(m.id)}/export?data=1" download title="One .zip with the mission and its full data files, ready to import elsewhere"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>Export mission + data</a>` : '') + `</span>`;
            el.innerHTML = `<div class="wrap">${hero}` + (missions.length ? '' : '<p>No missions yet.</p>')
                + missions.map(m => `<div class="mcard" role="button" tabindex="0" data-id="${esc(m.id)}"><div class="thumb loading"></div><div class="body"><b>${esc(m.title)}</b><span class="when"></span>`
                    + `<span class="sum">${esc(m.summary)}</span><div class="chips"><span class="chip ${m.platforms_available < m.platforms ? 'part' : ''}">${m.platforms_available} / ${m.platforms} data files</span></div>` + EXPORTS(m) + `</div></div>`).join('')
                + '<div class="hint">Missions are JSON files; a bundle (.zip) carries one together with its data. See missions/README.md.'
                + (local ? '' : ' To add one to this server, copy its bundle into the missions inbox.') + '</div></div>';
            const tools = document.createElement('div'); tools.className = 'tools';
            tools.innerHTML = local ? '<button data-act="new">New mission</button><button data-act="import">Import mission (.zip / .json)</button><input type="file" accept=".zip,.json" hidden><span class="msg"></span>' : '';
            el.querySelector('.hero').insertAdjacentElement('afterend', tools);
            // Live missions arrive as the BODC feed is scanned, downloaded and processed: say so, and redraw when the list changes.
            const live = document.createElement('div'); live.className = 'livestat'; tools.insertAdjacentElement('afterend', live);
            const sig = (ms, st) => ms.map(m => m.id + ':' + m.platforms_available).join() + '|' + liveText(st);
            const shown = sig(missions, live_status); live.innerHTML = liveText(live_status); live.hidden = !live.innerHTML;
            clearTimeout(showPicker.timer);
            const poll = () => getJSON('/api/missions').then(j => { if (!live.isConnected) return; if (sig(j.missions, j.live_status) !== shown) showPicker(); else if (liveBusy(j.live_status)) showPicker.timer = setTimeout(poll, 5000); }).catch(() => { showPicker.timer = setTimeout(poll, 15000); });
            if (liveBusy(live_status)) showPicker.timer = setTimeout(poll, 5000);
            if (local) wireTools(tools);
            const open = c => { if (embedded) window.parent.postMessage({ type: 'missionOpen', id: c.dataset.id }, '*'); else location.href = '/missions/static/mission_three.html?id=' + encodeURIComponent(c.dataset.id); };
            el.querySelectorAll('.mcard').forEach(c => {
                c.addEventListener('click', e => { if (!e.target.closest('.exports')) open(c); }); c.addEventListener('keydown', e => { if (e.target === c && (e.key === 'Enter' || e.key === ' ')) open(c); });
                getJSON(`/api/missions/${encodeURIComponent(c.dataset.id)}/preview`).then(pv => {
                    c.querySelector('.thumb').classList.remove('loading'); drawThumb(c.querySelector('.thumb'), pv);
                    c.querySelector('.when').textContent = dates(pv.time);
                    const chips = Object.entries(pv.kinds || {}).map(([k, n]) => `<span class="chip"><img src="${iconFor(k)}" alt="">${n} × ${esc(KIND_NAMES[k] || k)}</span>`);
                    if (pv.floats) chips.push(`<span class="chip"><img src="${iconFor('argo')}" alt="">Argo floats</span>`);
                    (pv.ships || []).forEach(sh => chips.push(`<span class="chip"><img src="${iconFor(sh.model)}" alt="">${esc(sh.label)}</span>`));
                    c.querySelector('.chips').insertAdjacentHTML('afterbegin', chips.join(''));
                }).catch(() => { c.querySelector('.thumb').classList.remove('loading'); c.querySelector('.thumb').classList.add('empty'); });
            });
        }).catch(e => { el.textContent = 'Could not list missions: ' + e.message; });
    }


    if (missionId) { location.replace(`/missions/static/mission_three.html?id=${encodeURIComponent(missionId)}&theme=${encodeURIComponent(document.documentElement.getAttribute('data-theme'))}`); return; }
    showPicker();
})();
