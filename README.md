# Gruppled — SVG edition

A vector redraw of the Gruppled cursor theme (original bitmaps: gruppler, 2008, GPL v2).

- `gruppled_black/`, `gruppled_white/` — ready-to-use themes
  - `cursors_scalable/` — SVG cursors + `metadata.json` (Plasma 6.2+, Wayland)
  - `cursors/` — Xcursor fallback at sizes 24, 32, 36, 48, 64, 72, 96 (X11, XWayland, GTK)
- `build.py` — the generator; all geometry, colours, aliases and sizes live in this one file

## Install

    mkdir -p ~/.icons && cp -a gruppled_black gruppled_white ~/.icons/
    plasma-apply-cursortheme gruppled_black      # or System Settings → Cursors

Use `~/.icons`: the Plasma cursor settings module does not look in
`~/.local/share/icons` unless `XCURSOR_PATH` points there.

## Rebuild

    ./build.py                 # needs python3 and rsvg-convert (librsvg)
    ./build.py --no-xcursor    # SVG only; Xcursor can then be generated with kcursorgen

The sizes offered in System Settings come from the Xcursor files; change
`XCURSOR_SIZES` in `build.py` to add or remove sizes.
