// Decorative life: the occasional animal swimming through. Placed from the species table and animated entirely on
// the GPU — one instanced mesh per species, a handful of numbers per animal, and a vertex shader that works out where
// each one is from the clock; per frame the CPU sets one uniform. Every animal crosses the scene along its own path —
// some hug a depth contour round the land and banks, some meander, some go straight — in at one edge and out at
// another (cut off cleanly by the box), riding up over the seabed; then it stays away for
// a while before its next pass. Paths are worked out once, as evenly spaced points in a texture the shader reads.
import * as THREE from 'three';
import { RULES, EATS } from './species.js';

const BUNDLE_URL = '/static/3d_view/scenery/_bundle.json';
const CAST = 44;               // animals (a school counts as one) picked for a scene…
const AWAY = 1.0;              // …each spending this many crossing-times out of sight between passes: about CAST / (1 + AWAY) on stage at once
const SCHOOL = [6, 12];        // fish in a school
const SHOAL = [4, 9];          // squid in a shoal (species marked `shoal`)
const POD = [3, 7], POD_CHANCE = 0.6;      // dolphins in a pod, and how often dolphins come as one rather than alone
const IDLE_S = 10;             // paused and untouched for this long: the scene holds still (and the GPU rests)
const HUNTS = 8, HUNT_CHANCE = 0.9, HUNT_AWAY = 0.4;      // HUNT_AWAY: a chase is back sooner than a lone passer-by (x AWAY);      // at most this many chases in a scene (species.js EATS says who eats whom), and how often a hunter that could, does
// Big animals never swim through each other: they all keep one beat (BIG_CYCLE s, or twice it), so two whose paths
// cross always reach the crossing at the same moments of the beat — and each newcomer's is picked to miss the others.
const BIG = 0.015, BIG_CYCLE = 240;      // scene units long (a 3.5 m shark); seconds
const PATH_POINTS = 64;       // points a path is stored as (the shader runs a smooth curve through them)
const STYLES = [['contour', 0.45], ['meander', 0.35], ['straight', 0.2]];      // how a swimmer crosses the scene, and how likely each is
const EDGE_BAND = 0.06;        // how near a side of the box counts as "at the edge" (fraction of its width)
const OVERSHOOT = 0.09;        // lanes run this far past the box (fraction of its width): swimmers leave whole, cut off cleanly by the edge
// Seconds to cross the scene, and how the body moves: `swing` is how far the tail goes as a fraction of body length
// (> 0 side to side, fish; < 0 up and down, whales), `beat` how often (Hz). Only the rear half moves; big animals move
// slowly and barely. `pulse`: a jelly's bell; `jet` (Hz): a squid's — the mantle squeezes, the arms stream together, it surges.
const MOTION = {
    whale: { cross: 80, swing: -0.025, beat: 0.22, bob: 0.003 }, dolphin: { cross: 28, swing: -0.035, beat: 0.9, bob: 0.008 }, shark: { cross: 50, swing: 0.055, beat: 0.6, bob: 0.003 },
    school: { cross: 40, swing: 0.05, beat: 1.4, bob: 0.003 }, mola: { cross: 140, swing: 0.02, beat: 0.4, bob: 0.003 }, ray: { cross: 60, swing: -0.03, beat: 0.5, bob: 0.004 },
    turtle: { cross: 85, swing: -0.015, beat: 0.5, bob: 0.003 }, eel: { cross: 60, swing: 0.07, beat: 0.8, bob: 0.003 }, squid: { cross: 50, swing: 0, jet: 0.45, bob: 0.004 },
    octopus: { cross: 120, swing: 0.02, beat: 0.5, bob: 0.004 }, jelly: { cross: 260, swing: 0, bob: 0.006, pulse: 0.14 },
};

let bundle = null;
const loadBundle = () => bundle ||= fetch(BUNDLE_URL).then(r => { if (!r.ok) throw new Error('scenery models'); return r.json(); });
const rng = seed => () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };

// One species' parts as a single geometry with vertex colours, z-up -> y-up, scaled so its longest side is 1.
function geometryFor(parts) {
    const pos = [], col = [], index = [], c = new THREE.Color();
    let lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
    for (const p of Object.values(parts)) {
        const base = pos.length / 3; c.set(p.color);
        for (let q = 0; q < p.x.length; q++) { const v = [p.x[q], p.z[q], -p.y[q]]; pos.push(...v); col.push(c.r, c.g, c.b); v.forEach((w, k) => { lo[k] = Math.min(lo[k], w); hi[k] = Math.max(hi[k], w); }); }
        for (let q = 0; q < p.i.length; q++) index.push(base + p.i[q], base + p.j[q], base + p.k[q]);
    }
    const k = 1 / Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]), geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pos.map(v => v * k)), 3));
    geom.setAttribute('color', new THREE.BufferAttribute(new Float32Array(col), 3));
    geom.setIndex(index);
    return geom;
}

function material(shared, size) {
    const edge = (x, z, c) => new THREE.Plane(new THREE.Vector3(x, 0, z), c);      // nothing is drawn outside the box
    const mat = new THREE.MeshLambertMaterial({ vertexColors: true, flatShading: true, side: THREE.DoubleSide,
        clippingPlanes: [edge(1, 0, size.x / 2), edge(-1, 0, size.x / 2), edge(0, 1, size.z / 2), edge(0, -1, size.z / 2)] });
    mat.onBeforeCompile = sh => {
        Object.assign(sh.uniforms, shared);
        sh.vertexShader = `attribute vec3 aHunt;      // a chase: x > 0 the hunter: (gap behind at the start, where along the path it reaches the school's middle, how far ahead of that it ends up — it goes right through); x = -1 prey that is caught, gone from y on; x = -2 prey that scatters sideways round y
            attribute vec4 aLane, aMove, aBody;      // (path row, x offset, z offset, depth wander) | (phase, 1 / cycle s, y, size) | (time away per crossing, bob, swing, beat Hz or pulse)
            uniform float uTime, uVertical; uniform sampler2D uPaths;
            vec3 node(int i, int row) { return texelFetch(uPaths, ivec2(clamp(i, 0, ${PATH_POINTS - 1}), row), 0).xyz; }      // (x, z, the seabed to clear there)
            ` + sh.vertexShader.replace('#include <begin_vertex>', `
            float size = aMove.w, along = fract(uTime * aMove.y + aMove.x) * (1.0 + aBody.x);      // past 1: away until the next pass
            if (aHunt.x > 0.0) along = max(0.0, along - max(-aHunt.z, aHunt.x * (1.0 - along / aHunt.y)));      // closing in
            float f = min(along, 1.0) * ${(PATH_POINTS - 1).toFixed(1)}, u = fract(f); int k = int(f), row = int(aLane.x);      // Catmull-Rom through the path's points
            vec3 a3 = node(k - 1, row), b3 = node(k, row), c3 = node(k + 1, row), d3 = node(k + 2, row); vec2 a = a3.xy, b = b3.xy, c = c3.xy, d = d3.xy;
            vec2 at = aLane.yz * (aHunt.x < -1.5 ? 1.0 + 1.6 * smoothstep(aHunt.y - 0.06, aHunt.y + 0.03, along) : 1.0) + 0.5 * (2.0 * b + (c - a) * u + (2.0 * a - 5.0 * b + 4.0 * c - d) * u * u + (3.0 * b - 3.0 * c + d - a) * u * u * u);
            vec2 dir = normalize((c - a) + 2.0 * (2.0 * a - 5.0 * b + 4.0 * c - d) * u + 3.0 * (3.0 * b - 3.0 * c + d - a) * u * u + vec2(1e-9, 0.0));
            vec3 body = position;
            if (aBody.z != 0.0) {      // the rear half swings, the head holds steady
                float swing = sin(uTime * 6.2832 * aBody.w + aMove.x * 50.0 + body.x * 2.5) * smoothstep(0.05, -0.5, body.x) * abs(aBody.z);
                if (aBody.z > 0.0) body.z += swing; else body.y += swing;
            } else if (aBody.w > 0.0) { float pulse = 1.0 + aBody.w * sin(uTime * 2.2 + aMove.x * 60.0); body.xz *= pulse; body.y /= pulse; }      // jellies
            else if (aBody.w < 0.0) {      // squid: a quick squeeze of the mantle, a glide, and again
                float beat = fract(uTime * -aBody.w + aMove.x * 60.0), jet = smoothstep(0.0, 0.12, beat) * (1.0 - smoothstep(0.12, 0.6, beat)), arms = 1.0 - smoothstep(-0.4, -0.1, body.x);
                body.yz *= 1.0 - jet * mix(0.12, 0.4, arms);      // the arms close up behind it
                body.x += jet * (0.05 - arms * 0.08 * -body.x);
            }
            float wander = aLane.w * sin(along * (9.0 + fract(aMove.x * 7.0) * 9.0) + aMove.x * 40.0);      // slowly up and down through its depth band as it crosses
            // The seabed to clear comes with the path, already smoothed along it: read live, every ridge (drawn ~100x too tall) snapped the animal up and down.
            float ground = mix(b3.z, c3.z, u * u * (3.0 - 2.0 * u));
            float y = min(max(aMove.z + wander + sin(uTime * 0.6 + aMove.x * 70.0) * aBody.y, ground + size * 0.7), aMove.z > -1e-5 ? 0.0 : -size * 0.3);
            // Nose up on the way up, down on the way down: the slope of whichever it is following, its own wander or the rise of the seabed.
            float free = aMove.z + wander, rise = free > ground + size * 0.7 ? aLane.w * cos(along * (9.0 + fract(aMove.x * 7.0) * 9.0) + aMove.x * 40.0) * (9.0 + fract(aMove.x * 7.0) * 9.0)
                : (c3.z - b3.z) * 6.0 * u * (1.0 - u) * ${(PATH_POINTS - 1).toFixed(1)};
            if (y >= -size * 0.3 - 1e-6 || (aBody.z == 0.0 && aBody.w > 0.0)) rise = 0.0;      // held at the surface; jellies stay upright
            float tilt = clamp(atan(0.5 * rise / max(${(PATH_POINTS - 1).toFixed(1)} * length(c - b), 1e-6)), -0.5, 0.5);      // depth is drawn stretched, so half the drawn slope, and never more than ~30°
            body.xy = vec2(body.x * cos(tilt) - body.y * sin(tilt), body.x * sin(tilt) + body.y * cos(tilt));
            body *= along > 1.0 || (aHunt.x < 0.0 && aHunt.x > -1.5 && along > aHunt.y) ? 0.0 : size;      // away, or eaten
            body.y /= uVertical;      // the scene may be squashed to true height: the animals are not
            vec3 transformed = vec3(at.x + body.x * dir.x - body.z * dir.y, y + body.y, at.y + body.x * dir.y + body.z * dir.x);`);
    };
    return mat;
}

// `world` from createWorld. Resolves to { object, tick(playing) -> wants another frame, setVertical(v) }.
export async function buildScenery(world) {
    const models = await loadBundle(), { size, lat0, lon0 } = world, random = rng(Math.round((lat0 * 1000 + lon0) * 7919));
    const shared = { uTime: { value: 0 }, uVertical: { value: 1 }, uPaths: { value: null } };
    const mat = material(shared, size), object = new THREE.Group();
    const here = r => (r.region ? r.region.some(([a, b, c, d]) => lon0 >= a && lon0 <= b && lat0 >= c && lat0 <= d) : !r.lat || (Math.abs(lat0) >= r.lat[0] && Math.abs(lat0) <= r.lat[1]));
    const gauss = () => (random() + random() + random() - 1.5) * 1.15;

    // A lane for a swimmer of length `len` at scene depth `y`: a straight line right across the box, edge to edge (and
    // on past both), over water deep enough all the way. Swimmers only ever come and go at the edges of the scene.
    const lane = (y, len) => {
        for (let tries = 0; tries < 40; tries++) {
            const px = (random() - 0.5) * size.x, pz = (random() - 0.5) * size.z, a = random() * Math.PI * 2, d = [Math.cos(a), Math.sin(a)];
            const reach = sign => Math.min(...[[size.x / 2, px, d[0] * sign], [size.z / 2, pz, d[1] * sign]].map(([half, p, v]) => Math.abs(v) < 1e-9 ? Infinity : (v > 0 ? half - p : half + p) / Math.abs(v)));      // how far to the box's edge, that way
            const back = reach(-1), fwd = reach(1);
            let clear = back + fwd > size.x * 0.5;
            for (let k = -back + 0.005; clear && k < fwd; k += 0.01) { const g = world.heightAt(px + d[0] * k, pz + d[1] * k); clear = g != null && g < Math.min(y * 0.4, -len * 0.8); }
            if (clear) { const o = OVERSHOOT * size.x; return [[px - d[0] * (back + o), pz - d[1] * (back + o)], [px + d[0] * (fwd + o), pz + d[1] * (fwd + o)]]; }
        }
        return null;
    };
    const inside = (x, z) => Math.abs(x) <= size.x / 2 && Math.abs(z) <= size.z / 2;
    const deepEnough = (x, z, y, len) => { const g = world.heightAt(x, z); return g != null && g < Math.min(y * 0.4, -len * 0.8); };
    // A lane bent into a few lazy S-curves; the ends stay where they were, outside the box.
    const meander = (y, len) => {
        for (let tries = 0; tries < 8; tries++) {
            const l = lane(y, len); if (!l) return null;
            const [[x0, z0], [x1, z1]] = l, L = Math.hypot(x1 - x0, z1 - z0), n = [-(z1 - z0) / L, (x1 - x0) / L], waves = 1 + random() * 2, amp = size.x * (0.04 + random() * 0.08) * (random() < 0.5 ? -1 : 1), pts = [];
            for (let q = 0; q <= 80; q++) { const t = q / 80, w = amp * Math.sin(t * Math.PI * waves * 2) * Math.sin(t * Math.PI); pts.push([x0 + (x1 - x0) * t + n[0] * w, z0 + (z1 - z0) * t + n[1] * w]); }
            if (pts.every(([x, z]) => !inside(x, z) || deepEnough(x, z, y, len))) return pts;
        }
        return null;
    };
    // Along a depth contour: in at an edge, then keeping the seabed at the depth it was there — round headlands, along
    // shelf breaks and banks — until it runs out of the box. Shallower starts are preferred: those hug the land.
    const contour = (y, len) => {
        const step = size.x * 0.008, eps = size.x * 0.012, o = OVERSHOOT * size.x, h = (x, z) => world.heightAt(Math.max(-size.x / 2, Math.min(size.x / 2, x)), Math.max(-size.z / 2, Math.min(size.z / 2, z)));
        for (let tries = 0; tries < 30; tries++) {
            let best = null;
            for (let q = 0; q < 6; q++) {      // a few spots round the edge; the shallowest that will do
                const e = Math.floor(random() * 4), t = random() - 0.5, x = e < 2 ? (e ? 0.499 : -0.499) * size.x : t * size.x, z = e < 2 ? t * size.z : (e === 2 ? 0.499 : -0.499) * size.z;
                if (deepEnough(x, z, y, len) && (!best || h(x, z) > best.level)) best = { x, z, level: h(x, z), inward: e < 2 ? [e ? -1 : 1, 0] : [0, e === 2 ? -1 : 1] };
            }
            if (!best) continue;
            let { x, z } = best, dir = null; const pts = []; let hand = random() < 0.5 ? 1 : -1;
            for (let q = 0; q < 500 && inside(x, z); q++) {
                pts.push([x, z]);
                const gx = (h(x + eps, z) - h(x - eps, z)) / (2 * eps), gz = (h(x, z + eps) - h(x, z - eps)) / (2 * eps), g = Math.hypot(gx, gz);
                let want = dir || best.inward;
                if (g > 1e-6) {      // along the slope, leaning back towards the contour when it drifts off it
                    const pull = Math.max(-1, Math.min(1, (best.level - h(x, z)) / (g * step * 4)));
                    const along = k => [-gz / g * k + gx / g * pull, gx / g * k + gz / g * pull];
                    if (!dir && along(hand)[0] * best.inward[0] + along(hand)[1] * best.inward[1] < 0) hand = -hand;      // the way that leads into the box
                    want = along(hand);
                }
                const mix = dir ? [dir[0] * 0.88 + want[0] * 0.12, dir[1] * 0.88 + want[1] * 0.12] : want, m = Math.hypot(mix[0], mix[1]) || 1;
                dir = [mix[0] / m, mix[1] / m]; x += dir[0] * step; z += dir[1] * step;
            }
            if (inside(x, z) || pts.length < 60 || !pts.every(p => deepEnough(p[0], p[1], y, len))) continue;      // went round in circles, barely clipped a corner, or ran aground
            const first = pts[0], d0 = [pts[3][0] - first[0], pts[3][1] - first[1]], m0 = Math.hypot(d0[0], d0[1]) || 1;
            return [[first[0] - d0[0] / m0 * o, first[1] - d0[1] / m0 * o], ...pts, [x + dir[0] * o, z + dir[1] * o]];
        }
        return null;
    };
    // PATH_POINTS points evenly spaced along `pts`, so a swimmer keeps one speed; and the path's length.
    const smooth = raw => {
        let pts = raw;      // rounded off: a path's points are further apart than a contour's wiggles, which would turn a swimmer's heading to and fro
        for (let pass = 0; pass < 6 && raw.length > 12; pass++) pts = pts.map((p, q) => { const a = pts[Math.max(0, q - 3)], b = pts[Math.min(pts.length - 1, q + 3)]; return q < 2 || q > pts.length - 3 ? p : [(a[0] + 2 * p[0] + b[0]) / 4, (a[1] + 2 * p[1] + b[1]) / 4]; });
        return pts;
    };
    const resample = pts => {
        const run = [0]; for (let q = 1; q < pts.length; q++) run.push(run[q - 1] + Math.hypot(pts[q][0] - pts[q - 1][0], pts[q][1] - pts[q - 1][1]));
        const out = [], total = run[run.length - 1];
        for (let q = 0, k = 0; q < PATH_POINTS; q++) {
            const s = total * q / (PATH_POINTS - 1); while (k < pts.length - 2 && run[k + 1] < s) k++;
            const t = (s - run[k]) / Math.max(run[k + 1] - run[k], 1e-9); out.push(pts[k][0] + (pts[k + 1][0] - pts[k][0]) * t, pts[k][1] + (pts[k + 1][1] - pts[k][1]) * t, 0, 0);
        }
        // Each point's seabed: the highest within reach of a school's spread, widened to its neighbours, then rounded off — a long ramp over a bank, not a step.
        const reach = size.x * 0.04, n = PATH_POINTS, tops = [];
        for (let q = 0; q < n; q++) {
            let top = -size.y;
            for (let k = 0; k < 9; k++) { const a = k * Math.PI / 4, r = k === 8 ? 0 : reach, g = world.heightAt(out[q * 4] + Math.cos(a) * r, out[q * 4 + 1] + Math.sin(a) * r); if (g != null && g > top) top = g; }
            tops.push(top);
        }
        let floor = tops.map((_, q) => Math.max(...tops.slice(Math.max(0, q - 2), q + 3)));
        for (let pass = 0; pass < 4; pass++) floor = floor.map((v, q) => (floor[Math.max(0, q - 1)] + 2 * v + floor[Math.min(n - 1, q + 1)]) / 4);
        for (let q = 0; q < n; q++) out[q * 4 + 2] = Math.max(floor[q], tops[q]);
        return { points: out, length: total };
    };
    const pickStyle = () => { let r = random(); for (const [name, share] of STYLES) if ((r -= share) < 0) return name; return 'straight'; };
    // Swimmers come in or go out; they don't hang about half-clipped. A route may spend only so long within EDGE_BAND of
    // the box's sides — enough to cross it twice at 30° or steeper — so ones that run along an edge, or dip in and out, are dropped.
    const lingers = pts => {
        const band = EDGE_BAND * size.x, mids = pts.slice(1).map((p, q) => [(p[0] + pts[q][0]) / 2, (p[1] + pts[q][1]) / 2, Math.hypot(p[0] - pts[q][0], p[1] - pts[q][1])]);
        const depthIn = ([x, z]) => Math.min(size.x / 2 - Math.abs(x), size.z / 2 - Math.abs(z));      // how far inside the box (< 0: outside)
        let near = 0, crossings = 0, turns = 0;
        mids.forEach((m, q) => {
            const d = depthIn(m), before = q ? depthIn(mids[q - 1]) : d;
            if (d >= 0 && d < band) near += m[2];
            if ((d >= 0) !== (before >= 0)) crossings++;          // over the box's edge
            if ((d >= band) !== (before >= band)) turns++;        // into or out of the middle
        });
        return near > band * 3.2 || crossings !== 2 || turns !== 2;      // once in, once out — and no coming back to the edge in between
    };
    const dense = pts => (pts.length > 2 ? pts : Array.from({ length: 41 }, (_, q) => [pts[0][0] + (pts[1][0] - pts[0][0]) * q / 40, pts[0][1] + (pts[1][1] - pts[0][1]) * q / 40]));
    const route = (y, len) => {
        const style = pickStyle();
        for (let tries = 0; tries < 12; tries++) {
            const pts = (style === 'contour' && tries < 6 && contour(y, len)) || (style !== 'straight' && tries < 9 && meander(y, len)) || lane(y, len);
            const path = pts && smooth(dense(pts));      // judged as it will be swum
            if (path && !lingers(path)) return resample(path);
        }
        return null;
    };

    // The cast: species that live here, shuffled, at most one entry per group before any group gets a second.
    const local = RULES.filter(r => models[r.model] && MOTION[r.group] && here(r)).sort(() => random() - 0.5), cast = [], turn = {};
    const SHARE = { squid: 2 };      // groups that get this many entries per round
    for (let round = 0; cast.length < CAST && round < 6; round++) for (const r of local) if (cast.length < CAST && !cast.includes(r) && (turn[r.group] || 0) < (round + 1) * (SHARE[r.group] || 1)) { cast.push(r); turn[r.group] = (turn[r.group] || 0) + 1; }
    let count = 0; const paths = [];
    // One species on stage: alone, or as a school / pod / shoal. `shared`: a chase's two halves use the same path, depth
    // and timing; `hunt`: 'hunter' | 'prey'.
    const bandOf = r => (r.surface ? [0, 0] : r.float);
    const bigOnes = [];
    const plan = (bandM, len, cross, hunting) => {
        const band = (bandM[1] - bandM[0]) * world.ky, wander = band * (0.15 + random() * 0.25);      // it ranges over 30–80 % of the depths it lives at
        const y = -bandM[0] * world.ky - wander - random() * (band - 2 * wander), l = route(y, len);
        if (!l) return null;
        let away = AWAY * (0.6 + random() * 0.8) * (hunting ? HUNT_AWAY : 1), cycle = cross * l.length / size.x * (0.85 + random() * 0.3) * (1 + away), phase = random();
        if (len >= BIG) {
            const crossing = cycle / (1 + away); cycle = crossing * 1.25 <= BIG_CYCLE ? BIG_CYCLE : BIG_CYCLE * 2; away = cycle / crossing - 1;
            const me = { points: l.points, y, reach: wander + len, len, cycle, away }, spot = (a, t, ph) => {      // where it is at time t: [x, z] or null while away
                const along = ((t / a.cycle + ph) % 1) * (1 + a.away); if (along > 1) return null;
                const f = along * (PATH_POINTS - 1), k = Math.min(PATH_POINTS - 2, Math.floor(f)), u = f - k;
                return [a.points[k * 4] + (a.points[k * 4 + 4] - a.points[k * 4]) * u, a.points[k * 4 + 1] + (a.points[k * 4 + 5] - a.points[k * 4 + 1]) * u];
            };
            const rivals = bigOnes.filter(b => Math.abs(b.y - y) < b.reach + me.reach);      // depths that can meet
            const meets = ph => { for (let t = 0; t < BIG_CYCLE * 2; t += 1.5) { const a = spot(me, t, ph); if (a) for (const b of rivals) { const c = spot(b, t, b.phase); if (c && Math.hypot(a[0] - c[0], a[1] - c[1]) < (len + b.len) * 0.75) return true; } } return false; };
            let tries = 0; while (meets(phase) && ++tries < 24) phase = random();
            if (tries === 24) return null;      // no room for it
            bigOnes.push({ ...me, phase });
        }
        return { y, wander, row: paths.push(l.points) - 1, away, cycle, phase, gap: 0.25 + random() * 0.15, where: 0.45 + random() * 0.25 };
    };
    const spawn = (rule, p, hunt) => {
        const motion = MOTION[rule.group], pod = (rule.group === 'dolphin' || rule.shoal) && random() < POD_CHANCE, school = hunt !== 'hunter' && (rule.group === 'school' || pod), rows = [];
        const [few, many] = rule.shoal ? SHOAL : pod ? POD : SCHOOL, spread = pod ? 0.03 : 0.02, n = school ? few + Math.floor(random() * (many - few + 1)) : 1;
        // Prey: a lone animal is caught. In a school the hunter goes through the middle from the back: the fish in its way
        // (nearest the path's line, about 40 %; one of a pod) go one by one as it reaches each, the rest scatter sideways.
        const fish = Array.from({ length: n }, (_, q) => ({ off: school ? [gauss() * spread, gauss() * spread] : [0, 0], ahead: school ? (q * 0.002 + random() * (pod ? 0.012 : 0.006)) : 0 }));
        if (hunt === 'prey') [...fish].sort((a, b) => Math.hypot(...a.off) - Math.hypot(...b.off)).slice(0, n === 1 ? 1 : pod ? 1 : Math.ceil(n * 0.4)).forEach(f => { f.eaten = true; f.off = f.off.map(v => v * 0.4); });
        if (hunt === 'prey') p.lead = Math.max(...fish.map(f => f.ahead)) * (1 + p.away) + 0.01;
        for (const f of fish) {
            const a = f.ahead * (1 + p.away), caught = p.where * (1 + a / p.gap) + a;      // where along its own way the hunter draws level with this fish
            rows.push({ lane: [p.row, f.off[0], f.off[1], p.wander], move: [p.phase + f.ahead, 1 / p.cycle, p.y + (school ? gauss() * (f.eaten ? 0.002 : 0.006) : 0), rule.size * (0.85 + random() * 0.3)],
                        body: [p.away, motion.bob, motion.swing, motion.swing ? motion.beat * (0.85 + random() * 0.3) : motion.jet ? -motion.jet * (0.8 + random() * 0.4) : motion.pulse || 0],
                        hunt: hunt === 'hunter' ? [p.gap, p.where, p.lead || 0.01] : f.eaten ? [-1, caught, 0] : hunt === 'prey' ? [-2, caught, 0] : [0, 0, 0] });
        }
        const geom = geometryFor(models[rule.model]);
        for (const [name, key, width] of [['aLane', 'lane', 4], ['aMove', 'move', 4], ['aBody', 'body', 4], ['aHunt', 'hunt', 3]]) geom.setAttribute(name, new THREE.InstancedBufferAttribute(new Float32Array(rows.flatMap(r => r[key])), width));
        const mesh = new THREE.InstancedMesh(geom, mat, rows.length);
        mesh.frustumCulled = false;      // the shader moves them: their bounds mean nothing
        object.add(mesh); count++;
    };
    // Chases: a hunter in the cast whose prey (EATS: model names, or 'group:<group>') lives here too, at depths they share.
    const preyOf = r => (EATS[r.model] || []).flatMap(name => local.filter(v => v !== r && !v.surface && (name === 'group:' + v.group || name === v.model)));
    let hunts = 0;
    for (const rule of cast) {
        const prey = hunts < HUNTS && !rule.surface && random() < HUNT_CHANCE ? preyOf(rule).filter(v => Math.max(v.float[0], rule.float[0]) < Math.min(v.float[1], rule.float[1])) : [];
        const quarry = prey[Math.floor(random() * prey.length)];
        const p = quarry ? plan([Math.max(quarry.float[0], rule.float[0]), Math.min(quarry.float[1], rule.float[1])], Math.max(rule.size, quarry.size), Math.max(MOTION[rule.group].cross, MOTION[quarry.group].cross * 0.5), true)
            : plan(bandOf(rule), rule.size, MOTION[rule.group].cross);
        if (!p) continue;
        if (quarry) { hunts++; spawn(quarry, p, 'prey'); }
        spawn(rule, p, quarry ? 'hunter' : null);
    }

    const pathTex = new THREE.DataTexture(new Float32Array(paths.length ? paths.flat() : PATH_POINTS * 4), PATH_POINTS, Math.max(1, paths.length), THREE.RGBAFormat, THREE.FloatType); pathTex.needsUpdate = true;
    shared.uPaths.value = pathTex;
    let lastInput = performance.now();
    for (const ev of ['pointerdown', 'pointermove', 'wheel', 'keydown']) window.addEventListener(ev, () => { lastInput = performance.now(); }, { passive: true });
    const tick = playing => { shared.uTime.value = performance.now() / 1000; return object.visible && (playing || performance.now() - lastInput < IDLE_S * 1000); };
    return { object, tick, setVertical: v => { shared.uVertical.value = v; }, count };
}
