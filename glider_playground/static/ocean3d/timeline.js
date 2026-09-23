// The mission clock and its bar: play / pause, speed, scrubber, keyboard. Runs its own frames only while playing.
// Mission seconds per wall-clock second.
// [rate, on the button, in its tooltip]
const SPEEDS = [[1, '1×', 'Real time'], [60, '1 min/s', '1 minute / sec'], [600, '10 min/s', '10 minutes / sec'], [3600, '1 h/s', '1 hour / sec'], [36000, '10 h/s', '10 hours / sec'], [86400, '1 day/s', '1 day / sec']], DEFAULT_SPEED = 3600;
const RESUME_MS = 1500;
const fmt = ms => new Date(ms).toISOString().slice(0, 16).replace('T', ' ');      // data is UTC throughout
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const fmtShort = ms => { const d = new Date(ms), p = n => String(n).padStart(2, '0'); return `${p(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`; };

// `speed`: the speed to start at (one of SPEEDS, else `defaultSpeed`, else the default); `onSpeed(x)` when the user changes it.
// `onWindow(a, b)`: the span of track drawn (ms) whenever it changes — set by the clip handles at either end of the
// scrubber. The playhead always sits inside it: reaching a handle pushes it along, and if the other handle has been moved
// off its end too it comes along as well, so a chosen length of trail keeps its length as the vehicle moves.
export function createTimeline(el, t0, t1, onTime, { speed: startSpeed, defaultSpeed = DEFAULT_SPEED, onSpeed, onWindow } = {}) {
    el.style.setProperty('--resume', RESUME_MS + 'ms');
    el.innerHTML = `<button class="play" aria-label="Play"><span class="icon"></span><svg class="ring" viewBox="0 0 30 30"><rect x="2" y="2" width="26" height="26" rx="7" pathLength="100"/></svg></button>
        <span class="scrubWrap"><span class="rail"><span class="fill"></span></span><input class="scrub" type="range" min="0" max="1000" value="0" aria-label="Time"><span class="dim l"></span><span class="dim r"></span>
        <span class="clip l" role="slider" tabindex="0" aria-label="Track drawn from" title="Drag to hide the track before here (double-tap to reset)"></span>
        <span class="clip r" role="slider" tabindex="0" aria-label="Track drawn to" title="Drag to hide the track after here — down to the vehicle for a trail only (double-tap to reset)"></span></span>
        <button class="speed"></button><span class="clock"><span class="full"></span><span class="short"></span></span>`;
    const btn = el.querySelector('.play'), icon = btn.querySelector('.icon'), scrub = el.querySelector('.scrub'), clock = el.querySelector('.clock'), speedBtn = el.querySelector('.speed');
    const wrap = el.querySelector('.scrubWrap'), fill = el.querySelector('.fill'), clips = { l: el.querySelector('.clip.l'), r: el.querySelector('.clip.r') }, dims = { l: el.querySelector('.dim.l'), r: el.querySelector('.dim.r') };
    const speed = { value: SPEEDS.some(([x]) => x === startSpeed) ? startSpeed : defaultSpeed };
    // The speed tag steps through SPEEDS on each tap, wrapping round.
    const showSpeed = () => { const [, short, long] = SPEEDS.find(([x]) => x === speed.value); speedBtn.textContent = short; speedBtn.title = `Playback speed: ${long.toLowerCase()} — tap for the next`; speedBtn.setAttribute('aria-label', `Playback speed ${long.toLowerCase()}, press for the next speed`); };
    speedBtn.addEventListener('click', () => { const k = SPEEDS.findIndex(([x]) => x === speed.value); speed.value = SPEEDS[(k + 1) % SPEEDS.length][0]; showSpeed(); if (onSpeed) onSpeed(speed.value); });
    showSpeed();
    const ICON = { play: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
                   pause: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>' };
    let now = t0, playing = false, last = 0, holding = false, cooldown = 0;

    // The drawn window [a, b] and its handles. The scrubber is drawn by hand (rail, fill, thumb) so the thumb, the
    // handles and the greyed bands all share one geometry: PAD = half the thumb's width, where the thumb's travel starts and ends.
    const win = { a: t0, b: t1 }, sent = { a: NaN, b: NaN }, PAD = 6;
    const frac = t => (t - t0) / (t1 - t0);
    const placeClips = () => {
        for (const [k, t] of [['l', win.a], ['r', win.b]]) {
            const f = frac(t);
            clips[k].style.left = `calc(${PAD}px + ${f} * (100% - ${2 * PAD}px))`; clips[k].classList.toggle('home', k === 'l' ? win.a <= t0 : win.b >= t1);
            clips[k].setAttribute('aria-valuetext', fmt(t) + ' UTC');
            dims[k].style.width = `calc(${k === 'l' ? f : 1 - f} * (100% - ${2 * PAD}px))`;
        }
    };
    const settleWindow = () => {
        if (now > win.b) { const d = now - win.b; win.b = now; if (win.a > t0) win.a = Math.min(now, win.a + d); }
        if (now < win.a) { const d = win.a - now; win.a = now; if (win.b < t1) win.b = Math.max(now, win.b - d); }
        if (win.a === sent.a && win.b === sent.b) return;
        sent.a = win.a; sent.b = win.b; placeClips();
        if (onWindow) onWindow(win.a, win.b);
    };
    for (const k of ['l', 'r']) {
        const clip = clips[k];
        const at = e => { const r = wrap.getBoundingClientRect(); return t0 + (t1 - t0) * Math.max(0, Math.min(1, (e.clientX - r.left - PAD) / (r.width - 2 * PAD))); };
        // A handle snaps onto the playhead when close (the right one dragged onto it leaves a trail only) and pushes it
        // along when dragged past it, as the playhead pushes the handles.
        const move = t => { if (Math.abs(t - now) < (t1 - t0) * 0.015) t = now; if (k === 'l') win.a = t; else win.b = t; setTime(k === 'l' ? Math.max(now, t) : Math.min(now, t)); };
        clip.addEventListener('pointerdown', e => { e.preventDefault(); clip.setPointerCapture(e.pointerId); clip.classList.add('drag'); move(at(e)); });
        clip.addEventListener('pointermove', e => { if (clip.classList.contains('drag')) move(at(e)); });
        for (const ev of ['pointerup', 'pointercancel']) clip.addEventListener(ev, () => clip.classList.remove('drag'));
        clip.addEventListener('dblclick', () => move(k === 'l' ? t0 : t1));
        clip.addEventListener('keydown', e => {
            const step = (t1 - t0) / 100, t = k === 'l' ? win.a : win.b;
            if (e.key === 'ArrowLeft') move(t - step); else if (e.key === 'ArrowRight') move(t + step); else if (e.key === 'Home' || e.key === 'End') move(k === 'l' ? t0 : t1); else return;
            e.preventDefault(); e.stopPropagation();
        });
    }

    const setTime = (t, fromScrub) => {
        now = Math.max(t0, Math.min(t1, t));
        if (!fromScrub) scrub.value = Math.round((now - t0) / (t1 - t0) * 1000);
        fill.style.width = `calc(${scrub.value / 1000} * 100%)`;
        settleWindow();
        // Write the clock only when its text changes (playback ticks every frame; the minute doesn't).
        const full = fmt(now) + ' UTC', short = fmtShort(now);
        if (clock.firstChild.textContent !== full) clock.firstChild.textContent = full;
        if (clock.lastChild.textContent !== short) clock.lastChild.textContent = short;
        onTime(now, playing);
    };
    const tick = ts => {
        if (!playing) return;
        if (holding || cooldown) last = 0;      // held: time stands still, and none is owed afterwards
        else { if (last) setTime(now + (ts - last) * +speed.value); last = ts; }
        if (now >= t1) play(false); else requestAnimationFrame(tick);
    };
    // Moving the camera holds playback; it carries on by itself RESUME_MS after the user lets go, the ring round the
    // play button counting that down. A manual pause stays paused.
    const clearCooldown = () => { clearTimeout(cooldown); cooldown = 0; btn.classList.remove('cooldown'); };
    const hold = on => {
        holding = on; clearCooldown();
        if (on || !playing) return;
        void btn.offsetWidth;      // restart the ring
        btn.classList.add('cooldown');
        cooldown = setTimeout(clearCooldown, RESUME_MS);
    };
    const play = on => {
        clearCooldown();
        if (on && now >= t1) setTime(t0);
        playing = on; last = 0;
        icon.innerHTML = on ? ICON.pause : ICON.play; btn.setAttribute('aria-label', on ? 'Pause' : 'Play');
        if (on) requestAnimationFrame(tick);
    };
    btn.addEventListener('click', () => play(!playing));
    scrub.addEventListener('input', () => { play(false); setTime(t0 + (t1 - t0) * scrub.value / 1000, true); });
    document.addEventListener('visibilitychange', () => { last = 0; });      // no jump ahead after the tab was hidden
    window.addEventListener('keydown', e => {
        if (e.target.matches('input, select, textarea, .clip')) return;
        const step = +speed.value * 10000;      // arrow keys: ten seconds' worth of playback
        if (e.key === ' ') play(!playing);
        else if (e.key === 'ArrowLeft') { play(false); setTime(now - step); }
        else if (e.key === 'ArrowRight') { play(false); setTime(now + step); }
        else if (e.key === 'Home') { play(false); setTime(t0); }
        else if (e.key === 'End') { play(false); setTime(t1); }
        else return;
        e.preventDefault();
    });
    play(false); setTime(t0);
    return { setTime, play, hold, refresh: () => setTime(now), playing: () => playing && !holding && !cooldown, window: () => [win.a, win.b] };
}
