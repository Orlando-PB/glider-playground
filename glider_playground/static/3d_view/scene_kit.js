// Scene kit: what 3d_view.html and the mission view (missions/static/mission_view.js) both need to draw
// the same ocean box: palette, bathy upsampling, seabed colouring, walls/base, water panes, box scaling,
// model loading, the synthetic Argo cycle, track colour scales and the Plotly-internals fast path.
// Change the look here and both views follow. Page-specific behaviour (camera, playback, labels) stays in the pages.
window.SceneKit = (() => {
    const LOOK = {
        earthColour: '#c2b2a4', earthGradientEnd: '#e5d9cc',
        earthDeepColour: '#7d7060',            // deep-seabed tone for the depth-shaded ramp
        landColour: '#a8bf9a', landGradientEnd: '#c9d8bd',
        snowColour: '#cfd8df', snowGradientEnd: '#e9eef2',   // polar land: ice-grey, off pure white so it reads on a light background
        snowLatCutoff: 60, snowBlendKm: 200,   // |lat| beyond the cutoff is snow, blended across this width
        waterTopColour: '#90e0ef', waterBottomColour: '#004e89',
        waterOpacity: 0.28,                    // five translucent panes stack up when looking through the box, so keep each one light
        bathyUpsampleTo: 280,                  // bathy grid is upsampled to ~this many points on its longer side (the coast gets finer cells of its own)
        landExaggeration: 5,                   // land is stretched at most this much, however tall the water column is drawn
        coastStepFrac: 0.012,                  // land's minimum height as a fraction of the box depth (coast step)
        trackColour: '#d9c45c',                // plain dive track (muted yellow so the vehicle stands out against it)
        noDataColour: '#9aa3ad',               // coloured track: samples without a value
        clipNear: 0.02, clipFar: 12,           // Plotly's 0.01..1000 default z-fights on this flat scene; scene ≲ 3 units across
    };
    const LIGHT = {
        // epsilon 0: the defaults zero the normals of very small triangles, leaving them ambient-only (dark squares)
        seabed: { ambient: 0.65, diffuse: 0.75, specular: 0.05, roughness: 0.8, fresnel: 0.05, vertexnormalsepsilon: 0, facenormalsepsilon: 0 },
        flat: { ambient: 1, diffuse: 0, specular: 0, roughness: 1, fresnel: 0 },
        // Mostly flat tint: specular/fresnel on the panes reads as a reflective haze.
        model: { ambient: 0.75, diffuse: 0.5, specular: 0.1, roughness: 0.8 },
        water: { ambient: 0.9, diffuse: 0.35, specular: 0, roughness: 1, fresnel: 0 },
    };
    const M_PER_DEG = 111111;
    // Model sizes in scene units (scene ~1 across): hull length, or height for upright objects.
    const SIZES = { slocum: 0.011, seaglider: 0.011, alr: 0.019, argo: 0.008, ship: 0.0325, buoy: 0.014 };
    // Coast from the globe's basemap: cells it crosses are meshed up to subMax× finer within quadBudget quads; water = blue − red above
    // waterBlue; sea is what connects to a cell deeper than seaSeedDepth; boxes with nothing shallower than skipBelow have no coast to fix.
    const COAST = { subMax: 10, quadBudget: 350000, seaSeedDepth: -15, maxTiles: 60, waterBlue: 20, skipBelow: -50 };

    // ── Colour helpers ──
    const hexToRgb = hex => { const n = parseInt(hex.slice(1), 16); return [n >> 16, (n >> 8) & 255, n & 255]; };
    const rgbToHex = rgb => '#' + rgb.map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
    const lerpHex = (a, b, t) => { const A = hexToRgb(a), B = hexToRgb(b); return rgbToHex(A.map((v, i) => v + (B[i] - v) * t)); };

    // Catmull-Rom upsample of the bathy grid to ~`target` points on its longer side (integer factor, max 4×).
    function upsampleGrid(lon, lat, z, target) {
        const nx = lon.length, ny = lat.length;
        const f = Math.min(4, Math.max(1, Math.round(target / Math.max(nx, ny))));
        if (f === 1 || nx < 2 || ny < 2) return { lon, lat, z };
        const NX = (nx - 1) * f + 1, NY = (ny - 1) * f + 1;
        const cr = (p0, p1, p2, p3, t) => 0.5 * (2 * p1 + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t + (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t);
        const at = (arr, i) => arr[Math.max(0, Math.min(arr.length - 1, i))];
        const lin = (arr, N) => { const o = new Array(N); for (let X = 0; X < N; X++) { const i = Math.floor(X / f), t = (X % f) / f; o[X] = i + 1 < arr.length ? arr[i] + (arr[i + 1] - arr[i]) * t : arr[i]; } return o; };
        const rows = z.map(row => { const o = new Array(NX); for (let X = 0; X < NX; X++) { const i = Math.floor(X / f), t = (X % f) / f; o[X] = t ? cr(at(row, i - 1), row[i], at(row, i + 1), at(row, i + 2), t) : row[i]; } return o; });
        const Z = new Array(NY);
        for (let Y = 0; Y < NY; Y++) {
            const j = Math.floor(Y / f), t = (Y % f) / f;
            if (!t) { Z[Y] = rows[j]; continue; }
            const r0 = at(rows, j - 1), r1 = rows[j], r2 = at(rows, j + 1), r3 = at(rows, j + 2), o = new Array(NX);
            for (let X = 0; X < NX; X++) o[X] = cr(r0[X], r1[X], r2[X], r3[X], t);
            Z[Y] = o;
        }
        return { lon: lin(lon, NX), lat: lin(lat, NY), z: Z };
    }

    // Value of grid `z` at the cell nearest lon/lat (clamped to the grid).
    const clampIdx = (arr, v) => Math.max(0, Math.min(arr.length - 1, Math.round((v - arr[0]) / (arr[arr.length - 1] - arr[0]) * (arr.length - 1))));
    const gridAt = (lon, lat, z, x, y) => z[clampIdx(lat, y)][clampIdx(lon, x)];

    // Snow fraction from latitude: 0 equatorward of the cutoff, 1 poleward, smooth across snowBlendKm.
    const snowAt = lat => { const c = Math.max(0, Math.min(1, (Math.abs(lat) - LOOK.snowLatCutoff) / (LOOK.snowBlendKm / 111.111) + 0.5)); return c * c * (3 - 2 * c); };

    // (z, snow 0..1) → hex for the seabed/land mesh, quantised and memoised; applied per vertex.
    // depthShade: seabed darkens with depth (else the plain earth ramp). greenLand: land green → snow (else earth).
    function seabedColourer(zMin, zMax, { depthShade = true, greenLand = true } = {}) {
        const span = Math.max(zMax - zMin, 1e-6), cache = new Map();
        const stopsFor = snow => {
            const stops = [];
            if (zMin < 0) stops.push([zMin, depthShade ? LOOK.earthDeepColour : LOOK.earthColour], [0, depthShade ? LOOK.earthColour : LOOK.earthGradientEnd]);
            if (zMax > 0) stops.push([Math.max(0, zMin), greenLand ? lerpHex(LOOK.landColour, LOOK.snowColour, snow) : LOOK.earthColour],
                                     [zMax, greenLand ? lerpHex(LOOK.landGradientEnd, LOOK.snowGradientEnd, snow) : LOOK.earthGradientEnd]);
            if (stops.length === 1) stops.push([stops[0][0] + 1, stops[0][1]]);
            return stops;
        };
        return (z, snow) => {
            const zq = Math.round((z - zMin) / span * 128) / 128, sq = Math.round(snow * 8) / 8, key = zq + ':' + sq;
            let col = cache.get(key);
            if (col) return col;
            const zz = zMin + zq * span, stops = stopsFor(sq);
            col = stops[stops.length - 1][1];
            for (let q = 1; q < stops.length; q++) {
                if (zz <= stops[q][0]) { const [z0, c0] = stops[q - 1], [z1, c1] = stops[q]; col = z1 > z0 ? lerpHex(c0, c1, (zz - z0) / (z1 - z0)) : c1; break; }
            }
            cache.set(key, col);
            return col;
        };
    }

    // Land (and the coastline) from the globe's basemap (Esri Ocean Base): tiles covering the box stitched into one canvas and classed
    // land / water per pixel. Returns { keepSea(seeds), at(lon, lat) → 3×3 land fraction, frac(box) → land fraction }, or null if nothing loaded.
    async function loadBasemap(lon0, lon1, lat0, lat1) {
        const mercY = lat => (1 - Math.asinh(Math.tan(lat * Math.PI / 180)) / Math.PI) / 2, clampLat = lat => Math.max(-85, Math.min(85, lat));
        let zl = 12, tx0, tx1, ty0, ty1;
        for (; zl >= 0; zl--) {
            const n = 2 ** zl;
            tx0 = Math.floor((lon0 + 180) / 360 * n); tx1 = Math.floor((lon1 + 180) / 360 * n);
            ty0 = Math.floor(mercY(clampLat(lat1)) * n); ty1 = Math.floor(mercY(clampLat(lat0)) * n);
            if ((tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= COAST.maxTiles) break;
        }
        const n = 2 ** zl, W = (tx1 - tx0 + 1) * 256, H = (ty1 - ty0 + 1) * 256;
        const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
        const ctx = cv.getContext('2d', { willReadFrequently: true });
        const jobs = [];
        for (let ty = ty0; ty <= ty1; ty++) for (let tx = tx0; tx <= tx1; tx++) jobs.push(new Promise(res => {
            const im = new Image(); im.crossOrigin = 'anonymous';
            im.onload = () => { ctx.drawImage(im, (tx - tx0) * 256, (ty - ty0) * 256); res(1); }; im.onerror = () => res(0);
            im.src = `https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/${zl}/${ty}/${((tx % n) + n) % n}`;
        }));
        if (!(await Promise.all(jobs)).some(Boolean)) return null;
        const px = ctx.getImageData(0, 0, W, H).data;
        cv.width = cv.height = 0;      // free the canvas' backing store (up to ~50 MB): only the mask is kept
        const mask = new Uint8Array(W * H);      // 0 no tile, 1 water, 2 land
        for (let q = 0, o = 0; q < mask.length; q++, o += 4) mask[q] = !px[o + 3] ? 0 : px[o + 2] - px[o] > COAST.waterBlue ? 1 : 2;
        const PX = lon => ((lon + 180) / 360 * n - tx0) * 256, PY = lat => (mercY(clampLat(lat)) * n - ty0) * 256;
        return {
            // Water that isn't connected to the open sea (lakes, stray blue pixels) becomes land. Seeds: [lon, lat] points known to be sea.
            keepSea(seeds) {
                const stack = new Int32Array(W * H); let top = 0;
                const visit = q => { if (mask[q] === 1) { mask[q] = 3; stack[top++] = q; } };
                for (const [lon, lat] of seeds) { const X = Math.round(PX(lon)), Y = Math.round(PY(lat)); if (X >= 0 && Y >= 0 && X < W && Y < H) visit(Y * W + X); }
                while (top) { const q = stack[--top], X = q % W; if (X > 0) visit(q - 1); if (X < W - 1) visit(q + 1); if (q >= W) visit(q - W); if (q < W * (H - 1)) visit(q + W); }
                for (let q = 0; q < mask.length; q++) mask[q] = mask[q] === 3 ? 1 : mask[q] === 1 ? 2 : mask[q];
            },
            at(lon, lat) {
                const cx = Math.round(PX(lon)), cy = Math.round(PY(lat));
                let land = 0, c = 0;
                for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
                    const X = cx + dx, Y = cy + dy; if (X < 0 || Y < 0 || X >= W || Y >= H) continue;
                    const m = mask[Y * W + X]; if (!m) continue;
                    c++; if (m === 2) land++;
                }
                return c ? land / c : null;
            },
            frac(lonA, latA, lonB, latB) {
                const xa = Math.max(0, Math.floor(PX(lonA)) - 2), xb = Math.min(W - 1, Math.ceil(PX(lonB)) + 2), ya = Math.max(0, Math.floor(PY(latB)) - 2), yb = Math.min(H - 1, Math.ceil(PY(latA)) + 2);
                let land = 0, c = 0;
                for (let Y = ya; Y <= yb; Y++) for (let X = xa; X <= xb; X++) { const m = mask[Y * W + X]; if (m) { c++; if (m === 2) land++; } }
                return c ? land / c : null;
            },
        };
    }
    // The basemap for an (upsampled) bathy grid with its lakes filled in, ready for buildSeabed. Null: no coast in the box, or no tiles.
    async function loadCoast(grid) {
        if (!grid.z.some(row => row.some(v => v > COAST.skipBelow))) return null;
        const bLon = grid.lon, bLat = grid.lat;
        const basemap = await loadBasemap(bLon[0], bLon[bLon.length - 1], bLat[0], bLat[bLat.length - 1]).catch(() => null);
        if (!basemap) return null;
        const seeds = [];
        for (let iy = 0; iy < bLat.length; iy += 2) for (let ix = 0; ix < bLon.length; ix += 2) if (grid.z[iy][ix] < COAST.seaSeedDepth) seeds.push([bLon[ix], bLat[iy]]);
        basemap.keepSea(seeds);
        return basemap;
    }

    // How much land heights are pre-scaled so that, under the box's vExag, land ends up stretched by at most LOOK.landExaggeration.
    const landScale = vExag => Math.min(LOOK.landExaggeration, vExag) / vExag;

    // Seabed + land + walls + base over an upsampled grid. `basemap` (loadCoast) overrides the bathymetry's land/water split near the
    // coast (low land ↔ sea only). Returns { bathyZ: the drawn heights per grid vertex, traces(opts), colours(opts) };
    // opts = seabedColourer's. Geometry is independent of opts, so a restyle only needs colours().
    function buildSeabed(grid, { landScale, coastStep, baseZ, basemap }) {
        const bLon = grid.lon, bLat = grid.lat, xLen = bLon.length, yLen = bLat.length;
        // Land gets a small minimum height so the coast is a clean step above the water plane.
        const bathyZ = grid.z.map(row => row.map(v => (v > 0 ? Math.max(v * landScale, coastStep) : v)));
        // One height rule for every point, coarse vertex or coast sub-vertex: land sits at or above the coast step, water at or below
        // minus it, and a point whose 3×3 pixels straddle the coast takes a height between, so the waterline lands on the basemap's coast.
        const coastH = (lon, lat, base, sea) => {
            const land = basemap.at(lon, lat); if (land == null) return base;
            return land === 1 ? Math.max(base, coastStep) : land === 0 ? Math.min(sea, -coastStep) : (land - 0.5) * 2 * coastStep;
        };
        // Cells the coast passes through are meshed coastSub× finer (fewer if the coast is long), from the basemap alone.
        let isCoast = null, coastSub = 0;
        if (basemap) {
            for (let iy = 0; iy < yLen; iy++) for (let ix = 0; ix < xLen; ix++) bathyZ[iy][ix] = coastH(bLon[ix], bLat[iy], bathyZ[iy][ix], bathyZ[iy][ix]);
            isCoast = new Set();
            for (let iy = 1; iy < yLen - 2; iy++) for (let ix = 1; ix < xLen - 2; ix++) {      // not the rim cells: walls and water panes follow those
                const f = basemap.frac(bLon[ix], bLat[iy], bLon[ix + 1], bLat[iy + 1]);
                if (f != null && f > 0 && f < 1) isCoast.add(iy * xLen + ix);
            }
            coastSub = Math.max(2, Math.min(COAST.subMax, Math.floor(Math.sqrt(COAST.quadBudget / Math.max(isCoast.size, 1)))));
        }
        // One mesh with shared rim positions (no z-fighting slivers). Grid vertex (ix, iy) is index iy*xLen+ix.
        const G = { x: [], y: [], z: [], i: [], j: [], k: [] };
        for (let iy = 0; iy < yLen; iy++) for (let ix = 0; ix < xLen; ix++) { G.x.push(bLon[ix]); G.y.push(bLat[iy]); G.z.push(bathyZ[iy][ix]); }
        const V = (ix, iy) => iy * xLen + ix, face = (a, b, c) => { G.i.push(a); G.j.push(b); G.k.push(c); };
        // Land faces go to their own evenly lit trace: shaded like the seabed, every small slope on land reads as a different green.
        const L = { i: [], j: [], k: [] }, shore = -coastStep * 0.999;
        const top = (a, b, c) => { if (G.z[a] > shore || G.z[b] > shore || G.z[c] > shore) { L.i.push(a); L.j.push(b); L.k.push(c); } else face(a, b, c); };
        for (let iy = 0; iy < yLen - 1; iy++) for (let ix = 0; ix < xLen - 1; ix++) {
            const v00 = V(ix, iy), v10 = V(ix + 1, iy), v01 = V(ix, iy + 1), v11 = V(ix + 1, iy + 1);
            if (isCoast && isCoast.has(v00)) continue;
            // Split along alternating diagonals so ridges don't all lean one way.
            if ((ix + iy) % 2) { top(v00, v10, v11); top(v00, v11, v01); } else { top(v00, v10, v01); top(v10, v11, v01); }
        }
        if (isCoast && isCoast.size) {      // coast cells: a coastSub × coastSub patch each, vertices shared across patches so the shading has no seams
            // Under water a coast cell slopes between its sea corners only (land corners count as the shore), so no shelf or kink catches the light.
            const seaZ = bathyZ.map(row => row.map(z => Math.min(z, -coastStep)));
            const k = coastSub, fineW = (xLen - 1) * k + 1, seen = new Map();
            const fv = (ix, iy, sx, sy) => {
                const gx = ix * k + sx, gy = iy * k + sy;
                if (gx % k === 0 && gy % k === 0) return V(gx / k, gy / k);
                const key = gy * fineW + gx; let v = seen.get(key); if (v != null) return v;
                const u = sx / k, w = sy / k, bil = a => (a[iy][ix] * (1 - u) + a[iy][ix + 1] * u) * (1 - w) + (a[iy + 1][ix] * (1 - u) + a[iy + 1][ix + 1] * u) * w;
                const lon = bLon[ix] + (bLon[ix + 1] - bLon[ix]) * u, lat = bLat[iy] + (bLat[iy + 1] - bLat[iy]) * w;
                G.x.push(lon); G.y.push(lat); G.z.push(coastH(lon, lat, bil(bathyZ), bil(seaZ)));
                v = G.x.length - 1; seen.set(key, v); return v;
            };
            for (const c of isCoast) {
                const ix = c % xLen, iy = (c - ix) / xLen;
                for (let sy = 0; sy < k; sy++) for (let sx = 0; sx < k; sx++) {
                    const a = fv(ix, iy, sx, sy), b = fv(ix, iy, sx + 1, sy), d = fv(ix, iy, sx, sy + 1), e = fv(ix, iy, sx + 1, sy + 1);
                    if ((sx + sy) % 2) { top(a, b, e); top(a, e, d); } else { top(a, b, d); top(b, e, d); }
                }
            }
        }
        basemap = null;      // the returned closures outlive the build: don't pin the coast mask
        const nTop = G.x.length;
        // Walls + base get their OWN copy of the rim (anticlockwise from the SW corner) so seabed colours don't blend down the wall.
        const rim = [];
        for (let ix = 0; ix < xLen - 1; ix++) rim.push(V(ix, 0));
        for (let iy = 0; iy < yLen - 1; iy++) rim.push(V(xLen - 1, iy));
        for (let ix = xLen - 1; ix > 0; ix--) rim.push(V(ix, yLen - 1));
        for (let iy = yLen - 1; iy > 0; iy--) rim.push(V(0, iy));
        const copy = z => rim.map(v => { G.x.push(G.x[v]); G.y.push(G.y[v]); G.z.push(z == null ? G.z[v] : z); return G.x.length - 1; });
        const rimTop = copy(null), rimBase = copy(baseZ);
        for (let q = 0; q < rim.length; q++) { const r = (q + 1) % rim.length; face(rimTop[q], rimBase[q], rimBase[r]); face(rimTop[q], rimBase[r], rimTop[r]); }
        for (let q = 1; q < rimBase.length - 1; q++) face(rimBase[0], rimBase[q + 1], rimBase[q]);      // base: a fan from the SW corner

        let zMin = Infinity, zMax = -Infinity;
        for (const row of bathyZ) for (const v of row) { if (v < zMin) zMin = v; if (v > zMax) zMax = v; }
        const colours = (opts = {}) => {
            const colourAt = seabedColourer(zMin, zMax, opts), snow = opts.greenLand === false ? () => 0 : snowAt, col = new Array(G.x.length);
            for (let v = 0; v < nTop; v++) col[v] = colourAt(G.z[v], snow(G.y[v]));
            for (let v = nTop; v < col.length; v++) col[v] = LOOK.earthColour;
            return col;
        };
        const mesh = (faces, col, name, lighting) => ({ type: 'mesh3d', x: G.x, y: G.y, z: G.z, i: faces.i, j: faces.j, k: faces.k, vertexcolor: col,
            showscale: false, flatshading: false, hoverinfo: 'skip', lighting, name });
        // 'Land' is present whenever the box has land, whatever the opts: the trace list never changes on a restyle.
        const traces = opts => { const col = colours(opts); return [mesh(G, col, 'Seabed', LIGHT.seabed)].concat(L.i.length ? [mesh(L, col, 'Land', LIGHT.flat)] : []); };
        return { bathyZ, traces, colours };
    }

    // Water: the sea surface plus four truly vertical side panes as ONE mesh3d. Pane bottoms follow the seabed
    // (`bathyZ`; null = straight down to baseZ), clamped to sea level so no water is drawn up a land edge.
    function waterTrace(bLon, bLat, bathyZ, baseZ) {
        const xLen = bLon.length, yLen = bLat.length, x0 = bLon[0], x1 = bLon[xLen - 1], y0 = bLat[0], y1 = bLat[yLen - 1];
        const W = { x: [], y: [], z: [], i: [], j: [], k: [] };
        const wv = (x, y, z) => { W.x.push(x); W.y.push(y); W.z.push(z); return W.x.length - 1; };
        const wquad = (a, b, c, d) => { W.i.push(a, a); W.j.push(b, c); W.k.push(c, d); };
        const edgeZ = (ix, iy) => (bathyZ ? Math.min(bathyZ[iy][ix], 0) : baseZ);
        wquad(wv(x0, y0, 0), wv(x1, y0, 0), wv(x1, y1, 0), wv(x0, y1, 0));
        const wpane = (n, at) => {
            let pt = null, pb = null, pz = 0;
            for (let q = 0; q < n; q++) {
                const [x, y, z] = at(q), t = wv(x, y, 0), b = wv(x, y, z);
                if (q && (z < 0 || pz < 0)) wquad(pt, t, b, pb);   // skip dry (zero-height) cells
                pt = t; pb = b; pz = z;
            }
        };
        wpane(xLen, q => [bLon[q], y0, edgeZ(q, 0)]); wpane(xLen, q => [bLon[q], y1, edgeZ(q, yLen - 1)]);
        wpane(yLen, q => [x0, bLat[q], edgeZ(0, q)]); wpane(yLen, q => [x1, bLat[q], edgeZ(xLen - 1, q)]);
        return { type: 'mesh3d', x: W.x, y: W.y, z: W.z, i: W.i, j: W.j, k: W.k, intensity: W.z, colorscale: [[0, LOOK.waterBottomColour], [1, LOOK.waterTopColour]], cmin: baseZ, cmax: 0,
            opacity: LOOK.waterOpacity, showscale: false, hoverinfo: 'skip', flatshading: true, lighting: LIGHT.water, name: 'Water' };
    }

    // Horizontal size of a lon/lat box in metres.
    function footprint(lon0, lon1, lat0, lat1) {
        const cosLat = Math.cos((lat0 + lat1) / 2 * Math.PI / 180);
        const distX = (lon1 - lon0) * M_PER_DEG * cosLat, distY = (lat1 - lat0) * M_PER_DEG;
        return { cosLat, distX, distY, maxHoriz: Math.max(distX, distY, 1) };
    }
    // Axis ranges, aspect ratio and data-units-per-scene-unit for a box drawn over the grid [lon0..lon1] × [lat0..lat1].
    // hTop: headroom (scene units) above the highest ground zTop0, so models/scenery aren't clipped. Objects are sized in
    // scene units and kz depends on the range, so solve top = zTop0 + h·(top − baseZ)/aspectZ.
    function sceneBox(fp, lon0, lon1, lat0, lat1, baseZ, vExag, zTop0, hTop) {
        const tiny = 1e-5, aspectX = fp.distX / fp.maxHoriz, aspectY = fp.distY / fp.maxHoriz, aspectZ = Math.abs(baseZ) / fp.maxHoriz * vExag;
        const xRange = [lon0 - tiny, lon1 + tiny], yRange = [lat0 - tiny, lat1 + tiny];
        const zRange = [baseZ, hTop < aspectZ * 0.5 ? (zTop0 - hTop * baseZ / aspectZ) / (1 - hTop / aspectZ) : zTop0 + hTop * (zTop0 - baseZ) / aspectZ];
        const scale = { kx: (xRange[1] - xRange[0]) / aspectX, ky: (yRange[1] - yRange[0]) / aspectY, kz: (zRange[1] - zRange[0]) / aspectZ };
        return { xRange, yRange, zRange, aspectX, aspectY, aspectZ, scale };
    }
    function layout(box, camera, sceneExtra) {
        const noAxis = { showgrid: false, zeroline: false, showline: false, showticklabels: false, showbackground: false, title: '', visible: false, showspikes: false };
        return {
            hovermode: false, margin: { l: 0, r: 0, b: 0, t: 0 }, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', showlegend: false,
            scene: { xaxis: { ...noAxis, range: box.xRange, autorange: false }, yaxis: { ...noAxis, range: box.yRange, autorange: false }, zaxis: { ...noAxis, range: box.zRange, autorange: false },
                aspectmode: 'manual', aspectratio: { x: box.aspectX, y: box.aspectY, z: box.aspectZ }, camera, ...sceneExtra },
        };
    }

    // ── Models (static/3d_view/models/<name>.json): metres, +x nose, y to port, +z up ──
    // Merge parts into one mesh; each triangle carries its part's colour index (intensitymode:'cell').
    function mergeParts(parts) {
        const M = { x: [], y: [], z: [], i: [], j: [], k: [], cell: [] }, colors = [];
        for (const p of Object.values(parts)) {
            if (!colors.includes(p.color)) colors.push(p.color);
            const ci = colors.indexOf(p.color), base = M.x.length;
            M.x.push(...p.x); M.y.push(...p.y); M.z.push(...p.z);
            M.i.push(...p.i.map(v => v + base)); M.j.push(...p.j.map(v => v + base)); M.k.push(...p.k.map(v => v + base));
            for (let q = 0; q < p.i.length; q++) M.cell.push(ci);
        }
        M.cmax = Math.max(colors.length - 1, 1);
        M.colorscale = colors.map((c, q) => [q / M.cmax, c]);
        if (colors.length === 1) M.colorscale.push([1, colors[0]]);
        const span = a => Math.max(...a) - Math.min(...a);
        M.height = span(M.z); M.hullLen = span(M.x);
        M.extent = Math.max(M.hullLen, span(M.y), M.height);
        M.zMin = Math.min(...M.z); M.zMid = M.zMin + M.height / 2;
        M.length = parts.hull ? span(parts.hull.x) : M.height;      // hull nose-to-tail; upright objects: height
        const deck = parts.deck || parts.aft;                        // ships: working-deck height, for carried vehicles
        M.deckZ = deck ? Math.min(...deck.z) : 0;
        return M;
    }
    const modelCache = {};
    // `name` is a model in static/3d_view/models, or a full URL (starts with '/'). Rejects if it can't be loaded.
    const loadModel = name => modelCache[name] || (modelCache[name] = fetch(name.startsWith('/') ? name : `/static/3d_view/models/${name}.json`)
        .then(r => { if (!r.ok) throw new Error(name); return r.json(); }).then(mergeParts));
    const modelTrace = (M, xyz, lighting, name) => ({ type: 'mesh3d', ...xyz, i: M.i, j: M.j, k: M.k, intensity: M.cell, intensitymode: 'cell', cmin: 0, cmax: M.cmax,
        colorscale: M.colorscale, showscale: false, flatshading: true, hoverinfo: 'skip', lighting, name });
    // n vertices collapsed onto one point: how a mesh is "hidden" without changing the trace list.
    const collapsed = (n, pt) => ({ x: new Array(n).fill(pt[0]), y: new Array(n).fill(pt[1]), z: new Array(n).fill(pt[2]) });

    // ── Argo floats: the index only gives surfacings, so between them the float follows a synthetic
    // standard mission (surface, park drift, deep dip, ascent). Depths in m, positive down; times in hours. ──
    const ARGO = {
        surfaceH: 1,        // hours at the surface after a profile
        descendH: 6,        // hours from surface to park depth
        parkM: 1000,        // park depth (m) - the mission default
        deepStartH: 12,     // hours before the next surfacing the deep dip begins
        deepEndH: 7,        // hours before the next surfacing the ascent begins
        profileM: 2000,     // profile depth (m)
        leadH: 12,          // show a float this many hours before its first known profile (ascending)
    };
    // Depth `since` hours into a cycle of T hours; cycles shorter than ~19 h compress every phase proportionally.
    function argoCycleDepth(since, T) {
        const need = ARGO.surfaceH + ARGO.descendH + ARGO.deepStartH, k = T < need ? T / need : 1;
        const keys = [[0, 0], [ARGO.surfaceH * k, 0], [(ARGO.surfaceH + ARGO.descendH) * k, ARGO.parkM], [T - ARGO.deepStartH * k, ARGO.parkM], [T - ARGO.deepEndH * k, ARGO.profileM], [T, 0]];
        for (let q = 1; q < keys.length; q++) if (since <= keys[q][0]) { const [t0, d0] = keys[q - 1], [t1, d1] = keys[q]; return t1 > t0 ? d0 + (d1 - d0) * (since - t0) / (t1 - t0) : d1; }
        return 0;
    }
    // After the last known profile: surface stint, descend, park for good.
    const argoParkDepth = since => (since < ARGO.surfaceH ? 0 : Math.min(ARGO.parkM, ARGO.parkM * (since - ARGO.surfaceH) / ARGO.descendH));

    // ── Track colour ──
    const paletteFor = cmap => {
        const P = (window.GP_PLOT_PRESETS || {}).palettes || {}, arr = P[String(cmap || '').replace(/_r$/, '')] || ['#440154', '#21918c', '#fde725'];
        return /_r$/.test(cmap || '') ? arr.slice().reverse() : arr;
    };
    // Plotly colorscale for cmin..cmax with missing values just below it, on a grey band at its foot.
    function colourScale(pal, cmin, cmax) {
        const plotMin = cmin - (cmax - cmin) * 0.02, f0 = 0.02 / 1.02;
        const scale = [[0, LOOK.noDataColour], [f0 * 0.99, LOOK.noDataColour]].concat(pal.map((c, q) => [f0 + (1 - f0) * q / Math.max(pal.length - 1, 1), c]));
        scale[scale.length - 1][0] = 1;      // Plotly rejects a colorscale whose last stop isn't exactly 1 (and silently falls back to its default)
        return { plotMin, scale };
    }
    const clampValues = (vals, cmin, cmax, plotMin) => vals.map(v => (v == null ? plotMin : Math.min(cmax, Math.max(cmin, v))));
    const fmtValue = v => (Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));

    // ── Plotly internals (not public options: everything here falls back or fails quietly) ──
    const glScene = plotDiv => plotDiv._fullLayout.scene._scene;
    // Tight clip planes, and no picking: hover is off for every trace, but gl-plot3d still draws the whole scene a second
    // time into its pick buffer on every dirty frame and reads the full canvas back (readPixels: a synchronous GPU stall).
    // toImage reads the default framebuffer, so it still works. Call again after anything that makes fresh WebGL objects.
    function tuneGl(plotDiv) {
        try {
            const gp = glScene(plotDiv).glplot, gl = gp.gl;
            gp.zNear = LOOK.clipNear; gp.zFar = LOOK.clipFar;
            (gp.objects || []).forEach(o => { if (o.drawPick) o.drawPick = null; });
            if (gl._gpNoPickRead) return;
            gl._gpNoPickRead = true;
            const read = gl.readPixels;
            gl.readPixels = function () { if (gl.getParameter(gl.FRAMEBUFFER_BINDING)) return; return read.apply(gl, arguments); };
        } catch (_) {}
    }
    // gl-plot3d's own rAF loop draws once per frame when any object is dirty. Flag one rather than calling
    // glplot.redraw(), which renders synchronously (a second full draw on top of the loop's own).
    const markDirty = scene => { const o = scene.glplot.objects; if (o && o.length) o[0].dirty = true; else scene.glplot.redraw(); };
    // mesh3d update() rebuilds the colour texture on every call; drop `colormap` on the way through.
    // Scatter3d wrappers have no .mesh and take the plain update.
    const traceUpdate = (w, ft) => {
        const m = w.mesh;
        if (!m || typeof m.update !== 'function') { w.update(ft); return; }
        const own = Object.prototype.hasOwnProperty.call(m, 'update'), orig = m.update;
        m.update = p => { if (p) delete p.colormap; return orig.call(m, p); };
        try { w.update(ft); } finally { if (own) m.update = orig; else delete m.update; }
    };
    // Hand trace attributes straight to the WebGL wrapper; a normal restyle if the internals aren't where expected
    // (`beforeRestyle` may return false to skip that restyle).
    function fastPatch(plotDiv, ti, patch, beforeRestyle) {
        try {
            const scene = glScene(plotDiv), ft = plotDiv._fullData[ti], w = scene.traces[ft.uid];
            if (!w || typeof w.update !== 'function') throw new Error('no wrapper');
            Object.assign(plotDiv.data[ti], patch); Object.assign(ft, patch);
            traceUpdate(w, ft); markDirty(scene);
        } catch (_) {
            if (beforeRestyle && beforeRestyle() === false) return;
            Plotly.restyle(plotDiv, Object.fromEntries(Object.entries(patch).map(([k, v]) => [k, [v]])), [ti]);
        }
    }

    return { LOOK, LIGHT, hexToRgb, rgbToHex, lerpHex, SIZES, upsampleGrid, gridAt, snowAt, seabedColourer, loadCoast, landScale, buildSeabed, waterTrace,
        footprint, sceneBox, layout, mergeParts, loadModel, modelTrace, collapsed, ARGO, argoCycleDepth, argoParkDepth,
        paletteFor, colourScale, clampValues, fmtValue, glScene, tuneGl, markDirty, fastPatch };
})();
