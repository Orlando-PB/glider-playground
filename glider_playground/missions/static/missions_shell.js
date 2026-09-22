// Missions inside the shell (index.html). The mission view is an iframe laid over the workspace; the header stays
// (logo, files, views, share, theme) but the plot presets and Settings, which mean nothing to a mission, are hidden.
// URLs: /missions = the list, /missions/<id> = one mission (both serve this same shell, see routes.py).
(() => {
    const selector = document.getElementById('viewSelector');
    const nav = document.getElementById('mainNav');
    if (!selector || !nav) return;

    const theme = () => (typeof getCurrentTheme === 'function' ? getCurrentTheme() : (document.documentElement.getAttribute('data-theme') || 'light'));
    const css = document.createElement('style');
    css.textContent = 'html.gp-mission #presetsGroup, html.gp-mission #settingsRow, html.gp-mission #settingsToggle { display: none !important; }'
        + ' html.gp-mission #viewSelector .view-seg-btn.active { background-color: transparent; color: var(--text-secondary); }'
        + ' html.gp-mission #viewSelector .view-seg-btn.active .material-symbols-outlined { color: inherit; }';
    document.head.appendChild(css);

    const wrap = document.createElement('div');
    wrap.className = 'view-seg'; wrap.style.marginLeft = '8px';
    wrap.innerHTML = '<button class="view-seg-btn" id="missionsBtn" title="Multi-platform missions: several gliders, floats and ships in one 3D scene with a shared time bar"><span class="material-symbols-outlined text-[14px]">route</span>Missions</button>'
        + '<button class="view-seg-btn" id="missionBackBtn" style="display:none" title="Back to the mission you were in"><span class="material-symbols-outlined text-[14px]">undo</span>Back to mission</button>';
    selector.insertAdjacentElement('afterend', wrap);
    const btn = wrap.firstElementChild;

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:55;display:none;background:var(--bg-app,#fff)';
    const frame = document.createElement('iframe');
    frame.style.cssText = 'width:100%;height:100%;border:0;display:block'; frame.title = 'Missions';
    overlay.appendChild(frame); document.body.appendChild(overlay);

    const back = wrap.lastElementChild;      // second half of the button, once a mission has been opened

    const idFromPath = () => { const m = location.pathname.match(/^\/missions(?:\/([^/]+))?\/?$/); return m ? (m[1] ? decodeURIComponent(m[1]) : '') : null; };   // null = not a mission URL
    const pathFor = id => (id == null ? '/' : id ? '/missions/' + encodeURIComponent(id) : '/missions');
    const fit = () => { overlay.style.top = nav.getBoundingClientRect().bottom + 'px'; };
    let current = null, lastMission = '';      // null = closed, '' = list, else mission id

    function show(id, push) {
        const open = id != null;
        document.documentElement.classList.toggle('gp-mission', open);
        overlay.style.display = open ? 'block' : 'none';
        try { frame.contentWindow.postMessage({ type: 'missionVisible', on: open }, '*'); } catch (_) {}
        if (open) {
            if (id !== current || !frame.getAttribute('src')) frame.src = id ? `/missions/static/mission_three.html?theme=${encodeURIComponent(theme())}&id=${encodeURIComponent(id)}` : `/missions/static/mission_view.html?embed=1&theme=${encodeURIComponent(theme())}`;      // the list is still mission_view.html
            if (id) lastMission = id;
            requestAnimationFrame(fit);
        }
        current = id;
        btn.classList.toggle('active', open && !id);
        const here = open && !!id && id === lastMission;      // already in it: greyed out
        back.disabled = here; back.style.opacity = here ? '0.4' : ''; back.style.cursor = here ? 'default' : '';
        back.style.display = lastMission ? '' : 'none';
        if (push && location.pathname !== pathFor(id)) history.pushState(null, '', pathFor(id) + (open ? '' : location.search));
    }

    btn.addEventListener('click', () => show('', true));                 // always the list
    back.addEventListener('click', () => show(lastMission, true));
    window.addEventListener('popstate', () => show(idFromPath(), false));
    window.addEventListener('resize', () => { if (current != null) fit(); });
    new ResizeObserver(() => { if (current != null) fit(); }).observe(nav);
    // Any normal view button leaves the mission.
    selector.addEventListener('click', e => { if (e.target.closest('.view-seg-btn') && current != null) show(null, true); });

    window.addEventListener('message', e => {
        const msg = e.data;
        if (!msg || e.source !== frame.contentWindow) return;
        if (msg.type === 'missionOpen') show(msg.id || '', true);
        if (msg.type === 'requestActivate' && msg.from === 'mission') {       // the shell's own handler selects the file
            show(null, true);
            if (typeof selectView === 'function') setTimeout(() => { try { selectView('classic'); } catch (_) {} }, 0);
        }
    });
    new MutationObserver(() => { try { frame.contentWindow.postMessage({ type: 'setTheme', theme: theme() }, '*'); } catch (_) {} })
        .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    const legacy = new URLSearchParams(location.search).get('mission');
    if (legacy) { history.replaceState(null, '', pathFor(legacy)); }
    if (idFromPath() != null) show(idFromPath(), false);
})();
