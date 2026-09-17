"""Generate the coloured map icons (SVG) from the 3D view's low-poly models.

    python make_icons.py        # needs shapely; rewrites *-mapicon.svg next to this file

Each model is projected orthographically (side-on, slightly from above, nose east),
hidden surfaces are removed triangle by triangle, and what's left is merged into one
flat path per colour so the files stay ~1-3 kB.
"""
import json
import math
from pathlib import Path

from shapely.geometry import Polygon
from shapely.ops import unary_union

HERE = Path(__file__).parent
MODELS = HERE.parent / "3d_view" / "models"

# icon name -> (model, camera elevation in degrees; 0 = side-on, 90 = straight down)
ICONS = {
    "slocum": ("slocum", 22),
    "seaglider": ("seaglider", 22),
    "alr": ("alr", 18),
    "ship": ("rrs_sir_david_attenborough", 0),
    "rrs-discovery": ("rrs_discovery", 0),
    "rrs-james-cook": ("rrs_james_cook", 0),
    "argo": ("argo", 12),
}
OUTLINE = "#1b2229"
OUTLINE_W = 0.7                  # viewBox units
MUTE = 0.18                      # blend each model colour this far towards its own grey
HEIGHT = 48                      # viewBox units for the tallest extent (before padding)


def _flat(hex_col):
    rgb = [int(hex_col[i:i + 2], 16) for i in (1, 3, 5)]
    grey = sum(rgb) / 3
    return "#%02x%02x%02x" % tuple(round(c + (grey - c) * MUTE) for c in rgb)


def _path(geom, scale, x0, y1):
    geom = geom.simplify(0.2 / scale)
    polys = getattr(geom, "geoms", [geom])
    out = []
    for p in polys:
        if p.geom_type != "Polygon" or p.area * scale * scale < 0.4:
            continue
        for ring in [p.exterior, *p.interiors]:
            pts = [f"{(x - x0) * scale:.1f},{(y1 - y) * scale:.1f}" for x, y in ring.coords[:-1]]
            out.append("M" + "L".join(pts) + "Z")
    return "".join(out)


def build(model, elev):
    parts = json.loads((MODELS / f"{model}.json").read_text())
    e = math.radians(elev)
    up = (0.0, math.sin(e), math.cos(e))        # screen-up in model space
    into = (0.0, math.cos(e), -math.sin(e))     # camera looks along this

    tris = []
    for part in parts.values():
        vs = list(zip(part["x"], part["y"], part["z"]))
        for a, b, c in zip(part["i"], part["j"], part["k"]):
            A, B, C = vs[a], vs[b], vs[c]
            poly = Polygon([(P[0], sum(p * q for p, q in zip(P, up))) for P in (A, B, C)])
            if poly.area < 1e-9:
                continue
            depth = sum(sum(p * q for p, q in zip(P, into)) for P in (A, B, C)) / 3
            tris.append((depth, _flat(part["color"]), poly))

    # Front to back: keep only what earlier (nearer) triangles haven't covered.
    tris.sort(key=lambda t: t[0])
    covered, by_col = None, {}
    for _, col, poly in tris:
        vis = poly if covered is None else poly.difference(covered)
        if not vis.is_empty:
            by_col.setdefault(col, []).append(vis)
        covered = poly if covered is None else unary_union([covered, poly])

    x0, y0, x1, y1 = covered.bounds
    scale = HEIGHT / max(y1 - y0, (x1 - x0) * 24 / 56)
    pad = 1.5
    w, h = (x1 - x0) * scale + 2 * pad, (y1 - y0) * scale + 2 * pad
    grow = 0.02 / scale                                        # close hairline seams between colours
    body = "".join(
        f'<path fill="{col}" d="{_path(unary_union(ps).buffer(grow), scale, x0, y1)}"/>'
        for col, ps in by_col.items())
    outline = _path(covered.buffer(OUTLINE_W / scale), scale, x0, y1)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad:g} {-pad:g} {w:.1f} {h:.1f}">'
            f'<path fill="{OUTLINE}" d="{outline}"/>{body}</svg>')


if __name__ == "__main__":
    for name, (model, elev) in ICONS.items():
        svg = build(model, elev)
        (HERE / f"{name}-mapicon.svg").write_text(svg)
        print(f"{name}-mapicon.svg  {len(svg)} bytes")
