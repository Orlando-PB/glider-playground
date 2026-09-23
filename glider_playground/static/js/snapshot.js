// High-resolution snapshots of a page or panel: what's on screen, minus the controls. Every page that can live in a
// shell panel (plots, globe, 3D, mission) answers the shell's `snapshot` message with a PNG of itself, and the shell
// tiles those into one image of the whole workspace.
//
// capture(root, { scale, hide, canvasImage }) draws `root` into a canvas at `scale` device pixels per CSS pixel: the
// DOM is serialised into an SVG <foreignObject> with computed styles inlined, every <canvas> swapped in place for an
// <img> of its pixels (so WebGL content keeps its place in the stacking order: Plotly's points sit between its two SVG
// layers). Best effort: external fonts and icon ligatures can't load in there, so icon glyphs are dropped, and if the
// browser taints the canvas (Safari) only the canvases are drawn, at their own rects. `hide` is a selector for the
// controls to leave out. `canvasImage(canvas)` may return a canvas / <img> / data URL to use for a canvas — a WebGL
// page re-renders its scene at the scale here — or null for the canvas's current pixels.
(function () {
    const ICON_SEL = '.material-symbols-outlined';
    const SKIP = new Set(['CANVAS', 'SCRIPT', 'IFRAME', 'LINK', 'STYLE', 'NOSCRIPT']);

    const rectIn = (el, root) => {
        const a = el.getBoundingClientRect(), b = root.getBoundingClientRect();
        return { x: a.left - b.left, y: a.top - b.top, w: a.width, h: a.height };
    };

    const LOAD_TIMEOUT_MS = 15000;
    const loadImage = src => new Promise((res, rej) => {
        if (src instanceof HTMLCanvasElement || src instanceof HTMLImageElement) return res(src);
        const img = new Image();
        const t = setTimeout(() => rej(new Error('image load timed out')), LOAD_TIMEOUT_MS);
        img.onload = () => { clearTimeout(t); res(img); };
        img.onerror = () => { clearTimeout(t); rej(new Error('image failed to load')); };
        img.src = src;
    });
    const toUrl = src => src instanceof HTMLCanvasElement ? src.toDataURL('image/png') : src instanceof HTMLImageElement ? src.src : src;

    // Copy every computed style onto the clone so it renders without the page's stylesheets; canvases become images.
    function inlineStyles(src, dst, hideSel, canvasImage) {
        const nodes = [[src, dst]];
        while (nodes.length) {
            const [s, d] = nodes.pop();
            if (s.nodeType !== 1) continue;
            const cs = getComputedStyle(s);
            const gone = cs.display === 'none' || cs.visibility === 'hidden' || (hideSel && s.matches(hideSel)) || s.matches(ICON_SEL)
                || (SKIP.has(s.tagName) && s.tagName !== 'CANVAS');
            if (gone) { d.remove(); continue; }
            let css = '';
            for (let i = 0; i < cs.length; i++) { const p = cs[i]; css += `${p}:${cs.getPropertyValue(p)};`; }
            if (s.tagName === 'CANVAS') {
                let url = null;
                try { url = toUrl((canvasImage && canvasImage(s)) || s); } catch (_) {}
                if (!url) { d.remove(); continue; }
                const img = document.createElement('img');
                img.setAttribute('style', css); img.setAttribute('src', url);
                d.replaceWith(img); continue;
            }
            d.setAttribute('style', css);
            d.removeAttribute('class');
            if (s.tagName === 'INPUT' || s.tagName === 'TEXTAREA') d.setAttribute('value', s.value);
            if (s.tagName === 'IMG') {
                try {
                    const c = document.createElement('canvas'); c.width = s.naturalWidth; c.height = s.naturalHeight;
                    c.getContext('2d').drawImage(s, 0, 0); d.setAttribute('src', c.toDataURL());
                } catch (_) { d.remove(); continue; }
            }
            // Scrollable lists: no scrollbars, and what's in view rather than the top.
            if (/auto|scroll/.test(cs.overflowX + cs.overflowY)) d.style.overflow = 'hidden';
            if (s.scrollTop || s.scrollLeft) {
                for (const c of d.children) c.style.transform = `translate(${-s.scrollLeft}px, ${-s.scrollTop}px) ${c.style.transform || ''}`;
            }
            const sc = s.childNodes, dc = d.childNodes;
            for (let i = 0; i < sc.length; i++) nodes.push([sc[i], dc[i]]);
        }
    }

    async function domLayer(root, w, h, scale, hideSel, canvasImage) {
        const t0 = performance.now();
        const clone = root.cloneNode(true);
        inlineStyles(root, clone, hideSel, canvasImage);
        clone.style.margin = '0'; clone.style.position = 'static'; clone.style.transform = 'none';
        clone.style.width = w + 'px'; clone.style.height = h + 'px'; clone.style.overflow = 'hidden';
        clone.style.background = 'transparent';
        clone.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><foreignObject width="100%" height="100%">${new XMLSerializer().serializeToString(clone)}</foreignObject></svg>`;
        console.info(`snapshot: ${w}x${h} @${scale}, svg ${(svg.length / 1e6).toFixed(1)} MB, styled in ${Math.round(performance.now() - t0)} ms`);
        // A data: URL, never a blob: one — Chrome taints the canvas for an SVG image loaded from a blob URL.
        const img = await loadImage('data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg));
        const c = document.createElement('canvas'); c.width = Math.round(w * scale); c.height = Math.round(h * scale);
        const ctx = c.getContext('2d'); ctx.scale(scale, scale); ctx.drawImage(img, 0, 0, w, h);
        c.toDataURL();      // throws (Safari) if the foreignObject tainted it: then the caller draws the canvases alone
        console.info(`snapshot: drawn in ${Math.round(performance.now() - t0)} ms`);
        return c;
    }

    async function capture(root, { scale = 2, hide = '', canvasImage = null, background } = {}) {
        // A page's body can measure 0 tall (everything in it absolutely positioned): use the viewport for it.
        const w = root === document.body ? innerWidth : (root.clientWidth || root.getBoundingClientRect().width);
        const h = root === document.body ? innerHeight : (root.clientHeight || root.getBoundingClientRect().height);
        const out = document.createElement('canvas'); out.width = Math.round(w * scale); out.height = Math.round(h * scale);
        const ctx = out.getContext('2d'); ctx.scale(scale, scale);
        const bg = background || getComputedStyle(root).backgroundColor;
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') { ctx.fillStyle = bg; ctx.fillRect(0, 0, w, h); }
        try {
            const layer = await domLayer(root, w, h, scale, hide, canvasImage);
            ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.drawImage(layer, 0, 0);
        } catch (e) {
            console.warn('snapshot: DOM layer skipped, canvases only', e);
            const hidden = hide ? Array.from(root.querySelectorAll(hide)) : [];
            for (const c of root.querySelectorAll('canvas')) {
                if (hidden.some(x => x.contains(c)) || !c.offsetWidth) continue;
                const r = rectIn(c, root);
                try { ctx.drawImage(await loadImage((canvasImage && canvasImage(c)) || c), r.x, r.y, r.w, r.h); } catch (err) { console.warn('snapshot canvas', err); }
            }
        }
        return out;
    }

    // In a panel page: answer the shell's { type: 'snapshot', id, scale } with { type: 'snapshotResult', id, dataUrl }.
    // `provide(scale)` returns a canvas (or a promise of one).
    function listen(provide) {
        window.addEventListener('message', async e => {
            const m = e.data;
            if (!m || m.type !== 'snapshot' || !e.source) return;
            let reply;
            try { reply = { type: 'snapshotResult', id: m.id, dataUrl: (await provide(m.scale || 2)).toDataURL('image/png') }; }
            catch (err) { console.error('snapshot', err); reply = { type: 'snapshotResult', id: m.id, error: String(err && err.message || err) }; }
            e.source.postMessage(reply, '*');
        });
    }

    // A three.js renderer's canvas drawn at `scale` device pixels per CSS pixel: resized, rendered once by `render()`,
    // copied, and put back. The copy is what canvasImage() hands to capture().
    function threeFrame(renderer, render, scale) {
        const canvas = renderer.domElement, w = canvas.clientWidth, h = canvas.clientHeight, pr = renderer.getPixelRatio();
        const copy = document.createElement('canvas');
        try {
            renderer.setPixelRatio(scale); renderer.setSize(w, h, false); render();
            copy.width = canvas.width; copy.height = canvas.height;
            copy.getContext('2d').drawImage(canvas, 0, 0);
        } finally { renderer.setPixelRatio(pr); renderer.setSize(w, h, false); render(); }
        return copy;
    }

    window.gpSnapshot = { capture, listen, threeFrame, rectIn };
})();
