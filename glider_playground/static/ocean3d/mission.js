// The mission page: several platforms in one box, from /api/missions/<id>, with the mission's own furniture on top —
// title, key (show / hide + track colour), timeline card, and labels pinned to the scene (places, stations, stage
// pills, numbered events, platform name tags). Schema: missions/README.md.
import { createView, getJSON } from './view.js';
import { createLabels } from './labels.js';

const id = new URLSearchParams(location.search).get('id');
const api = `/api/missions/${encodeURIComponent(id)}`;
const SEABED_GRID = 1000;      // seabed points along the box's longer side; fetched once per mission, then cached server-side
const KIND_NAMES = { alr: 'Autosub Long Range (ALR)', slocum: 'Slocum glider', seaglider: 'Seaglider', argo: 'Argo float' };
const SHIP_COLOUR = '#e8702a', STATION_COLOUR = '#0f8b8d', PLATFORM_COLOUR = '#d9c45c';
const DAY = 24 * 3600e3;
// Aft-deck cargo slots as fractions of the ship's hull length [x from midships (+ = bow), y to port]; DECK_LEN = cargo length (floats: height).
const DECK_SLOTS = { alr: [[-0.37, 0.046], [-0.37, -0.046]], glider: [[-0.415, 0.014], [-0.415, -0.014], [-0.325, 0.014], [-0.325, -0.014]],
                     float: [[-0.262, 0.064], [-0.262, 0.022], [-0.262, -0.022], [-0.262, -0.064]] };
const DECK_LEN = { alr: 0.17, glider: 0.08, float: 0.07 };

const $ = i => document.getElementById(i);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const parseUTC = s => { const v = String(s).trim().replace(' ', 'T'); return Date.parse((v.length <= 10 ? v + 'T00:00:00' : v) + 'Z'); };      // naive UTC: never a bare new Date(str)
const iconFor = kind => `/static/icons/${kind === 'rrs_discovery' ? 'rrs-discovery' : kind === 'rrs_james_cook' ? 'rrs-james-cook' : /^rrs|ship/.test(kind) ? 'ship' : encodeURIComponent(kind)}-mapicon.svg`;
const toShell = msg => { try { parent.postMessage(msg, '*'); } catch (_) {} };

// Where a served track ({time_ms, lon, lat, z}, nulls allowed) is at time t, interpolated; null outside it.
function sampleTrack(tr, t) {
    let a = -1, b = -1;
    for (let q = 0; q < tr.time_ms.length; q++) { if (tr.lon[q] == null || tr.lat[q] == null) continue; if (tr.time_ms[q] <= t) a = q; else { b = q; break; } }
    if (a < 0) return null;
    if (b < 0) return tr.time_ms[a] === t ? { lon: tr.lon[a], lat: tr.lat[a], z: tr.z[a] || 0 } : null;
    const f = (t - tr.time_ms[a]) / (tr.time_ms[b] - tr.time_ms[a]), mix = k => (tr[k][a] || 0) + ((tr[k][b] || 0) - (tr[k][a] || 0)) * f;
    return { lon: mix('lon'), lat: mix('lat'), z: mix('z') };
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
// A ship's legs as tracks ({time_ms, lat, lon, z, label}): schematic points timed by distance along them, or a
// daily mean of the platforms a leg follows.
function shipLegs(ship, tracks, cosLat) {
    const legs = [];
    for (const leg of ship.legs || []) {
        const [a, b] = leg.time.map(parseUTC);
        let pts = [];
        if (leg.follow) {
            const who = leg.follow.map(k => tracks[k]).filter(Boolean), dLat = -(leg.offset_km || 0) / 111.2;
            for (let t = a; t <= b; t += DAY) {
                const ps = who.map(tr => sampleTrack(tr, t)).filter(Boolean);
                if (ps.length) pts.push([t, ps.reduce((s, v) => s + v.lat, 0) / ps.length + dLat, ps.reduce((s, v) => s + v.lon, 0) / ps.length]);
            }
        } else if ((leg.points || []).length > 1) {
            const c = catmull(leg.points), len = [0];
            for (let q = 1; q < c.length; q++) len.push(len[q - 1] + Math.hypot((c[q][1] - c[q - 1][1]) * cosLat, c[q][0] - c[q - 1][0]));
            pts = c.map((v, q) => [a + (b - a) * len[q] / Math.max(len[len.length - 1], 1e-9), v[0], v[1]]);
        }
        if (pts.length > 1) legs.push({ time_ms: pts.map(v => v[0]), lat: pts.map(v => v[1]), lon: pts.map(v => v[2]), label: leg.label, start: a });
    }
    return legs;
}

const loading = text => { $('loaderText').textContent = text; };

async function start() {
    if (!id) throw new Error('No mission: add ?id=<mission id>');
    loading('Loading seabed (the first time takes about a minute)…');
    const missionP = getJSON(api);
    missionP.then(m => { $('loaderTitle').textContent = m.title || id; }).catch(() => {});
    const [mission, scene] = await Promise.all([missionP, getJSON(`${api}/scene?grid=${SEABED_GRID}`)]);
    document.title = `${String(mission.title || id).replace(/\s+/g, ' ')} · Glider Playground`;
    $('title').textContent = mission.title || id;
    loading('Loading tracks…');
    const view = createView(scene, { store: 'gp_mission_view', ownLayers: true, defaultSpeed: 36000, speed: (mission.time || {}).speed }), world = view.world;
    const lift = -world.deepest * 0.004;      // metres: surface lines and labels sit a hair above the water
    const track = (key, colour) => getJSON(`${api}/track/${encodeURIComponent(key)}${colour ? `?colour=${encodeURIComponent(colour)}` : ''}`);

    // ── Platforms, floats, ships, stations ──
    const tracks = {}, byKey = {}, state = {};      // state: platform key -> 'ready' | 'processing' | …
    await Promise.all(mission.platforms.map(async p => {
        const data = p.status === 'ready' ? await track(p.key).catch(() => ({ status: 'error' })) : { status: p.status };
        state[p.key] = data.status;
        if (data.status !== 'ready') return;
        tracks[p.key] = data;
        byKey[p.key] = Object.assign(await view.addPlatform(p.key, p.label || p.key, data, p.colour || PLATFORM_COLOUR, p.model || p.kind), { kind: p.kind, fileId: data.file_id, showLabel: !!p.show_label });
    }));
    const floats = Object.fromEntries((scene.floats || []).filter(f => f.wmo).map(f => [f.wmo, f]));      // chosen by the mission's "floats" block
    await view.addFloats(floats, (mission.floats || {}).colour);
    for (const p of view.platforms) if (p.float) p.wmo = p.key.slice(5);

    const legLabels = [], cosLat = Math.cos(world.lat0 * Math.PI / 180);
    for (const sh of mission.ships || []) {
        const legs = shipLegs(sh, tracks, cosLat), colour = sh.colour || SHIP_COLOUR;
        if (!legs.length) continue;
        const whole = { time_ms: legs.flatMap(l => l.time_ms), lat: legs.flatMap(l => l.lat), lon: legs.flatMap(l => l.lon) };
        whole.z = whole.lon.map(() => 0);
        const ship = Object.assign(await view.addPlatform(sh.key || 'ship', sh.label || 'Ship', whole, colour, sh.model || 'rrs_discovery', { line: false, sizeKey: 'ship' }), { ship: true });
        byKey[ship.key] = ship;
        // Deck cargo: platforms ride the aft deck until their first fix (`deploys`) or from their last one (`recovers`).
        const used = { glider: 0, float: 0, alr: 0 };
        for (const [list, mode] of [[sh.deploys, 'deploy'], [sh.recovers, 'recover']]) for (const k of list || []) for (const p of k === 'floats' ? view.platforms.filter(v => v.float) : [byKey[k]].filter(Boolean)) {
            const kind = p.float ? 'float' : p.kind === 'alr' ? 'alr' : 'glider', slot = p.carry ? p.carry.slot : DECK_SLOTS[kind][used[kind]++];
            if (slot) view.carry(p, ship, slot, DECK_LEN[kind], mode);
        }
        for (const l of legs) {
            view.addLine({ ...l, z: l.lon.map(() => lift) }, colour, { width: 4, owner: ship });
            const mid = l.lon.length >> 1;
            if (l.label) legLabels.push({ text: l.label, colour, owner: ship, time: l.start, anchor: world.place(l.lon[mid], l.lat[mid], lift) });
        }
    }
    const stations = [];      // per station: what the key's eye switches
    for (const st of mission.stations || []) {
        const n = 48, rLat = (st.radius_km || 50) / 111.2, rLon = rLat / cosLat, ring = Array.from({ length: n + 1 }, (_, q) => q / n * 2 * Math.PI);
        const objects = [view.addLine({ lon: ring.map(a => st.lon + rLon * Math.cos(a)), lat: ring.map(a => st.lat + rLat * Math.sin(a)), z: ring.map(() => lift) }, st.colour || STATION_COLOUR, { width: 3 })];
        if (['green', 'red', 'blue'].includes(st.buoy)) objects.push(await view.addModel('buoy_' + st.buoy, st.lon, st.lat, 'buoy').catch(() => null));
        stations.push(objects.filter(Boolean));
    }

    const timeline = view.startTimeline(...scene.time_ms);
    if (mission.time && mission.time.open_at) view.jumpTo(parseUTC(mission.time.open_at));      // opens paused on that date

    // ── Labels ──
    const labels = createLabels($('labels'), $('leaders'), $('view'), view.vertical);
    const anchorOf = at => {
        if (at && at.platform) { const tr = tracks[at.platform], p = tr && sampleTrack(tr, parseUTC(at.time)); return p ? world.place(p.lon, p.lat, p.z) : null; }
        return at && at.lat != null ? world.place(at.lon, at.lat, Math.max(0, world.depthAt(at.lon, at.lat)) + lift) : null;
    };
    const open = p => {
        if (p.fileId) toShell({ type: 'requestActivate', id: p.fileId, from: 'mission' });
        else if (p.wmo) window.open(`https://fleetmonitoring.euro-argo.eu/float/${encodeURIComponent(p.wmo)}`, '_blank', 'noopener');
    };
    for (const p of mission.places || []) labels.add(`<div class="place ${esc(p.style || '')}">${esc(p.text)}</div>`, { anchor: anchorOf(p), layer: 'places' });
    (mission.stations || []).forEach((s, q) => labels.add(`<div class="place station" style="color:${esc(s.colour || STATION_COLOUR)}">${esc(s.label || '')}</div>`,
        { anchor: world.place(s.lon, s.lat + (s.radius_km || 50) / 111.2 * 1.25, lift), layer: 'station:' + q }));
    for (const l of legLabels) labels.add(`<div class="place" style="color:${esc(l.colour)};font-weight:700">${esc(l.text)}</div>`, { anchor: l.anchor, owner: l.owner, time: l.time, offset: [0, -14] });
    const stageIcon = iconFor(mission.stage_icon || 'alr');
    for (const s of mission.stages || []) {
        const anchor = anchorOf(s.at), time = s.at && s.at.time ? parseUTC(s.at.time) : null;
        labels.add('<div class="dot"></div>', { anchor, layer: 'stages', time });
        labels.add(`<div class="pill"><span class="logo"><img src="${stageIcon}" alt=""></span><span><b>${esc(s.title)}</b><small>${esc(s.detail || '')}</small></span></div>`,
            { anchor, layer: 'stages', time, offset: s.offset || [0, -60], leader: '#12295c', onClick: time == null ? null : () => view.jumpTo(time) });
    }
    (mission.events || []).forEach((e, q) => { const time = e.time ? parseUTC(e.time) : null; labels.add(`<span class="badge pin">${q + 1}</span>`, { anchor: anchorOf(e.at), layer: 'events', time, offset: e.offset, onClick: time == null ? null : () => view.jumpTo(time) }); });
    for (const p of view.platforms) {
        const node = labels.add(`<div class="tag ${p.showLabel || p.ship ? '' : 'quiet'}">${esc(p.label)}</div>`, { platform: p, drop: p.colour, onClick: p.fileId || p.wmo ? () => open(p) : null });
        if (node) node.title = p.fileId ? 'Open this platform’s data' : p.wmo ? 'Open on Euro-Argo fleet monitoring' : '';
    }
    view.stage.beforeRender.push(camera => { labels.place(camera); });

    // ── Timeline card ──
    const events = (mission.events || []).map((e, q) => ({ ...e, n: q + 1, t: e.time ? parseUTC(e.time) : null }));
    $('timelineRows').innerHTML = events.map(e => `<div class="row ${e.t != null ? 'jump' : ''}" title="${e.t != null ? 'Jump to this date' : ''}"><span class="badge sm">${e.n}</span><b>${esc(e.date)}</b><span>${esc(e.text)}</span></div>`).join('');
    const rows = [...$('timelineRows').children];
    rows.forEach((r, q) => { if (events[q].t != null) r.addEventListener('click', () => view.jumpTo(events[q].t)); });
    $('timeline').hidden = !events.length;
    let current = null;
    view.onTime(now => {
        labels.setTime(now);
        let cur = null;
        events.forEach((e, q) => { if (e.t == null) return; rows[q].classList.toggle('future', e.t > now); if (e.t <= now && (!cur || e.t >= cur.t)) cur = { t: e.t, row: rows[q] }; });
        if ((cur && cur.row) === current) return;      // the latest event passed: marked, and kept in view if the card scrolls
        if (current) current.classList.remove('current');
        current = cur && cur.row;
        if (current) { current.classList.add('current'); current.scrollIntoView({ block: 'nearest' }); }
    });

    // ── Key: one section per kind of vehicle, its tracks beneath; every row has a show / hide eye ──
    const eye = '<button class="eye" title="Show / hide"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/><path class="slash" d="M4 4l16 16"/></svg></button>';
    const html = [], seen = new Set();
    const section = (kind, text, group) => html.push(`<div class="krow head" data-group="${esc(group)}"><span class="sw"><img src="${iconFor(kind)}" alt=""></span><span>${esc(text)}</span>${eye}</div>`);
    for (const p of mission.platforms) {
        if (seen.has(p.kind)) continue;
        seen.add(p.kind); section(p.kind, p.group || KIND_NAMES[p.kind] || p.kind, 'kind:' + p.kind);
        for (const q of mission.platforms.filter(v => v.kind === p.kind)) {
            const ok = !!byKey[q.key], note = ok ? '' : `<small>${state[q.key] === 'processing' ? 'processing…' : 'no data loaded'}</small>`;
            html.push(`<div class="krow sub ${ok ? 'open' : 'off'}" data-key="${esc(q.key)}" title="${ok ? 'Open this platform’s data' : ''}"><span class="sw"><i style="background:${esc(q.colour || PLATFORM_COLOUR)};height:${q.width ? 5 : 3}px"></i></span><span>${esc(q.key_label || q.label || q.key)}</span>${note}${ok ? eye : ''}</div>`);
        }
    }
    for (const p of view.platforms.filter(v => v.ship)) {
        const sh = (mission.ships || []).find(s => (s.key || 'ship') === p.key) || {};
        section(sh.model || 'rrs_discovery', p.label, 'ship:' + p.key);
        html.push(`<div class="krow sub" data-layer="legs:${esc(p.key)}"><span class="sw"><i style="background:${esc(p.colour)};height:4px"></i></span><span>Track (approximate)</span>${eye}</div>`);
    }
    if (view.platforms.some(p => p.float)) {
        section('argo', KIND_NAMES.argo + 's', 'floats');
        html.push(`<div class="krow sub" data-layer="traces"><span class="sw"><i style="border-top:3px dotted ${esc((mission.floats || {}).colour || STATION_COLOUR)}"></i></span><span>Dives between surfacings</span>${eye}</div>`);
    }
    const layers = (mission.stations || []).map((st, q) => ['station:' + q, st.label || 'Station', `<i style="border-top:3px dashed ${esc(st.colour || STATION_COLOUR)}"></i>`]);
    if ((mission.stages || []).length) layers.push(['stages', 'Stage labels', `<span class="klogo"><img src="${stageIcon}" alt=""></span>`]);
    if (events.length) layers.push(['events', 'Timeline markers', '<span class="badge sm">1</span>']);
    if ((mission.places || []).length) layers.push(['places', 'Place names', '<b style="font-size:11px;color:var(--muted)">Abc</b>']);
    layers.push(['scenery', 'Wildlife', '<svg viewBox="0 0 24 24" width="22" height="22" fill="#3b82a6"><path d="M2 12c3-4.5 7-6 11-6 3.5 0 6 2.5 7 4l3-3v10l-3-3c-1 1.5-3.500 4-7 4-4 0-8-1.500-11-6z"/><circle cx="8" cy="11" r="1.200" fill="#fff"/></svg>']);
    html.push('<div class="krow head plain"><span>Scene</span></div>', ...layers.map(([layer, text, sw]) => `<div class="krow sub" data-layer="${esc(layer)}"><span class="sw">${sw}</span><span>${esc(text)}</span>${eye}</div>`));
    const keyRows = $('keyRows'), shipByKey = Object.fromEntries(view.platforms.filter(v => v.ship).map(v => [v.key, v]));
    keyRows.innerHTML = html.join(''); $('key').hidden = false;
    const membersOf = group => view.platforms.filter(p => group === 'floats' ? p.float : group.startsWith('ship:') ? p.ship && p.key === group.slice(5) : p.fileId && 'kind:' + p.kind === group);
    const paint = () => keyRows.querySelectorAll('.krow:not(.plain)').forEach(r => {
        const layer = r.dataset.layer;
        if (layer) { if (layer === 'scenery') r.classList.toggle('hiddenRow', !view.sceneryOn()); else if (layer === 'traces') r.classList.toggle('hiddenRow', !view.tracesOn() || membersOf('floats').every(p => p.hidden)); else if (layer.startsWith('legs:')) r.classList.toggle('hiddenRow', !view.linesOn(shipByKey[layer.slice(5)]) || shipByKey[layer.slice(5)].hidden); return; }      // a track goes with its owner
        const who = r.dataset.key ? [byKey[r.dataset.key]].filter(Boolean) : membersOf(r.dataset.group);
        r.classList.toggle('hiddenRow', who.length > 0 && who.every(p => p.hidden));
    });
    keyRows.addEventListener('click', e => {
        const r = e.target.closest('.krow'); if (!r) return;
        if (!e.target.closest('.eye')) { const p = byKey[r.dataset.key]; if (p && !p.hidden && r.classList.contains('open')) open(p); return; }
        const layer = r.dataset.layer;
        if (layer) {       // a section's eye switches everything in it, a row's eye just that platform
            const on = r.classList.contains('hiddenRow'); r.classList.toggle('hiddenRow', !on);
            if (layer === 'scenery') view.setScenery(on);
            else if (layer === 'traces') { view.setTraces(on); if (on) view.setHidden(membersOf('floats'), false); }
            else if (layer.startsWith('legs:')) { const ship = shipByKey[layer.slice(5)]; view.setLines(ship, on); if (on) view.setHidden([ship], false); }
            else labels.showLayer(layer, on);
            if (layer === 'events') $('timeline').hidden = !on;      // the timeline card goes with its markers
            if (layer.startsWith('station:')) for (const o of stations[+layer.slice(8)] || []) o.visible = on;
            view.stage.redraw(); paint(); return;
        }
        const who = r.dataset.key ? [byKey[r.dataset.key]].filter(Boolean) : membersOf(r.dataset.group);
        view.setHidden(who, !who.every(p => p.hidden)); paint();
    });
    paint();

    view.ready();
    $('loader').classList.add('done');
    getJSON(`${api}/colours`).then(({ options }) => { if (options && options.length) view.setLegend(options, (key, opt) => tracks[key] ? track(key, opt.key).then(d => d.colour || null) : null); }).catch(console.error);

    // The shell hides this iframe rather than unloading it ("Back to mission"): stand still until it is shown again.
    let resume = false;
    window.addEventListener('message', e => {
        if (!e.data || e.data.type !== 'missionVisible') return;
        if (!e.data.on) { resume = timeline.playing(); timeline.play(false); } else if (resume) { resume = false; timeline.play(true); }
    });
    // Files still processing: reload once they're ready.
    if (Object.values(state).includes('processing')) {
        const poll = setInterval(() => getJSON(api).then(m => { if (!m.platforms.some(p => p.status !== 'ready' && p.status !== 'missing')) { clearInterval(poll); location.reload(); } }).catch(() => {}), 8000);
    }
}
start().catch(e => { loading(e.message); $('loader').classList.add('failed'); console.error(e); });
