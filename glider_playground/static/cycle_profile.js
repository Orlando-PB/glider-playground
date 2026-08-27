/**
 * cycle_profile.js
 *
 * Self-contained module managing profile, cycle, SCI_PHASE and direction
 * filtering state + UI for Glider Playground.
 *
 * Usage in index.html:
 *   CycleProfile.init(domElements, onChangeFn);
 *   CycleProfile.loadFile(fileId);
 *   CycleProfile.setZoomBounds(bounds, isXDateTime);
 *   const extra = CycleProfile.getParams();   // add to plot URL
 *   CycleProfile.resetState();                // clear filters (keep lists)
 *   CycleProfile.fullReset();                 // clear everything on file change
 *
 * Cross-linking:
 *   - Profile arrows constrain to profiles within the selected cycle.
 *   - Cycle arrows, when a profile is selected, jump to the cycle containing
 *     that profile (clearing the profile selection), then step normally.
 *   - Prev/next arrows are disabled at list boundaries.
 */

const CycleProfile = (() => {
    'use strict';

    // ── Display maps ─────────────────────────────────────────────────────────
    const DIR_ICONS  = { 1: 'arrow_upward', '-1': 'arrow_downward', 0: 'swap_horiz' };
    const DIR_LABELS = { 1: 'Ascending', '-1': 'Descending', 0: 'Transect' };

    const PHASE_NAMES = {
        0: 'Unknown', 1: 'Ascent', 2: 'Descent', 3: 'Surfacing',
        4: 'Parking', 5: 'Inflection', 6: 'Propelled', 7: 'Transition',
        8: 'Phase 8', 9: 'Phase 9',
    };

    const PHASE_COLORS = [
        '#9ca3af', '#22c55e', '#3b82f6', '#f97316', '#a855f7',
        '#06b6d4', '#ef4444', '#eab308', '#ec4899', '#84cc16',
    ];

    // ── State ────────────────────────────────────────────────────────────────
    let _profileList  = [];
    let _cycleList    = [];
    let _cycleVar     = null;
    let _hasSciPhase  = false;
    let _hasDirection = false;

    let _profileNum   = null;
    let _cycleNum     = null;
    let _sciPhases    = [];
    let _dirFilter    = [];

    let _zoomBounds   = null;
    let _isXDateTime  = false;

    let _els          = {};
    let _onChange     = null;

    // ── Public API ───────────────────────────────────────────────────────────

    function init(elements, onChangeFn) {
        _els      = elements;
        _onChange = onChangeFn;
        _bindProfileEvents();
        _bindCycleEvents();
        _bindPhaseEvents();
        _bindDirEvents();
    }

    async function loadFile(fileId) {
        fullReset();
        await Promise.all([_loadProfiles(fileId), _loadCycles(fileId)]);
    }

    function setZoomBounds(bounds, isXDateTime) {
        _zoomBounds  = bounds;
        _isXDateTime = isXDateTime;
    }

    function getParams() {
        const p = {};
        if (_profileNum !== null) p.profile_num      = _profileNum;
        if (_cycleNum   !== null) p.cycle_num        = _cycleNum;
        if (_cycleVar)            p.cycle_var        = _cycleVar;
        if (_sciPhases.length)    p.sci_phases       = _sciPhases.join(',');
        if (_dirFilter.length)    p.direction_filter = _dirFilter.join(',');
        return p;
    }

    /** Returns which features are available in the current file. */
    function getCapabilities() {
        return {
            has_profiles:  _profileList.length > 0,
            has_cycles:    _cycleList.length > 0,
            has_sci_phase: _hasSciPhase,
            has_direction: _hasDirection,
        };
    }

    /** Set the phase filter externally (e.g. from Jelly). Empty array = show all. */
    function setPhases(arr) {
        _sciPhases = Array.isArray(arr) ? arr.map(Number).filter(n => !isNaN(n)) : [];
        _syncPhaseUI();
        _fire();
    }

    /** Set the direction filter externally (e.g. from Jelly). Empty array = show all. */
    function setDirection(arr) {
        _dirFilter = Array.isArray(arr) ? arr.map(Number).filter(n => !isNaN(n)) : [];
        _syncDirUI();
        _fire();
    }

    function resetState() {
        _profileNum = null;
        _cycleNum   = null;
        _sciPhases  = [];
        _dirFilter  = [];
        _syncProfileUI();
        _syncCycleUI();
        _syncPhaseUI();
        _syncDirUI();
    }

    function fullReset() {
        _profileList  = [];
        _cycleList    = [];
        _cycleVar     = null;
        _hasSciPhase  = false;
        _hasDirection = false;
        resetState();
        _hideElement(_els.profileContainer);
        _hideElement(_els.cycleContainer);
        _hideElement(_els.cycleNavSep);
        _hideElement(_els.navigateContainer);
        _hideElement(_els.navigateDivider);
        _hideElement(_els.phaseContainer);
        _hideElement(_els.phaseDivider);
        _hideElement(_els.dirContainer);
    }

    /** Show/hide the outer navigate group based on inner containers. */
    function _syncNavigateContainer() {
        const profVis  = _els.profileContainer && _els.profileContainer.style.display !== 'none';
        const cycleVis = _els.cycleContainer   && _els.cycleContainer.style.display   !== 'none';
        const anyVis   = profVis || cycleVis;
        if (anyVis) {
            _showElement(_els.navigateContainer);
            _showElement(_els.navigateDivider, 'block');
        } else {
            _hideElement(_els.navigateContainer);
            _hideElement(_els.navigateDivider);
        }
        if (profVis && cycleVis) _showElement(_els.cycleNavSep, 'block');
        else                     _hideElement(_els.cycleNavSep);
    }

    // ── Cross-linking helpers ────────────────────────────────────────────────

    /** Returns profiles whose time window overlaps with the selected cycle. */
    function _profilesInCycle(cycleNum) {
        if (cycleNum === null || !_cycleList.length) return _profileList;
        const cyc = _cycleList.find(c => c.number === cycleNum);
        if (!cyc || !cyc.time_min || !cyc.time_max) return _profileList;
        const cMin = _parseUTC(cyc.time_min);
        const cMax = _parseUTC(cyc.time_max);
        const filtered = _profileList.filter(p => {
            if (!p.time_min || !p.time_max) return true;
            return _parseUTC(p.time_max) >= cMin &&
                   _parseUTC(p.time_min) <= cMax;
        });
        return filtered.length ? filtered : _profileList;
    }

    /** Returns the cycle number that temporally contains the given profile, or null. */
    function _cycleForProfile(profileNum) {
        if (profileNum === null || !_cycleList.length) return null;
        const prof = _profileList.find(p => p.number === profileNum);
        if (!prof || !prof.time_min || !prof.time_max) return null;
        const pMid = (_parseUTC(prof.time_min) + _parseUTC(prof.time_max)) / 2;
        const containing = _cycleList.filter(c => {
            if (!c.time_min || !c.time_max) return false;
            return _parseUTC(c.time_min) <= pMid &&
                   pMid <= _parseUTC(c.time_max);
        });
        if (containing.length) return containing[0].number;
        // Fallback: nearest cycle by time distance
        let best = null, bestDist = Infinity;
        for (const c of _cycleList) {
            if (!c.time_min || !c.time_max) continue;
            const cMid = (_parseUTC(c.time_min) + _parseUTC(c.time_max)) / 2;
            const dist = Math.abs(cMid - pMid);
            if (dist < bestDist) { bestDist = dist; best = c.number; }
        }
        return best;
    }

    // ── Arrow-greying ─────────────────────────────────────────────────────────

    function _setArrowState(prevBtn, nextBtn, idx, total) {
        if (!prevBtn || !nextBtn) return;
        if (total <= 0) {
            prevBtn.disabled = nextBtn.disabled = true;
        } else if (idx < 0) {
            // Nothing selected yet: next (→) picks the first, prev (←) the last,
            // so both arrows must stay enabled.
            prevBtn.disabled = nextBtn.disabled = false;
        } else {
            prevBtn.disabled = idx <= 0;
            nextBtn.disabled = idx >= total - 1;
        }
        [prevBtn, nextBtn].forEach(btn => {
            btn.classList.toggle('opacity-30', btn.disabled);
            btn.style.cursor = btn.disabled ? 'not-allowed' : '';
        });
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    function _hideElement(el) { if (el) el.style.display = 'none'; }
    function _showElement(el, display) { if (el) el.style.display = display || 'flex'; }
    function _fire() { if (_onChange) _onChange(); }

    // time_min/time_max (from /api/profiles, /api/cycles) and zoom bounds are
    // naive-UTC timestamps with no timezone designator (e.g. '2026-04-25T10:08:25'
    // or '2026-04-25 10:08:25'). `new Date(str)` on a string like that is parsed
    // as BROWSER-LOCAL time per the JS spec (only date-only strings default to
    // UTC), so anywhere the browser isn't UTC this silently shifted every
    // profile/cycle time comparison by the local offset (e.g. BST = +1h) — the
    // same class of bug main_plot.html works around for Plotly's axis strings.
    // Force UTC by normalizing to 'T' and appending 'Z' before parsing.
    function _parseUTC(v) {
        if (typeof v === 'number') return v;
        if (!v) return NaN;
        return new Date(String(v).replace(' ', 'T') + 'Z').getTime();
    }

    // ── Profile ───────────────────────────────────────────────────────────────

    async function _loadProfiles(fileId) {
        try {
            const res  = await fetch(`/api/profiles?id=${encodeURIComponent(fileId)}`);
            const data = await res.json();
            if (data.has_profiles && data.profiles.length > 0) {
                _profileList = data.profiles;
                _showElement(_els.profileContainer);
            } else {
                _hideElement(_els.profileContainer);
            }
            _syncNavigateContainer();
        } catch (e) {
            console.error('[CycleProfile] loadProfiles failed:', e);
            _hideElement(_els.profileContainer);
            _syncNavigateContainer();
        }
        _syncProfileUI();
    }

    function _syncProfileUI() {
        const inp  = _els.profileNumInput;
        const icon = _els.profileDirIcon;
        const clr  = _els.profileClearBtn;
        if (!inp) return;

        if (_profileNum === null) {
            inp.value = '';
            if (icon) { icon.textContent = ''; icon.title = ''; }
            if (clr)  clr.style.display = 'none';
        } else {
            inp.value = _profileNum;
            if (clr) clr.style.display = 'inline-flex';
            const entry = _profileList.find(p => p.number === _profileNum);
            const dir   = entry && entry.direction !== undefined ? entry.direction : null;
            if (icon) {
                if (dir !== null && DIR_ICONS[dir] !== undefined) {
                    icon.textContent = DIR_ICONS[dir];
                    icon.title = DIR_LABELS[dir] || '';
                } else {
                    icon.textContent = '';
                    icon.title = '';
                }
            }
        }

        // Update arrow states based on constrained list
        const pool = _profilesInCycle(_cycleNum);
        const nums = pool.map(p => p.number);
        const idx  = _profileNum !== null ? nums.indexOf(_profileNum) : -1;
        _setArrowState(_els.profilePrevBtn, _els.profileNextBtn,
            _profileNum === null ? -1 : idx, nums.length);
    }

    function _stepProfile(delta) {
        // When a cycle is selected, constrain profiles to that cycle
        const pool = _profilesInCycle(_cycleNum);

        let candidates = pool;

        // Further zoom-constrain when no profile is selected. _zoomBounds comes
        // from the plot iframe's own Plotly relayout event, NOT server data — its
        // date strings follow Plotly's own (occasionally browser-local) format,
        // so treating them as naive-UTC via _parseUTC would misapply the fix
        // meant for time_min/time_max. Bare Date parsing matches what main_plot's
        // own zoom-echo handling expects here.
        if (_profileNum === null && _zoomBounds && _isXDateTime &&
                pool.some(p => p.time_min && p.time_max)) {
            const zMin = new Date(_zoomBounds.xMin).getTime();
            const zMax = new Date(_zoomBounds.xMax).getTime();
            const inZoom = pool.filter(p => {
                if (!p.time_min || !p.time_max) return false;
                return _parseUTC(p.time_max) >= zMin &&
                       _parseUTC(p.time_min) <= zMax;
            });
            if (inZoom.length) candidates = inZoom;
        }

        const nums = candidates.map(p => p.number);
        let idx = nums.indexOf(_profileNum);
        if (idx === -1) idx = delta > 0 ? -1 : nums.length;
        idx = Math.max(0, Math.min(nums.length - 1, idx + delta));
        _profileNum = nums[idx];
        _syncProfileUI();
        _fire();
    }

    function _bindProfileEvents() {
        _els.profilePrevBtn  ?.addEventListener('click',  () => _stepProfile(-1));
        _els.profileNextBtn  ?.addEventListener('click',  () => _stepProfile(1));
        _els.profileClearBtn ?.addEventListener('click',  () => {
            _profileNum = null; _syncProfileUI(); _fire();
        });
        _els.profileNumInput?.addEventListener('change', () => {
            const v = _els.profileNumInput.value.trim();
            _profileNum = v === '' ? null : (isNaN(Number(v)) ? _profileNum : Number(v));
            _syncProfileUI(); _fire();
        });
    }

    // ── Cycle ─────────────────────────────────────────────────────────────────

    async function _loadCycles(fileId) {
        try {
            const res  = await fetch(`/api/cycles?id=${encodeURIComponent(fileId)}`);
            const data = await res.json();

            _cycleVar     = data.cycle_var || null;
            _hasSciPhase  = !!data.has_sci_phase;
            _hasDirection = !!data.has_direction;

            if (data.has_cycles && data.cycles.length > 0) {
                _cycleList = data.cycles;
                _showElement(_els.cycleContainer);
            } else {
                _hideElement(_els.cycleContainer);
            }
            _syncNavigateContainer();

            if (_hasSciPhase) {
                _showElement(_els.phaseContainer);
                _showElement(_els.phaseDivider, 'block');
                _buildPhaseChips();
            } else {
                _hideElement(_els.phaseContainer);
                _hideElement(_els.phaseDivider);
            }

            if (_hasDirection) _showElement(_els.dirContainer);
            else               _hideElement(_els.dirContainer);

        } catch (e) {
            console.error('[CycleProfile] loadCycles failed:', e);
            _hideElement(_els.cycleContainer);
            _syncNavigateContainer();
        }
        _syncCycleUI();
        _syncPhaseUI();
        _syncDirUI();
    }

    function _syncCycleUI() {
        const inp = _els.cycleNumInput;
        const clr = _els.cycleClearBtn;
        if (!inp) return;
        if (_cycleNum === null) {
            inp.value = '';
            if (clr) clr.style.display = 'none';
        } else {
            inp.value = _cycleNum;
            if (clr) clr.style.display = 'inline-flex';
        }

        const nums = _cycleList.map(c => c.number);
        const idx  = _cycleNum !== null ? nums.indexOf(_cycleNum) : -1;
        _setArrowState(_els.cyclePrevBtn, _els.cycleNextBtn,
            _cycleNum === null ? -1 : idx, nums.length);
    }

    function _stepCycle(delta) {
        if (!_cycleList.length) return;

        // When a profile is selected, first snap to the cycle containing it
        if (_profileNum !== null && _cycleNum === null) {
            const containing = _cycleForProfile(_profileNum);
            if (containing !== null) {
                _cycleNum   = containing;
                _profileNum = null;   // profile no longer needed; cycle shows it all
                _syncProfileUI();
                _syncCycleUI();
                _fire();
                return;   // first press just snaps; subsequent presses step normally
            }
        }

        let candidates = _cycleList;

        if (_cycleNum === null && _zoomBounds && _isXDateTime &&
                _cycleList.some(c => c.time_min && c.time_max)) {
            const zMin = _parseUTC(_zoomBounds.xMin);
            const zMax = _parseUTC(_zoomBounds.xMax);
            const inZoom = _cycleList.filter(c => {
                if (!c.time_min || !c.time_max) return false;
                return _parseUTC(c.time_max) >= zMin &&
                       _parseUTC(c.time_min) <= zMax;
            });
            if (inZoom.length) candidates = inZoom;
        }

        const nums = candidates.map(c => c.number);
        let idx = nums.indexOf(_cycleNum);
        if (idx === -1) idx = delta > 0 ? -1 : nums.length;
        idx = Math.max(0, Math.min(nums.length - 1, idx + delta));
        _cycleNum = nums[idx];
        // A profile selected within the OLD cycle (e.g. profile 10 of cycle 22)
        // doesn't necessarily belong to the new one — clear it so stepping cycles
        // doesn't silently keep showing a single stale profile from before.
        if (_profileNum !== null) { _profileNum = null; _syncProfileUI(); }
        _syncCycleUI();
        _fire();
    }

    function _bindCycleEvents() {
        _els.cyclePrevBtn  ?.addEventListener('click',  () => _stepCycle(-1));
        _els.cycleNextBtn  ?.addEventListener('click',  () => _stepCycle(1));
        _els.cycleClearBtn ?.addEventListener('click',  () => {
            _cycleNum = null; _syncCycleUI(); _fire();
        });
        _els.cycleNumInput?.addEventListener('change', () => {
            const v = _els.cycleNumInput.value.trim();
            _cycleNum = v === '' ? null : (isNaN(Number(v)) ? _cycleNum : Number(v));
            _syncCycleUI(); _fire();
        });
    }

    // ── Phase filter ─────────────────────────────────────────────────────────

    function _buildPhaseChips() {
        const container = _els.phaseChipsContainer;
        if (!container || container.dataset.built) return;
        container.dataset.built = '1';
        container.innerHTML = '';
        for (let i = 0; i <= 7; i++) {
            const btn = document.createElement('button');
            btn.className = 'phase-chip toggle-btn active px-1.5 py-0.5 rounded text-[9px] font-medium';
            btn.dataset.phase = i;
            btn.title = `${i}: ${PHASE_NAMES[i]}`;
            btn.textContent = i;
            btn.style.borderBottom = `2px solid ${PHASE_COLORS[i]}`;
            container.appendChild(btn);
        }
        _bindPhaseEvents();
    }

    function _syncPhaseUI() {
        const chips = _els.phaseChipsContainer
            ? [..._els.phaseChipsContainer.querySelectorAll('.phase-chip')]
            : [];
        chips.forEach(btn => {
            const v = parseInt(btn.dataset.phase);
            btn.classList.toggle('active', _sciPhases.length === 0 || _sciPhases.includes(v));
        });
    }

    function _bindPhaseEvents() {
        const chips = _els.phaseChipsContainer
            ? [..._els.phaseChipsContainer.querySelectorAll('.phase-chip')]
            : [];
        chips.forEach(btn => {
            const fresh = btn.cloneNode(true);
            btn.replaceWith(fresh);
            fresh.addEventListener('click', () => {
                const v = parseInt(fresh.dataset.phase);
                if (_sciPhases.length === 0) {
                    _sciPhases = [v];
                } else {
                    const idx = _sciPhases.indexOf(v);
                    if (idx >= 0) _sciPhases.splice(idx, 1);
                    else { _sciPhases.push(v); _sciPhases.sort((a, b) => a - b); }
                    if (_sciPhases.length === 8) _sciPhases = [];
                }
                _syncPhaseUI();
                _fire();
            });
        });
        _els.phaseAllBtn?.addEventListener('click', () => {
            _sciPhases = [];
            _syncPhaseUI();
            _fire();
        });
    }

    // ── Direction filter ─────────────────────────────────────────────────────

    function _syncDirUI() {
        const btns = _els.dirContainer
            ? [..._els.dirContainer.querySelectorAll('.dir-btn')]
            : [];
        btns.forEach(btn => {
            const v = parseInt(btn.dataset.dir);
            btn.classList.toggle('active', _dirFilter.length === 0 || _dirFilter.includes(v));
        });
    }

    function _bindDirEvents() {
        const container = _els.dirContainer;
        if (!container) return;
        container.querySelectorAll('.dir-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const v = parseInt(btn.dataset.dir);
                if (_dirFilter.length === 0) {
                    _dirFilter = [v];
                } else {
                    const idx = _dirFilter.indexOf(v);
                    if (idx >= 0) _dirFilter.splice(idx, 1);
                    else { _dirFilter.push(v); _dirFilter.sort((a, b) => a - b); }
                    if (_dirFilter.length === 3) _dirFilter = [];
                }
                _syncDirUI();
                _fire();
            });
        });
    }

    // ── Exports ───────────────────────────────────────────────────────────────

    return { init, loadFile, setZoomBounds, getParams, getCapabilities, resetState, fullReset, setPhases, setDirection, PHASE_COLORS, PHASE_NAMES };
})();
