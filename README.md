# Gruppled — SVG edition

A vector rebuild of the Gruppled cursor themes (original bitmaps: gruppler, 2008, GPL v2),
in both weights of the original:

| theme                 | look                                   |
|-----------------------|----------------------------------------|
| `gruppled_black`      | dark body, white outline               |
| `gruppled_white`      | light body, black outline              |
| `gruppled_black_lite` | dark body, thin white outline          |
| `gruppled_white_lite` | light body, thin black outline         |

Each theme contains

- `cursors_scalable/` — SVG cursors + `metadata.json` (Plasma 6.2+, Wayland)
- `cursors/` — Xcursor fallback at sizes 24, 32, 36, 48, 64, 72, 96 (X11, XWayland, GTK)

`build.py` is the generator; all geometry, colours, aliases and sizes live in this one file.

## Install

    mkdir -p ~/.icons && cp -a gruppled_* ~/.icons/
    plasma-apply-cursortheme gruppled_black      # or System Settings → Cursors

Use `~/.icons`: the Plasma cursor settings module does not look in
`~/.local/share/icons` unless `XCURSOR_PATH` points there.

## Rebuild

    ./build.py                 # needs python3 and rsvg-convert (librsvg)
    ./build.py --no-xcursor    # SVG only; Xcursor can then be generated with kcursorgen

The sizes offered in System Settings come from the Xcursor files; change
`XCURSOR_SIZES` in `build.py` to add or remove sizes.

## Credits and licence

Based on **Gruppled Cursors** and **Gruppled Lite Cursors** by gruppler (Craig Laparo), 2008:

- https://store.kde.org/p/999974
- lite: originally gnome-look.org content 86145, mirrored at
  https://github.com/nim65s/gruppled-lite-cursors

The originals shipped 39 px bitmaps only. Every cursor here was rebuilt as SVG by
fitting curves to those bitmaps (shape, stroke width, round joins, gradient,
shadow), so at cursor size 24 the result lands on the same pixels as the
original. Both weights share the same paths; lite differs in stroke width, in
thinner cross and text bars, and in drawing the ring over the ball and the ring
symbols. Several missing shapes were added in the same style (`grabbing`,
`zoom-in`, `zoom-out`, `context-menu`, `vertical-text`, `right_ptr`).

Licence: GNU GPL v2, same as the originals — see `COPYING`.
