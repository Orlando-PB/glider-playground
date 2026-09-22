// The 3D view of one file: its track and vehicle in its own box, from /api/3d_data + /api/3d_bathy.
import { createView, getJSON, setStatus } from './view.js';
import { byFloat } from './argo.js';

const id = new URLSearchParams(location.search).get('id'), q = `id=${encodeURIComponent(id)}`;
const SEABED_GRID = 600;       // seabed points along the box's longer side; fetched once per file, then cached with it
const TRACK_COLOUR = '#12295c';
const FLOAT_MARGIN_DAYS = 10;

// Vehicle to draw: the detected platform kind, then name heuristics; a Slocum if unknown.
function modelFor(rec) {
    const kind = rec.platform_kind, n = String(rec.name || '').toLowerCase();
    if (['slocum', 'seaglider', 'alr'].includes(kind)) return kind;
    return /^alr[_\-\d]/.test(n) ? 'alr' : /^(sg\d|seaglider)/.test(n) ? 'seaglider' : 'slocum';
}

// No id, or one the server doesn't know: offer the files it has.
function pickFile(files) {
    const el = document.getElementById('status');
    el.textContent = files.length ? 'Pick a file: ' : 'No files loaded yet.';
    for (const f of files) { const a = document.createElement('a'); a.href = `?id=${encodeURIComponent(f.id)}`; a.textContent = f.name; a.style.cssText = 'display:block;margin-top:4px;color:inherit'; el.append(a); }
}

// The track, packed by the server once per file: uint32 n, padding, float64 time_ms[n], float32 lon, lat, z, pitch [n].
async function getTrack() {
    const r = await fetch(`/api/3d_track?${q}`);
    if (!r.ok) throw new Error(`No 3D track for this file (${r.status})`);
    const buf = await r.arrayBuffer(), n = new Uint32Array(buf, 0, 1)[0], f32 = k => new Float32Array(buf, 8 + n * 8 + k * n * 4, n);
    return { time_ms: new Float64Array(buf, 8, n), lon: f32(0), lat: f32(1), z: f32(2), pitch: f32(3) };
}

async function start() {
    const known = (await getJSON('/api/files').catch(() => ({ files: [] }))).files || [];
    if (!known.some(f => f.id === id)) return pickFile(known);
    setStatus('Loading seabed…');
    const [track, seabed] = await Promise.all([getTrack(), getJSON(`/api/3d_bathy?${q}&grid=${SEABED_GRID}`)]);
    const rec = known.find(f => f.id === id);
    document.title = rec.name || '3D view'; setStatus(rec.name || '');
    const view = createView(seabed, { floatTraces: false, store: 'gp_3d_view' });      // floats pass through as models; their dive lines are a Layers option
    await view.addPlatform(id, rec.name, track, TRACK_COLOUR, modelFor(rec));
    const times = track.time_ms.filter(Number.isFinite), t0 = times[0], t1 = times[times.length - 1];
    view.startTimeline(t0, t1);
    // Every Argo float that surfaces in the box while the vehicle is out (a cycle's margin either side). Never blocks the view.
    const lons = seabed.bathy_lon, lats = seabed.bathy_lat, margin = FLOAT_MARGIN_DAYS * 864e5;
    getJSON(`/api/argo/profiles?min_lat=${lats[0]}&max_lat=${lats[lats.length - 1]}&min_lon=${lons[0]}&max_lon=${lons[lons.length - 1]}&t0=${t0 - margin}&t1=${t1 + margin}`)
        .then(res => res.status === 'ready' ? view.addFloats(byFloat(res.profiles)) : console.info('Argo index:', res.status)).catch(console.error);
    // Track colour: the file's presets, or whatever variable the shell's selected plot shows ("followColour").
    let follow = null, legend = null;
    const followPlot = () => { if (legend && follow && follow.var) legend.choose({ key: `var:${follow.var}`, label: follow.var, cmap: follow.cmap || 'thermal', var: follow.var }); };
    window.addEventListener('message', e => { if (e.data && e.data.type === 'followColour') { follow = e.data; followPlot(); } });
    getJSON(`/api/3d_colours?${q}`).then(({ options }) => {
        legend = view.setLegend(options, (key, opt) => getJSON(`/api/3d_colour?${q}&${opt.var ? `var=${encodeURIComponent(opt.var)}&cmap=${encodeURIComponent(opt.cmap)}` : `preset=${encodeURIComponent(opt.key)}`}`).then(d => d.values ? { values: d.values, units: d.units } : null));
        followPlot();
    }).catch(console.error);
    view.ready();
}
start().catch(e => { setStatus(e.message); console.error(e); });
