"""Generate the low-poly scenery meshes (seaweeds, seagrasses, corals, trees, sea life).

Same JSON format as the vehicle models in ../models: one file per object,
`{ part: {x, y, z, i, j, k, color} }`, metres, +z up. Scenery origins sit
at ground level (z = 0 is the seabed / land the object stands on) so the
3D view can drop them straight onto the bathymetry.

Run from anywhere:  python make_models.py   (writes *.json next to itself)
"""
import json
import math
import os
import random

OUT = os.path.dirname(os.path.abspath(__file__))


class Mesh:
    def __init__(self):
        self.parts = {}

    def part(self, name, color):
        p = self.parts.setdefault(name, {"x": [], "y": [], "z": [], "i": [], "j": [], "k": [], "color": color})
        return p

    @staticmethod
    def add_vert(p, x, y, z):
        p["x"].append(round(x, 3)); p["y"].append(round(y, 3)); p["z"].append(round(z, 3))
        return len(p["x"]) - 1

    @staticmethod
    def add_tri(p, a, b, c):
        p["i"].append(a); p["j"].append(b); p["k"].append(c)

    def quad(self, p, a, b, c, d):
        self.add_tri(p, a, b, c); self.add_tri(p, a, c, d)

    def save(self, name):
        with open(os.path.join(OUT, name + ".json"), "w") as f:
            json.dump(self.parts, f, separators=(",", ":"))


def prism(m, p, pts, r, seg=3, lean=(0, 0), at=(0, 0)):
    """Tapered polygonal column through `pts` = [(z, radius_scale)...]; `lean`
    shifts the top by (dx, dy) metres, linearly with height; `at` offsets
    the whole column in xy."""
    top = pts[-1][0]
    rings = []
    for z, s in pts:
        ring = []
        for q in range(seg):
            a = 2 * math.pi * q / seg
            ring.append(m.add_vert(p, at[0] + r * s * math.cos(a) + lean[0] * z / top, at[1] + r * s * math.sin(a) + lean[1] * z / top, z))
        rings.append(ring)
    for r0, r1 in zip(rings, rings[1:]):
        for q in range(seg):
            m.quad(p, r0[q], r0[(q + 1) % seg], r1[(q + 1) % seg], r1[q])
    return rings


def cone(m, p, z0, z1, r, seg=6, cx=0, cy=0):
    apex = m.add_vert(p, cx, cy, z1)
    ring = [m.add_vert(p, cx + r * math.cos(2 * math.pi * q / seg), cy + r * math.sin(2 * math.pi * q / seg), z0) for q in range(seg)]
    for q in range(seg):
        m.add_tri(p, ring[q], ring[(q + 1) % seg], apex)
    base = m.add_vert(p, cx, cy, z0)
    for q in range(seg):
        m.add_tri(p, ring[(q + 1) % seg], ring[q], base)


def blade(m, p, base, length, width, azimuth, tilt, droop=0.0):
    """Flat tapered leaf from `base` = (x, y, z): points along `azimuth`
    (radians, in the xy plane) rising at `tilt` (radians above horizontal);
    the tip drops by `droop` metres. Two triangles (a kite)."""
    ca, sa = math.cos(azimuth), math.sin(azimuth)
    ct, st = math.cos(tilt), math.sin(tilt)
    bx, by, bz = base
    root = m.add_vert(p, bx, by, bz)
    mx, my, mz = bx + ca * ct * length * 0.5, by + sa * ct * length * 0.5, bz + st * length * 0.5
    left = m.add_vert(p, mx - sa * width / 2, my + ca * width / 2, mz)
    right = m.add_vert(p, mx + sa * width / 2, my - ca * width / 2, mz)
    tip = m.add_vert(p, bx + ca * ct * length, by + sa * ct * length, bz + st * length - droop)
    m.add_tri(p, root, right, left)
    m.add_tri(p, left, right, tip)


def _norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def rod(m, p, a, b, r0, r1, seg=3):
    """Tapered rod between two arbitrary points (coral branches)."""
    d = _norm(tuple(b[q] - a[q] for q in range(3)))
    u = _norm(_cross(d, (0, 0, 1) if abs(d[2]) < 0.9 else (1, 0, 0)))
    v = _cross(d, u)
    rings = []
    for c, r in ((a, r0), (b, r1)):
        rings.append([m.add_vert(p, *(c[n] + r * (math.cos(2 * math.pi * q / seg) * u[n] + math.sin(2 * math.pi * q / seg) * v[n]) for n in range(3))) for q in range(seg)])
    for q in range(seg):
        m.quad(p, rings[0][q], rings[0][(q + 1) % seg], rings[1][(q + 1) % seg], rings[1][q])
    if seg == 3:
        m.add_tri(p, *rings[1])


def tree(m, p, rnd, a, d, length, r, depth, kids=2, spread=0.7, shrink=0.72, plane=None, lift=0.3):
    """Recursive branching from `a` along unit vector `d`. `plane` (a unit
    horizontal vector) keeps every branch in one vertical plane — sea fans."""
    b = tuple(a[q] + d[q] * length for q in range(3))
    rod(m, p, a, b, r, r * shrink)
    if depth == 0:
        return
    for q in range(kids):
        if plane:
            t = spread * ((2 * q / (kids - 1) - 1) if kids > 1 else 0) * rnd.uniform(0.7, 1.1)
            j = (plane[0] * t, plane[1] * t, 0)
        else:
            ang = 2 * math.pi * (q + rnd.uniform(-0.3, 0.3)) / kids + depth
            j = (math.cos(ang) * spread, math.sin(ang) * spread, 0)
        nd = _norm((d[0] + j[0], d[1] + j[1], d[2] + lift))
        tree(m, p, rnd, b, nd, length * rnd.uniform(0.65, 0.85), r * shrink, depth - 1, kids, spread, shrink, plane, lift)


def ribbon(m, p, base, azimuth, path, widths):
    """Strap-like blade from `base` heading along `azimuth`: `path` =
    [(distance out, height)...] from the base, `widths` the strap width at
    each point (kelp blades, seagrass leaves)."""
    ca, sa = math.cos(azimuth), math.sin(azimuth)
    prev = None
    for (out, z), w in zip(path, widths):
        cx, cy = base[0] + ca * out, base[1] + sa * out
        pair = (m.add_vert(p, cx - sa * w / 2, cy + ca * w / 2, base[2] + z), m.add_vert(p, cx + sa * w / 2, cy - ca * w / 2, base[2] + z))
        if prev:
            m.quad(p, prev[0], prev[1], pair[1], pair[0])
        prev = pair


# ── Seaweeds ──────────────────────────────────────────────────────────
# Kelps are brown algae: golden to dark olive-brown, never grass green.

# Giant kelp (Macrocystis pyrifera), 20 m: rope-like stipes from one
# holdfast, a blade every metre or so, and a canopy trailing at the top.
m = Mesh()
stipe = m.part("stipe", "#6B5B2A")
fronds = m.part("blades", "#A8892E")
for lx, ly, top in ((1.5, 0.5, 20), (-0.8, 1.2, 17)):
    prism(m, stipe, [(0, 1.0), (top / 2, 0.8), (top, 0.5)], 0.09, seg=3, lean=(lx, ly))
    for q in range(11):
        z = 2.0 + q * (top - 3.5) / 10
        side = 1 if q % 2 else -1
        blade(m, fronds, (lx * z / top, ly * z / top, z), 2.2, 0.5, side * math.pi / 2 + q * 0.5, 0.3, droop=0.3)
    heading = math.atan2(ly, lx)
    for q in range(4):   # surface canopy, streaming one way
        blade(m, fronds, (lx, ly, top), 3.2, 0.6, heading + (q - 1.5) * 0.35, 0.05, droop=0.1)
m.save("kelp_giant")

# Oarweed / tangle (Laminaria hyperborea & digitata), 2 m: stiff stipe
# carrying one palm-shaped blade split into fingers that flop to one side.
m = Mesh()
stipe = m.part("stipe", "#4A3B1E")
prism(m, stipe, [(0, 1.0), (1.3, 0.6)], 0.05, seg=4, lean=(0.15, 0))
fronds = m.part("blades", "#6E5A24")
for q in range(6):
    a = (q - 2.5) * 0.36
    path = [(1.1 * s, 0.5 * math.sin(math.pi * s) - 0.3 * s) for s in (0, 0.35, 0.7, 1.0)]
    ribbon(m, fronds, (0.15, 0, 1.3), a, path, [0.12, 0.17, 0.13, 0.03])
m.save("kelp_oarweed")

# Sugar kelp (Saccharina latissima), 2 m: short stipe, one long crinkled
# undivided ribbon; drawn as a small clump.
m = Mesh()
fronds = m.part("blades", "#8F742C")
for q in range(3):
    a = 2 * math.pi * q / 3 + 0.4
    path = [(1.2 * s * s, 2.5 * s * (1 - 0.35 * s)) for s in (0, 0.12, 0.3, 0.5, 0.7, 0.85, 1.0)]
    ribbon(m, fronds, (0.08 * math.cos(a), 0.08 * math.sin(a), 0), a, path, [0.04, 0.05, 0.42, 0.3, 0.42, 0.28, 0.05])
m.save("kelp_sugar")

# Ecklonia (E. radiata / E. maxima), 1.5 m: single stipe, a crown of
# straps drooping all round — the common kelp of S Africa and Australasia.
m = Mesh()
stipe = m.part("stipe", "#4F3F1E")
prism(m, stipe, [(0, 1.0), (1.0, 0.7)], 0.05, seg=4)
fronds = m.part("blades", "#7A6428")
for q in range(8):
    a = 2 * math.pi * q / 8
    path = [(0.8 * s, 0.35 * math.sin(math.pi * s) - 0.5 * s) for s in (0, 0.4, 0.75, 1.0)]
    ribbon(m, fronds, (0, 0, 1.0), a, path, [0.1, 0.2, 0.15, 0.03])
m.save("kelp_ecklonia")

# Himantothallus grandifolius, the big Antarctic brown alga: broad leathery
# straps several metres long lying along the seabed.
m = Mesh()
fronds = m.part("blades", "#5B4A2A")
for q, a in enumerate((0.2, 1.9, 3.3, 4.8)):
    path = [(3.0 * s, 0.08 + 0.3 * math.sin(2 * math.pi * s + q) ** 2) for s in (0, 0.2, 0.45, 0.7, 1.0)]
    ribbon(m, fronds, (0, 0, 0), a, path, [0.15, 0.7, 0.8, 0.6, 0.1])
m.save("himantothallus")

# ── Seagrasses ────────────────────────────────────────────────────────
def seagrass(name, colour, n, height, width, bend, seed):
    rnd = random.Random(seed)
    m = Mesh()
    g = m.part("blades", colour)
    for q in range(n):
        a = 2 * math.pi * q / n + rnd.uniform(-0.3, 0.3)
        h = height * rnd.uniform(0.7, 1.0)
        base = (0.12 * rnd.uniform(0, 1) * math.cos(a), 0.12 * rnd.uniform(0, 1) * math.sin(a), 0)
        ribbon(m, g, base, a, [(bend * s * s, h * s) for s in (0, 0.5, 1.0)], [width, width, width * 0.4])
    m.save(name)

seagrass("seagrass_eel", "#4F9A4A", 8, 1.0, 0.05, 0.4, 1)         # Zostera marina: long, narrow ribbons
seagrass("seagrass_posidonia", "#2F7D46", 12, 0.8, 0.09, 0.3, 2)  # Posidonia oceanica: dense dark tufts
seagrass("seagrass_turtle", "#5FA84A", 6, 0.35, 0.12, 0.12, 3)    # Thalassia: short, broad blades

# ── Warm-water reef corals ────────────────────────────────────────────
m = Mesh()   # Staghorn (Acropora): open antler-like thicket
tree(m, m.part("coral", "#D2A866"), random.Random(4), (0, 0, 0), (0, 0, 1), 0.35, 0.06, 3, kids=3, spread=0.9, lift=0.5)
m.save("coral_staghorn")

m = Mesh()   # Table Acropora: short stalk under a wide flat plate
c = m.part("coral", "#A9905C")
prism(m, c, [(0, 1.0), (0.5, 0.6)], 0.25, seg=5)
prism(m, c, [(0.5, 0.2), (0.75, 1.0), (0.82, 1.0), (0.82, 0.0)], 0.95, seg=8)
m.save("coral_table")

m = Mesh()   # Massive boulder / brain coral (Porites, Diploria): rounded domes
c = m.part("coral", "#B9A24E")
prism(m, c, [(0, 0.9), (0.3, 1.0), (0.6, 0.7), (0.75, 0.0)], 0.6, seg=7)
prism(m, c, [(0, 0.9), (0.18, 1.0), (0.34, 0.65), (0.42, 0.0)], 0.32, seg=6, at=(0.75, 0.3))
m.save("coral_brain")

for name, colour in (("sea_fan_purple", "#8E5BA8"), ("sea_fan_red", "#C8502E")):   # gorgonian fans: one flat plane across the current
    m = Mesh()
    tree(m, m.part("coral", colour), random.Random(5), (0, 0, 0), (0, 0, 1), 0.28, 0.035, 3, kids=3, spread=0.9, plane=(1, 0, 0), lift=0.6)
    m.save(name)

# ── Cold-water & deep-sea ─────────────────────────────────────────────
m = Mesh()   # Lophelia (Desmophyllum pertusum): white live thicket on a grey mound of dead framework
prism(m, m.part("rubble", "#9A948A"), [(0, 1.0), (0.25, 0.75), (0.35, 0.0)], 0.7, seg=6)
live = m.part("coral", "#F3E9DF")
rnd = random.Random(6)
for q in range(3):
    a = 2 * math.pi * q / 3
    tree(m, live, rnd, (0.3 * math.cos(a), 0.3 * math.sin(a), 0.2), _norm((0.4 * math.cos(a), 0.4 * math.sin(a), 1)), 0.3, 0.05, 2, kids=3, spread=1.0, lift=0.4)
m.save("coral_lophelia")

m = Mesh()   # Bubblegum coral (Paragorgia arborea): thick pink-red tree, roughly fan-shaped, metres tall
tree(m, m.part("coral", "#D9536F"), random.Random(7), (0, 0, 0), (0, 0, 1), 0.7, 0.12, 3, kids=2, spread=0.75, shrink=0.75, plane=(1, 0, 0), lift=0.5)
m.save("coral_bubblegum")

m = Mesh()   # Bamboo coral (Isididae): white candelabra, arms out then straight up
c = m.part("coral", "#EFEAE0")
rod(m, c, (0, 0, 0), (0, 0, 0.45), 0.04, 0.035)
for side, reach, top in ((1, 0.22, 1.0), (-1, 0.22, 0.95), (1, 0.5, 0.85), (-1, 0.5, 0.8)):
    rod(m, c, (0, 0, 0.3 if reach > 0.3 else 0.45), (side * reach, 0, 0.5), 0.03, 0.025)
    rod(m, c, (side * reach, 0, 0.5), (side * reach, 0, top), 0.025, 0.012)
rod(m, c, (0, 0, 0.45), (0, 0, 1.05), 0.035, 0.012)
m.save("coral_bamboo")

m = Mesh()   # Sea pen (Pennatulacea): a quill standing in soft mud
rod(m, m.part("stalk", "#E8C9A0"), (0, 0, 0), (0, 0, 1.0), 0.035, 0.012)
leaves = m.part("leaves", "#E0803C")
for q in range(7):
    z = 0.35 + q * 0.09
    L = 0.3 * math.sin(math.pi * (q + 1) / 8) + 0.08
    for az in (0, math.pi):
        blade(m, leaves, (0, 0, z), L, 0.1, az, 0.7)
m.save("sea_pen")

m = Mesh()   # Glass / vase sponges (Hexactinellida): pale open-topped vases
c = m.part("sponge", "#E8E2C8")
prism(m, c, [(0, 0.35), (0.15, 0.3), (0.6, 0.8), (1.0, 1.0)], 0.3, seg=6)
prism(m, c, [(0, 0.35), (0.1, 0.3), (0.4, 0.8), (0.6, 1.0)], 0.2, seg=6, at=(0.45, 0.2))
m.save("sponge_glass")

# ── Conifer: trunk + two stacked cones, 10 m ──
m = Mesh()
trunk = m.part("trunk", "#6B4A2B")
prism(m, trunk, [(0, 1.0), (3, 0.8)], 0.25, seg=4)
canopy = m.part("canopy", "#2F6B3A")
cone(m, canopy, 2.5, 7.0, 2.4, seg=6)
cone(m, canopy, 5.5, 10.0, 1.6, seg=6)
m.save("conifer")

# ── Palm: leaning trunk + 6 drooping fronds, 8 m ──
m = Mesh()
trunk = m.part("trunk", "#8A6A45")
prism(m, trunk, [(0, 1.0), (4, 0.8), (7, 0.7)], 0.22, seg=4, lean=(0.8, 0.3))
fr = m.part("fronds", "#4C9A3C")
for q in range(6):
    a = 2 * math.pi * q / 6
    blade(m, fr, (0.8, 0.3, 7), 3.0, 0.9, a, 0.35, droop=1.6)
m.save("palm")

# ── Floating things (origin at body centre, +x nose) ──────────────────
# Parametric builders + a species table; one file per species. Lengths in
# metres, colours are flat per part. Shapes are cartoons, not field guides.

def revolve(m, p, profile, seg=8, squash=0.8, lean_z=0.0, arc=None, span=None):
    """Body of revolution along x: profile = [(x, radius)...]; y radius = r,
    z radius = r*squash; `lean_z` lifts the tail end (whales' fluke stock).
    `arc` = (q0, q1) draws only that run of segments (a belly shell); `span`
    = the full body's (nose x, tail x) so a partial profile leans with it."""
    rings = []
    x0, x1 = span or (profile[0][0], profile[-1][0])
    for x, r in profile:
        lift = lean_z * (x1 - x) / (x1 - x0)
        if r == 0:
            rings.append([m.add_vert(p, x, 0, lift)] * seg)
        else:
            rings.append([m.add_vert(p, x, r * math.cos(2 * math.pi * q / seg), lift + r * squash * math.sin(2 * math.pi * q / seg)) for q in range(seg)])
    for r0, r1 in zip(rings, rings[1:]):
        for q in range(*(arc or (0, seg))):
            m.quad(p, r0[q], r0[(q + 1) % seg], r1[(q + 1) % seg], r1[q])


def tri(m, p, a, b, c):
    m.add_tri(p, m.add_vert(p, *a), m.add_vert(p, *b), m.add_vert(p, *c))


def _rad(prof, x):
    """Profile radius at x (linear between stations; profile runs nose to tail)."""
    for (xa, ra), (xb, rb) in zip(prof, prof[1:]):
        if xb <= x <= xa:
            return ra + (rb - ra) * (xa - x) / (xa - xb)
    return 0.0


def _skin(prof, L, squash, girth=1.0, lean=0.0):
    """Point on a revolved body's surface: S(x fraction, angle in degrees from
    the +y flank, up positive, side, k = how far proud of the skin)."""
    x0, x1 = prof[0][0], prof[-1][0]
    def S(x, deg, side=1, k=1.02):
        r, t = _rad(prof, x) * girth * L * k, math.radians(deg)
        return (x * L, side * r * math.cos(t), lean * L * (x0 - x) / (x0 - x1) + r * squash * math.sin(t))
    return S


def _shade(colour, k):
    return "#" + "".join("%02X" % min(255, int(int(colour[q:q + 2], 16) * k)) for q in (1, 3, 5))


WHALE_TAIL = [(0.0, 0.113), (-0.15, 0.098), (-0.28, 0.07), (-0.38, 0.042), (-0.46, 0.02)]
WHALE_HEADS = {
    "round": [(0.5, 0.0), (0.485, 0.03), (0.45, 0.055), (0.38, 0.085), (0.28, 0.105), (0.15, 0.115)],
    "blunt": [(0.5, 0.0), (0.495, 0.06), (0.46, 0.095), (0.35, 0.115), (0.2, 0.118)],
    "pointed": [(0.5, 0.0), (0.47, 0.02), (0.4, 0.048), (0.3, 0.085), (0.15, 0.112)],
}


def whale(name, L, colour, fin_colour, head="round", dorsal=0.06, flipper=0.25, fluke=0.2, belly=None, tusk=0.0, beak=None, flank=None, eyepatch=None):
    """Baleen/toothed whale or dolphin: body, horizontal flukes, pectoral
    flippers, an optional dorsal fin (height as a fraction of L), tusk,
    beak = (length fraction, colour), flank = side-blaze colour and
    eyepatch = colour of an orca-style patch behind the eye. The big ones
    (L >= 4 m) get a smoother body, a wrapped belly, swept fins and eyes."""
    m = Mesh()
    big = L >= 4
    body = m.part("hull", colour)
    if big:
        prof = WHALE_HEADS[head] + WHALE_TAIL
        revolve(m, body, [(x * L, r * L) for x, r in prof], seg=12, squash=0.85, lean_z=0.02 * L)
    else:
        nose = [(0.5, 0.0), (0.42, 0.06)] if head == "round" else [(0.5, 0.0), (0.46, 0.09), (0.35, 0.115)] if head == "blunt" else [(0.5, 0.0), (0.4, 0.045)]
        prof = nose + [(0.2, 0.11), (0.0, 0.115), (-0.2, 0.09), (-0.38, 0.045), (-0.46, 0.02)]
        revolve(m, body, [(x * L, r * L) for x, r in prof], seg=8, squash=0.85, lean_z=0.02 * L)
    fins = m.part("fins", fin_colour)
    for side in (1, -1):   # flukes (horizontal) + flippers
        if big:
            root, tip = (-0.43 * L, 0, 0.02 * L), (-0.45 * L - fluke * 0.55 * L, side * fluke * L, 0.03 * L)
            mid = (-0.5 * L - fluke * 0.2 * L, side * fluke * 0.45 * L, 0.027 * L)
            tri(m, fins, root, tip, mid)
            tri(m, fins, root, mid, (-0.485 * L, 0, 0.022 * L))   # notch
            rf, rr = (0.2 * L, side * 0.1 * L, -0.04 * L), (0.07 * L, side * 0.1 * L, -0.05 * L)
            lead, tip = (0.1 * L, side * (0.1 + flipper * 0.6) * L, -0.058 * L), (-0.1 * L, side * (0.1 + flipper) * L, -0.08 * L)
            tri(m, fins, rf, lead, rr)
            tri(m, fins, lead, tip, rr)
        else:
            tri(m, fins, (-0.45 * L, 0, 0.02 * L), (-0.45 * L - fluke * 0.6 * L, side * fluke * L, 0.03 * L), (-0.5 * L, side * 0.03 * L, 0.025 * L))
            tri(m, fins, (0.18 * L, side * 0.1 * L, -0.04 * L), (-0.1 * L, side * (0.1 + flipper) * L, -0.07 * L), (0.04 * L, side * 0.1 * L, -0.05 * L))
    if dorsal and big:   # swept back, hollow trailing edge
        front, apex = (-0.07 * L, 0, 0.08 * L), (-0.235 * L, 0, (0.085 + dorsal) * L)
        tri(m, fins, front, apex, (-0.2 * L, 0, (0.085 + dorsal * 0.35) * L))
        tri(m, fins, front, (-0.2 * L, 0, (0.085 + dorsal * 0.35) * L), (-0.22 * L, 0, 0.075 * L))
    elif dorsal:
        tri(m, fins, (-0.08 * L, 0, 0.09 * L), (-0.22 * L, 0, 0.09 * L), (-0.2 * L, 0, (0.09 + dorsal) * L))
    if belly and big:
        under = [(x * L, r * L * 1.02) for x, r in prof if -0.3 <= x <= 0.485]
        revolve(m, m.part("belly", belly), under, seg=12, squash=0.85, lean_z=0.02 * L, arc=(7, 11), span=(0.5 * L, -0.46 * L))
    elif belly:
        b = m.part("belly", belly)
        for side in (1, -1):
            tri(m, b, (0.3 * L, side * 0.1 * L, -0.06 * L), (-0.15 * L, side * 0.09 * L, -0.07 * L), (0.1 * L, side * 0.02 * L, -0.1 * L))
    if big:
        S, e = _skin(prof, L, 0.85, lean=0.02), m.part("eye", "#0E1114")
        for side in (1, -1):
            tri(m, e, S(0.385, -12, side), S(0.36, -12, side), S(0.372, 2, side))
    if beak:
        prism_axis(m, m.part("beak", beak[1]), (0.455 * L, 0, -0.012 * L), ((0.47 + beak[0]) * L, 0, -0.02 * L), 0.032 * L)
    if flank:
        f = m.part("flank", flank)
        for side in (1, -1):
            tri(m, f, (0.2 * L, side * 0.13 * L, 0.012 * L), (-0.24 * L, side * 0.11 * L, 0.025 * L), (0.0, side * 0.134 * L, -0.045 * L))
    if eyepatch:
        e = m.part("eyepatch", eyepatch)
        for side in (1, -1):
            tri(m, e, (0.37 * L, side * 0.082 * L, 0.035 * L), (0.27 * L, side * 0.105 * L, 0.045 * L), (0.3 * L, side * 0.103 * L, 0.012 * L))
    if tusk:
        t = m.part("tusk", "#EDE6D6")
        prism_axis(m, t, (0.48 * L, 0.01 * L, 0.0), (0.48 * L + tusk * L, 0.01 * L, 0.0), 0.012 * L)
    m.save(name)


def prism_axis(m, p, a, b, r):
    """Thin triangular rod from point a to point b."""
    ring0 = [m.add_vert(p, a[0], a[1] + r * math.cos(2 * math.pi * q / 3), a[2] + r * math.sin(2 * math.pi * q / 3)) for q in range(3)]
    tip = m.add_vert(p, *b)
    for q in range(3):
        m.add_tri(p, ring0[q], ring0[(q + 1) % 3], tip)


SHARK_TAIL = [(0.15, 0.086), (0.0, 0.082), (-0.15, 0.066), (-0.3, 0.038), (-0.42, 0.016)]


def shark(name, L, colour, belly="#D8DEE3", dorsal=0.14, tail=0.22, hammer=0.0, blunt=False, girth=1.0, flat=1.1, spots=None):
    """Shark: pointed snout, swept dorsal fins, notched asymmetric tail, gill
    slits and a wrapped pale belly; `spots` = colour of whale-shark back spots."""
    m = Mesh()
    body = m.part("hull", colour)
    nose = [(0.5, 0.0), (0.495, 0.05), (0.46, 0.078), (0.38, 0.087)] if blunt else [(0.5, 0.0), (0.47, 0.022), (0.4, 0.05), (0.3, 0.074)]
    prof = nose + SHARK_TAIL
    revolve(m, body, [(x * L, r * girth * L) for x, r in prof], seg=10, squash=flat)
    S = _skin(prof, L, flat, girth)
    top = lambda x: _rad(prof, x) * girth * flat * 0.9 * L
    if hammer:   # cephalofoil: a flat wing across the snout
        for side in (1, -1):
            tri(m, body, (0.5 * L, 0, 0.004 * L), (0.49 * L, side * hammer * L, 0.004 * L), (0.42 * L, side * hammer * L, 0.004 * L))
            tri(m, body, (0.5 * L, 0, 0.004 * L), (0.42 * L, side * hammer * L, 0.004 * L), (0.4 * L, 0, 0.004 * L))
    fins = m.part("fins", colour)
    z = top(-0.08)
    front, apex, hollow = (0.0, 0, z), (-0.165 * L, 0, z + dorsal * L), (-0.135 * L, 0, z + dorsal * 0.3 * L)
    tri(m, fins, front, apex, hollow)                                                                    # first dorsal
    tri(m, fins, front, hollow, (-0.16 * L, 0, z * 0.9))
    z = top(-0.31)
    tri(m, fins, (-0.27 * L, 0, z), (-0.345 * L, 0, z + dorsal * 0.28 * L), (-0.33 * L, 0, z))           # second dorsal
    tri(m, fins, (-0.29 * L, 0, -z), (-0.355 * L, 0, -z - dorsal * 0.22 * L), (-0.34 * L, 0, -z))        # anal
    root_up, root_lo, fork = (-0.4 * L, 0, 0.014 * L), (-0.4 * L, 0, -0.014 * L), (-0.475 * L, 0, 0.0)
    tri(m, fins, root_up, (-0.55 * L, 0, tail * L), fork)                                                # upper tail lobe
    tri(m, fins, root_lo, fork, (-0.5 * L, 0, -tail * 0.55 * L))                                         # lower tail lobe
    tri(m, fins, root_up, fork, root_lo)
    for side in (1, -1):
        rf, rr = (0.16 * L, side * 0.07 * L, -0.03 * L), (0.04 * L, side * 0.07 * L, -0.04 * L)
        lead, tip = (0.07 * L, side * 0.19 * L, -0.048 * L), (-0.09 * L, side * 0.27 * L, -0.065 * L)
        tri(m, fins, rf, lead, rr)                                                                       # pectoral
        tri(m, fins, lead, tip, rr)
        zb = -top(-0.18)
        tri(m, fins, (-0.14 * L, side * 0.025 * L, zb), (-0.24 * L, side * 0.085 * L, zb - 0.02 * L), (-0.22 * L, side * 0.025 * L, zb))   # pelvic
    under = [(x * L, r * girth * L * 1.02) for x, r in prof if -0.3 <= x <= 0.47]
    revolve(m, m.part("belly", belly), under, seg=10, squash=flat, arc=(6, 9))
    gills = m.part("gills", _shade(colour, 0.6))
    eye = m.part("eye", "#0E1114")
    for side in (1, -1):
        for x in (0.27, 0.245, 0.22, 0.195):
            tri(m, gills, S(x, -18, side), S(x, 22, side), S(x - 0.009, 2, side))
        if hammer:
            y = side * (hammer + 0.002) * L
            tri(m, eye, (0.47 * L, y, -0.006 * L), (0.445 * L, y, -0.006 * L), (0.457 * L, y, 0.012 * L))
        else:
            tri(m, eye, S(0.425, 2, side), S(0.405, 2, side), S(0.415, 20, side))
    if spots:
        sp = m.part("spots", spots)
        for q in range(7):
            x = 0.36 - q * 0.095
            for deg in ((38, 90, 142) if q % 2 else (62, 118)):
                tri(m, sp, S(x + 0.012, deg), S(x - 0.012, deg - 5), S(x - 0.012, deg + 5))
    m.save(name)


def fish(m, p, cx, cy, cz, L, yaw=0.0, deep=0.14, pb=None):
    """One low-poly fish: elongated octahedron body + a flat tail fin; with
    `pb` the upper half is drawn again in that (darker, back-colour) part."""
    ca, sa = math.cos(yaw), math.sin(yaw)
    P = lambda x, y, z: m.add_vert(p, cx + x * ca - y * sa, cy + x * sa + y * ca, cz + z)
    nose, tail = P(L * 0.5, 0, 0), P(-L * 0.35, 0, 0)
    top, bot = P(L * 0.05, 0, L * deep), P(L * 0.05, 0, -L * deep * 0.85)
    left, right = P(L * 0.05, L * 0.07, 0), P(L * 0.05, -L * 0.07, 0)
    for a, b in ((top, left), (left, bot), (bot, right), (right, top)):
        m.add_tri(p, nose, a, b); m.add_tri(p, tail, b, a)
    t0, t1, t2 = P(-L * 0.3, 0, 0), P(-L * 0.5, 0, L * 0.12), P(-L * 0.5, 0, -L * 0.12)
    m.add_tri(p, t0, t1, t2)
    if pb:
        B = lambda x, y, z: m.add_vert(pb, cx + x * ca - y * sa, cy + x * sa + y * ca, cz + z)
        nose, tail, top = B(L * 0.5, 0, L * 0.004), B(-L * 0.35, 0, L * 0.004), B(L * 0.05, 0, L * deep * 1.04)
        left, right = B(L * 0.05, L * 0.075, L * 0.01), B(L * 0.05, -L * 0.075, L * 0.01)
        for a in (left, right):
            m.add_tri(pb, nose, top, a); m.add_tri(pb, tail, a, top)


SCHOOL_SPOTS = [(0, 0, 0), (0.7, 0.4, 0.2), (-0.6, 0.5, -0.1), (0.3, -0.6, 0.3), (-0.8, -0.3, 0.1), (0.9, -0.2, -0.3),
                (-0.2, 0.9, -0.3), (0.4, 0.8, -0.1), (-0.9, 0.1, 0.3), (0.1, -0.9, -0.2)]


def school(name, fish_len, colour, count=7, spread=2.0, deep=0.14, back=None):
    """A loose shoal, `spread` metres across; optional darker back colour."""
    m = Mesh()
    p = m.part("hull", colour)
    pb = m.part("back", back) if back else None
    for q, (x, y, z) in enumerate(SCHOOL_SPOTS[:count]):
        fish(m, p, x * spread / 2, y * spread / 2, z * spread / 2, fish_len, yaw=0.15 * (q % 3 - 1), deep=deep, pb=pb)
    m.save(name)


def jelly(name, r, profile, colour, tent=None, arms=None, marks=None, rim=None, seg=8, top=None, extra=None):
    """Jellyfish, origin at the bell margin, everything in bell radii (`r` m).
    profile: bell outline [(z, radius_scale)...] from the margin up to the apex.
    tent: (n, length, colour, width[, up]) marginal tentacles; `up` holds them
      stiffly above the bell (Periphylla) instead of trailing.
    arms: (n, length, colour, radius) thick oral arms hanging from the middle.
    marks: (n, k, t0, t1, half_angle, colour) radial wedges painted along
      profile segment k between fractions t0..t1 (gonad rings, compass Vs).
    rim: colour of a band round the margin. top: (radius, height, colour) dome
    on the apex. extra(m): species-specific additions.
    The bell part is called "hull" so the view sizes a jelly by bell diameter."""
    m = Mesh()
    prism(m, m.part("hull", colour), [(z * r, s) for z, s in profile], r, seg=seg)
    if rim:
        prism(m, m.part("rim", rim), [(-0.12 * r, 0.98), (0.03 * r, 1.04)], r, seg=seg)
    if top:
        tr, th, tc = top
        z0 = profile[-1][0] * r - 0.05 * r
        prism(m, m.part("top", tc), [(z0, 1.0), (z0 + th * r * 0.6, 0.8), (z0 + th * r, 0.0)], tr * r, seg=seg)
    if marks:
        n, k, t0, t1, dw, mc = marks
        p = m.part("marks", mc)
        (za, sa), (zb, sb) = profile[k], profile[k + 1]
        at = lambda t, ang: ((sa + (sb - sa) * t) * r * 1.03 * math.cos(ang), (sa + (sb - sa) * t) * r * 1.03 * math.sin(ang), (za + (zb - za) * t) * r + 0.03 * r)
        for q in range(n):
            ang = 2 * math.pi * q / n
            tri(m, p, at(t1, ang), at(t0, ang - dw), at(t0, ang + dw))
    if tent:
        n, length, tc, w = tent[:4]
        p = m.part("tentacles", tc)
        L = length * r
        path = [(0, 0), (0.55 * L, 0.5 * L), (0.75 * L, L)] if len(tent) > 4 and tent[4] else [(0, 0), (0.12 * L, -0.5 * L), (0.05 * L, -L)]
        for q in range(n):
            ang = 2 * math.pi * (q + 0.5) / n
            ribbon(m, p, (0.95 * r * math.cos(ang), 0.95 * r * math.sin(ang), 0), ang, path, [w * r, w * r * 0.8, w * r * 0.3])
    if arms:
        n, length, ac, ar = arms
        p = m.part("arms", ac)
        L = length * r
        for q in range(n):
            ang = 2 * math.pi * q / n + 0.4
            ca, sa_ = math.cos(ang), math.sin(ang)
            mid = (0.45 * r * ca, 0.45 * r * sa_, -0.5 * L)
            rod(m, p, (0.2 * r * ca, 0.2 * r * sa_, 0.05 * r), mid, ar * r, ar * r * 0.8, seg=4)
            rod(m, p, mid, (0.25 * r * ca, 0.25 * r * sa_, -L), ar * r * 0.8, ar * r * 0.25, seg=4)
    if extra:
        extra(m)
    m.save(name)


SAUCER = [(0, 1.0), (0.25, 0.85), (0.4, 0.0)]
DOME = [(0, 1.0), (0.35, 0.95), (0.7, 0.6), (0.85, 0.0)]


def ray(name, L, span, top, belly, tail=0.8, lobes=False, snout=0.0, rounded=False, rest=False):
    """Ray / skate, disc length L, wingspan `span`: flat diamond (or rounded
    disc), dark above and pale below, whip tail; `lobes` adds a manta's
    cephalic fins; `rest` lifts it to lie on the seabed (origin at ground)."""
    m = Mesh()
    hull, under = m.part("hull", top), m.part("belly", belly)
    z0 = 0.045 * L if rest else 0.0
    half = span / (2 * L)
    if rounded:
        outline = [(0.38 + snout, 0), (0.25, 0.65 * half), (-0.02, half), (-0.3, 0.6 * half), (-0.4, 0), (-0.3, -0.6 * half), (-0.02, -half), (0.25, -0.65 * half)]
    else:
        outline = [(0.35 + snout, 0), (0.22, 0.14), (-0.08, half), (-0.22, 0.3 * half), (-0.4, 0), (-0.22, -0.3 * half), (-0.08, -half), (0.22, -0.14)]
    for p, zc in ((hull, 0.075), (under, -0.04)):
        for (x0, y0), (x1, y1) in zip(outline, outline[1:] + outline[:1]):
            tri(m, p, (0.05 * L, 0, zc * L + z0), (x0 * L, y0 * L, z0), (x1 * L, y1 * L, z0))
    if lobes:
        for side in (1, -1):
            tri(m, hull, (0.3 * L, side * 0.09 * L, z0), (0.5 * L, side * 0.1 * L, z0 - 0.02 * L), (0.26 * L, side * 0.17 * L, z0))
    prism_axis(m, hull, (-0.38 * L, 0, z0), ((-0.4 - tail) * L, 0, z0), 0.014 * L)
    m.save(name)


def turtle(name, L, shell, skin, profile=None, head=0.07, flipper=0.55, ridges=None):
    """Sea turtle: domed shell (`profile` along x, in L), head, long swept
    front flippers, stubby rear ones; `ridges` = colour of a leatherback's keels."""
    m = Mesh()
    prof = profile or [(0.4, 0.0), (0.3, 0.2), (0.0, 0.31), (-0.35, 0.2), (-0.47, 0.0)]
    revolve(m, m.part("hull", shell), [(x * L, r * L) for x, r in prof], seg=8, squash=0.45)
    sk = m.part("skin", skin)
    x0 = prof[0][0]
    revolve(m, sk, [((x0 + 0.2) * L, 0.0), ((x0 + 0.15) * L, head * 0.8 * L), ((x0 + 0.05) * L, head * L), ((x0 - 0.04) * L, head * 0.6 * L)], seg=6, squash=0.8)
    for side in (1, -1):
        tri(m, sk, (0.3 * L, side * 0.17 * L, 0), (-0.08 * L, side * (0.25 + flipper) * L, -0.03 * L), (0.12 * L, side * 0.26 * L, 0))
        tri(m, sk, (-0.34 * L, side * 0.15 * L, 0), (-0.62 * L, side * 0.27 * L, -0.01 * L), (-0.43 * L, side * 0.07 * L, 0))
    if ridges:
        rp = m.part("ridges", ridges)
        for ang in (math.pi / 4, math.pi / 2, 3 * math.pi / 4):
            pt = lambda x, r, da: (x * L, r * 1.04 * L * math.cos(ang + da), r * 1.04 * L * 0.45 * math.sin(ang + da) + 0.004 * L)
            for (xa, ra), (xb, rb) in zip(prof[1:-1], prof[2:-1]):
                tri(m, rp, pt(xa, ra, -0.05), pt(xb, rb, -0.05), pt(xb, rb, 0.05))
                tri(m, rp, pt(xa, ra, -0.05), pt(xb, rb, 0.05), pt(xa, ra, 0.05))
    m.save(name)


def eel(name, L, colour, r, pose="swim", amp=0.07, waves=1.5, n=9, fin=None):
    """Eel: a tapering tube along a sine wave. pose 'swim' = mid-water,
    'rest' = lying on the seabed, 'rear' = front third raised out of a crevice
    (moray). `fin` = colour of a ribbon dorsal fin."""
    m = Mesh()
    hull = m.part("hull", colour)
    pts = []
    for q in range(n + 1):
        t = q / n
        z = 0.0 if pose == "swim" else r + (0.4 * L * max(0.0, 1 - t / 0.45) ** 1.5 if pose == "rear" else 0.0)
        rad = r * (0.65 if q == 0 else 1.1 - 0.9 * t)
        pts.append(((0.5 - t) * L, amp * L * math.sin(2 * math.pi * waves * t) * (0.3 + 0.7 * t), z, rad))
    for a, b in zip(pts, pts[1:]):
        rod(m, hull, a[:3], b[:3], a[3], b[3], seg=4)
    if fin:
        fp = m.part("fin", fin)
        for a, b in zip(pts[2:], pts[3:]):
            tri(m, fp, (a[0], a[1], a[2] + a[3] * 0.9), (b[0], b[1], b[2] + b[3] * 0.9), ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2 + a[3] * 2.2))
    m.save(name)


def octopus(name, L, colour, arm_colour, reach=1.0):
    """Benthic octopus sitting on the seabed: bulbous mantle (length L) slung
    behind the head, eight arms sprawled and curling over the ground."""
    m = Mesh()
    revolve(m, m.part("hull", colour), [(0.05 * L, 0.0), (-0.05 * L, 0.2 * L), (-0.4 * L, 0.3 * L), (-0.8 * L, 0.2 * L), (-0.95 * L, 0.0)], seg=6, squash=0.9, lean_z=0.0)
    for k in ("z",):   # lift the mantle clear of the arms
        m.parts["hull"][k] = [round(v + 0.32 * L, 3) for v in m.parts["hull"][k]]
    arms = m.part("arms", arm_colour)
    for q in range(8):
        a = 2 * math.pi * (q + 0.5) / 8
        R = reach * L * (1.0 + 0.25 * (q % 2))
        p0 = (0.05 * L, 0, 0.22 * L)
        p1 = (0.05 * L + 0.55 * R * math.cos(a), 0.55 * R * math.sin(a), 0.06 * L)
        p2 = (0.05 * L + R * math.cos(a + 0.35), R * math.sin(a + 0.35), 0.03 * L)
        p3 = (0.05 * L + 1.2 * R * math.cos(a + 0.9), 1.2 * R * math.sin(a + 0.9), 0.1 * L)
        rod(m, arms, p0, p1, 0.09 * L, 0.06 * L); rod(m, arms, p1, p2, 0.06 * L, 0.035 * L); rod(m, arms, p2, p3, 0.035 * L, 0.01 * L)
    m.save(name)


# ── Seafloor animals (origin at ground level, +x forward; the part named
# "hull" is what the view measures to size them) ─────────────────────────

def blob(m, p, c, rx, ry, rz, seg=8, flat=0.5):
    """Low-poly ellipsoid centred on c; the underside is squashed by `flat`."""
    ring = lambda z, sc: [m.add_vert(p, c[0] + rx * sc * math.cos(2 * math.pi * q / seg), c[1] + ry * sc * math.sin(2 * math.pi * q / seg), c[2] + z) for q in range(seg)]
    r0, r1 = ring(0, 1.0), ring(rz * 0.65, 0.72)
    top, bot = m.add_vert(p, c[0], c[1], c[2] + rz), m.add_vert(p, c[0], c[1], c[2] - rz * flat)
    for q in range(seg):
        n = (q + 1) % seg
        m.quad(p, r0[q], r0[n], r1[n], r1[q]); m.add_tri(p, r1[q], r1[n], top); m.add_tri(p, r0[n], r0[q], bot)


def crab(name, W, colour, claw_colour, tip_colour, leg=0.7, claw=1.0, shape=(0.4, 0.5), spikes=False, leg_r=0.035):
    """Crab, carapace width W: shape = (half length, half width) of the shell
    in W; `leg` = leg length, `claw` = claw bulk; `spikes` for king crabs."""
    m = Mesh()
    hx, hy = shape[0] * W, shape[1] * W
    h = 0.28 * W
    hull = m.part("hull", colour)
    blob(m, hull, (0, 0, h), hx, hy, 0.17 * W, seg=8)
    if spikes:
        for q in range(7):
            a = 2 * math.pi * q / 7
            cone(m, hull, h + 0.1 * W, h + 0.3 * W, 0.05 * W, seg=3, cx=0.22 * W * math.cos(a), cy=0.28 * W * math.sin(a))
    legs = m.part("legs", claw_colour)
    for side in (1, -1):
        for q in range(4):
            x = (0.22 - q * 0.17) * W
            fan = (0.5 - q * 0.33)          # front legs point forward, back legs back
            knee = (x + fan * 0.4 * leg * W, side * (hy + 0.45 * leg * W), h + 0.3 * leg * W)
            foot = (x + fan * 0.8 * leg * W, side * (hy + 0.95 * leg * W), 0)
            rod(m, legs, (x, side * hy * 0.85, h), knee, leg_r * W, leg_r * 0.8 * W)
            rod(m, legs, knee, foot, leg_r * 0.8 * W, leg_r * 0.25 * W)
        elbow = (hx + 0.12 * W, side * (hy * 0.9 + 0.1 * W), h)
        wrist = (hx + 0.38 * W, side * 0.3 * W, h * 0.8)
        rod(m, legs, (hx * 0.7, side * hy * 0.7, h), elbow, 0.05 * W, 0.05 * W)
        rod(m, legs, elbow, wrist, 0.07 * claw * W, 0.1 * claw * W)
        rod(m, m.part("tips", tip_colour), wrist, (hx + 0.62 * W, side * 0.12 * W, h * 0.8), 0.09 * claw * W, 0.015 * W)
    m.save(name)


def lobster(name, L, colour, claw_colour, claws=1.0, antennae=0.6, antenna_r=0.008):
    """Lobster, body length L: `claws` = claw bulk (0 for spiny lobsters, which
    get thick `antennae` instead)."""
    m = Mesh()
    h = 0.1 * L
    hull = m.part("hull", colour)
    revolve(m, hull, [(0.5 * L, 0.0), (0.44 * L, 0.05 * L), (0.2 * L, 0.075 * L), (0.0, 0.07 * L), (-0.3 * L, 0.055 * L), (-0.42 * L, 0.04 * L)], seg=6, squash=0.9)
    hull["z"] = [round(v + h, 3) for v in hull["z"]]
    for dy in (-0.11, 0.0, 0.11):   # tail fan
        tri(m, hull, (-0.4 * L, dy * 0.4 * L, h), (-0.56 * L, (dy - 0.05) * L, h * 0.6), (-0.56 * L, (dy + 0.05) * L, h * 0.6))
    limbs = m.part("limbs", claw_colour)
    for side in (1, -1):
        for q in range(4):
            x = (0.25 - q * 0.09) * L
            rod(m, limbs, (x, side * 0.05 * L, h), (x + 0.03 * L, side * 0.2 * L, 0), 0.014 * L, 0.006 * L)
        rod(m, limbs, (0.48 * L, side * 0.025 * L, h * 1.2), ((0.5 + antennae) * L, side * (0.1 + 0.4 * antennae) * L, h * (1.5 + 3 * antennae)), antenna_r * L, 0.003 * L)
        if claws:
            elbow = (0.55 * L, side * 0.17 * L, h * 0.8)
            rod(m, limbs, (0.36 * L, side * 0.06 * L, h), elbow, 0.022 * L, 0.025 * L)
            rod(m, limbs, elbow, (0.85 * L, side * 0.13 * L, h * 0.8), 0.055 * claws * L, 0.015 * L, seg=4)
    m.save(name)


def squid(name, L, colour, fin_colour, fin=(0.3, 0.2), arms=0.3, tentacles=0.6, girth=1.0):
    """Squid, mantle length L, mantle tip (+x) leading as when jetting; `fin` =
    (length, half-width) of the tail fins, arms/tentacles trail behind."""
    m = Mesh()
    revolve(m, m.part("hull", colour), [(0.5 * L, 0.0), (0.38 * L, 0.05 * girth * L), (0.05 * L, 0.095 * girth * L), (-0.45 * L, 0.1 * girth * L), (-0.5 * L, 0.07 * girth * L)], seg=6, squash=1.0)
    f = m.part("fins", fin_colour)
    for side in (1, -1):
        tri(m, f, (0.5 * L, 0, 0), ((0.5 - fin[0] * 0.55) * L, side * fin[1] * L, 0), ((0.5 - fin[0]) * L, side * 0.06 * L, 0))
    a = m.part("arms", fin_colour)
    blob(m, a, (-0.58 * L, 0, 0), 0.09 * L, 0.075 * L, 0.07 * L, seg=6, flat=1.0)   # head
    for q in range(8):
        ang = 2 * math.pi * q / 8 + 0.2
        rod(m, a, (-0.64 * L, 0.04 * L * math.cos(ang), 0.04 * L * math.sin(ang)), ((-0.64 - arms) * L, 0.11 * L * math.cos(ang), 0.11 * L * math.sin(ang)), 0.02 * L, 0.004 * L)
    for side in (1, -1):
        club = ((-0.64 - tentacles) * L, side * 0.05 * L, -0.03 * L)
        rod(m, a, (-0.64 * L, side * 0.02 * L, 0), club, 0.012 * L, 0.008 * L)
        rod(m, a, club, (club[0] - 0.12 * L, club[1], club[2]), 0.025 * L, 0.004 * L)
    m.save(name)


def star(name, n, R, inner, colour, h=0.12, disc=None):
    """Starfish / brittle star: n arms of length R, valleys at inner*R, centre
    raised by h*R. `disc` = (radius, colour) central disc for brittle stars."""
    m = Mesh()
    p = m.part("hull", colour)
    centre = (0, 0, h * R + 0.02 * R)
    for q in range(n):
        a0, a1, a2 = (2 * math.pi * (q - 0.5) / n, 2 * math.pi * q / n, 2 * math.pi * (q + 0.5) / n)
        tip = (R * math.cos(a1), R * math.sin(a1), 0.015 * R)
        for av in (a0, a2):
            tri(m, p, centre, (inner * R * math.cos(av), inner * R * math.sin(av), 0.015 * R), tip)
    if disc:
        blob(m, m.part("disc", disc[1]), (0, 0, 0.03 * R), disc[0] * R, disc[0] * R, 0.06 * R, seg=5)
    m.save(name)


def urchin(name, r, colour, spine_colour, spine=0.6, n=22):
    """Sea urchin: test of radius r with n spines of length spine*r."""
    m = Mesh()
    blob(m, m.part("hull", colour), (0, 0, 0.55 * r), r, r, 0.75 * r, seg=7)
    sp = m.part("spines", spine_colour)
    for q in range(n):
        zc = 1 - (q + 0.5) / n * 1.15            # Fibonacci sphere, upper part only
        ang = q * 2.399963
        rr = math.sqrt(max(0.0, 1 - zc * zc))
        d = (rr * math.cos(ang), rr * math.sin(ang), zc)
        b = (d[0] * r * 0.85, d[1] * r * 0.85, 0.55 * r + d[2] * r * 0.65)
        rod_tip = (b[0] + d[0] * spine * r, b[1] + d[1] * spine * r, max(0.0, b[2] + d[2] * spine * r))
        t = (-d[1], d[0], 0) if rr > 0.2 else (1, 0, 0)
        w = 0.07 * r
        tri(m, sp, (b[0] + t[0] * w, b[1] + t[1] * w, b[2]), (b[0] - t[0] * w, b[1] - t[1] * w, b[2]), rod_tip)
    m.save(name)


def anemone(name, h, r, colour, crown_colour, n=12, reach=0.8, fluffy=False):
    """Anemone: column of height h, radius r, crown of n tentacles; `fluffy`
    gives the plumose anemone's dense feathery head."""
    m = Mesh()
    prism(m, m.part("column", colour), [(0, 1.15), (h * 0.6, 0.85), (h, 1.05)], r, seg=6)
    c = m.part("crown", crown_colour)
    for ring, (tilt, ln) in enumerate(((0.35, 1.0), (0.9, 0.8)) if not fluffy else ((0.2, 0.7), (0.7, 0.75), (1.2, 0.7))):
        for q in range(n):
            a = 2 * math.pi * (q + 0.5 * ring) / n
            blade(m, c, (0.6 * r * math.cos(a), 0.6 * r * math.sin(a), h), reach * r * 2 * ln, r * (0.5 if fluffy else 0.3), a, tilt, droop=0.1 * r)
    m.save(name)


def flatfish(name, L, colour, fin_colour, spots=None, slim=0.24):
    """Flatfish lying on the seabed, eyed side up; `spots` = colour of a
    plaice's orange spots."""
    m = Mesh()
    p = m.part("hull", colour)
    z0, zc = 0.012 * L, 0.06 * L
    edge = [((0.08 + 0.4 * math.cos(2 * math.pi * q / 10)) * L, slim * L * math.sin(2 * math.pi * q / 10), z0) for q in range(10)]
    for a, b in zip(edge, edge[1:] + edge[:1]):
        tri(m, p, (0.08 * L, 0, zc), a, b)
    f = m.part("fins", fin_colour)
    tri(m, f, (-0.3 * L, 0, z0), (-0.5 * L, 0.11 * L, z0), (-0.5 * L, -0.11 * L, z0))
    for side in (1, -1):   # fringing dorsal/anal fins
        tri(m, f, (0.4 * L, side * 0.1 * L, z0 * 0.8), (0.05 * L, side * (slim + 0.07) * L, z0 * 0.8), (-0.3 * L, side * 0.08 * L, z0 * 0.8))
    if spots:
        sp = m.part("spots", spots)
        for x, y in ((0.25, 0.08), (0.05, -0.1), (-0.1, 0.09), (-0.18, -0.04), (0.2, -0.06), (0.0, 0.14)):
            rho = math.hypot((x) / 0.4, y / slim)
            z = z0 + (zc - z0) * max(0.0, 1 - rho) + 0.006 * L
            tri(m, sp, ((x + 0.08 + 0.03) * L, y * L, z), ((x + 0.08 - 0.02) * L, (y + 0.028) * L, z), ((x + 0.08 - 0.02) * L, (y - 0.028) * L, z))
    m.save(name)


# Whales: (name, length m, colour, fin colour, options)
whale("whale_humpback", 14, "#4A5568", "#3A4353", flipper=0.32, belly="#B9C2CC")
whale("whale_minke", 8, "#5B6673", "#4A5563", dorsal=0.05, flipper=0.16, belly="#D5DBE1")
whale("whale_fin", 20, "#55606C", "#47515C", dorsal=0.04, flipper=0.14, belly="#C9D0D6")
whale("whale_blue", 25, "#6E8296", "#5C6E80", dorsal=0.025, flipper=0.14)
whale("whale_sperm", 16, "#5A5250", "#4A4340", head="blunt", dorsal=0.0, flipper=0.12)
whale("whale_orca", 7, "#1F2429", "#1F2429", dorsal=0.2, flipper=0.2, belly="#F2F4F6", eyepatch="#F2F4F6")
whale("whale_pilot", 6, "#2B2F35", "#2B2F35", head="blunt", dorsal=0.09, flipper=0.2)
whale("whale_beluga", 4.5, "#EEF1F3", "#E1E5E9", head="round", dorsal=0.0, flipper=0.15)
whale("whale_narwhal", 4.5, "#B9BDC3", "#A6ABB2", head="round", dorsal=0.0, flipper=0.13, tusk=0.5)
whale("whale_sei", 15, "#4F5A66", "#414A55", head="pointed", dorsal=0.06, flipper=0.14, belly="#C7CED5")
# Dolphins & porpoise: beak = (length, colour), flank = side blaze
whale("dolphin_common", 2.3, "#3A4654", "#2F3A46", dorsal=0.1, flipper=0.16, belly="#F0F0EC", beak=(0.07, "#2F3A46"), flank="#E0C47A")          # tan hourglass flank
whale("dolphin_bottlenose", 3.2, "#7A8794", "#65717D", dorsal=0.1, flipper=0.16, belly="#D9DEE2", beak=(0.04, "#7A8794"))                          # plain grey, stubby beak
whale("dolphin_whitebeaked", 2.8, "#2C333B", "#23292F", dorsal=0.12, flipper=0.17, belly="#EEF0F1", beak=(0.03, "#F0F0EE"), flank="#AEB6BD")     # white beak, pale blaze
whale("dolphin_spinner", 2.0, "#55616E", "#3A434D", head="pointed", dorsal=0.11, flipper=0.15, belly="#ECE6E0", beak=(0.1, "#3A434D"))             # slender, very long beak
whale("dolphin_hourglass", 1.8, "#15181C", "#15181C", dorsal=0.12, flipper=0.16, belly="#F4F5F6", beak=(0.025, "#15181C"), flank="#F4F5F6")      # black with white flank patches
whale("porpoise_harbour", 1.6, "#4A525B", "#3C434B", head="blunt", dorsal=0.05, flipper=0.12, belly="#DDE1E4")                                     # small, beakless, low triangular fin

# Sharks
shark("shark_basking", 8, "#6B6558", dorsal=0.12, blunt=True)
shark("shark_white", 5, "#6C7A88")
shark("shark_blue", 3, "#4A78B5")
shark("shark_greenland", 4, "#4B4F55", belly="#6E737A", dorsal=0.06)
shark("shark_porbeagle", 2.5, "#4E5C6A")
shark("shark_hammerhead", 3.5, "#6F7C87", dorsal=0.17, hammer=0.14)
shark("shark_whale", 10, "#4F6577", belly="#E4E8EA", dorsal=0.09, blunt=True, girth=1.15, flat=0.8, spots="#DCE6EC")

# Fish schools
school("fish_herring", 0.3, "#C4D0DC", count=10, spread=2.0, back="#3E5E78")
school("fish_mackerel", 0.35, "#C9D6DA", count=8, spread=2.0, deep=0.11, back="#1F6F6A")
school("fish_sardine", 0.2, "#D2DAE2", count=10, spread=1.5, back="#3F6C8C")
school("fish_tuna", 1.2, "#C2CAD2", count=4, spread=3.5, deep=0.2, back="#1E3358")
school("fish_cod", 0.8, "#D8D2B8", count=4, spread=2.5, deep=0.17, back="#7A7244")
school("fish_lanternfish", 0.08, "#9FD0E0", count=10, spread=0.8, deep=0.16, back="#2A2F3A")
school("fish_capelin", 0.18, "#CBD6D2", count=10, spread=1.5, deep=0.1, back="#5E7A4E")
school("fish_anchovy", 0.15, "#CDD6DE", count=10, spread=1.2, deep=0.1, back="#3F7A7A")

# Jellyfish (name, bell radius m, bell profile, colour, features)
# Moon jelly (Aurelia): clear saucer, four pink horseshoe gonads, a short fringe.
jelly("jelly_moon", 0.2, SAUCER, "#E6ECF2", tent=(12, 0.35, "#D6DEE6", 0.06), arms=(4, 0.7, "#DCE4EC", 0.08), marks=(4, 1, 0.25, 0.75, 0.45, "#C58BC9"))
# Lion's mane (Cyanea capillata): broad red-brown bell, a dense mane of very long fine tentacles.
jelly("jelly_lionsmane", 0.6, SAUCER, "#B5482A", tent=(16, 5.0, "#C8703E", 0.05), arms=(4, 1.6, "#8C2F1E", 0.2))
# Compass (Chrysaora hysoscella): cream bell with brown V-shaped rays, long frilly arms.
jelly("jelly_compass", 0.15, DOME, "#E8D9B5", tent=(12, 3.0, "#7A4A25", 0.05), arms=(4, 3.0, "#EFE3C8", 0.1), marks=(8, 1, 0.0, 1.0, 0.16, "#7A4A25"), rim="#7A4A25")
# Barrel / dustbin-lid (Rhizostoma): big solid dome, purple rim, cauliflower arms, no tentacles.
jelly("jelly_barrel", 0.45, [(0, 1.0), (0.5, 1.0), (0.95, 0.65), (1.15, 0.0)], "#C9D6E8", arms=(4, 1.5, "#B9C4DE", 0.3), rim="#5B3F8C")
# Blue jellyfish (Cyanea lamarckii): small blue cousin of the lion's mane.
jelly("jelly_blue", 0.15, SAUCER, "#4F74C4", tent=(12, 2.5, "#7F9AD8", 0.05), arms=(4, 1.2, "#C9D4EE", 0.15), marks=(8, 1, 0.1, 0.9, 0.12, "#2F4A94"))
# Mauve stinger (Pelagia noctiluca): pink-mauve open-ocean jelly, eight tentacles, long frilled arms.
jelly("jelly_mauve", 0.05, DOME, "#C97FB0", tent=(8, 5.0, "#B0608F", 0.07), arms=(4, 3.0, "#E0A8C8", 0.12))
# Pacific sea nettle (Chrysaora fuscescens): golden bell, maroon tentacles, long white arms.
jelly("jelly_nettle", 0.25, DOME, "#C98A2E", tent=(12, 5.0, "#7A2A22", 0.05), arms=(4, 4.0, "#F0E6DA", 0.12))
# Box jellyfish (Chironex): squared-off clear bell, tentacle bundles at the four corners.
def _box_tentacles(m, r=0.15):
    p = m.part("tentacles", "#E8EEF5")
    for c in range(4):
        for off in (-0.25, 0, 0.25):
            ang = math.pi / 2 * c
            ribbon(m, p, (r * math.cos(ang), r * math.sin(ang), 0), ang + off, [(0, 0), (0.5 * r, -2.5 * r), (0.3 * r, -5 * r)], [0.08 * r, 0.06 * r, 0.02 * r])
jelly("jelly_box", 0.15, [(0, 1.0), (1.0, 1.0), (1.3, 0.75), (1.4, 0.0)], "#CFE3F2", seg=4, extra=_box_tentacles)
# Helmet jelly (Periphylla): deep-red pointed cone, stiff tentacles held up over the bell.
jelly("jelly_helmet", 0.1, [(0, 1.0), (0.5, 0.95), (1.0, 0.6), (2.0, 0.0)], "#8C1F2B", tent=(12, 1.8, "#B5323C", 0.06, True), rim="#5E121C")
# Atolla (crown jelly): flat deep-red disc with a grooved crown and one long trailing tentacle.
def _atolla_trail(m, r=0.08):
    ribbon(m, m.part("tentacles", "#C0404A"), (0.9 * r, 0, 0), 0, [(0, 0), (1.5 * r, -3 * r), (1.0 * r, -7 * r)], [0.08 * r, 0.06 * r, 0.02 * r])
jelly("jelly_atolla", 0.08, [(0, 1.0), (0.15, 1.0), (0.2, 0.6), (0.45, 0.5), (0.55, 0.0)], "#A3232F", tent=(10, 1.0, "#C0404A", 0.06), extra=_atolla_trail)
# Diplulmaris antarctica: white Antarctic saucer with orange rays and arms.
jelly("jelly_antarctic", 0.2, SAUCER, "#F2F2EE", tent=(12, 1.5, "#FFFFFF", 0.05), arms=(4, 2.0, "#E8A050", 0.1), marks=(8, 1, 0.1, 0.9, 0.1, "#E8902E"))
# Cannonball (Stomolophus): firm milky hemisphere, brown rim, one stubby arm cluster, no tentacles.
jelly("jelly_cannonball", 0.1, [(0, 1.0), (0.5, 1.0), (0.9, 0.7), (1.1, 0.0)], "#E6DCCB", arms=(4, 0.7, "#D9CDB8", 0.3), rim="#7A4B2E")
# Fried-egg jelly (Cotylorhiza): flat pale bell, orange yolk dome, short purple-tipped arms.
jelly("jelly_friedegg", 0.15, [(0, 1.0), (0.12, 0.9), (0.22, 0.0)], "#F2E6B0", arms=(8, 0.7, "#7B55B0", 0.1), top=(0.45, 0.35, "#E8A82E"))

# Portuguese man o' war (Physalia): not a true jelly — a gas float with a pink-edged
# sail at the surface and long dark-blue tentacles below. +x along the float.
m = Mesh()
L = 0.25
revolve(m, m.part("hull", "#8FA8E8"), [(x * L, rr * L) for x, rr in [(0.5, 0.0), (0.3, 0.13), (0.0, 0.17), (-0.35, 0.1), (-0.5, 0.0)]], seg=6, squash=0.9)
sail = m.part("sail", "#D78AC0")
tri(m, sail, (0.32 * L, 0, 0.1 * L), (-0.05 * L, 0, 0.14 * L), (0.1 * L, 0, 0.34 * L))
tri(m, sail, (-0.05 * L, 0, 0.14 * L), (-0.35 * L, 0, 0.08 * L), (-0.15 * L, 0, 0.3 * L))
t = m.part("tentacles", "#2B3F9C")
for q in range(8):
    x = (0.3 - q * 0.08) * L
    n = (3 + (q * 5) % 4) * L
    ribbon(m, t, (x, 0, -0.1 * L), math.pi + (q % 3 - 1) * 0.5, [(0, 0), (0.15 * n, -0.5 * n), (0.3 * n, -n)], [0.05 * L, 0.04 * L, 0.015 * L])
m.save("manowar")

# ── Sunfish, rays, turtles, eels, octopuses ──
# Ocean sunfish (Mola mola): a tall, slab-sided disc chopped off behind the
# fins, with one high dorsal and one matching anal fin, no real tail.
m = Mesh()
L = 2.0
revolve(m, m.part("hull", "#9AA3AB"), [(x * L, r * L) for x, r in [(0.5, 0.0), (0.42, 0.035), (0.15, 0.07), (-0.25, 0.072), (-0.4, 0.055), (-0.45, 0.0)]], seg=8, squash=5.0)
fins = m.part("fins", "#6F7982")
for sgn in (1, -1):
    tri(m, fins, (-0.12 * L, 0, sgn * 0.33 * L), (-0.4 * L, 0, sgn * 0.27 * L), (-0.33 * L, 0, sgn * 0.85 * L))
    tri(m, fins, (0.12 * L, sgn * 0.072 * L, 0.02 * L), (-0.02 * L, sgn * 0.09 * L, 0.12 * L), (0.02 * L, sgn * 0.075 * L, 0.0))
m.save("mola_mola")

ray("ray_manta", 2.2, 4.8, "#1B1F26", "#EEF0F2", tail=0.5, lobes=True)                      # oceanic manta: huge pointed wings, head fins
ray("ray_eagle", 1.2, 2.2, "#2A3550", "#F2F2F0", tail=1.6, snout=0.12)                      # spotted eagle ray: duck-bill snout, very long tail
ray("ray_thornback", 0.8, 0.6, "#8A7A55", "#ECE6D6", tail=0.55, rest=True)                  # thornback skate: kite on the seabed
ray("ray_sting", 1.0, 1.0, "#7E7A68", "#EFEAE0", tail=1.0, rounded=True, rest=True)         # stingray: rounded disc half-buried in sand

turtle("turtle_green", 1.1, "#5F6238", "#8A8F6A")
turtle("turtle_loggerhead", 1.0, "#9A5A2E", "#C9A86A", head=0.095)                          # big-headed, red-brown
turtle("turtle_hawksbill", 0.85, "#B07A2A", "#6B5A38", head=0.06, flipper=0.5)              # amber shell, narrow head
turtle("turtle_leatherback", 2.0, "#23272E", "#2B3038", profile=[(0.42, 0.0), (0.3, 0.17), (0.05, 0.25), (-0.35, 0.15), (-0.6, 0.0)], flipper=0.75, ridges="#8E969E")

eel("eel_european", 0.9, "#B9C0C4", 0.03, pose="swim", fin="#4A4F3A")                       # silver eel on its ocean migration
eel("eel_conger", 2.0, "#5B6068", 0.055, pose="rest", amp=0.09, fin="#2E3238")              # conger: big grey eel of wrecks and rocky ground
eel("eel_moray", 1.5, "#6F7F35", 0.06, pose="rear", amp=0.06, waves=1.0, fin="#56642A")     # green moray, head raised from the reef

octopus("octopus_common", 0.25, "#A8553A", "#B8674A")
octopus("octopus_giant_pacific", 0.6, "#B5422E", "#C4553C", reach=1.3)

# Dumbo octopus (Grimpoteuthis), ~25 cm: soft rounded head, a small paddle fin
# sticking out each side like an ear, and short arms webbed into an umbrella.
m = Mesh()
R = 0.1
blob(m, m.part("hull", "#EBC9BC"), (0, 0, 0.1), R, R * 0.95, R * 1.15, seg=8, flat=0.3)
web = m.part("web", "#DDAA98")
top = [m.add_vert(web, 0.85 * R * math.cos(2 * math.pi * q / 16), 0.85 * R * math.sin(2 * math.pi * q / 16), 0.07) for q in range(16)]
rim = [m.add_vert(web, (1.75 if q % 2 == 0 else 1.3) * R * math.cos(2 * math.pi * q / 16), (1.75 if q % 2 == 0 else 1.3) * R * math.sin(2 * math.pi * q / 16), -0.1 if q % 2 == 0 else -0.04) for q in range(16)]
for q in range(16):
    m.quad(web, top[q], top[(q + 1) % 16], rim[(q + 1) % 16], rim[q])
for side in (1, -1):   # fins: low rounded paddles out to the side, not up
    base = [(0.035, side * 0.085, 0.15), (-0.035, side * 0.085, 0.15)]
    tri(m, web, base[0], base[1], (-0.045, side * 0.15, 0.17))
    tri(m, web, base[0], (-0.045, side * 0.15, 0.17), (0.03, side * 0.155, 0.175))
    tri(m, m.part("eyes", "#2A2430"), (0.092, side * 0.04, 0.115), (0.08, side * 0.062, 0.11), (0.087, side * 0.05, 0.085))
m.save("octopus_dumbo")

# ── Crabs & lobsters ──
crab("crab_brown", 0.2, "#9C5A3C", "#8A4C32", "#1E1A18", leg=0.55, claw=1.3)                              # edible crab: pie-crust oval, black-tipped claws
crab("crab_spider", 0.18, "#B5643A", "#C27A4A", "#E8D0B0", leg=1.1, claw=0.6, shape=(0.5, 0.42), spikes=True)  # spiny spider crab: pear-shaped, leggy
crab("crab_snow", 0.14, "#C9773F", "#D98B4F", "#F0E0D0", leg=1.7, claw=0.6, shape=(0.45, 0.45), leg_r=0.03)   # snow crab: small disc, very long legs
crab("crab_king", 0.22, "#A3241F", "#B02C24", "#7A1815", leg=1.4, claw=0.9, shape=(0.48, 0.5), spikes=True, leg_r=0.05)  # red king crab: big, thorny
lobster("lobster_european", 0.5, "#23355E", "#2C4273", claws=1.0)                                           # alive they're blue
lobster("lobster_american", 0.55, "#4A4A2E", "#6B4A2A", claws=1.1)
lobster("lobster_norway", 0.2, "#E89058", "#F0A070", claws=0.55)                                            # Nephrops / langoustine: slim, pale orange
lobster("lobster_spiny", 0.45, "#A85A2A", "#C47A3A", claws=0, antennae=0.9, antenna_r=0.022)                # no claws, heavy antennae

# ── Squid, cuttlefish, nautilus ──
squid("squid_common", 0.4, "#E0B8B0", "#D09A90", fin=(0.6, 0.22))                                           # Loligo: long diamond fins
squid("squid_humboldt", 1.2, "#A8342E", "#8C2A26", fin=(0.4, 0.3), girth=1.1)
squid("squid_giant", 2.0, "#B8443A", "#9C3830", fin=(0.2, 0.12), arms=0.6, tentacles=1.8)                   # Architeuthis: small fins, enormous tentacles
squid("squid_colossal", 2.5, "#9C3A3A", "#7E2E30", fin=(0.45, 0.35), arms=0.4, tentacles=0.9, girth=1.5)    # Mesonychoteuthis: barrel body, big fins

m = Mesh()   # Cuttlefish (Sepia): broad flattened oval, fin skirt all round, short arms held forward (+x)
L = 0.35
prof = [(-0.5, 0.0), (-0.4, 0.13), (-0.1, 0.2), (0.2, 0.17), (0.32, 0.1)]
revolve(m, m.part("hull", "#9A7A52"), [(x * L, r * L) for x, r in prof], seg=8, squash=0.55)
sk = m.part("skirt", "#DCCDAA")
for side in (1, -1):
    for (xa, ra), (xb, rb) in zip(prof, prof[1:]):
        tri(m, sk, (xa * L, side * ra * L, 0), (xb * L, side * rb * L, 0), (xb * L, side * (rb + 0.06) * L, 0))
        tri(m, sk, (xa * L, side * ra * L, 0), (xb * L, side * (rb + 0.06) * L, 0), (xa * L, side * (ra + (0.06 if ra else 0)) * L, 0))
ar = m.part("arms", "#B8966A")
blob(m, ar, (0.38 * L, 0, 0), 0.09 * L, 0.11 * L, 0.07 * L, seg=6, flat=1.0)
for q in range(6):
    y = (q - 2.5) * 0.035 * L
    rod(m, ar, (0.44 * L, y, 0), (0.68 * L, y * 0.5, -0.06 * L), 0.022 * L, 0.004 * L)
m.save("cuttlefish")

m = Mesh()   # Nautilus: upright coiled shell, white with rust tiger stripes, tentacles from the opening
L = 0.2
blob(m, m.part("hull", "#F0EBE0"), (0, 0, 0), 0.5 * L, 0.2 * L, 0.5 * L, seg=8, flat=1.0)
st = m.part("stripes", "#9C4A2A")
for side in (1, -1):
    for q in range(5):
        a0 = math.pi * 0.35 + q * 0.5
        pt = lambda a, rr, yy: (rr * L * math.cos(a), side * yy * L, rr * L * math.sin(a))
        tri(m, st, pt(a0, 0.08, 0.215), pt(a0 - 0.1, 0.44, 0.1), pt(a0 + 0.12, 0.44, 0.1))
hood = m.part("arms", "#D9B48A")
for q in range(6):
    rod(m, hood, (0.4 * L, (q - 2.5) * 0.04 * L, -0.15 * L), (0.75 * L, (q - 2.5) * 0.08 * L, -0.3 * L), 0.03 * L, 0.005 * L)
m.save("nautilus")

octopus("octopus_antarctic", 0.2, "#C9A8A0", "#D8B8AE", reach=0.9)                                          # Pareledone: pale, warty, Southern Ocean shelf

# ── Shells ──
def scallop(name, colour, rib_colour, R=0.12):
    """Scallop lying flat: ribbed fan + hinge 'ears', ribs in alternating colours."""
    m = Mesh()
    parts = (m.part("hull", colour), m.part("ribs", rib_colour))
    hinge = (-0.8 * R, 0, 0.03 * R)
    n = 10
    for q in range(n):
        a0, a1 = (math.radians(-75 + 150 * q / n), math.radians(-75 + 150 * (q + 1) / n))
        e = lambda a: (hinge[0] + 1.8 * R * math.cos(a), 1.8 * R * math.sin(a) * 0.62, 0.02 * R)
        mid = (hinge[0] + 0.9 * R * math.cos((a0 + a1) / 2), 0.9 * R * math.sin((a0 + a1) / 2) * 0.62, 0.3 * R)
        p = parts[q % 2]
        tri(m, p, hinge, e(a0), mid); tri(m, p, hinge, mid, e(a1)); tri(m, p, mid, e(a0), e(a1))
    for side in (1, -1):
        tri(m, parts[0], hinge, (-0.95 * R, side * 0.4 * R, 0.02 * R), (-0.55 * R, side * 0.35 * R, 0.05 * R))
    m.save(name)

scallop("scallop_king", "#E8D2B8", "#B5604A")        # Pecten maximus
scallop("scallop_sea", "#E0C8B0", "#C98A6A")         # Placopecten, NW Atlantic
scallop("scallop_antarctic", "#C9A0A8", "#A87884")   # Adamussium colbecki

m = Mesh()   # Giant clam (Tridacna): thick fluted valves gaping upward, electric-blue mantle between the lips
L = 0.8
shell, mantle = m.part("hull", "#D9D2C0"), m.part("mantle", "#2F7FBF")
lip = [((q / 6 - 0.5) * L, (0.2 + 0.05 * (-1) ** q) * L, (0.55 + 0.1 * (-1) ** q) * L) for q in range(7)]
for side in (1, -1):
    for (xa, ya, za), (xb, yb, zb) in zip(lip, lip[1:]):
        tri(m, shell, (xa, side * 0.06 * L, 0), (xb, side * 0.06 * L, 0), (xb, side * yb, zb))
        tri(m, shell, (xa, side * 0.06 * L, 0), (xb, side * yb, zb), (xa, side * ya, za))
        tri(m, mantle, (xa, side * ya * 0.9, za * 0.97), (xb, side * yb * 0.9, zb * 0.97), ((xa + xb) / 2, 0, 0.36 * L))
    for x, y, z in (lip[0], lip[-1]):
        tri(m, shell, (x, side * 0.06 * L, 0), (x, side * y, z), (x * 1.0, 0, 0.3 * L))
m.save("clam_giant")

m = Mesh()   # Mussel bed: a clump of blue-black shells
rnd = random.Random(8)
p = m.part("hull", "#1F2A44")
for q in range(8):
    a = rnd.uniform(0, 2 * math.pi); d = rnd.uniform(0, 0.16)
    long_x = q % 2 == 0
    blob(m, p, (d * math.cos(a), d * math.sin(a), 0.03), 0.07 if long_x else 0.035, 0.035 if long_x else 0.07, 0.045, seg=6)
m.save("mussels")

# ── Starfish, urchins, sea cucumbers ──
star("starfish_common", 5, 0.15, 0.3, "#E0742E")                   # Asterias rubens
star("starfish_sunflower", 16, 0.4, 0.55, "#8A4A9C", h=0.08)       # Pycnopodia: many-armed, NE Pacific
star("starfish_crown", 14, 0.25, 0.5, "#8C3A4A", h=0.1)            # crown-of-thorns
star("starfish_blue", 5, 0.15, 0.22, "#2F6FD0", h=0.1)             # Linckia, Indo-Pacific reefs
star("starfish_cushion", 5, 0.2, 0.6, "#D9842E", h=0.3)            # Oreaster: fat Caribbean cushion star
star("starfish_antarctic", 5, 0.08, 0.45, "#B5302A", h=0.15)       # Odontaster validus
star("brittle_star", 5, 0.15, 0.06, "#B8A890", h=0.03, disc=(0.16, "#8A7A66"))

urchin("urchin_edible", 0.07, "#C97A9C", "#E8D0DC", spine=0.35)    # Echinus esculentus: pink globe, short pale spines
urchin("urchin_diadema", 0.05, "#15151A", "#15151A", spine=3.0)    # long-spined black reef urchin
urchin("urchin_antarctic", 0.04, "#8C2F3A", "#B5606A", spine=0.8)  # Sterechinus

m = Mesh()   # Sea pig (Scotoplanes): the sea cucumber of the abyssal plains — plump, pink, on tube feet
L = 0.12
blob(m, m.part("hull", "#E8B8B0"), (0, 0, 0.3 * L), 0.5 * L, 0.26 * L, 0.24 * L, seg=8)
ft = m.part("feet", "#F0CCC4")
for side in (1, -1):
    for q in range(4):
        x = (0.3 - q * 0.2) * L
        rod(m, ft, (x, side * 0.18 * L, 0.2 * L), (x, side * 0.26 * L, 0), 0.035 * L, 0.025 * L)
    for x in (0.2, -0.05):
        rod(m, ft, (x * L, side * 0.1 * L, 0.5 * L), ((x - 0.12) * L, side * 0.15 * L, 0.68 * L), 0.025 * L, 0.006 * L)
m.save("sea_pig")

anemone("anemone_plumose", 0.3, 0.06, "#E8E2D8", "#FFFFFF", n=10, reach=0.9, fluffy=True)   # Metridium: tall white column, feathery head
anemone("anemone_reef", 0.12, 0.12, "#8C4A9C", "#D9C08A", n=12, reach=0.7)                 # magnificent anemone: purple column, tan tentacles
anemone("anemone_antarctic", 0.15, 0.08, "#E0945A", "#F0C8A0", n=12, reach=0.9)

flatfish("plaice", 0.45, "#7A6A48", "#6B5C3E", spots="#E8842E")
flatfish("halibut", 1.8, "#5E5F4A", "#4E4F3E", slim=0.18)

# ── Bush: squat 6-sided dome, 1.2 m ──
m = Mesh()
b = m.part("leaves", "#5F8F45")
cone(m, b, 0, 1.2, 0.9, seg=6)
m.save("bush")
# One bundle for the view (a single request instead of one per model).
names = sorted(f[:-5] for f in os.listdir(OUT) if f.endswith(".json") and not f.startswith("_"))
with open(os.path.join(OUT, "_bundle.json"), "w") as f:
    json.dump({n: json.load(open(os.path.join(OUT, n + ".json"))) for n in names}, f, separators=(",", ":"))
print("wrote", len(names), "models + _bundle.json")
