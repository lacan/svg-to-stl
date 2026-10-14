# svg-to-stl

Convert an SVG into one STL per fill color for multi-color 3D printing, or
into a printable stamp with a screw-on handle. Runs entirely in the browser.

## About

Inspired by the work of [svg2solid](https://svg2solid.rknt.de/), I wanted a
tool to simplify working with multicolor flat prints based off SVGs. The
result is this tool.

**Disclaimer:** this was done using [Claude Code](https://claude.com/claude-code).

## Modes

The left panel has two tabs:

- **Extrude**: each fill color is extruded upwards by its own depth, with an
  optional backplate (the union of all shapes) extruded downwards. Import all
  STLs together into your slicer as one multi-part object and give each part
  a filament.
- **Stamp**: makes a rubber-stamp style print in two files:
  - `<name>_stamp.stl`: the relief on its box, as one object. The relief is
    the design mirrored left-right so the stamped print reads correctly; each
    color keeps its own height, so set the background color(s) to 0. The box
    is the design's bounding box plus a margin (optionally rounded), with a
    blind threaded hole in its back.
  - `<name>_handle.stl`: a knob (30 mm tall by default) with a matching
    threaded post, exported upright for printing (flat top on the bed, post
    up, no supports needed).

  Print the stamp relief up, sand the relief flat, then
  screw the handle in. The thread is a right-handed trapezoidal thread
  (16 mm, 2 mm pitch, 8 mm long, 0.3 mm radial clearance by default); raise
  the clearance if it is too tight for your printer.

## Website (runs in the visitor's browser)

`site/` is a static page. The converter (`svg2stl.py`) runs in the browser
with [Pyodide](https://pyodide.org), so the SVG never leaves the visitor's
computer and there is no server to pay for. STLs are downloaded as one ZIP.

```
serve_site.bat                   # build _site/, serve it and open http://127.0.0.1:8000/
python build_site.py --serve     # same, on any OS
```

- Opening `site/index.html` directly from disk does not work (browsers block
  workers on `file://`); always go through the local server above.
- `build_site.py` copies `site/` and `svg2stl.py` into `_site/`. It only needs
  Python's standard library.
- Pyodide (Python + numpy + shapely, ~15 MB on the first visit, then cached)
  is loaded from the jsDelivr CDN; svgelements and three.js are bundled in
  `site/vendor/`.
- Meshes are built by `extrude_triangles()` (shapely's constrained Delaunay
  triangulation); the stamp's box and handle are generated directly as closed
  meshes (`box_triangles()`, `handle_triangles()`).
- Ko-fi: set `KOFI_URL` near the top of the script in `site/index.html`; the
  buttons stay hidden while it is empty.

### Publishing on GitHub Pages

1. Push the repo to GitHub (Pages is free for public repos).
2. *Settings › Pages › Build and deployment › Source*: **GitHub Actions**.
3. Every push to `main` runs `.github/workflows/pages.yml`, which builds
   `_site/` and deploys it to `https://<user>.github.io/<repo>/`. It can also
   be started by hand from the *Actions* tab.

## Notes

- Only **fills** are used; strokes are ignored. In Inkscape use *Path > Stroke to Path* first.
- Text must be converted to paths (*Path > Object to Path*).
- Gradient fills use the color of their first stop.
- Size comes from the SVG's physical units (`width="80mm"` etc., 96 dpi for px).

## License

BSD 3-Clause, see [LICENSE](LICENSE).

Third-party code, all under permissive licenses compatible with BSD-3:

| Component | License | How it is used |
|---|---|---|
| [svgelements](https://github.com/meerk40t/svgelements) | MIT | dependency; wheel bundled in `site/vendor/` (license inside the wheel) |
| [Shapely](https://github.com/shapely/shapely) | BSD-3-Clause | dependency (bundles the GEOS library, LGPL-2.1) |
| [NumPy](https://numpy.org) | BSD-3-Clause | dependency |
| [three.js](https://threejs.org) | MIT | bundled in `site/vendor/`, see `LICENSE-three.js.txt` |
| [Pyodide](https://pyodide.org) | MPL-2.0 | loaded by the website from the jsDelivr CDN, not redistributed |
