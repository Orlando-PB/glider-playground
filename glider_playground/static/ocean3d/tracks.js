// One platform's track as a thick solid line, uploaded once and never touched again: playback only moves the vehicle
// along it. So a vehicle is never buried in a zig-zag (its own or a neighbour's), every line turns see-through where it
// passes between the camera and the water column a vehicle is in (the whole column, so the clearing stays put while the vehicle dives) — done in the line's shader. Fixes outside the box (bad GPS, 0/0) are dropped.
import * as THREE from 'three';

const WIDTH = 3;               // px
const CLEARING = { radius: 0.02, opacity: 0.12 };      // round each vehicle as seen from the camera: fraction of the box width, and how faint the line gets
const MAX_VEHICLES = 8;
const NO_DATA = new THREE.Color('#9aa5ad');
const HEADING_FIXES = 6;       // heading: the horizontal run over this many fixes either side (a dive cycle or so), so it doesn't twitch
const PITCH_FIXES = 1;         // pitch: the vehicle's own sensor, averaged over this many fixes either side
const FALLBACK_PITCH = 25;     // degrees nose up/down while climbing/diving, where the file has no pitch
const PITCH_GAIN = 2, PITCH_MAX = 70;      // measured pitch is drawn steeper, towards the stretched path's slope, but never standing on end

// Where every vehicle is (world space; far outside the scene = none), and the same on screen for the lines' shader:
// (x, y in clip space, the clearing's radius there, distance from the camera). projectClearings() fills the second
// from the first once per frame, so the shader has nothing to work out per pixel.
const AWAY = 1e3, vehicles = Array.from({ length: MAX_VEHICLES }, () => new THREE.Vector3(AWAY, AWAY, AWAY));
const onScreen = { value: Array.from({ length: MAX_VEHICLES }, () => new THREE.Vector4(0, 0, 0, -1)) }, radius = { value: 0 };
// The clearing is the whole water column the vehicle is in, not a spot that rides up and down with every dive: the
// same again for the foot of the column (onScreen is its top, at the surface).
const onScreenFoot = { value: Array.from({ length: MAX_VEHICLES }, () => new THREE.Vector4(0, 0, 0, -1)) };
let columnDepth = 1;
let slots = 0;
const seen = new THREE.Vector3();
export function projectClearings(camera) {
    const focal = camera.projectionMatrix.elements[5];
    vehicles.forEach((v, q) => {
        const top = onScreen.value[q], foot = onScreenFoot.value[q];
        if (v.x === AWAY) { top.w = -1; return; }
        for (const [end, y] of [[top, 0], [foot, -columnDepth]]) {
            const depth = -seen.set(v.x, y, v.z).applyMatrix4(camera.matrixWorldInverse).z;      // distance in front of the camera
            if (depth <= 0) { top.w = -1; return; }
            seen.set(v.x, y, v.z).project(camera);
            end.set(seen.x, seen.y, radius.value * focal / depth, depth);
        }
    });
}

// Opaque, so the dense zig-zags cost one layer of pixels, not hundreds blended; the clearing is a screen-door (a
// dither pattern of dropped pixels) instead of blending. `shown`: the fix indices drawn (uniform, from the time bar's
// clip handles) — each segment carries its two fix indices, and pixels outside the window are dropped.
function lineMaterial(world, colour, width, shown) {
    const mat = new THREE.LineMaterial({ color: colour, linewidth: width });
    radius.value = CLEARING.radius * world.size.x; columnDepth = world.size.y;
    mat.onBeforeCompile = sh => {
        sh.uniforms.uClearings = onScreen; sh.uniforms.uFeet = onScreenFoot; sh.uniforms.uClearing = radius; sh.uniforms.uWindow = shown;
        sh.vertexShader = sh.vertexShader.replace('void main() {', 'varying vec4 vClip; varying float vIdx; attribute vec2 instanceIdx;\nvoid main() {\nvIdx = ( position.y < 0.5 ) ? instanceIdx.x : instanceIdx.y;').replace('gl_Position = clip;', 'gl_Position = clip;\nvClip = clip;');
        sh.fragmentShader = sh.fragmentShader.replace('void main() {', `varying vec4 vClip; varying float vIdx; uniform vec2 uWindow;
            uniform vec4 uClearings[${MAX_VEHICLES}], uFeet[${MAX_VEHICLES}]; uniform float uClearing; uniform vec2 resolution;
            float clearing() {
                float keep = 1.0; vec2 aspect = vec2(resolution.x / resolution.y, 1.0), here = vClip.xy / vClip.w;
                for (int q = 0; q < ${MAX_VEHICLES}; q++) {
                    vec4 top = uClearings[q], foot = uFeet[q];
                    if (top.w <= 0.0) continue;      // no vehicle
                    vec2 column = (foot.xy - top.xy) * aspect, to = (here - top.xy) * aspect;
                    float t = clamp(dot(to, column) / max(dot(column, column), 1e-9), 0.0, 1.0); vec2 v = mix(top.zw, foot.zw, t);      // the nearest point of the column: (radius, distance) there
                    if (vClip.w > v.y + uClearing) continue;      // this bit of line is behind it
                    keep = min(keep, smoothstep(v.x * 0.6, v.x * 1.4, length(to - column * t)));
                }
                return mix(${CLEARING.opacity.toFixed(2)}, 1.0, keep);
            }
            void main() {
                if (vIdx < uWindow.x || vIdx > uWindow.y) discard;`).replace('float alpha = opacity;', `float alpha = opacity;
                ivec2 cell = ivec2(gl_FragCoord.xy) & 3; float door = float((cell.x * 5 + cell.y * 7 + (cell.x ^ cell.y) * 3) & 15) / 16.0 + 0.03;      // 4x4 ordered dither
                if (clearing() < door) discard;`);
    };
    return mat;
}

// `upright`: a float — it hangs vertically whatever it is doing. `width`: line width in px.
export function buildTrack(world, track, colour, { upright = false, width = WIDTH } = {}) {
    const n = track.lon.length, pos = new Float32Array(n * 3), time = new Float64Array(n), pitch = track.pitch && new Float32Array(n), src = new Uint32Array(n);
    let m = 0;
    for (let q = 0; q < n; q++) {
        if (!(Number.isFinite(track.lon[q]) && Number.isFinite(track.lat[q]) && Number.isFinite(track.z[q]) && Number.isFinite(track.time_ms[q]))) continue;      // null or NaN
        const p = world.place(track.lon[q], track.lat[q], track.z[q]);
        if (Math.abs(p[0]) > world.size.x / 2 || Math.abs(p[2]) > world.size.z / 2) continue;
        pos.set(p, m * 3); time[m] = track.time_ms[q]; src[m] = q; if (pitch) pitch[m] = Number.isFinite(track.pitch[q]) ? track.pitch[q] : NaN; m++;
    }
    if (m < 2) return { object: new THREE.Group(), at: () => null, park() {}, setColours() {}, setWindow() {} };
    const own = new THREE.Color(colour || '#12295c'), geometry = new THREE.LineGeometry(), shown = { value: new THREE.Vector2(-1, m) };
    geometry.setPositions(pos.subarray(0, m * 3));
    const idx = new Float32Array((m - 1) * 2);
    for (let q = 0; q < m - 1; q++) { idx[q * 2] = q; idx[q * 2 + 1] = q + 1; }
    geometry.setAttribute('instanceIdx', new THREE.InstancedBufferAttribute(idx, 2));
    const object = new THREE.Line2(geometry, lineMaterial(world, own, width, shown)), spot = upright ? new THREE.Vector3() : vehicles[slots++ % MAX_VEHICLES];      // lines only clear round vehicles, not floats

    // Colour the line by a value per served fix (`values`, aligned with the track as served; `scale(v)` -> THREE.Color),
    // or back to the platform's own colour with no arguments.
    const setColours = (values, scale) => {
        if (values) {
            const tint = new Float32Array(m * 3);
            for (let q = 0; q < m; q++) { const v = values[src[q]]; (v == null ? NO_DATA : scale(v)).toArray(tint, q * 3); }
            geometry.setColors(tint);
        }
        object.material.vertexColors = !!values; object.material.color.set(values ? '#ffffff' : own); object.material.needsUpdate = true;
    };

    // Where the platform is at `now` (world space, under the stage's `vertical` scale) — always a point on the drawn line. Null before its first fix; its last fix once
    // the track has ended. Heading follows the path; pitch is the vehicle's measured pitch (degrees, nose up +): the
    // scene's depth is stretched ~100x, so the drawn line's own slope would stand every glider on its nose.
    let heading = 0;
    const at = (now, vertical = 1) => {
        if (now < time[0]) { spot.set(AWAY, AWAY, AWAY); return null; }
        let lo = 0, hi = m - 1;
        while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (time[mid] <= now) lo = mid; else hi = mid - 1; }
        const next = Math.min(m - 1, lo + 1), f = next > lo ? Math.min(1, (now - time[lo]) / (time[next] - time[lo])) : 0;
        const position = [0, 1, 2].map(k => pos[lo * 3 + k] + (pos[next * 3 + k] - pos[lo * 3 + k]) * f);
        position[1] *= vertical;
        spot.fromArray(position);

        if (upright) return { position, heading: 0, pitch: 0 };
        const a = Math.max(0, lo - HEADING_FIXES), b = Math.min(m - 1, lo + HEADING_FIXES), dx = pos[b * 3] - pos[a * 3], dz = pos[b * 3 + 2] - pos[a * 3 + 2];
        if (Math.hypot(dx, dz) > world.size.x * 1e-5) heading = Math.atan2(-dz, dx);      // about +y, 0 = east; held while drifting on the spot
        let tilt = 0, c = 0;
        if (pitch) for (let q = Math.max(0, lo - PITCH_FIXES); q <= Math.min(m - 1, lo + 1 + PITCH_FIXES); q++) if (!Number.isNaN(pitch[q])) { tilt += pitch[q]; c++; }
        const dy = pos[next * 3 + 1] - pos[lo * 3 + 1], moving = Math.abs(dy) > world.size.y * 1e-4;
        const degrees = c ? tilt / c : moving ? Math.sign(dy) * FALLBACK_PITCH : 0;
        return { position, heading, pitch: (vertical === 1 ? Math.max(-PITCH_MAX, Math.min(PITCH_MAX, degrees * PITCH_GAIN)) : degrees) * Math.PI / 180 };      // true height: true pitch
    };
    const park = () => spot.set(AWAY, AWAY, AWAY);      // hidden: no clearing round where it would be
    // Draw only the fixes between times `a` and `b` (ms; the cut falls part-way along a segment).
    const index = t => {
        if (t < time[0]) return -1;
        if (t >= time[m - 1]) return m;
        let lo = 0, hi = m - 1;
        while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (time[mid] <= t) lo = mid; else hi = mid - 1; }
        return lo + (t - time[lo]) / (time[lo + 1] - time[lo]);
    };
    const setWindow = (a, b) => shown.value.set(index(a), index(b));
    return { object, at, park, setColours, setWindow, span: [time[0], time[m - 1]] };
}
