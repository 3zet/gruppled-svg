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

Original bitmap theme: Gruppled by gruppler (2008), GPL v2.
"""
import json, math, os, shutil, struct, subprocess, sys, tempfile, zlib

CANVAS = 40          # svg user units; original bitmaps were 39/40 px
NOMINAL = 24         # cursor size the 40-unit canvas stands for
XCURSOR_SIZES = [24, 32, 36, 48, 64, 72, 96]
OUTLINE = 2.0        # outline thickness, user units
FRAMES, FRAME_MS = 16, 50

THEMES = {
    "gruppled_black": dict(
        title="Gruppled Black", outline="#ffffff", body=("#8a8a8a", "#0c0c0c"),
        glyph="#8fd03c"),
    "gruppled_white": dict(
        title="Gruppled White", outline="#000000", body=("#fafafa", "#cecece"),
        glyph="#6db035"),
}

# ----------------------------------------------------------------------------
# tiny path toolkit: a subpath is a list of ('M',x,y) ('L',x,y) ('A',r,large,sweep,x,y) ('Z',)

def fmt(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s

def d_of(subpaths):
    out = []
    for sp in subpaths:
        for c in sp:
            if c[0] in "ML":
                out.append(f"{c[0]}{fmt(c[1])} {fmt(c[2])}")
            elif c[0] == "A":
                out.append(f"A{fmt(c[1])} {fmt(c[1])} 0 {c[2]} {c[3]} {fmt(c[4])} {fmt(c[5])}")
            else:
                out.append("Z")
    return "".join(out)

def xf(sp, rot=0.0, flip=False, tx=0.0, ty=0.0):
    """mirror in x (optional), rotate by `rot` degrees (clockwise on screen), translate."""
    a = math.radians(rot); ca, sa = math.cos(a), math.sin(a)
    def p(x, y):
        if flip: x = -x
        return (x * ca - y * sa + tx, x * sa + y * ca + ty)
    out = []
    for c in sp:
        if c[0] in "ML":
            out.append((c[0],) + p(c[1], c[2]))
        elif c[0] == "A":
            out.append(("A", c[1], c[2], c[3] ^ (1 if flip else 0)) + p(c[4], c[5]))
        else:
            out.append(c)
    return out

def circle(cx, cy, r):
    return [("M", cx - r, cy), ("A", r, 1, 1, cx + r, cy), ("A", r, 1, 1, cx - r, cy), ("Z",)]

def poly(pts):
    return [("M",) + pts[0]] + [("L",) + p for p in pts[1:]] + [("Z",)]

def bar(hw, hl):
    """axis-aligned vertical bar centred on the origin"""
    return poly([(-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl)])

def teardrop(cx, cy, px, py, r):
    """circle (cx,cy,r) joined by its two tangents to the tip (px,py)"""
    vx, vy = px - cx, py - cy
    L = math.hypot(vx, vy); phi = math.atan2(vy, vx); g = math.acos(r / L)
    t1 = (cx + r * math.cos(phi + g), cy + r * math.sin(phi + g))
    t2 = (cx + r * math.cos(phi - g), cy + r * math.sin(phi - g))
    return [("M", px, py), ("L",) + t1, ("A", r, 1, 1) + t2, ("Z",)]

def lens(a, b):
    rho = (a * a + b * b) / (2 * b)
    return [("M", -a, 0), ("A", rho, 0, 1, a, 0), ("A", rho, 0, 1, -a, 0), ("Z",)]

def star(a, d):
    """four-pointed star, tips at distance a, concave sides passing (d,d)"""
    c = (a * a - 2 * d * d) / (2 * a - 4 * d)
    r = math.sqrt(2) * (c - d)
    pts = [(a, 0), (0, a), (-a, 0), (0, -a)]
    sp = [("M",) + pts[0]]
    for q in pts[1:] + pts[:1]:
        sp.append(("A", r, 0, 0) + q)
    return sp + [("Z",)]

# ----------------------------------------------------------------------------
# cursor building blocks

TD_R, TD_L = 7.5, 15.1                      # teardrop body radius / centre-to-tip (fill shape)
TD_OUT = TD_L * (1 + OUTLINE / TD_R)        # centre to outer (outlined) tip
RING_HOLE, RING_OUT = 3.0, 4.7              # see-through hole and ring radius of the big ring
C = CANVAS / 2

class Cur:
    def __init__(self, hot, grad=(8, 32)):
        self.hot, self.grad = hot, grad
        self.parts = []      # (outline subpaths, fill subpaths)
        self.extra = []      # callables(theme) -> svg snippet drawn on top
    def part(self, out, fill=None):
        self.parts.append((out, fill if fill is not None else out)); return self
    def add(self, fn):
        self.extra.append(fn); return self

def ringed(shape, hx, hy, hole=RING_HOLE, ring=RING_OUT):
    """shape with a see-through hole surrounded by an outline-coloured ring"""
    return ([shape, circle(hx, hy, hole + OUTLINE)], [shape, circle(hx, hy, ring)])

def td_cursor(apex, direction, ring=True, grad=None):
    """teardrop whose outlined tip sits on `apex`; `direction` = unit vector centre->tip"""
    dx, dy = direction
    cx, cy = apex[0] - dx * TD_OUT, apex[1] - dy * TD_OUT
    shape = teardrop(cx, cy, cx + dx * TD_L, cy + dy * TD_L, TD_R)
    top = min(cy - TD_R, cy + dy * TD_L); bot = max(cy + TD_R, cy + dy * TD_L)
    c = Cur((round(apex[0]), round(apex[1])), grad or (top, bot))
    c.centre = (cx, cy)
    if ring: c.part(*ringed(shape, cx, cy))
    else:    c.part([shape])
    return c

_n = math.hypot(7.5, 13.1)
PTR_DIR = (-7.5 / _n, -13.1 / _n)            # the main pointer leans like the original
def pointer(ring=True, flip=False):
    if flip:
        return td_cursor((CANVAS - 12, 4.9), (-PTR_DIR[0], PTR_DIR[1]), ring)
    return td_cursor((12, 4.9), PTR_DIR, ring)

def lerp(a, b, t):
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]; pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))

def ball(cx, cy, base, hi_top, hi_bot, r=5.0):
    rh = r - 0.6; hy = cy - 0.3; chord = hy + 1.2
    hw = math.sqrt(rh * rh - 1.2 * 1.2)
    d = (f"M{fmt(cx - hw)} {fmt(chord)}A{fmt(rh)} {fmt(rh)} 0 1 1 {fmt(cx + hw)} {fmt(chord)}"
         f"A9 9 0 0 1 {fmt(cx - hw)} {fmt(chord)}Z")
    def fn(th):
        return (f'<linearGradient id="hi" gradientUnits="userSpaceOnUse" x1="0" y1="{fmt(hy - rh)}" x2="0" y2="{fmt(chord + 0.6)}">'
                f'<stop offset="0" stop-color="{hi_top}"/><stop offset="1" stop-color="{hi_bot}"/></linearGradient>'
                f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{fmt(r)}" fill="{base}"/>'
                f'<path d="{d}" fill="url(#hi)"/>')
    return fn

GREEN  = ("#7eb331", "#cdeb9f", "#b9e080")
GREEN_PRESSED = ("#5d8a22", "#a8cf78", "#8fbd58")
YELLOW = ("#e4a100", "#ffdd8c", "#ffd162")
RED    = ("#e40000", "#ff8c8c", "#ff6262")
GREY   = ("#706957", "#cdc9bf", "#bcb7a8")

def glyph(kind, cx, cy, s=1.0):
    def line(x1, y1, x2, y2, w):
        return (f'<path d="M{fmt(cx + x1 * s)} {fmt(cy + y1 * s)}L{fmt(cx + x2 * s)} {fmt(cy + y2 * s)}" '
                f'fill="none" stroke="{{g}}" stroke-width="{fmt(w * s)}" stroke-linecap="butt"/>')
    if kind == "plus":
        body = line(-2.4, 0, 2.4, 0, 1.5) + line(0, -2.4, 0, 2.4, 1.5)
    elif kind == "minus":
        body = line(-2.4, 0, 2.4, 0, 1.5)
    elif kind == "menu":
        body = line(-2.1, -1.3, 2.1, -1.3, 1.1) + line(-2.1, 1.3, 2.1, 1.3, 1.1)
    elif kind == "dot":
        body = f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{fmt(1.9 * s)}" fill="{{g}}"/>'
    elif kind == "help":
        body = (f'<path d="M{fmt(cx - 1.35)} {fmt(cy - 1.0)}A1.4 1.4 0 1 1 {fmt(cx + 0.05)} {fmt(cy + 0.25)}L{fmt(cx + 0.05)} {fmt(cy + 0.95)}" '
                f'fill="none" stroke="{{g}}" stroke-width="1.15"/>'
                f'<circle cx="{fmt(cx + 0.05)}" cy="{fmt(cy + 2.15)}" r="0.65" fill="{{g}}"/>')
    return lambda th: body.format(g=th["glyph"])

def rot_parts(parts, rot, flip=False):
    return [([xf(s, rot, flip, C, C) for s in o], [xf(s, rot, flip, C, C) for s in f]) for o, f in parts]

def centred(parts_at_origin, rot=0, grad=(8, 32)):
    c = Cur((20, 20), grad)
    c.parts = rot_parts(parts_at_origin, rot)
    return c

LENS = lambda: ringed(lens(11.5, 5.2), 0, 0, hole=2.0, ring=3.8)
ANNULUS = [circle(0, 0, 10.5), circle(0, 0, 8)]

def build_cursors():
    cur = {}
    # --- the pointer family ---------------------------------------------------
    cur["default"]   = pointer()
    cur["right_ptr"] = pointer(flip=True)
    for name, kind in (("help", "help"), ("copy", "plus"), ("alias", "dot"),
                       ("dnd-move", "minus"), ("context-menu", "menu")):
        p = pointer(); cur[name] = p.add(glyph(kind, *p.centre))
    p = pointer(ring=False); cur["pointer"]  = p.add(ball(*p.centre, *GREEN))
    p = pointer(ring=False); cur["grab"]     = p.add(ball(*p.centre, *GREEN))
    p = pointer(ring=False); cur["grabbing"] = p.add(ball(*p.centre, *GREEN_PRESSED, r=4.1))
    for name, col in (("progress", YELLOW), ("wait", RED)):
        frames = []
        for i in range(FRAMES):
            t = 0.9 * (0.5 - 0.5 * math.cos(2 * math.pi * i / FRAMES))
            p = pointer(ring=False)
            frames.append(p.add(ball(*p.centre, *(lerp(a, b, t) for a, b in zip(col, GREY)))))
        cur[name] = frames
    # --- edges and corners: the drop points at the edge -----------------------
    k = math.sqrt(0.5)
    cur["n-resize"]  = td_cursor((20, 5),  (0, -1))
    cur["s-resize"]  = td_cursor((20, 34), (0, 1))
    cur["w-resize"]  = td_cursor((6, 20),  (-1, 0))
    cur["e-resize"]  = td_cursor((34, 20), (1, 0))
    cur["nw-resize"] = td_cursor((8, 8),   (-k, -k))
    cur["ne-resize"] = td_cursor((32, 8),  (k, -k))
    cur["sw-resize"] = td_cursor((8, 32),  (-k, k))
    cur["se-resize"] = td_cursor((32, 32), (k, k))
    # --- double arrows ("eyes") ------------------------------------------------
    cur["ew-resize"]   = centred([LENS()], 0,   (15, 25))
    cur["ns-resize"]   = centred([LENS()], 90,  (8, 32))
    cur["nwse-resize"] = centred([LENS()], 45,  (11, 29))
    cur["nesw-resize"] = centred([LENS()], -45, (11, 29))
    cur["col-resize"] = centred([LENS(), ([bar(1.4, 11.5)],) * 2], 0, (8, 32))
    cur["row-resize"] = centred([LENS(), ([bar(1.4, 11.5)],) * 2], 90, (8, 32))
    cur["all-scroll"] = centred([ringed(star(10.6, 4.5), 0, 0, hole=2.2, ring=4.0)], 0, (9, 31))
    # --- precision / text ------------------------------------------------------
    h = 1.4
    plus = poly([(-h, -9), (h, -9), (h, -h), (9, -h), (9, h), (h, h), (h, 9), (-h, 9),
                 (-h, h), (-9, h), (-9, -h), (-h, -h)])
    cur["crosshair"]     = centred([([plus],) * 2], 0, (10, 30))
    cur["text"]          = centred([([bar(1.4, 9)],) * 2], 0, (10, 30))
    cur["vertical-text"] = centred([([bar(1.4, 9)],) * 2], 90, (17, 23))
    # --- circles ----------------------------------------------------------------
    cur["not-allowed"] = centred([(ANNULUS,) * 2, ([xf(bar(1.3, 9), 45)],) * 2], 0, (9, 31))
    cur["dotbox"]      = centred([(ANNULUS,) * 2, ([circle(0, 0, 1.6)],) * 2], 0, (9, 31))
    cur["X_cursor"]    = centred([([xf(bar(1.6, 10), 45)],) * 2, ([xf(bar(1.6, 10), -45)],) * 2], 0, (12, 28))
    for name, kind in (("zoom-in", "plus"), ("zoom-out", "minus")):
        z = Cur((17, 17), (8, 34))
        ann = [circle(17, 17, 8.2), circle(17, 17, 5.7)]
        handle = xf(bar(1.6, 4.6), -45, False, 17 + k * 12.6, 17 + k * 12.6)
        z.part([handle]).part(ann)
        cur[name] = z.add(glyph(kind, 17, 17, 1.25))
    # --- pencil -----------------------------------------------------------------
    ux, uy, nx, ny = k, -k, k, k
    T = (12.0, 28.0)
    def P(along, side): return (T[0] + ux * along + nx * side, T[1] + uy * along + ny * side)
    pen = Cur((9, 31), (11, 29))
    pen.part([poly([T, P(2.5, -1.4), P(23, -1.4), P(23, 1.4), P(2.5, 1.4)])])
    cur["pencil"] = pen
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

SHADOW_DY = 2.4
SHADOW_LAYERS = [(1.0, 0.085), (2.5, 0.075), (4.0, 0.065), (5.5, 0.055), (7.0, 0.045), (8.5, 0.03)]

def svg_of(c, th):
    out_d = [d_of(o) for o, _ in c.parts]
    fill_d = [d_of(f) for _, f in c.parts]
    y0, y1 = c.grad
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS}" height="{CANVAS}" viewBox="0 0 {CANVAS} {CANVAS}">',
         f'<linearGradient id="body" gradientUnits="userSpaceOnUse" x1="0" y1="{fmt(y0)}" x2="0" y2="{fmt(y1)}">'
         f'<stop offset="0" stop-color="{th["body"][0]}"/><stop offset="1" stop-color="{th["body"][1]}"/></linearGradient>']
    # soft drop shadow without filters: stacked, widening silhouettes
    s.append(f'<g transform="translate(0 {SHADOW_DY})" fill="#000" stroke="#000" stroke-linejoin="round" fill-rule="evenodd">')
    for w, a in SHADOW_LAYERS:
        s.append(f'<g opacity="{a}" stroke-width="{fmt(w)}">' + "".join(f'<path d="{d}"/>' for d in out_d) + '</g>')
    s.append('</g>')
    s.append(f'<g fill="{th["outline"]}" stroke="{th["outline"]}" stroke-width="{fmt(2 * OUTLINE)}" '
             f'stroke-linejoin="miter" stroke-miterlimit="10" fill-rule="evenodd">'
             + "".join(f'<path d="{d}"/>' for d in out_d) + '</g>')
    s.append('<g fill="url(#body)" fill-rule="evenodd">' + "".join(f'<path d="{d}"/>' for d in fill_d) + '</g>')
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
            meta, files = [], []
            for i, fr in enumerate(frames):
                fn = f"{name}.svg" if len(frames) == 1 else f"{name}-{i + 1:02d}.svg"
                with open(os.path.join(sdir, fn), "w") as f: f.write(svg_of(fr, th))
                e = {"filename": fn, "hotspot_x": fr.hot[0], "hotspot_y": fr.hot[1], "nominal_size": NOMINAL}
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
                        images.append((nominal, w, h, round(fr.hot[0] * px / CANVAS), round(fr.hot[1] * px / CANVAS),
                                       FRAME_MS if len(frames) > 1 else 0, argb))
                write_xcursor(os.path.join(root, "cursors", name), images)
            for al in ALIASES.get(name, "").split():
                link(name, os.path.join(root, "cursors_scalable", al))
                if xcursor: link(name, os.path.join(root, "cursors", al))
        print(f"{tname}: {len(cursors)} cursors, {sum(len(v.split()) for v in ALIASES.values())} aliases")

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    build(args[0] if args else os.path.dirname(os.path.abspath(__file__)), "--no-xcursor" not in sys.argv)
