// The low-poly vehicle meshes in static/3d_view/models ({part: {x, y, z, i, j, k, color}}, metres, +x nose, +z up) as
// three.js objects. A model is built once and posed by moving it, never by re-uploading vertices.
import * as THREE from 'three';

// Drawn size (longest side) as a fraction of the box width: nothing would be visible at true scale.
const SIZES = { slocum: 0.011, seaglider: 0.011, seaexplorer: 0.011, spray: 0.011, alr: 0.019, argo: 0.008, ship: 0.0325, buoy: 0.014 };
const TURN_S = 0.35;      // wall-clock seconds for a vehicle to swing most of the way to a new attitude
const cache = {};
const fetchParts = name => cache[name] ||= fetch(`/static/3d_view/models/${name}.json`).then(r => { if (!r.ok) throw new Error(`model ${name}`); return r.json(); });

export async function loadModel(name, world, sizeKey = name) {
    const parts = await fetchParts(name), body = new THREE.Group();
    let x0 = Infinity, x1 = -Infinity, bottom = Infinity, top = -Infinity;
    for (const p of Object.values(parts)) {
        const pos = new Float32Array(p.x.length * 3), index = [];
        for (let q = 0; q < p.x.length; q++) { pos.set([p.x[q], p.z[q], -p.y[q]], q * 3); x0 = Math.min(x0, p.x[q]); x1 = Math.max(x1, p.x[q]); bottom = Math.min(bottom, p.z[q]); top = Math.max(top, p.z[q]); }      // model z-up -> scene y-up
        for (let q = 0; q < p.i.length; q++) index.push(p.i[q], p.j[q], p.k[q]);
        const geom = new THREE.BufferGeometry();
        geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        geom.setIndex(index);
        body.add(new THREE.Mesh(geom, new THREE.MeshLambertMaterial({ color: p.color, flatShading: true, side: THREE.DoubleSide })));
    }
    const scale = (SIZES[sizeKey] || SIZES.slocum) * world.size.x / Math.max(x1 - x0, top - bottom),      // its longest side: upright things (floats, buoys) are taller than they are long
         span = a => Math.max(...a) - Math.min(...a);
    body.scale.setScalar(scale);
    // Model metres: for cargo carried on a ship's deck. `length`: hull nose to tail. `deck`: a ship's working-deck height.
    const deck = parts.deck || parts.aft, dims = { length: parts.hull ? span(parts.hull.x) : x1 - x0, hullLength: x1 - x0, height: top - bottom, bottom, deck: deck ? Math.min(...deck.z) : 0 };
    const object = new THREE.Group();
    object.add(body); object.visible = false;
    // `smooth`: ease towards the new attitude (playback) instead of snapping to it (scrubbing, jumps).
    const target = new THREE.Quaternion(), euler = new THREE.Euler(0, 0, 0, 'YZX');      // heading about y, then pitch about the body's own z
    let last = 0;
    const pose = (p, smooth) => {
        const shown = object.visible; object.visible = !!p;
        if (!p) return;
        object.position.fromArray(p.position); body.scale.setScalar(p.scale || scale);      // `scale`: drawn at another size (aboard a ship)
        if (p.quaternion) { object.quaternion.copy(p.quaternion); return; }      // fixed to something else (a ship's deck): its attitude exactly, no easing of its own
        target.setFromEuler(euler.set(0, p.heading, p.pitch));
        const ts = performance.now(), dt = (ts - last) / 1000; last = ts;
        if (smooth && shown && dt < 0.25) object.quaternion.slerp(target, 1 - Math.exp(-dt / TURN_S)); else object.quaternion.copy(target);
    };
    return { object, pose, dims, scale };
}
