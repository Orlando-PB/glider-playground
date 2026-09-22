// HTML labels pinned to points in the scene, re-placed on every drawn frame. A label is fixed (`anchor`, a
// world.place() point: its height stretches with the vertical scale) or rides a platform (`platform`: at surface
// height straight above it, a dotted line dropping to the vehicle).
import * as THREE from 'three';

const SVG = 'http://www.w3.org/2000/svg';

// `layer`/`svg`: a <div> and an <svg> laid over the canvas. `vertical()`: the stage's vertical scale.
export function createLabels(layer, svg, canvas, vertical) {
    const items = [], hiddenLayers = new Set(), v = new THREE.Vector3();
    let now = Infinity;
    const project = (camera, x, y, z) => {
        if (v.set(x, y, z).applyMatrix4(camera.matrixWorldInverse).z >= 0) return null;      // behind the camera
        v.set(x, y, z).project(camera);
        return [(0.5 + 0.5 * v.x) * canvas.clientWidth, (0.5 - 0.5 * v.y) * canvas.clientHeight];
    };
    const lineTo = (el, a, b) => { el.setAttribute('x1', a[0]); el.setAttribute('y1', a[1]); el.setAttribute('x2', b[0]); el.setAttribute('y2', b[1]); };
    const newLine = (colour, width, dash) => {
        const el = document.createElementNS(SVG, 'line');
        el.setAttribute('stroke', colour); el.setAttribute('stroke-width', width); if (dash) el.setAttribute('stroke-dasharray', dash);
        svg.append(el); return el;
    };

    // `o`: { anchor | platform, layer, owner (a platform: hidden with it), time (ms: faded until then), offset [dx, dy] px,
    // leader (colour of a line from the anchor to the offset label), drop (colour of a platform label's drop line), onClick }.
    const add = (html, o) => {
        if (!o.anchor && !o.platform) return null;
        const node = Object.assign(document.createElement('div'), { innerHTML: html }).firstElementChild;
        layer.append(node);
        if (o.onClick) { node.classList.add('clickable'); node.addEventListener('click', o.onClick); }
        items.push({ ...o, node, leader: o.leader && newLine(o.leader, 2), drop: o.drop && newLine(o.drop, 1, '2 3') });
        return node;
    };

    const place = camera => {
        const k = vertical();
        for (const it of items) {
            const p = it.platform, off = hiddenLayers.has(it.layer) || (it.owner && it.owner.hidden) || (p && (p.hidden || !p.position));
            const at = off ? null : p ? project(camera, p.position[0], Math.max(0, p.position[1]), p.position[2]) : project(camera, it.anchor[0], it.anchor[1] * k, it.anchor[2]);
            if (it.drop) {
                const below = at && p.position[1] < 0 && project(camera, ...p.position);
                it.drop.style.display = below ? '' : 'none';
                if (below) lineTo(it.drop, at, below);
            }
            it.node.style.display = at ? '' : 'none';
            if (it.leader) it.leader.style.display = at ? '' : 'none';
            if (!at) continue;
            const x = at[0] + (it.offset ? it.offset[0] : 0), y = at[1] + (it.offset ? it.offset[1] : 0), future = it.time != null && it.time > now;
            it.node.style.translate = `${x.toFixed(1)}px ${y.toFixed(1)}px`;
            it.node.classList.toggle('future', future);
            if (it.leader) { it.leader.style.opacity = future ? 0.3 : 1; lineTo(it.leader, at, [x, y]); }
        }
    };
    return { add, place, setTime: t => { now = t; }, showLayer: (id, on) => { hiddenLayers[on ? 'delete' : 'add'](id); } };
}
