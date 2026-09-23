// Renderer, camera and orbit controls (left drag orbits, right drag pans, wheel zooms to the cursor). Draws only when
// something changed: call stage.redraw().
import * as THREE from 'three';

const MAX_PIXEL_RATIO = 2;      // of the 3D canvas only (the page's text is HTML and stays sharp): 2 costs ~1.8x the pixels for little gain
const ANIMATION_FPS = 30;      // frames only an animation asked for (scenery idling along): headroom for the rest of the machine
const FOLLOW_DISTANCE = 0.45;      // how close following starts, as a fraction of the box width (zoom still works)
const FOLLOW_EASE_S = { across: 0.5, depth: 4 };      // the camera trails the vehicle by about this long: barely sideways, a lot in depth, so dives don't bounce the view

export function createStage(canvas) {
    // Multisampling costs about a third of the frame rate; on a high-density screen the pixels are small enough without it.
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: devicePixelRatio < 1.5 });
    renderer.setPixelRatio(Math.min(devicePixelRatio, MAX_PIXEL_RATIO));
    renderer.localClippingEnabled = true;      // scenery is cut off at the box's edges
    const scene = new THREE.Scene();
    const content = new THREE.Group();      // everything that stretches with the vertical scale (vehicles don't: they sit in `scene`)
    scene.add(content);
    scene.background = new THREE.Color('#dcebf5');
    scene.add(new THREE.HemisphereLight('#ffffff', '#5b7083', 2.2));
    const sun = new THREE.DirectionalLight('#ffffff', 1.6);
    sun.position.set(-0.5, 1, 0.3);
    scene.add(sun);

    const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 100);
    const controls = new THREE.OrbitControls(camera, canvas);
    controls.maxPolarAngle = Math.PI / 2;         // level with the horizon at most: never looking up from underneath
    controls.zoomToCursor = true;
    controls.screenSpacePanning = false;          // dragging with the right button slides over the sea, like a map
    controls.enableDamping = true;                // movement eases out instead of stopping dead
    controls.dampingFactor = 0.12;

    // One frame per request; while the controls are still easing (or flying home) each frame asks for the next.
    const beforeRender = [];      // fn(camera), called every drawn frame; return true to ask for the next one
    let queued = false, size = null, home = null, flight = null, floor = null, clearance = 0, followed = null, vertical = 1;
    const spot = new THREE.Vector3(); let lastDraw = 0;
    // redraw(): something changed, draw on the next frame. redraw(false): only an animation wants another frame —
    // those are held to ANIMATION_FPS, so a scene that is just idling along doesn't run the GPU at the display's full rate.
    let urgent = false, lastRender = 0;
    const redraw = (now = true) => { urgent ||= now; if (!queued) { queued = true; requestAnimationFrame(draw); } };
    const draw = ts => {
        queued = false;
        if (!urgent && ts - lastRender < 1000 / ANIMATION_FPS - 1) { redraw(false); return; }
        urgent = false; lastRender = ts;
        if (flight) {
            const t = Math.min(1, (ts - (flight.t0 ??= ts)) / 600), e = t * t * (3 - 2 * t);
            camera.position.lerpVectors(flight.from.position, flight.to.position, e);
            controls.target.lerpVectors(flight.from.target, flight.to.target, e);
            if (t === 1) flight = null;
            redraw();
        }
        // Following: the camera rides along with the vehicle, keeping whatever angle and distance the user has set.
        const at = !flight && followed && followed(), dt = Math.min(0.1, (ts - lastDraw) / 1000 || 0);
        lastDraw = ts;
        if (at) {
            spot.fromArray(at).sub(controls.target);      // how far the vehicle is from where the camera looks
            if (spot.length() > size.x * 0.2) flyTo({ position: camera.position.clone().add(spot), target: controls.target.clone().add(spot) });      // a jump in time: fly over
            else {
                const across = 1 - Math.exp(-dt / FOLLOW_EASE_S.across), depth = 1 - Math.exp(-dt / FOLLOW_EASE_S.depth);
                spot.set(spot.x * across, spot.y * depth, spot.z * across);
                if (spot.length() > size.x * 1e-6) { camera.position.add(spot); controls.target.add(spot); redraw(); }
            }
        }
        if (size) { const t = controls.target; t.x = THREE.MathUtils.clamp(t.x, -size.x / 2, size.x / 2); t.z = THREE.MathUtils.clamp(t.z, -size.z / 2, size.z / 2); t.y = THREE.MathUtils.clamp(t.y, -size.y * vertical, 0); }
        if (controls.update()) redraw();
        const ground = floor && floor(camera.position.x, camera.position.z);      // the camera passes through water, never through seabed or land
        if (ground != null && camera.position.y < ground * vertical + clearance) { camera.position.y = ground * vertical + clearance; camera.lookAt(controls.target); }
        camera.updateMatrixWorld();
        for (const fn of beforeRender) if (fn(camera) === true) redraw(false);      // true: it is animating, keep the frames coming
        renderer.render(scene, camera);
    };
    const resize = () => {
        renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);
        camera.aspect = canvas.clientWidth / canvas.clientHeight;
        camera.updateProjectionMatrix();
        redraw();
    };
    controls.addEventListener('change', redraw);
    controls.addEventListener('start', () => { flight = null; });      // grabbing the scene cancels a flight home
    // `onHold(true)` while the user is moving the camera, `onHold(false)` when they let go.
    const onHold = fn => { controls.addEventListener('start', () => fn(true)); controls.addEventListener('end', () => fn(false)); };
    new ResizeObserver(resize).observe(canvas);

    // Frames the world box (`box` is its {x, y, z} extent, centred on the origin at the surface) and makes that the home view.
    // `heightAt(x, z)` is the terrain under a point (null outside the box): the camera is kept above it.
    const frame = (box, heightAt) => {
        size = box; floor = heightAt; clearance = box.x * 0.015;
        controls.minDistance = box.x * 0.06;      // close enough to follow one glider
        controls.maxDistance = box.x * 3;         // far enough to see the whole box with room round it
        controls.target.set(0, -box.y / 2, 0);
        camera.position.set(-box.x * 0.55, box.x * 0.55, box.z * 1.5);
        home = { position: camera.position.clone(), target: controls.target.clone() };
        redraw();
    };
    // Vertical scale of the content relative to how it was built: 1 = as built (stretched), smaller = flatter.
    const setVertical = v => {
        const k = v / vertical; vertical = v; content.scale.y = v;
        controls.target.y *= k; if (home) home.target.y *= k;
        redraw();
    };
    const flyTo = to => { flight = { from: { position: camera.position.clone(), target: controls.target.clone() }, to }; redraw(); };
    const goHome = () => { if (home) { followed = null; flyTo(home); } };
    // `getPosition()` -> [x, y, z] | null each frame, or null to stop. Starts by flying in close, from the current side.
    const follow = getPosition => {
        followed = getPosition;
        const at = getPosition && getPosition();
        if (!at) return;
        const target = new THREE.Vector3().fromArray(at), away = camera.position.clone().sub(controls.target).setLength(size.x * FOLLOW_DISTANCE);
        flyTo({ position: target.clone().add(away), target });
    };
    // The current view drawn at `scale` device pixels per CSS pixel, as a copy of the canvas (for snapshots).
    const snapshot = scale => gpSnapshot.threeFrame(renderer, () => renderer.render(scene, camera), scale);
    return { scene, content, camera, beforeRender, snapshot, setBackground: colour => { scene.background = new THREE.Color(colour || '#dcebf5'); }, redraw, frame, goHome, follow, onHold, setVertical };
}
