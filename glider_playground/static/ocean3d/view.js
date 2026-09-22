// What the mission page and the 3D view have in common: the box of ocean, platforms (track + vehicle) driven by one
// time bar, the Style panel (options + track colour) and the messages the shell exchanges with a panel. A page
// fetches its own data and hands it over.
import * as THREE from 'three';
import { createStage } from './stage.js';
import { createWorld, buildSeabed, buildGround, buildWater } from './world.js';
import { buildTrack, projectClearings } from './tracks.js';
import { loadModel } from './models.js';
import { createTimeline } from './timeline.js';
import { createLegend, palette, limits, colourScale } from './colours.js';
import { cycleTrack, FLOAT_COLOUR } from './argo.js';
import { buildScenery } from './scenery.js';

const $ = id => document.getElementById(id);
export const getJSON = url => fetch(url).then(r => { if (!r.ok) throw new Error(`${url} → ${r.status}`); return r.json(); });
export const setStatus = text => { $('status').textContent = text; };
const toShell = msg => { try { if (parent !== window) parent.postMessage(msg, '*'); else window.postMessage(msg, '*'); } catch (_) {} };

// What the user last chose, per page kind (`store`): { trueHeight, scenery, floats, traces, colour, speed }.
const recall = store => { try { return JSON.parse(localStorage.getItem(store) || '{}'); } catch (_) { return {}; } };

// `seabed`: {bathy_lon, bathy_lat, bathy_z}. `floatTraces`: whether Argo floats' dive lines start switched on.
// `ownLayers`: the page lists vehicles, floats and scenery itself (the mission key), so the Style panel leaves them out.
// `speed`: start at this rate whatever the user last chose; `defaultSpeed`: the rate when nothing was chosen.
export function createView(seabed, { floatTraces = true, store = 'gp_ocean3d', ownLayers = false, defaultSpeed, speed } = {}) {
    const saved = recall(store), keep = patch => { Object.assign(saved, patch); try { localStorage.setItem(store, JSON.stringify(saved)); } catch (_) {} };
    const stage = createStage($('view')), world = createWorld(seabed), platforms = [];
    const floor = buildSeabed(world);
    stage.content.add(floor, buildGround(world), buildWater(world));
    stage.frame(world.size, world.heightAt);
    stage.beforeRender.push(projectClearings);

    // The shell's side of things: a panel says when it is touched; the shell says what theme to wear.
    for (const ev of ['pointerdown', 'touchstart']) document.addEventListener(ev, () => toShell({ type: 'panelActivate' }), true);
    const wear = () => stage.setBackground(getComputedStyle(document.documentElement).getPropertyValue('--sky').trim());
    wear();

    let vertical = 1, timeline = null, scenery = null, following = -1;
    const show = { floats: ownLayers || saved.floats !== false, traces: saved.traces ?? floatTraces, scenery: saved.scenery !== false }, extras = [], timeWatchers = [];
    const shown = p => !p.hidden && (!p.float || show.floats);
    const setVertical = trueHeight => {
        vertical = trueHeight ? 1 / world.stretch : 1;
        stage.setVertical(vertical); if (scenery) scenery.setVertical(vertical);
        $('scaleNote').textContent = trueHeight ? 'True height' : `Vertical scale ×${Math.round(world.stretch)}`;
        if (timeline) timeline.refresh();
    };

    // Follow (the option, and F): off -> each vehicle in turn -> off. Home (and H) also lets go.
    const setFollowing = k => {
        while (k >= 0 && k < platforms.length && (platforms[k].float || platforms[k].hidden)) k++;
        following = k < platforms.length ? k : -1;
        stage.follow(following >= 0 ? () => platforms[following].position : null);
        listOptions();
    };
    const goHome = () => { setFollowing(-1); stage.goHome(); };
    window.addEventListener('keydown', e => { if (e.key === 'f' || e.key === 'F') setFollowing(following + 1); else if (e.key === 'h' || e.key === 'H') goHome(); });

    const applyLayers = () => {
        for (const p of platforms) { p.track.object.visible = shown(p) && p.drawn && (!p.float || show.traces); if (!shown(p)) { p.track.park(); p.position = null; if (p.model) p.model.pose(null); } }
        for (const x of extras) x.object.visible = !x.off && !(x.owner && x.owner.hidden);
        if (scenery) scenery.object.visible = show.scenery;
        if (following >= 0 && !shown(platforms[following])) setFollowing(-1);
        if (timeline) timeline.refresh(); else stage.redraw();
    };
    // The Style panel's options: text rows under View (home, follow, true height) and Layers (each vehicle when there
    // are several, Argo, scenery), a check beside the ones that are on.
    const ICON = { home: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10"/>', follow: '<circle cx="12" cy="12" r="6"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>', height: '<path d="M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4"/>' };
    function listOptions() {
        const rows = $('options'), vehicles = platforms.filter(p => !p.float), floats = platforms.length - vehicles.length;
        const el = (tag, className, text = '') => Object.assign(document.createElement(tag), { className, textContent: text });
        const head = text => rows.append(el('div', 'head', text));
        const option = (label, on, change, { icon, colour, title } = {}) => {
            const b = el('button', on ? 'on' : ''); b.title = title || ''; if (on != null) b.setAttribute('aria-pressed', !!on);
            if (icon) b.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICON[icon]}</svg>`;
            if (colour) b.append(Object.assign(el('span', 'sw'), { style: `background:${colour}` }));
            b.append(el('span', 'name', label)); b.addEventListener('click', () => change(!on)); rows.append(b);
        };
        rows.textContent = '';
        head('View');
        option('Home', null, goHome, { icon: 'home', title: 'Back to the starting view (H)' });
        if (vehicles.length) option(following >= 0 && vehicles.length > 1 ? platforms[following].label : 'Follow', following >= 0, on => setFollowing(vehicles.length > 1 ? following + 1 : on ? 0 : -1), { icon: 'follow', title: vehicles.length > 1 ? 'Follow each vehicle in turn (F)' : 'Follow the vehicle (F)' });
        option('True height', vertical !== 1, on => { keep({ trueHeight: on }); setVertical(on); listOptions(); }, { icon: 'height', title: `Depth is drawn ${Math.round(world.stretch)}× taller than true scale` });
        if (floats || !ownLayers) head('Layers');
        if (vehicles.length > 1 && !ownLayers) for (const p of vehicles) option(p.label, !p.hidden, on => { p.hidden = !on; applyLayers(); listOptions(); }, { colour: p.colour });
        if (floats) { if (!ownLayers) option(`Argo (${floats})`, show.floats, on => { show.floats = on; keep({ floats: on }); applyLayers(); listOptions(); }, { colour: FLOAT_COLOUR }); option('Dive lines', show.traces, on => { show.traces = on; keep({ traces: on }); applyLayers(); listOptions(); }, { colour: FLOAT_COLOUR }); }
        if (!ownLayers) option('Wildlife', show.scenery, on => { show.scenery = on; keep({ scenery: on }); applyLayers(); listOptions(); });
    }
    setVertical(!!saved.trueHeight); listOptions();

    // Scenery: cosmetic, never blocks the view; it keeps frames coming while anything is going on.
    buildScenery(world).then(sc => { scenery = sc; sc.setVertical(vertical); sc.object.visible = show.scenery; stage.content.add(sc.object); stage.beforeRender.push(() => sc.tick(!!timeline && timeline.playing())); stage.redraw(); }).catch(console.error);

    // `track`: {lon, lat, z, time_ms, pitch?}, one entry per fix. `float`: an Argo float — a drawn-in dive, not
    // measurements: it never reshapes the seabed, is not coloured by variable and is skipped by Follow.
    // `line: false`: the vehicle only (a ship, whose legs are drawn with addLine). `sizeKey`: whose size the model takes.
    const addPlatform = async (key, label, track, colour, modelName, { float = false, line: drawn = true, sizeKey, width } = {}) => {
        if (!float && drawn) floor.refresh(world.carve(track.lon, track.lat, track.z));      // measured depths beat the chart
        const line = buildTrack(world, track, colour, float ? { upright: true, width: 1.5 } : width ? { width } : {}), model = await loadModel(modelName || 'slocum', world, sizeKey || modelName || 'slocum').catch(() => null);
        stage.content.add(line.object); if (model) stage.scene.add(model.object);
        const platform = { key, label, colour, track: line, model, position: null, float, hidden: false, drawn };
        platforms.push(platform);
        line.object.visible = drawn && (!float || show.traces);
        listOptions();
        if (timeline) timeline.refresh(); else stage.redraw();      // added after playback began: pose it now
        return platform;
    };
    // A line with no vehicle of its own (a ship's leg, a station's circle); hidden along with `owner`, a platform.
    const addLine = (track, colour, { width, owner } = {}) => {
        const line = buildTrack(world, { ...track, time_ms: track.time_ms || track.lon.map(() => 0) }, colour, { upright: true, width });
        stage.content.add(line.object); extras.push({ object: line.object, owner, off: false }); stage.redraw();
        return line.object;
    };
    // A model standing still (a buoy).
    const addModel = async (name, lon, lat, sizeKey = name) => {
        const model = await loadModel(name, world, sizeKey);
        stage.scene.add(model.object); model.pose({ position: world.place(lon, lat, 0), heading: 0, pitch: 0 }); stage.redraw();
        return model.object;
    };
    // Deck cargo: `p` rides `ship`'s deck until its first fix (`deploy`) and/or from its last one (`recover`). `slot`:
    // [x from midships (+ = bow), y to port] as fractions of the ship's hull length; `length`: the cargo's, likewise.
    const carry = (p, ship, slot, length, mode) => { p.carry = { ...p.carry, ship, slot, length, [mode]: true }; };
    const aboard = (p, now) => !!p.carry && !!p.track.span && ((p.carry.deploy && now < p.track.span[0]) || (p.carry.recover && now >= p.track.span[1]));
    const stow = p => {
        const { ship, slot, length } = p.carry, at = !ship.hidden && ship.pose;
        p.track.park();
        if (!p.model) return;
        if (!at || !ship.model) { p.model.pose(null); return; }
        const S = ship.model.dims, M = p.model.dims, L = S.hullLength, ks = ship.model.scale, kc = length * L / (p.float ? M.height : M.length);
        // In the ship's own frame as drawn (its model eases onto a new heading): the cargo turns with the deck, never about it.
        const hull = ship.model.object, spot = new THREE.Vector3(slot[0] * L * ks, (S.deck - M.bottom * kc) * ks, -slot[1] * L * ks).applyQuaternion(hull.quaternion).add(hull.position);
        p.model.pose({ position: spot.toArray(), quaternion: hull.quaternion, scale: kc * ks });
    };
    const setHidden = (who, hide) => { for (const p of who) p.hidden = hide; applyLayers(); listOptions(); };
    const setScenery = on => { show.scenery = on; keep({ scenery: on }); applyLayers(); };
    // Argo dive lines, and the lines drawn for `owner` (a ship's legs), on their own — the platforms stay.
    const setTraces = on => { show.traces = on; keep({ traces: on }); applyLayers(); listOptions(); };
    const setLines = (owner, on) => { for (const x of extras) if (x.owner === owner) x.off = !on; applyLayers(); };
    const addFloats = (floats, colour = FLOAT_COLOUR) => Promise.all(Object.entries(floats).map(([wmo, f]) => addPlatform(`argo:${wmo}`, `Argo ${wmo}`, cycleTrack(world, f), colour, 'argo', { float: true })));

    const startTimeline = (t0, t1) => {
        timeline = createTimeline($('timebar'), t0, t1, (now, playing) => {
            for (const p of platforms) { if (!shown(p)) continue; p.pose = aboard(p, now) ? null : p.track.at(now, vertical); p.position = p.pose && p.pose.position; if (p.model && !aboard(p, now)) p.model.pose(p.pose, playing); }
            for (const p of platforms) if (shown(p) && aboard(p, now)) stow(p);
            for (const fn of timeWatchers) fn(now);
            stage.redraw();
        }, { speed: speed || saved.speed, defaultSpeed, onSpeed: speed => keep({ speed }) });
        stage.onHold(timeline.hold);
        if (!matchMedia('(prefers-reduced-motion: reduce)').matches) timeline.play(true); else timeline.setTime(t1);
        return timeline;
    };

    // Track colour. `options`: [{key, label, cmap}]; `fetchValues(platformKey, option)` -> {values, units} | null, values
    // aligned with the track as handed to addPlatform. One shared scale over every platform that has the variable.
    // Returns the legend: .choose(option) colours by an option the list may not have (the shell's "follow the plot").
    const setLegend = (options, fetchValues) => {
        const fetched = {};
        return createLegend($('legend'), options, saved.colour, async opt => {
            keep({ colour: opt ? opt.key : '' });
            if (!opt) { for (const p of platforms) p.track.setColours(); stage.redraw(); return null; }
            const got = await Promise.all(platforms.map(p => p.float ? null : fetched[`${p.key}|${opt.key}`] ||= Promise.resolve(fetchValues(p.key, opt)).catch(() => null)));
            const stops = palette(opt.cmap), range = stops.discrete ? [0, stops.length - 1] : limits(got.filter(Boolean).map(c => c.values)), scale = colourScale(stops, range);
            platforms.forEach((p, q) => got[q] ? p.track.setColours(got[q].values, scale) : p.track.setColours());
            stage.redraw();
            return got.some(Boolean) ? { limits: range, units: (got.find(Boolean) || {}).units } : null;
        });
    };

    // Messages from the shell: theme; a point pinned on a plot or the map (jump there and hold; unpinning carries on).
    let resumeAfterPin = false, pinned = false;
    window.addEventListener('message', e => {
        const m = e.data || {};
        if (m.type === 'setTheme') { document.documentElement.setAttribute('data-theme', m.theme === 'dark' ? 'dark' : 'light'); wear(); stage.redraw(); }
        else if (m.type === 'pinTime' && typeof m.timeMs === 'number' && timeline) { if (!pinned) resumeAfterPin = timeline.playing(); pinned = true; timeline.play(false); timeline.setTime(m.timeMs); }
        else if (m.type === 'clearPin' && pinned && timeline) { pinned = false; if (resumeAfterPin) timeline.play(true); }
    });
    const jumpTo = t => { if (timeline) { timeline.play(false); timeline.setTime(t); } };
    return { stage, world, platforms, addPlatform, addFloats, addLine, addModel, carry, setHidden, setScenery, setTraces, setLines, sceneryOn: () => show.scenery, tracesOn: () => show.traces, linesOn: owner => extras.some(x => x.owner === owner && !x.off), vertical: () => vertical, onTime: fn => timeWatchers.push(fn), jumpTo,
        startTimeline, setLegend, ready: () => toShell({ type: 'view3dReady' }) };
}
