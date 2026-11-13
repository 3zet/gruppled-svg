#!/usr/bin/env python3
"""
Gruppled cursors -- SVG rebuild.

Generates, for both colour variants:
  <theme>/cursors_scalable/<name>/{*.svg,metadata.json}   (KDE Plasma >= 6.2, SVG cursors)
  <theme>/cursors/<name>                                  (Xcursor fallback: X11, XWayland, GTK...)
  <theme>/index.theme

Usage:  ./build.py [output-dir]          (default: directory of this script)

Only needs Python 3 and `rsvg-convert` (librsvg) for the Xcursor fallback.
Pass --no-xcursor to skip the raster fallback (then use
`kcursorgen --svg-theme-to-xcursor` from Plasma 6.3+ instead).

Geometry
--------
All numbers below were measured from the original 39/40 px bitmaps (least-squares
fit of the shapes to the sub-pixel outline), so at cursor size 24 the SVGs land on
the same pixels as the originals.  The originals were drawn in Inkscape and the
shapes are rebuilt the same way:

  * every shape is one path, filled with a vertical gradient spanning its bounding
    box and stroked on top with *round* joins;
  * the drop ("teardrop") is a circle whose top node was pulled out into a cusp,
    so its sides are gentle curves, not straight tangents;
  * the "eye" of the double arrows is a circle with two opposite nodes pulled out;
    the four-way star is the union of two eyes.

Original bitmap theme: Gruppled by gruppler (2008), GPL v2.
"""
import json, math, os, shutil, struct, subprocess, sys, zlib

CANVAS = 40          # svg user units = pixels of the original bitmaps
NOMINAL = 24         # cursor size the 40-unit canvas stands for
XCURSOR_SIZES = [24, 32, 36, 48, 64, 72, 96]
FRAME_MS = 50        # original animation: 14 frames, 50 ms each

THEMES = {
    "gruppled_black": dict(
        title="Gruppled Black", outline="#ffffff", body=("#909090", "#050505"),
        glyph=("#909090", "#050505")),
    "gruppled_white": dict(
        title="Gruppled White", outline="#000000", body=("#fcfcfc", "#cecece"),
        glyph=("#7abd43", "#4f7d2a")),
}

# measured stroke widths
SW_DROP = 1.75       # the drop-shaped pointers
SW = 1.67            # everything else
SW_X = 1.46          # the X letter

# ----------------------------------------------------------------------------
# tiny path toolkit
# a subpath is a list of ('M',p) ('L',p) ('C',c1,c2,p) ('A',r,large,sweep,p) ('Z',)

def fmt(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s

def pt(p):
    return f"{fmt(p[0])} {fmt(p[1])}"

def d_of(subpaths):
    out = []
    for sp in subpaths:
        for c in sp:
            if c[0] in "ML":
                out.append(c[0] + pt(c[1]))
            elif c[0] == "C":
                out.append(f"C{pt(c[1])} {pt(c[2])} {pt(c[3])}")
            elif c[0] == "A":
                out.append(f"A{fmt(c[1])} {fmt(c[1])} 0 {c[2]} {c[3]} {pt(c[4])}")
            else:
                out.append("Z")
    return "".join(out)

def xf(sp, rot=0.0, flip=False, tx=0.0, ty=0.0):
    """mirror in x (optional), rotate by `rot` degrees (clockwise on screen), translate"""
    a = math.radians(rot); ca, sa = math.cos(a), math.sin(a)
    def p(q):
        x, y = q
        if flip: x = -x
        return (x * ca - y * sa + tx, x * sa + y * ca + ty)
    out = []
    for c in sp:
        if c[0] in "ML":
            out.append((c[0], p(c[1])))
        elif c[0] == "C":
            out.append(("C", p(c[1]), p(c[2]), p(c[3])))
        elif c[0] == "A":
            out.append(("A", c[1], c[2], c[3] ^ (1 if flip else 0), p(c[4])))
        else:
            out.append(c)
    return out

def place(subpaths, rot=0.0, tx=0.0, ty=0.0, flip=False):
    return [xf(sp, rot, flip, tx, ty) for sp in subpaths]

def circle(cx, cy, r):
    return [("M", (cx - r, cy)), ("A", r, 1, 1, (cx + r, cy)), ("A", r, 1, 1, (cx - r, cy)), ("Z",)]

def poly(pts):
    return [("M", pts[0])] + [("L", p) for p in pts[1:]] + [("Z",)]

def rect(x0, y0, x1, y1):
    return poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])

def bar(hw, hl):
    """vertical bar centred on the origin"""
    return rect(-hw, -hl, hw, hl)

# cubic Bezier helpers (a segment is its four control points)
def bez(seg, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg; u = 1 - t
    return (u*u*u*x0 + 3*u*u*t*x1 + 3*u*t*t*x2 + t*t*t*x3,
            u*u*u*y0 + 3*u*u*t*y1 + 3*u*t*t*y2 + t*t*t*y3)

def split(seg, t):
    l = lambda a, b: (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    p0, p1, p2, p3 = seg
    a, b, c = l(p0, p1), l(p1, p2), l(p2, p3); d, e = l(a, b), l(b, c); f = l(d, e)
    return (p0, a, d, f), (f, e, c, p3)

def solve(seg, fn):
    """parameter where fn(point) crosses zero (fn < 0 at t=0, > 0 at t=1)"""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if fn(bez(seg, mid)) < 0: lo = mid
        else: hi = mid
    return (lo + hi) / 2

def rev(seg): return seg[::-1]
def mapseg(seg, f): return tuple(f(p) for p in seg)
def C(seg): return ("C", seg[1], seg[2], seg[3])

# ----------------------------------------------------------------------------
# the shapes of the original theme (centred on the origin)

DROP_R, DROP_L, DROP_K, DROP_RING = 8.0, 18.0, 0.81, 4.0
def drop(ring=True):
    """circle of radius R; top node pulled up to a cusp at distance L; the side nodes keep
    their handles (K*R), which is what makes the flanks bulge slightly"""
    R, L, k = DROP_R, DROP_L, DROP_K * DROP_R
    tip = (0.0, -L)
    sp = [("M", tip), ("C", tip, (R, -k), (R, 0)), ("A", R, 0, 1, (0, R)), ("A", R, 0, 1, (-R, 0)),
          ("C", (-R, -k), tip, tip), ("Z",)]
    return [sp, circle(0, 0, DROP_RING)] if ring else [sp]

EYE_A, EYE_B, EYE_H, EYE_RING = 13.5, 5.95, 4.85, 3.0
EYE_Q = ((0.0, -EYE_B), (EYE_H, -EYE_B), (EYE_A, 0.0), (EYE_A, 0.0))     # top node -> right tip
def eye():
    a, b, h = EYE_A, EYE_B, EYE_H
    sp = [("M", (-a, 0)), ("C", (-a, 0), (-h, -b), (0, -b)), ("C", (h, -b), (a, 0), (a, 0)),
          ("C", (a, 0), (h, b), (0, b)), ("C", (-h, b), (-a, 0), (-a, 0)), ("Z",)]
    return [sp, circle(0, 0, EYE_RING)]

def star():
    """union of a horizontal and a vertical eye"""
    t = solve(EYE_Q, lambda p: p[0] + p[1])            # where the flank meets the diagonal
    s1 = split(EYE_Q, t)[1]                              # diagonal -> right tip
    s0 = rev(mapseg(s1, lambda p: (-p[1], -p[0])))       # top tip -> diagonal (mirror image)
    sp = [("M", s0[0])]
    for q in range(4):
        r = lambda p, q=q: xf([("M", p)], 90 * q)[0][1]
        sp += [C(mapseg(s0, r)), C(mapseg(s1, r))]
    return [sp + [("Z",)], circle(0, 0, 3.1)]

SPLIT_HW, SPLIT_HL = 2.1, 12.65
def split_eye():
    """union of the eye and a bar across it; the ring is cut into two slivers by the bar"""
    hw, hl, r = SPLIT_HW, SPLIT_HL, EYE_RING
    t = solve(EYE_Q, lambda p: p[0] - hw)
    tr = split(EYE_Q, t)[1]                              # (hw, y*) -> right tip, upper side
    mx = lambda p: (-p[0], p[1]); my = lambda p: (p[0], -p[1]); mxy = lambda p: (-p[0], -p[1])
    tl, br, bl = rev(mapseg(tr, mx)), rev(mapseg(tr, my)), mapseg(tr, mxy)
    ys = tr[0][1]
    sp = [("M", tl[0]), C(tl), ("L", (-hw, -hl)), ("L", (hw, -hl)), ("L", (hw, ys)), C(tr),
          C(br), ("L", (hw, hl)), ("L", (-hw, hl)), ("L", (-hw, -ys)), C(bl), ("Z",)]
    c = math.sqrt(r * r - hw * hw)
    return [sp, [("M", (hw, -c)), ("A", r, 0, 1, (hw, c)), ("Z",)],
                [("M", (-hw, c)), ("A", r, 0, 1, (-hw, -c)), ("Z",)]]

BAR_HW, BAR_HL = 2.165, 9.95
def plus():
    h, l = BAR_HW, BAR_HL
    return [poly([(-h, -l), (h, -l), (h, -h), (l, -h), (l, h), (h, h), (h, l), (-h, l),
                  (-h, h), (-l, h), (-l, -h), (-h, -h)])]

RING_R1, RING_R2 = 11.64, 7.28
def no_entry():
    """ring with a diagonal bar: the inside of the ring is cut into two D-shaped holes"""
    hw, r = 2.24, RING_R2
    c = math.sqrt(r * r - hw * hw)
    holes = [[("M", (hw, -c)), ("A", r, 0, 1, (hw, c)), ("Z",)],
             [("M", (-hw, c)), ("A", r, 0, 1, (-hw, -c)), ("Z",)]]
    return [circle(0, 0, RING_R1)] + place(holes, 45)

def target():
    return [circle(0, 0, RING_R1), circle(0, 0, RING_R2), circle(0, 0, 2.21)]

PENCIL = [poly([(10.09, 28.91), (12.43, 23.50), (26.41, 9.56), (29.54, 12.49), (15.43, 26.64)])]

# the X is a double-struck serif letter; outline traced from the original
X_LETTER = [poly([(10.67, 9.9), (20.93, 9.9), (22.56, 14.01), (25.81, 9.9), (28.38, 9.9), (23.42, 16.17),
                  (28.69, 29.43), (21.59, 29.43), (18.7, 22.15), (18.1, 22.9), (20.69, 29.43), (18.42, 29.43),
                  (16.59, 24.81), (12.93, 29.43), (10.36, 29.43), (15.73, 22.64)]),
            poly([(17.84, 19.98), (14.24, 10.92), (13.34, 10.92), (17.24, 20.74)])]

def magnifier():
    """not in the original: ring with a handle, same construction as the rest"""
    r1, r2, hw, far = 8.6, 4.9, 2.1, 16.5
    d = math.asin(hw / r1); n = r1 * math.cos(d)
    # handle along +x, then rotated to point down-right
    sp = [("M", (n, -hw)), ("L", (far, -hw)), ("L", (far, hw)), ("L", (n, hw)),
          ("A", r1, 1, 1, (n, -hw)), ("Z",)]
    return place([sp], 45) + [circle(0, 0, r2)]

# ----------------------------------------------------------------------------
# cursors

class Cur:
    def __init__(self, body, sw, hot):
        self.body, self.sw, self.hot = body, sw, hot
        self.extra = []      # callables(theme) -> svg snippet drawn on top
    def add(self, fn):
        self.extra.append(fn); return self

def drop_cursor(cx, cy, angle, ring=True):
    """drop centred on (cx,cy); `angle` = how far the tip leans to the left of straight up"""
    c = Cur(place(drop(ring), -angle, cx, cy), SW_DROP, None)
    a = math.radians(angle); reach = DROP_L + SW_DROP / 2           # the visible tip
    c.hot = (cx - reach * math.sin(a), cy - reach * math.cos(a))
    c.centre = (cx, cy)
    return c

def pointer(ring=True):
    return drop_cursor(22.03, 21.88, 30, ring)

def centred(body, cx, cy, rot=0, sw=SW):
    return Cur(place(body, rot, cx, cy), sw, (cx, cy))

def lerp(a, b, t):
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]; pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))

def ball(cx, cy, base, hi, hi_edge, r=5.0):
    """glossy ball: flat base colour with a lighter cap"""
    s = r / 5.0
    def fn(th):
        return (f'<radialGradient id="hi" gradientUnits="userSpaceOnUse" cx="{fmt(cx + 0.2 * s)}" cy="{fmt(cy - 3.2 * s)}" r="{fmt(4.6 * s)}">'
                f'<stop offset="0" stop-color="{hi}"/><stop offset="1" stop-color="{hi_edge}"/></radialGradient>'
                f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{fmt(r)}" fill="{base}"/>'
                f'<ellipse cx="{fmt(cx)}" cy="{fmt(cy - 1.75 * s)}" rx="{fmt(4.35 * s)}" ry="{fmt(3.1 * s)}" fill="url(#hi)"/>')
    return fn

#         base       cap centre  cap edge
GREEN  = ("#7eb331", "#d9f2b3", "#b4de79")
GREEN_PRESSED = ("#5d8a22", "#bfdc98", "#93bd5c")
YELLOW = ("#e4a100", "#ffe5a7", "#ffce58"); YELLOW_OFF = ("#706957", "#dbd9d1", "#b7b2a2")
RED    = ("#e40000", "#ffa7a7", "#ff5858"); RED_OFF    = ("#705757", "#dbd1d1", "#b7a2a2")

def glyph(kind, cx, cy, s=1.0):
    """small symbol inside the ring; filled with its own top-to-bottom gradient"""
    R = lambda x0, y0, x1, y1: rect(cx + x0 * s, cy + y0 * s, cx + x1 * s, cy + y1 * s)
    P = lambda pts: poly([(cx + x * s, cy + y * s) for x, y in pts])
    if kind == "plus":
        sps = [R(-3, -0.9, 3, 0.9), R(-0.9, -3, 0.9, 3)]
    elif kind == "minus":
        sps = [R(-3, -0.9, 3, 0.9)]
    elif kind == "menu":
        sps = [R(-2.7, -2.4, 2.7, -1.2), R(-2.7, -0.6, 2.7, 0.6), R(-2.7, 1.2, 2.7, 2.4)]
    elif kind == "link":       # a bold diagonal cross
        sps = [xf(rect(-3.3 * s, -0.95 * s, 3.3 * s, 0.95 * s), r, False, cx, cy) for r in (45, -45)]
    elif kind == "help":       # bold "?" that overlaps the ring, as in the original
        arch = [("M", (cx - 3.75 * s, cy - 1.85 * s)), ("L", (cx - 3.75 * s, cy - 2.4 * s)),
                ("C", (cx - 3.0 * s, cy - 3.3 * s), (cx - 1.6 * s, cy - 3.7 * s), (cx - 0.2 * s, cy - 3.7 * s)),
                ("C", (cx + 1.2 * s, cy - 3.7 * s), (cx + 2.7 * s, cy - 3.3 * s), (cx + 3.45 * s, cy - 2.4 * s)),
                ("L", (cx + 3.45 * s, cy - 1.85 * s)), ("Z",)]
        sps = [arch, R(-3.75, -2.4, -1.95, -1.0), R(0.85, -2.4, 3.45, -0.9),
               P([(0.85, -1.7), (3.45, -1.7), (3.45, -0.9), (0.75, 1.1), (-1.4, 1.1), (-1.4, 0.5)]),
               R(-1.4, 0.5, 0.75, 1.75), R(-1.65, 2.25, 1.05, 4.1)]
    return lambda th: f'<path d="{d_of(sps)}" fill="url(#glyph)"/>'

def build_cursors():
    cur = {}
    # --- the pointer family ---------------------------------------------------
    cur["default"]   = pointer()
    cur["right_ptr"] = drop_cursor(CANVAS - 22.03, 21.88, -30)
    for name, kind in (("help", "help"), ("copy", "plus"), ("alias", "link"),
                       ("dnd-move", "minus"), ("context-menu", "menu")):
        p = pointer(); cur[name] = p.add(glyph(kind, *p.centre))
    p = pointer(ring=False); cur["pointer"]  = p.add(ball(*p.centre, *GREEN))
    p = pointer(ring=False); cur["grab"]     = p.add(ball(*p.centre, *GREEN))
    p = pointer(ring=False); cur["grabbing"] = p.add(ball(*p.centre, *GREEN_PRESSED, r=4.3))
    for name, on, off in (("progress", YELLOW, YELLOW_OFF), ("wait", RED, RED_OFF)):
        frames = []
        for i in list(range(8)) + list(range(6, 0, -1)):       # fade out and back, as the original
            p = pointer(ring=False)
            frames.append(p.add(ball(*p.centre, *(lerp(a, b, i / 7) for a, b in zip(on, off)))))
        cur[name] = frames
    # --- edges and corners: the drop points at the edge -----------------------
    cur["n-resize"]  = drop_cursor(19.50, 22.58, 0)
    cur["s-resize"]  = drop_cursor(19.50, 12.42, 180)
    cur["e-resize"]  = drop_cursor(14.42, 17.50, -90)
    cur["w-resize"]  = drop_cursor(24.58, 17.50, 90)
    cur["nw-resize"] = drop_cursor(20.68, 21.09, 45)
    cur["ne-resize"] = drop_cursor(15.91, 21.09, -45)
    cur["sw-resize"] = drop_cursor(20.70, 16.28, 135)
    cur["se-resize"] = drop_cursor(15.92, 16.30, -135)
    # --- double arrows ("eyes") ------------------------------------------------
    cur["ew-resize"]   = centred(eye(), 19.5, 19.5, 0)
    cur["ns-resize"]   = centred(eye(), 19.5, 17.5, 90)
    cur["nwse-resize"] = centred(eye(), 19.5, 19.5, 45)
    cur["nesw-resize"] = centred(eye(), 19.5, 19.5, -45)
    cur["col-resize"]  = centred(split_eye(), 19.5, 17.5, 0)
    cur["row-resize"]  = centred(split_eye(), 19.5, 17.5, 90)
    cur["all-scroll"]  = centred(star(), 19.5, 17.5)
    # --- precision / text ------------------------------------------------------
    cur["crosshair"]     = centred(plus(), 19.5, 19.5)
    cur["text"]          = centred([bar(BAR_HW, BAR_HL)], 19.5, 19.5)
    cur["vertical-text"] = centred([bar(BAR_HW, BAR_HL)], 19.5, 19.5, 90)
    # --- circles ----------------------------------------------------------------
    cur["not-allowed"] = centred(no_entry(), 19.5, 19.5)
    cur["dotbox"]      = centred(target(), 19.5, 19.5)
    cur["X_cursor"]    = Cur(X_LETTER, SW_X, (19.5, 19.5))
    for name, kind in (("zoom-in", "plus"), ("zoom-out", "minus")):
        z = centred(magnifier(), 16.5, 16.5)
        cur[name] = z.add(glyph(kind, 16.5, 16.5))
    # --- pencil -----------------------------------------------------------------
    cur["pencil"] = Cur(PENCIL, SW, (9.5, 29.5))
    return cur

ALIASES = {
    "default": "left_ptr arrow top_left_arrow",
    "right_ptr": "draft_large draft_small right-arrow-ptr",
    "pointer": "hand hand1 hand2 pointing_hand e29285e634086352946a0e7090d73106 9d800788f1b08800ae810202380a0822",
    "help": "question_arrow whats_this left_ptr_help dnd-ask d9ce0ab605698f320427677b458ad60b 5c6cd98b3f3ebcb1f9c7f1c204630408",
    "progress": "left_ptr_watch half-busy 3ecb610c1bf2410f44200f48c40d3599 08e8e1c95fe2fc01f976f1e063a24ccd 00000000000000020006000e7e9ffc3f",
    "wait": "watch",
    "copy": "dnd-copy 1081e37283d90000800003c07f3ef6bf 6407b0e94181790501fd1e167b474872 b66166c04f8c3109214a4fbd64a50fc8",
    "alias": "link dnd-link 3085a0e285430894940527032f8b26df 640fb0e74195791501fd1ed57b41487f a2a266d0498c3104214a47bd64ab0fc8",
    "dnd-move": "move 4498f0e0c1937ffe01fd06f973665830 9081237383d90e509aa00f00170e968f",
    "grab": "openhand 9141b49c8149039304290b508d208c40 5aca4d189052212118709018842178c0",
    "grabbing": "closedhand 208530c400c041818281048008011002 fcf21c00b30f7e3f83fe0dfd12e71cff",
    "not-allowed": "crossed_circle forbidden no-drop dnd-no-drop dnd-none 03b6e0fcb3499374a867c041f52298f0",
    "crosshair": "cross tcross cross_reverse plus cell color-picker",
    "text": "xterm ibeam",
    "all-scroll": "fleur size_all all-resize",
    "n-resize": "top_side up-arrow up_arrow center_ptr sb_up_arrow based_arrow_up",
    "s-resize": "bottom_side down-arrow down_arrow sb_down_arrow based_arrow_down",
    "e-resize": "right_side right-arrow right_arrow sb_right_arrow",
    "w-resize": "left_side left-arrow left_arrow sb_left_arrow",
    "nw-resize": "top_left_corner ul_angle",
    "ne-resize": "top_right_corner ur_angle",
    "sw-resize": "bottom_left_corner ll_angle",
    "se-resize": "bottom_right_corner lr_angle",
    "ew-resize": "h_double_arrow size_hor 028006030e0e7ebffc7f7070c0600140",
    "ns-resize": "v_double_arrow double_arrow size_ver 00008160000006810000408080010102",
    "nwse-resize": "bd_double_arrow size_fdiag c7088f0f3e6c8088236ef8e1e3e70000",
    "nesw-resize": "fd_double_arrow size_bdiag fcf1c3c7cd4491d801f1e1c78f100000",
    "col-resize": "sb_h_double_arrow split_h 14fef782d02440884392942c11205230",
    "row-resize": "sb_v_double_arrow split_v 2870a09082c103050810ffdffffe0204",
    "dotbox": "circle dot drapedbox pirate target icon",
    "pencil": "draft",
    "X_cursor": "x-cursor",
}

# ----------------------------------------------------------------------------
# svg output

# Soft drop shadow without filters: the silhouette is stacked several times, each copy a
# little wider and fainter.  Offsets and opacities were fitted to the original shadow
# (4 px down, about 1.5 px blur, 30 % black).
SHADOW_DY = 4.0
_ALPHAS = [0.067, 0.044, 0.025, 0.019, 0.022, 0.023, 0.019, 0.015, 0.013, 0.011, 0.008, 0.004, 0.003, 0.003, 0.004]
SHADOW_LAYERS = [(-0.8 + 0.3 * i, a) for i, a in enumerate(_ALPHAS)]     # (growth of the silhouette, opacity)

def svg_of(c, th):
    d = d_of(c.body)
    grad = lambda gid, stops: (f'<linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
                               f'<stop offset="0" stop-color="{stops[0]}"/><stop offset="1" stop-color="{stops[1]}"/></linearGradient>')
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS}" height="{CANVAS}" viewBox="0 0 {CANVAS} {CANVAS}">',
         grad("body", th["body"]) + grad("glyph", th["glyph"])]
    s.append(f'<g transform="translate(0 {SHADOW_DY})" fill="#000" stroke="#000" stroke-linejoin="round" fill-rule="evenodd">')
    for grow, a in SHADOW_LAYERS:
        w = c.sw + 2 * grow
        stroke = f' stroke-width="{fmt(w)}"' if w > 0.01 else ' stroke="none"'
        s.append(f'<g opacity="{a:.4f}"{stroke}><path d="{d}"/></g>')
    s.append('</g>')
    s.append(f'<path d="{d}" fill="url(#body)" fill-rule="evenodd" stroke="{th["outline"]}" '
             f'stroke-width="{fmt(c.sw)}" stroke-linejoin="round"/>')
    s += [fn(th) for fn in c.extra]
    s.append('</svg>')
    return "\n".join(s) + "\n"

# ----------------------------------------------------------------------------
# Xcursor output (stdlib PNG reader + writer for the Xcursor container)

def read_png_rgba(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    while pos < len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8]); body = data[pos + 8:pos + 8 + ln]; pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and ctype == 6 and interlace == 0, "expected 8-bit RGBA png"
        elif typ == b"IDAT":
            idat += body
    raw = zlib.decompress(idat); stride = w * 4
    rows, prev, p = [], bytearray(stride), 0
    for _ in range(h):
        ft = raw[p]; line = bytearray(raw[p + 1:p + 1 + stride]); p += 1 + stride
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0; b = prev[i]; cc = prev[i - 4] if i >= 4 else 0
            if ft == 1: line[i] = (line[i] + a) & 255
            elif ft == 2: line[i] = (line[i] + b) & 255
            elif ft == 3: line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ft == 4:
                pa, pb, pc = abs(b - cc), abs(a - cc), abs(a + b - 2 * cc)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else cc)) & 255
        rows.append(line); prev = line
    return w, h, b"".join(rows)

def argb_premul(rgba):
    out = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i:i + 4]
        out[i:i + 4] = bytes(((b * a + 127) // 255, (g * a + 127) // 255, (r * a + 127) // 255, a))
    return bytes(out)

def write_xcursor(path, images):
    """images: list of (nominal, w, h, xhot, yhot, delay_ms, argb_bytes)"""
    hdr = 16 + 12 * len(images)
    toc, chunks, pos = b"", b"", hdr
    for nominal, w, h, xh, yh, delay, px in images:
        toc += struct.pack("<III", 0xFFFD0002, nominal, pos)
        ch = struct.pack("<IIIIIIIII", 36, 0xFFFD0002, nominal, 1, w, h, xh, yh, delay) + px
        chunks += ch; pos += len(ch)
    with open(path, "wb") as f:
        f.write(b"Xcur" + struct.pack("<III", 16, 0x10000, len(images)) + toc + chunks)

def rasterize(svg_path, px):
    data = subprocess.run(["rsvg-convert", "-w", str(px), "-h", str(px), svg_path],
                          check=True, capture_output=True).stdout
    w, h, rgba = read_png_rgba(data)
    return w, h, argb_premul(rgba)

# ----------------------------------------------------------------------------

def link(target, path):
    if os.path.lexists(path): os.remove(path)
    os.symlink(target, path)

def build(outdir, xcursor=True):
    cursors = build_cursors()
    for tname, th in THEMES.items():
        root = os.path.join(outdir, tname)
        for sub in ("cursors", "cursors_scalable"):
            shutil.rmtree(os.path.join(root, sub), ignore_errors=True)
            os.makedirs(os.path.join(root, sub))
        with open(os.path.join(root, "index.theme"), "w") as f:
            f.write(f"[Icon Theme]\nName={th['title']}\nComment=Gruppled cursors, redrawn as SVG\n"
                    f"Example=default\nInherits=breeze_cursors\n")
        for name, c in cursors.items():
            frames = c if isinstance(c, list) else [c]
            sdir = os.path.join(root, "cursors_scalable", name); os.makedirs(sdir)
            meta, files, cache = [], [], {}
            for i, fr in enumerate(frames):
                fn = f"{name}.svg" if len(frames) == 1 else f"{name}-{i + 1:02d}.svg"
                with open(os.path.join(sdir, fn), "w") as f: f.write(svg_of(fr, th))
                e = {"filename": fn, "hotspot_x": round(fr.hot[0], 2), "hotspot_y": round(fr.hot[1], 2),
                     "nominal_size": NOMINAL}
                if len(frames) > 1: e["delay"] = FRAME_MS
                meta.append(e); files.append(os.path.join(sdir, fn))
            with open(os.path.join(sdir, "metadata.json"), "w") as f:
                json.dump(meta, f, indent=4); f.write("\n")
            if xcursor:
                images = []
                for nominal in XCURSOR_SIZES:
                    px = round(CANVAS * nominal / NOMINAL)
                    for fr, svg in zip(frames, files):
                        w, h, argb = rasterize(svg, px)
                        images.append((nominal, w, h, int(fr.hot[0] * px / CANVAS), int(fr.hot[1] * px / CANVAS),
                                       FRAME_MS if len(frames) > 1 else 0, argb))
                write_xcursor(os.path.join(root, "cursors", name), images)
            for al in ALIASES.get(name, "").split():
                link(name, os.path.join(root, "cursors_scalable", al))
                if xcursor: link(name, os.path.join(root, "cursors", al))
        print(f"{tname}: {len(cursors)} cursors, {sum(len(v.split()) for v in ALIASES.values())} aliases")

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    build(args[0] if args else os.path.dirname(os.path.abspath(__file__)), "--no-xcursor" not in sys.argv)
