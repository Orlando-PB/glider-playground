// Argo floats. The GDAC index only says where and when a float surfaced, so the dive between two surfacings is the
// standard mission drawn in: a spell at the surface, down to the park depth, a drift, a dip to the profile depth and
// the ascent — never deeper than the seabed. The result is an ordinary track for view.addPlatform.
const CYCLE = { surfaceH: 1, descendH: 6, parkM: 1000, deepStartH: 12, deepEndH: 7, profileM: 2000 };      // hours / metres; deep* = hours before the next surfacing
const OFF_SEABED_M = 30, AFTER_LAST_H = 24;
export const FLOAT_COLOUR = '#0f8b8d';

// [[wmo, lat, lon, epoch_ms], ...] (as /api/argo/profiles) -> {wmo: {time_ms, lat, lon}}, each sorted by time.
export function byFloat(profiles) {
    const out = {};
    for (const [wmo, lat, lon, ms] of [...profiles].sort((a, b) => a[3] - b[3])) { const f = out[wmo] ||= { time_ms: [], lat: [], lon: [] }; f.time_ms.push(ms); f.lat.push(lat); f.lon.push(lon); }
    return out;
}

// `float`: {time_ms, lat, lon}, one entry per surfacing.
export function cycleTrack(world, float) {
    const track = { lon: [], lat: [], z: [], time_ms: [] }, H = 3600e3;
    const add = (ms, lat, lon, depth) => { track.time_ms.push(ms); track.lat.push(lat); track.lon.push(lon); track.z.push(-Math.min(depth, Math.max(0, -world.depthAt(lon, lat) - OFF_SEABED_M))); };
    for (let q = 0; q < float.time_ms.length; q++) {
        const t0 = float.time_ms[q], last = q === float.time_ms.length - 1, T = last ? AFTER_LAST_H : (float.time_ms[q + 1] - t0) / H;
        const need = CYCLE.surfaceH + CYCLE.descendH + CYCLE.deepStartH, k = Math.min(1, T / need);      // a short cycle is squeezed to fit
        const keys = last ? [[0, 0], [CYCLE.surfaceH, 0], [CYCLE.surfaceH + CYCLE.descendH, CYCLE.parkM], [T, CYCLE.parkM]]
            : [[0, 0], [CYCLE.surfaceH * k, 0], [(CYCLE.surfaceH + CYCLE.descendH) * k, CYCLE.parkM], [T - CYCLE.deepStartH * k, CYCLE.parkM], [T - CYCLE.deepEndH * k, CYCLE.profileM]];
        for (const [h, depth] of keys) {
            const f = last ? 0 : h / T;      // drifting from this surfacing to the next
            add(t0 + h * H, float.lat[q] + ((float.lat[q + 1] ?? float.lat[q]) - float.lat[q]) * f, float.lon[q] + ((float.lon[q + 1] ?? float.lon[q]) - float.lon[q]) * f, depth);
        }
    }
    return track;
}
