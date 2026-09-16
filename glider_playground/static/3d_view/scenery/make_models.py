"""Generate the low-poly scenery meshes (kelp, seagrass, trees, bushes).

Same JSON format as the vehicle models in ../models: one file per object,
`{ part: {x, y, z, i, j, k, color} }`, metres, +z up. Scenery origins sit
at ground level (z = 0 is the seabed / land the object stands on) so the
3D view can drop them straight onto the bathymetry.

Run from anywhere:  python make_models.py   (writes *.json next to itself)
"""
import json
import math
import os

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


def prism(m, p, pts, r, seg=3, lean=(0, 0)):
    """Tapered polygonal column through `pts` = [(z, radius_scale)...]; `lean`
    shifts the top by (dx, dy) metres, linearly with height."""
    top = pts[-1][0]
    rings = []
    for z, s in pts:
        ring = []
        for q in range(seg):
            a = 2 * math.pi * q / seg
            ring.append(m.add_vert(p, r * s * math.cos(a) + lean[0] * z / top, r * s * math.sin(a) + lean[1] * z / top, z))
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


# ── Kelp (Macrocystis-style): 8 m stipe with blades alternating sides ──
m = Mesh()
stipe = m.part("stipe", "#5C5A2E")
prism(m, stipe, [(0, 1.0), (4, 0.8), (8, 0.5)], 0.06, seg=3, lean=(0.6, 0.2))
fronds = m.part("blades", "#7C8F2E")
for q in range(7):
    z = 1.5 + q * 0.95
    side = 1 if q % 2 else -1
    blade(m, fronds, (0.6 * z / 8, 0.2 * z / 8, z), 1.3, 0.35, side * math.pi / 2 + q * 0.5, 0.45, droop=0.1)
m.save("kelp")

# ── Seagrass: tuft of 6 thin blades, 0.8 m ──
m = Mesh()
g = m.part("blades", "#3F8F4A")
for q in range(6):
    a = 2 * math.pi * q / 6 + 0.3
    blade(m, g, (0.05 * math.cos(a), 0.05 * math.sin(a), 0), 0.8, 0.08, a, 1.25, droop=0.05)
m.save("seagrass")

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

def revolve(m, p, profile, seg=8, squash=0.8, lean_z=0.0):
    """Body of revolution along x: profile = [(x, radius)...]; y radius = r,
    z radius = r*squash; `lean_z` lifts the tail end (whales' fluke stock)."""
    rings = []
    x0, x1 = profile[0][0], profile[-1][0]
    for x, r in profile:
        lift = lean_z * (x1 - x) / (x1 - x0)
        if r == 0:
            rings.append([m.add_vert(p, x, 0, lift)] * seg)
        else:
            rings.append([m.add_vert(p, x, r * math.cos(2 * math.pi * q / seg), lift + r * squash * math.sin(2 * math.pi * q / seg)) for q in range(seg)])
    for r0, r1 in zip(rings, rings[1:]):
        for q in range(seg):
            m.quad(p, r0[q], r0[(q + 1) % seg], r1[(q + 1) % seg], r1[q])


def tri(m, p, a, b, c):
    m.add_tri(p, m.add_vert(p, *a), m.add_vert(p, *b), m.add_vert(p, *c))


def whale(name, L, colour, fin_colour, head="round", dorsal=0.06, flipper=0.25, fluke=0.2, belly=None, tusk=0.0):
    """Baleen/toothed whale: body, horizontal flukes, pectoral flippers, an
    optional dorsal fin (height as a fraction of L) and optional tusk."""
    m = Mesh()
    body = m.part("hull", colour)
    nose = [(0.5, 0.0), (0.42, 0.06)] if head == "round" else [(0.5, 0.0), (0.46, 0.09), (0.35, 0.115)] if head == "blunt" else [(0.5, 0.0), (0.4, 0.045)]
    prof = nose + [(0.2, 0.11), (0.0, 0.115), (-0.2, 0.09), (-0.38, 0.045), (-0.46, 0.02)]
    revolve(m, body, [(x * L, r * L) for x, r in prof], seg=8, squash=0.85, lean_z=0.02 * L)
    fins = m.part("fins", fin_colour)
    for side in (1, -1):   # flukes (horizontal)
        tri(m, fins, (-0.45 * L, 0, 0.02 * L), (-0.45 * L - fluke * 0.6 * L, side * fluke * L, 0.03 * L), (-0.5 * L, side * 0.03 * L, 0.025 * L))
        tri(m, fins, (0.18 * L, side * 0.1 * L, -0.04 * L), (-0.1 * L, side * (0.1 + flipper) * L, -0.07 * L), (0.04 * L, side * 0.1 * L, -0.05 * L))
    if dorsal:
        tri(m, fins, (-0.08 * L, 0, 0.09 * L), (-0.22 * L, 0, 0.09 * L), (-0.2 * L, 0, (0.09 + dorsal) * L))
    if belly:
        b = m.part("belly", belly)
        for side in (1, -1):
            tri(m, b, (0.3 * L, side * 0.1 * L, -0.06 * L), (-0.15 * L, side * 0.09 * L, -0.07 * L), (0.1 * L, side * 0.02 * L, -0.1 * L))
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


def shark(name, L, colour, belly="#D8DEE3", dorsal=0.14, tail=0.22):
    """Shark: pointed snout, tall dorsal fin, vertical asymmetric tail, pale belly."""
    m = Mesh()
    body = m.part("hull", colour)
    prof = [(0.5, 0.0), (0.38, 0.05), (0.15, 0.085), (-0.1, 0.075), (-0.3, 0.035), (-0.42, 0.015)]
    revolve(m, body, [(x * L, r * L) for x, r in prof], seg=6, squash=1.1)
    fins = m.part("fins", colour)
    tri(m, fins, (-0.02 * L, 0, 0.07 * L), (-0.18 * L, 0, 0.07 * L), (-0.14 * L, 0, (0.07 + dorsal) * L))            # dorsal
    tri(m, fins, (-0.42 * L, 0, 0.0), (-0.42 * L - 0.12 * L, 0, tail * L), (-0.5 * L, 0, 0.0))                          # upper tail lobe
    tri(m, fins, (-0.42 * L, 0, 0.0), (-0.5 * L, 0, 0.0), (-0.42 * L - 0.06 * L, 0, -tail * 0.5 * L))                   # lower tail lobe
    for side in (1, -1):
        tri(m, fins, (0.15 * L, side * 0.07 * L, -0.03 * L), (-0.08 * L, side * 0.26 * L, -0.06 * L), (0.02 * L, side * 0.07 * L, -0.04 * L))
    b = m.part("belly", belly)
    for side in (1, -1):
        tri(m, b, (0.32 * L, side * 0.06 * L, -0.04 * L), (-0.15 * L, side * 0.06 * L, -0.05 * L), (0.1 * L, side * 0.01 * L, -0.085 * L))
    m.save(name)


def fish(m, p, cx, cy, cz, L, yaw=0.0, deep=0.14):
    """One low-poly fish: elongated octahedron body + a flat tail fin."""
    ca, sa = math.cos(yaw), math.sin(yaw)
    P = lambda x, y, z: m.add_vert(p, cx + x * ca - y * sa, cy + x * sa + y * ca, cz + z)
    nose, tail = P(L * 0.5, 0, 0), P(-L * 0.35, 0, 0)
    top, bot = P(L * 0.05, 0, L * deep), P(L * 0.05, 0, -L * deep * 0.85)
    left, right = P(L * 0.05, L * 0.07, 0), P(L * 0.05, -L * 0.07, 0)
    for a, b in ((top, left), (left, bot), (bot, right), (right, top)):
        m.add_tri(p, nose, a, b); m.add_tri(p, tail, b, a)
    t0, t1, t2 = P(-L * 0.3, 0, 0), P(-L * 0.5, 0, L * 0.12), P(-L * 0.5, 0, -L * 0.12)
    m.add_tri(p, t0, t1, t2)


SCHOOL_SPOTS = [(0, 0, 0), (0.7, 0.4, 0.2), (-0.6, 0.5, -0.1), (0.3, -0.6, 0.3), (-0.8, -0.3, 0.1), (0.9, -0.2, -0.3),
                (-0.2, 0.9, -0.3), (0.4, 0.8, -0.1), (-0.9, 0.1, 0.3), (0.1, -0.9, -0.2)]


def school(name, fish_len, colour, count=7, spread=2.0, deep=0.14, back=None):
    """A loose shoal, `spread` metres across; optional darker back colour."""
    m = Mesh()
    p = m.part("hull", colour)
    for q, (x, y, z) in enumerate(SCHOOL_SPOTS[:count]):
        fish(m, p, x * spread / 2, y * spread / 2, z * spread / 2, fish_len, yaw=0.15 * (q % 3 - 1), deep=deep)
    m.save(name)


def jelly(name, r, h, tent, colour, tent_colour, n_tent=4, seg=6):
    """Bell (radius r, height h) with n_tent trailing tentacles of length `tent`; origin at bell base."""
    m = Mesh()
    bell = m.part("bell", colour)
    prism(m, bell, [(0.0, 1.0), (h * 0.6, 0.8), (h, 0.0)], r, seg=seg)
    t = m.part("tentacles", tent_colour)
    for q in range(n_tent):
        a = 2 * math.pi * q / n_tent + 0.4
        blade(m, t, (r * 0.5 * math.cos(a), r * 0.5 * math.sin(a), 0), tent, r * 0.15, a, -1.35)
    m.save(name)


# Whales: (name, length m, colour, fin colour, options)
whale("whale_humpback", 14, "#4A5568", "#3A4353", flipper=0.32, belly="#B9C2CC")
whale("whale_minke", 8, "#5B6673", "#4A5563", dorsal=0.05, flipper=0.16, belly="#D5DBE1")
whale("whale_fin", 20, "#55606C", "#47515C", dorsal=0.04, flipper=0.14, belly="#C9D0D6")
whale("whale_blue", 25, "#6E8296", "#5C6E80", dorsal=0.025, flipper=0.14)
whale("whale_sperm", 16, "#5A5250", "#4A4340", head="blunt", dorsal=0.0, flipper=0.12)
whale("whale_orca", 7, "#1F2429", "#1F2429", dorsal=0.2, flipper=0.2, belly="#F2F4F6")
whale("whale_pilot", 6, "#2B2F35", "#2B2F35", head="blunt", dorsal=0.09, flipper=0.2)
whale("whale_beluga", 4.5, "#EEF1F3", "#E1E5E9", head="round", dorsal=0.0, flipper=0.15)
whale("whale_narwhal", 4.5, "#B9BDC3", "#A6ABB2", head="round", dorsal=0.0, flipper=0.13, tusk=0.5)
whale("whale_sei", 15, "#4F5A66", "#414A55", head="pointed", dorsal=0.06, flipper=0.14, belly="#C7CED5")
whale("dolphin_common", 2.3, "#4B5A6B", "#3D4A58", head="pointed", dorsal=0.1, flipper=0.16, belly="#E8D9A8")

# Sharks
shark("shark_basking", 8, "#6B6558", dorsal=0.12)
shark("shark_white", 5, "#6C7A88")
shark("shark_blue", 3, "#4A78B5")
shark("shark_greenland", 4, "#4B4F55", belly="#6E737A", dorsal=0.06)
shark("shark_porbeagle", 2.5, "#4E5C6A")
shark("shark_hammerhead", 3.5, "#6F7C87")

# Fish schools
school("fish_herring", 0.3, "#B8C6D4", count=10, spread=2.0)
school("fish_mackerel", 0.35, "#4E8F9C", count=8, spread=2.0)
school("fish_sardine", 0.2, "#C9D3DC", count=10, spread=1.5)
school("fish_tuna", 1.2, "#3F5E80", count=4, spread=3.5, deep=0.18)
school("fish_cod", 0.8, "#8A8556", count=4, spread=2.5, deep=0.18)
school("fish_lanternfish", 0.08, "#8FB4C7", count=10, spread=0.8)
school("fish_capelin", 0.18, "#A9C4B0", count=10, spread=1.5)
school("fish_anchovy", 0.15, "#AFC0CF", count=10, spread=1.2)

# Jellyfish
jelly("jelly_moon", 0.2, 0.1, 0.15, "#E6ECF2", "#D6DEE6")
jelly("jelly_lionsmane", 0.6, 0.25, 1.8, "#C8552E", "#B7452A", n_tent=6)
jelly("jelly_compass", 0.15, 0.1, 0.4, "#D7B98A", "#8C6A3C", seg=8)
jelly("jelly_barrel", 0.45, 0.35, 0.5, "#9FB9D4", "#7A94B0")
jelly("jelly_blue", 0.15, 0.1, 0.3, "#5C7FC4", "#4A6AA8")

# ── Bush: squat 6-sided dome, 1.2 m ──
m = Mesh()
b = m.part("leaves", "#5F8F45")
cone(m, b, 0, 1.2, 0.9, seg=6)
m.save("bush")
print("wrote", len([f for f in os.listdir(OUT) if f.endswith(".json")]), "models")
