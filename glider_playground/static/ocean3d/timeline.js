// The mission clock and its bar: play / pause, speed, scrubber, keyboard. Runs its own frames only while playing.
// Mission seconds per wall-clock second.
// [rate, on the button, in its tooltip]
const SPEEDS = [[1, '1×', 'Real time'], [60, '1 min/s', '1 minute / sec'], [600, '10 min/s', '10 minutes / sec'], [3600, '1 h/s', '1 hour / sec'], [36000, '10 h/s', '10 hours / sec'], [86400, '1 day/s', '1 day / sec']], DEFAULT_SPEED = 3600;
const RESUME_MS = 1500;
const fmt = ms => new Date(ms).toISOString().slice(0, 16).replace('T', ' ');      // data is UTC throughout
const fmtShort = ms => new Date(ms).toLocaleString('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).replace(',', '');

// `speed`: the speed to start at (one of SPEEDS, else `defaultSpeed`, else the default); `onSpeed(x)` when the user changes it.
export function createTimeline(el, t0, t1, onTime, { speed: startSpeed, defaultSpeed = DEFAULT_SPEED, onSpeed } = {}) {
    el.style.setProperty('--resume', RESUME_MS + 'ms');
    el.innerHTML = `<button class="play" aria-label="Play"><span class="icon"></span><svg class="ring" viewBox="0 0 30 30"><rect x="2" y="2" width="26" height="26" rx="7" pathLength="100"/></svg></button>
        <input class="scrub" type="range" min="0" max="1000" value="0" aria-label="Time"><button class="speed"></button><span class="clock"><span class="full"></span><span class="short"></span></span>`;
    const btn = el.querySelector('.play'), icon = btn.querySelector('.icon'), scrub = el.querySelector('.scrub'), clock = el.querySelector('.clock'), speedBtn = el.querySelector('.speed');
    const speed = { value: SPEEDS.some(([x]) => x === startSpeed) ? startSpeed : defaultSpeed };
    // The speed tag steps through SPEEDS on each tap, wrapping round.
    const showSpeed = () => { const [, short, long] = SPEEDS.find(([x]) => x === speed.value); speedBtn.textContent = short; speedBtn.title = `Playback speed: ${long.toLowerCase()} — tap for the next`; };
    speedBtn.addEventListener('click', () => { const k = SPEEDS.findIndex(([x]) => x === speed.value); speed.value = SPEEDS[(k + 1) % SPEEDS.length][0]; showSpeed(); if (onSpeed) onSpeed(speed.value); });
    showSpeed();
    const ICON = { play: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
                   pause: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>' };
    let now = t0, playing = false, last = 0, holding = false, cooldown = 0;

    const setTime = (t, fromScrub) => {
        now = Math.max(t0, Math.min(t1, t));
        if (!fromScrub) scrub.value = Math.round((now - t0) / (t1 - t0) * 1000);
        clock.firstChild.textContent = fmt(now) + ' UTC'; clock.lastChild.textContent = fmtShort(now);
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
        if (e.target.matches('input, select, textarea')) return;
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
    return { setTime, play, hold, refresh: () => setTime(now), playing: () => playing && !holding && !cooldown };
}
