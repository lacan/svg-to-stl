# svg2stl

Convert an SVG into one STL per fill color for multi-color 3D printing. Runs fully offline.

## Setup (once)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Web UI with 3D preview

```
svg2stl_gui.bat [logo.svg]
```

Opens a page in your browser (served from `127.0.0.1` only; Three.js is
bundled in `web/vendor`, so no internet is needed). Pick a file with
*Browse…*, then adjust each color's depth, the backplate, size and curve
detail while watching the 3D preview, and click *Export*. The STLs are written
by the same code as the command line, into the same `<name>_stl` folder.
Settings are remembered per SVG. Close the console window to stop the server.

## Command line

```
svg2stl.bat logo.svg --list-colors                       # see which colors are in the file
svg2stl.bat logo.svg -d ff0000=2 -d #00f=0.6 --default-depth 1
svg2stl.bat logo.svg -i                                  # prompt for each color's depth
svg2stl.bat logo.svg --default-depth 1 -b 1.5            # + 1.5 mm backplate
```

Output goes to `logo_stl\` next to the SVG:

- `logo_<rrggbb>.stl` – one per fill color, extruded from Z=0 upwards
- `logo_backplate.stl` – (with `-b MM`) union of all shapes, extruded from Z=0 downwards

All files share one coordinate system: import them together into your slicer
as a single multi-part object and assign a filament to each part.

| Option | Meaning |
|---|---|
| `-d COLOR=MM` | depth for one color (repeatable; `0` skips the color) |
| `--default-depth MM` | depth for all other colors (default 1.0) |
| `-i` | ask for each depth interactively |
| `-b MM` | create a backplate MM thick |
| `--backplate-margin MM` | grow the backplate outline |
| `--width MM` / `-s F` | resize the design to a width / by a factor |
| `--tolerance MM` | curve smoothness (default 0.05) |
| `--no-occlusion` | keep shapes hidden under others (by default they are cut away so parts don't overlap) |

## Notes

- Only **fills** are used; strokes are ignored. In Inkscape use *Path > Stroke to Path* first.
- Text must be converted to paths (*Path > Object to Path*).
- Gradient fills use the color of their first stop.
- Size comes from the SVG's physical units (`width="80mm"` etc., 96 dpi for px).
