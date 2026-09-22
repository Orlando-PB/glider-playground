// Colour-by-variable: palettes from plot_presets.json (window.GP_PLOT_PRESETS, loaded by the page), a value -> colour
// scale, and the small legend card that picks the variable and shows its colourbar.
import * as THREE from 'three';

const UNITS = { degree_Celsius: '°C', degrees_Celsius: '°C', degC: '°C', '1': '' };      // CF spellings -> what people write
const FALLBACK = ['#440154', '#21918c', '#fde725'];

// Integer flags (science phases and the like): one fixed colour per value, as main_plot.html draws them.
const DISCRETE_09 = ['#9ca3af', '#22c55e', '#3b82f6', '#f97316', '#a855f7', '#06b6d4', '#ef4444', '#eab308', '#ec4899', '#84cc16'];

// Colour stops for a cmap name; `.discrete` marks one-colour-per-integer palettes.
export function palette(cmap) {
    const all = (window.GP_PLOT_PRESETS || {}).palettes || {}, name = String(cmap || '');
    if (name.startsWith('discrete')) return Object.assign([...DISCRETE_09], { discrete: true });
    const stops = all[name.replace(/_r$/, '')] || FALLBACK;
    return name.endsWith('_r') ? [...stops].reverse() : stops;
}

// Limits that ignore the wildest 2 % at each end, over every platform's values at once: one shared scale.
export function limits(arrays) {
    const all = [];
    for (const a of arrays) for (const v of a) if (v != null && Number.isFinite(v)) all.push(v);
    if (!all.length) return [0, 1];
    all.sort((a, b) => a - b);
    const lo = all[Math.floor(all.length * 0.02)], hi = all[Math.ceil(all.length * 0.98) - 1];
    return hi > lo ? [lo, hi] : [lo - 0.5, lo + 0.5];
}

export function colourScale(stops, [lo, hi]) {
    const cols = stops.map(c => new THREE.Color(c)), out = new THREE.Color();
    if (stops.discrete) return v => cols[Math.max(0, Math.min(cols.length - 1, Math.round(v)))];
    return v => {
        const t = Math.max(0, Math.min(1, (v - lo) / (hi - lo))) * (cols.length - 1), i = Math.min(cols.length - 2, Math.floor(t));
        return out.lerpColors(cols[i], cols[i + 1], t - i);
    };
}

// The track-colour picker and its colourbar. `options`: [{key, label, cmap}]; `remembered`: the key to start on;
// `onPick(option | null)` resolves to {limits, units} once the tracks are recoloured (null: nothing to show).
// Returns { choose(option) }: colour by an option, adding it to the list if it is new.
export function createLegend(el, options, remembered, onPick) {
    el.innerHTML = `<label>Track colour <select><option value="">Platform</option>${options.map(o => `<option value="${o.key}">${o.label}</option>`).join('')}</select></label>
        <div class="bar" hidden></div><div class="ends" hidden><span></span><span class="units"></span><span></span></div>`;
    const select = el.querySelector('select'), bar = el.querySelector('.bar'), ends = el.querySelector('.ends'), [lo, units, hi] = ends.children, all = [...options];
    const num = v => Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(1) : v.toPrecision(2);
    const pick = async () => {
        const opt = all.find(o => o.key === select.value) || null;
        select.disabled = true; el.classList.add('loading');
        const got = await onPick(opt).catch(e => { console.error(e); return null; });
        select.disabled = false; el.classList.remove('loading');
        bar.hidden = ends.hidden = !(opt && got);
        if (!opt || !got) { if (opt) select.value = ''; return; }
        const stops = palette(opt.cmap);
        bar.style.background = `linear-gradient(to right, ${stops.discrete ? stops.map((c, q) => `${c} ${q / stops.length * 100}% ${(q + 1) / stops.length * 100}%`).join(', ') : stops.join(', ')})`;
        lo.textContent = stops.discrete ? '0' : num(got.limits[0]); hi.textContent = stops.discrete ? String(stops.length - 1) : num(got.limits[1]); units.textContent = UNITS[got.units] ?? got.units ?? '';
    };
    select.addEventListener('change', pick);
    const choose = opt => {
        if (!all.some(o => o.key === opt.key)) { all.push(opt); select.append(new Option(opt.label, opt.key)); }
        if (select.value !== opt.key) { select.value = opt.key; pick(); }
    };
    if (remembered && all.some(o => o.key === remembered)) { select.value = remembered; pick(); }
    return { choose };
}
