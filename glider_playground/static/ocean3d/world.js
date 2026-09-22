// The mission's box of ocean: lon/lat/depth -> scene coordinates, plus the seabed, water surface and frame.
// Scene units: the box is 1 wide (east-west); y is up, the sea surface is y = 0.
import * as THREE from 'three';

const DEPTH_SHARE = 0.22;      // how tall the deepest point is drawn, as a fraction of the box width
const LAND_SCALE = 0.12;       // land relief relative to the seabed's vertical scale: texture, not mountains
const SMOOTH = 0;              // seabed smoothing, 0 (raw data, no pass at all) to 1 (each point fully replaced by its 3x3 neighbourhood average)
const CARVE_CLEAR_M = 10;      // where a vehicle went deeper than the charted seabed, the seabed is lowered to this far below it
const BASE_SHARE = 0.03;       // thickness of the block of ground under the deepest point, as a fraction of the box width
const LOOK = { shallow: '#e2d8ba', deep: '#33485a', shore: '#d8cfae', lowland: '#a8bf9a', upland: '#b7ad8b', rock: '#a59f96', snow: '#eef2f5', ground: '#6f7f8c', groundBase: '#56636e', water: '#7fc4e8' };

// Heights as one flat array, the sea floor blended towards its 3x3 (1-2-1) average. Land and the cells beside it are
// left alone so the coastline stays where the data puts it.
function smoothed(zs, nx, ny) {
    const out = new Float32Array(nx * ny);
    if (!SMOOTH) { for (let j = 0; j < ny; j++) out.set(zs[j], j * nx); return out; }
    for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
        let sum = 0, w = 0, wet = true;
        for (let dj = -1; dj <= 1; dj++) for (let di = -1; di <= 1; di++) {
            const z = zs[Math.min(ny - 1, Math.max(0, j + dj))][Math.min(nx - 1, Math.max(0, i + di))], k = (2 - Math.abs(di)) * (2 - Math.abs(dj));
            sum += z * k; w += k; if (z >= 0) wet = false;
        }
        out[j * nx + i] = wet ? zs[j][i] + (sum / w - zs[j][i]) * SMOOTH : zs[j][i];
    }
    return out;
}

export function createWorld(sceneData) {
    const lons = sceneData.bathy_lon, lats = sceneData.bathy_lat;      // the box is the grid's own extent
    const min_lon = lons[0], max_lon = lons[lons.length - 1], min_lat = lats[0], max_lat = lats[lats.length - 1];
    const nx = lons.length, ny = lats.length, heights = smoothed(sceneData.bathy_z, nx, ny);
    const lon0 = (min_lon + max_lon) / 2, lat0 = (min_lat + max_lat) / 2;
    const kx = 1 / (max_lon - min_lon), kz = kx / Math.cos(lat0 * Math.PI / 180);      // equal km per unit both ways
    let deepest = -1;
    for (const z of heights) if (z < deepest) deepest = z;
    const ky = DEPTH_SHARE / -deepest;
    const size = { x: 1, y: DEPTH_SHARE, z: (max_lat - min_lat) * kz };
    const place = (lon, lat, z) => [(lon - lon0) * kx, z * ky * (z > 0 ? LAND_SCALE : 1), -(lat - lat0) * kz];      // grid rows run south to north, scene z runs north to south
    const at = (i, j) => place(lons[i], lats[j], heights[j * nx + i]);      // grid point -> scene
    // Height of the terrain (scene y) under a scene position; null outside the box.
    const yOf = z => z * ky * (z > 0 ? LAND_SCALE : 1);
    const heightAt = (x, zPos) => {
        const fi = (x / size.x + 0.5) * (nx - 1), fj = (0.5 - zPos / size.z) * (ny - 1);
        if (fi < 0 || fj < 0 || fi > nx - 1 || fj > ny - 1) return null;
        const i = Math.min(nx - 2, Math.floor(fi)), j = Math.min(ny - 2, Math.floor(fj)), u = fi - i, v = fj - j, h = (a, b) => yOf(heights[b * nx + a]);
        return (h(i, j) * (1 - u) + h(i + 1, j) * u) * (1 - v) + (h(i, j + 1) * (1 - u) + h(i + 1, j + 1) * u) * v;
    };
    // A vehicle's own depths beat the chart: wherever a measured fix lies under the charted seabed, the four grid
    // points round it are lowered to just below the fix, which opens the water column above it. Returns the grid
    // points changed, for buildSeabed's refresh().
    const carve = (lon, lat, z) => {
        const changed = new Set(), sx = (nx - 1) / (lons[nx - 1] - lons[0]), sy = (ny - 1) / (lats[ny - 1] - lats[0]);
        for (let q = 0; q < z.length; q++) {
            if (!(z[q] < 0)) continue;      // also skips null and NaN
            const fi = (lon[q] - lons[0]) * sx, fj = (lat[q] - lats[0]) * sy;
            if (!(fi >= 0 && fj >= 0 && fi <= nx - 1 && fj <= ny - 1)) continue;
            const i = Math.min(nx - 2, Math.floor(fi)), j = Math.min(ny - 2, Math.floor(fj)), floor = z[q] - CARVE_CLEAR_M;
            for (const k of [j * nx + i, j * nx + i + 1, (j + 1) * nx + i, (j + 1) * nx + i + 1]) if (heights[k] > floor) { heights[k] = floor; changed.add(k); }
        }
        return changed;
    };
    const depthAt = (lon, lat) => { const p = place(lon, lat, 0), y = heightAt(p[0], p[2]); return y == null ? 0 : y / (ky * (y > 0 ? LAND_SCALE : 1)); };      // charted height (m, sea floor negative) at a position
    const sceneHeights = () => Float32Array.from(heights, yOf);      // the grid as scene y, row 0 = south (a height texture for shaders)
    const stretch = ky * (max_lon - min_lon) * 111320 * Math.cos(lat0 * Math.PI / 180);      // vertical exaggeration: scene units per metre, down vs across
    return { size, place, at, heightAt, depthAt, sceneHeights, carve, nx, ny, deepest, ky, kx, kz, lat0, lon0, stretch };
}

// Land bands by height (m): shore sand, then lowland green, upland tan, bare rock. Snow lies above a snowline that
// drops towards the poles — much sooner in the south (Antarctica is ice to the shore at latitudes where Iceland and
// Norway are green), and sooner over Greenland and Arctic Canada than the rest of the north. `lat` is where the
// snowline reaches sea level, `perDeg` how fast it climbs away from there. Where the snowline is low the lowlands
// are tundra, not green.
const LAND_BANDS_M = { shore: 12, upland: 450, rock: 1100 }, SNOW_BLEND_M = 250, TUNDRA_BELOW_M = 700;
const SNOW = { north: { lat: 75, perDeg: 90 }, south: { lat: 62, perDeg: 110 }, greenland: { lat: 66, perDeg: 90, lon: [-75, -27], minLat: 59 } };

// Lit like any Lambert surface, but coloured per pixel from its height (and latitude, for snow): the coast is the smooth
// y = 0 contour of the mesh, exactly where the water plane cuts it, not a staircase of grid cells.
function seabedMaterial(world) {
    const mat = new THREE.MeshLambertMaterial({ side: THREE.DoubleSide });
    const colours = { uShallow: LOOK.shallow, uDeep: LOOK.deep, uShore: LOOK.shore, uLowland: LOOK.lowland, uUpland: LOOK.upland, uRock: LOOK.rock, uSnow: LOOK.snow };
    mat.onBeforeCompile = sh => {
        for (const k in colours) sh.uniforms[k] = { value: new THREE.Color(colours[k]) };
        sh.uniforms.uDeepY = { value: world.deepest * world.ky };
        sh.uniforms.uLandM = { value: 1 / (world.ky * LAND_SCALE) };      // scene y -> metres, on land
        sh.uniforms.uLat = { value: new THREE.Vector2(world.lat0, -1 / world.kz) };      // latitude = x + y * scene z
        sh.uniforms.uLon = { value: new THREE.Vector2(world.lon0, 1 / world.kx) };       // longitude = x + y * scene x
        sh.vertexShader = 'varying vec3 vPos;\n' + sh.vertexShader.replace('#include <begin_vertex>', '#include <begin_vertex>\nvPos = position;');
        const f = v => v.toFixed(1), G = SNOW.greenland;
        sh.fragmentShader = `varying vec3 vPos;
            uniform vec3 uShallow, uDeep, uShore, uLowland, uUpland, uRock, uSnow;
            uniform float uDeepY, uLandM; uniform vec2 uLat, uLon;
            vec3 landColour(float m, float lat, float lon) {
                bool greenland = lat > ${f(G.minLat)} && lon > ${f(G.lon[0])} && lon < ${f(G.lon[1])};
                float snowline = lat < 0.0 ? max(0.0, ${f(SNOW.south.lat)} + lat) * ${f(SNOW.south.perDeg)}
                    : greenland ? max(0.0, ${f(G.lat)} - lat) * ${f(G.perDeg)} : max(0.0, ${f(SNOW.north.lat)} - lat) * ${f(SNOW.north.perDeg)};
                vec3 low = mix(uRock, uLowland, smoothstep(0.0, ${f(TUNDRA_BELOW_M)}, snowline));
                vec3 c = mix(uShore, low, smoothstep(0.0, ${f(LAND_BANDS_M.shore)}, m));
                c = mix(c, uUpland, smoothstep(${f(LAND_BANDS_M.upland * 0.3)}, ${f(LAND_BANDS_M.upland)}, m));
                c = mix(c, uRock, smoothstep(${f(LAND_BANDS_M.upland)}, ${f(LAND_BANDS_M.rock)}, m));
                return mix(c, uSnow, smoothstep(snowline - ${f(SNOW_BLEND_M)}, snowline + ${f(SNOW_BLEND_M)}, m));
            }
            ` + sh.fragmentShader.replace('#include <color_fragment>',
            'diffuseColor.rgb = vPos.y > 0.0 ? landColour(vPos.y * uLandM, uLat.x + uLat.y * vPos.z, uLon.x + uLon.y * vPos.x) : mix(uShallow, uDeep, sqrt(clamp(vPos.y / uDeepY, 0.0, 1.0)));');
    };
    return mat;
}

export function buildSeabed(world) {
    const { nx, ny } = world, pos = new Float32Array(nx * ny * 3);
    for (let j = 0, q = 0; j < ny; j++) for (let i = 0; i < nx; i++, q += 3) pos.set(world.at(i, j), q);
    const index = new Uint32Array((nx - 1) * (ny - 1) * 6);
    for (let j = 0, q = 0; j < ny - 1; j++) for (let i = 0; i < nx - 1; i++, q += 6) { const a = j * nx + i, b = a + nx; index.set([a, a + 1, b, a + 1, b + 1, b], q); }
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geom.setIndex(new THREE.BufferAttribute(index, 1));
    geom.computeVertexNormals();
    const mesh = new THREE.Mesh(geom, seabedMaterial(world));
    // After world.carve(): move the changed grid points and relight.
    mesh.refresh = changed => {
        if (!changed.size) return;
        for (const k of changed) pos[k * 3 + 1] = world.at(k % nx, Math.floor(k / nx))[1];
        geom.attributes.position.needsUpdate = true;
        geom.computeVertexNormals();
    };
    return mesh;
}

// The box's rim, walked once round: a curtain of quads from the seabed's edge to wherever `toY` says.
function curtain(world, toY) {
    const { nx, ny } = world, rim = [];
    for (let i = 0; i < nx; i++) rim.push([i, 0]);
    for (let j = 1; j < ny; j++) rim.push([nx - 1, j]);
    for (let i = nx - 2; i >= 0; i--) rim.push([i, ny - 1]);
    for (let j = ny - 2; j >= 0; j--) rim.push([0, j]);
    const pos = [];
    for (let q = 0; q < rim.length - 1; q++) {
        const a = world.at(...rim[q]), b = world.at(...rim[q + 1]), ya = toY(a[1]), yb = toY(b[1]);
        pos.push(a[0], a[1], a[2], b[0], b[1], b[2], a[0], ya, a[2], b[0], b[1], b[2], b[0], yb, b[2], a[0], ya, a[2]);
    }
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pos), 3));
    geom.computeVertexNormals();
    return geom;
}

// The solid block under the seabed: walls down from its edge, and a base.
export function buildGround(world) {
    const baseY = -(DEPTH_SHARE + BASE_SHARE), mat = new THREE.MeshLambertMaterial({ color: LOOK.ground, side: THREE.DoubleSide });
    const group = new THREE.Group(), base = new THREE.Mesh(new THREE.PlaneGeometry(world.size.x, world.size.z), new THREE.MeshBasicMaterial({ color: LOOK.groundBase }));      // unlit: no light reaches the underside
    base.rotation.x = Math.PI / 2; base.position.y = baseY;
    group.add(new THREE.Mesh(curtain(world, () => baseY), mat), base);
    return group;
}

// The body of water: the surface, and panes down the box's sides from the surface to the seabed.
export function buildWater(world) {
    const mat = new THREE.MeshBasicMaterial({ color: LOOK.water, transparent: true, opacity: 0.28, depthWrite: false, side: THREE.DoubleSide });
    const group = new THREE.Group(), top = new THREE.Mesh(new THREE.PlaneGeometry(world.size.x, world.size.z), mat);
    top.rotation.x = -Math.PI / 2;
    const sides = new THREE.Mesh(curtain({ ...world, at: (i, j) => { const p = world.at(i, j); p[1] = Math.min(p[1], 0); return p; } }, () => 0), mat);
    group.add(top, sides);
    return group;
}
