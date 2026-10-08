#!/usr/bin/env python3
"""
svg2stl - convert an SVG into one STL per fill color for multi-color 3D printing.

Each filled shape in the SVG is grouped by its fill color, extruded upwards
(+Z) by a depth chosen per color, and written to

    <svg folder>/<name>_stl/<name>_<rrggbb>.stl

Optionally a "backplate" (the union of every shape) is extruded downwards
(-Z) and written as <name>_backplate.stl. All files share the same coordinate
system, so they line up when imported together into a slicer.
"""

import argparse
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path as FsPath

import numpy as np
import shapely
import trimesh
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union
from svgelements import (SVG, Arc, Close, Color, CubicBezier, Line, Move,
                         Path, QuadraticBezier, Shape)

PX_TO_MM = 25.4 / 96.0  # svgelements resolves all units to 96 dpi pixels
MIN_AREA = 1e-4         # mm^2, polygons smaller than this are discarded
SLIVER = 0.01           # mm, gaps narrower than twice this are closed in the backplate


# --------------------------------------------------------------------------
# Color handling
# --------------------------------------------------------------------------

def normalize_hex(text):
    """'#F00', 'ff0000', '#FF0000' -> 'ff0000'."""
    h = text.strip().lstrip("#").lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if not re.fullmatch(r"[0-9a-f]{6}", h):
        raise ValueError(f"not a hex color: {text!r}")
    return h


def gradient_first_stops(svg_file):
    """Map gradient id -> hex of its first stop (follows xlink:href chains)."""
    root = ET.parse(svg_file).getroot()
    grads, links = {}, {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("linearGradient", "radialGradient") or "id" not in el.attrib:
            continue
        gid = el.attrib["id"]
        href = el.attrib.get("{http://www.w3.org/1999/xlink}href") or el.attrib.get("href")
        if href and href.startswith("#"):
            links[gid] = href[1:]
        for stop in el:
            if stop.tag.rsplit("}", 1)[-1] != "stop":
                continue
            color = stop.attrib.get("stop-color")
            m = re.search(r"stop-color\s*:\s*([^;]+)", stop.attrib.get("style", ""))
            if m:
                color = m.group(1)
            try:
                c = Color(color or "black")
                grads[gid] = f"{c.red:02x}{c.green:02x}{c.blue:02x}"
            except Exception:
                pass
            break
    for gid, target in links.items():
        seen = {gid}
        while gid not in grads and target in links and target not in seen:
            seen.add(target)
            target = links[target]
        if gid not in grads and target in grads:
            grads[gid] = grads[target]
    return grads


def element_fill_hex(element, gradients):
    """Return the fill color of an element as 'rrggbb', or None if unfilled."""
    raw = str(element.values.get("fill", "")).strip()
    m = re.match(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)", raw)
    if m:
        if m.group(1) in gradients:
            return gradients[m.group(1)]
        print(f"  warning: unsupported fill {raw} on <{element.id or type(element).__name__}>, skipped",
              file=sys.stderr)
        return None
    fill = element.fill
    if fill is None or fill.value is None or fill.alpha == 0:
        return None
    return f"{fill.red:02x}{fill.green:02x}{fill.blue:02x}"


# --------------------------------------------------------------------------
# SVG -> 2D geometry
# --------------------------------------------------------------------------

def flatten_rings(path, tolerance_px):
    """Approximate a path's subpaths as lists of (x, y) point rings."""
    rings, current = [], []
    for seg in path:
        if isinstance(seg, Move):
            if len(current) >= 3:
                rings.append(current)
            current = [(seg.end.x, seg.end.y)]
        elif isinstance(seg, (Line, Close)):
            if seg.end is not None:
                current.append((seg.end.x, seg.end.y))
        elif isinstance(seg, (CubicBezier, QuadraticBezier, Arc)):
            try:
                length = seg.length(error=1e-4, min_depth=4)
            except TypeError:
                length = seg.length()
            n = max(2, int(math.ceil(length / tolerance_px)))
            pts = seg.npoint(np.linspace(0, 1, n + 1)[1:])
            current.extend((float(x), float(y)) for x, y in pts)
    if len(current) >= 3:
        rings.append(current)
    return rings


def winding_numbers(ring, points):
    """Winding number of a closed ring around each point (vectorized)."""
    r = np.asarray(ring, dtype=float)
    a, b = r, np.roll(r, -1, axis=0)
    px, py = points[:, 0:1], points[:, 1:2]
    ax, ay, bx, by = a[:, 0], a[:, 1], b[:, 0], b[:, 1]
    cross = (bx - ax) * (py - ay) - (px - ax) * (by - ay)
    up = (ay <= py) & (by > py) & (cross > 0)
    down = (ay > py) & (by <= py) & (cross < 0)
    return up.sum(axis=1) - down.sum(axis=1)


def rings_to_geometry(rings, fill_rule):
    """Combine rings into a filled area, honoring SVG nonzero/evenodd rules."""
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return Polygon()
    if len(rings) == 1:
        return shapely.make_valid(Polygon(rings[0]))
    # Split the plane into faces bounded by all ring edges, then keep the
    # faces whose winding number satisfies the fill rule.
    lines = unary_union([LineString(r + [r[0]]) for r in rings])
    faces = list(polygonize(lines))
    if not faces:
        return Polygon()
    pts = np.array([[p.x, p.y] for p in (f.representative_point() for f in faces)])
    total = sum(winding_numbers(r, pts) for r in rings)
    keep = (total % 2 == 1) if fill_rule == "evenodd" else (total != 0)
    return unary_union([f for f, k in zip(faces, keep) if k])


def load_shapes(svg_file, tolerance_mm):
    """Return [(hex, geometry_in_px), ...] in SVG paint order."""
    gradients = gradient_first_stops(svg_file)
    svg = SVG.parse(str(svg_file), reify=True, ppi=96)
    shapes = []
    for el in svg.elements():
        if not isinstance(el, Shape):
            continue
        color = element_fill_hex(el, gradients)
        if color is None:
            continue
        path = Path(el)
        path.reify()
        rings = flatten_rings(path, tolerance_mm / PX_TO_MM)
        fill_rule = str(el.values.get("fill-rule", "nonzero")).strip().lower()
        geom = rings_to_geometry(rings, fill_rule)
        if not geom.is_empty and geom.area > 0:
            shapes.append((color, geom))
    for el in svg.elements():
        if type(el).__name__ == "Text":
            print("  warning: <text> found; convert text to paths in your editor "
                  "(Inkscape: Path > Object to Path), it is ignored here", file=sys.stderr)
            break
    return shapes


def to_mm(geom, scale):
    """Pixels -> mm, flipping Y (SVG is y-down, printers are y-up)."""
    k = PX_TO_MM * scale
    return shapely.transform(geom, lambda c: c * np.array([k, -k]))


def only_polygons(geom):
    """Extract the polygonal part of any geometry, dropping slivers."""
    geom = shapely.make_valid(geom)
    polys = []
    for g in getattr(geom, "geoms", [geom]):
        if isinstance(g, Polygon):
            polys.append(g)
        elif isinstance(g, MultiPolygon):
            polys.extend(g.geoms)
        elif hasattr(g, "geoms"):
            polys.extend(only_polygons(g).geoms)
    return MultiPolygon([p for p in polys if p.area > MIN_AREA])


def group_by_color(shapes, occlude):
    """
    Merge shapes per color. With occlusion, every shape is clipped by the
    shapes painted above it so colors never overlap (what you see in the SVG
    is what gets printed, and the parts don't intersect in the slicer).
    """
    groups = OrderedDict()
    covered = Polygon()
    for color, geom in reversed(shapes):
        visible = geom.difference(covered) if occlude else geom
        if occlude:
            covered = unary_union([covered, geom])
        groups.setdefault(color, []).append(visible)
    ordered = OrderedDict()
    for color in dict.fromkeys(c for c, _ in shapes):  # first-seen order
        merged = only_polygons(unary_union(groups[color]))
        if not merged.is_empty:
            ordered[color] = merged
    return ordered


# --------------------------------------------------------------------------
# 3D
# --------------------------------------------------------------------------

def extrude(geom, height, z0):
    parts = []
    # Near-duplicate / collinear points (left by flattening and boolean ops)
    # make the cap triangulation leave holes; a 1 micron simplify removes them.
    for poly in only_polygons(geom.simplify(1e-3)).geoms:
        poly = shapely.geometry.polygon.orient(poly)
        mesh = trimesh.creation.extrude_polygon(poly, height)
        mesh.apply_translation((0, 0, z0))
        parts.append(mesh)
    if not parts:
        return None
    return trimesh.util.concatenate(parts)


# --------------------------------------------------------------------------
# Pipeline (shared by the CLI and the web UI)
# --------------------------------------------------------------------------

def prepare(svg_file, scale=1.0, width=None, tolerance=0.05, occlude=True, shapes=None):
    """
    SVG -> (groups, everything): per-color MultiPolygons in mm with the
    design's lower-left corner at the origin, plus their union.
    `shapes` may be passed to reuse a previous load_shapes() result.
    """
    if shapes is None:
        shapes = load_shapes(svg_file, tolerance / scale)
    if not shapes:
        raise ValueError("no filled shapes found in the SVG")
    if width:
        minx, _, maxx, _ = unary_union([g for _, g in shapes]).bounds
        scale = width / ((maxx - minx) * PX_TO_MM)
    shapes = [(c, to_mm(g, scale)) for c, g in shapes]
    groups = group_by_color(shapes, occlude=occlude)

    # Put the design's lower-left corner at the origin; the same offset is
    # applied to every part so they stay aligned.
    everything = unary_union(list(groups.values()))
    minx, miny, _, _ = everything.bounds
    shift = lambda g: shapely.affinity.translate(g, -minx, -miny)
    groups = OrderedDict((c, shift(g)) for c, g in groups.items())
    return groups, shift(everything)


def backplate_outline(everything, margin=0.0):
    # Adjacent colors rarely touch exactly, so their union is riddled with
    # hairline gaps; close them (morphological closing) so the plate is solid.
    outline = everything.buffer(SLIVER, join_style="mitre").buffer(-SLIVER, join_style="mitre")
    if margin:
        outline = outline.buffer(margin, join_style="round")
    return only_polygons(outline)


def output_dir(svg_file):
    svg_file = FsPath(svg_file)
    return svg_file.parent / f"{svg_file.stem}_stl"


def write_stls(svg_file, groups, everything, depths, default_depth=1.0,
               backplate=None, backplate_margin=0.0, log=print):
    """Extrude and export. Returns the list of written paths."""
    svg_file = FsPath(svg_file)
    out_dir = output_dir(svg_file)
    out_dir.mkdir(exist_ok=True)
    log(f"Writing to {out_dir}")
    written = []
    for color, geom in groups.items():
        depth = depths.get(color, default_depth)
        if depth <= 0:
            log(f"  #{color}: skipped (depth {depth})")
            continue
        mesh = extrude(geom, depth, 0.0)
        target = out_dir / f"{svg_file.stem}_{color}.stl"
        mesh.export(target)
        written.append(target)
        log(f"  {target.name}  ({depth} mm, {len(mesh.faces)} triangles)")
    if backplate:
        mesh = extrude(backplate_outline(everything, backplate_margin), backplate, -backplate)
        target = out_dir / f"{svg_file.stem}_backplate.stl"
        mesh.export(target)
        written.append(target)
        log(f"  {target.name}  ({backplate} mm, {len(mesh.faces)} triangles)")
    return written


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_depths(items):
    depths = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--depth expects COLOR=MM, got {item!r}")
        color, value = item.split("=", 1)
        try:
            depths[normalize_hex(color)] = float(value)
        except ValueError as e:
            raise SystemExit(f"--depth {item!r}: {e}")
    return depths


def ask_float(prompt, default):
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        try:
            return float(answer)
        except ValueError:
            print("  please enter a number")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert an SVG into per-color STL files for multi-color printing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  svg2stl logo.svg --list-colors\n"
               "  svg2stl logo.svg --default-depth 1 --depth ff0000=2 --depth #00f=0.6\n"
               "  svg2stl logo.svg --interactive --backplate 1.5\n")
    ap.add_argument("svg", type=FsPath, help="input SVG file")
    ap.add_argument("-d", "--depth", action="append", metavar="COLOR=MM",
                    help="extrusion depth for one color, e.g. ff0000=2 (repeatable)")
    ap.add_argument("--default-depth", type=float, default=1.0, metavar="MM",
                    help="depth for colors without --depth (default: 1.0)")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="prompt for the depth of each color found")
    ap.add_argument("-b", "--backplate", type=float, metavar="MM",
                    help="also create a backplate (union of all shapes) extruded MM downwards")
    ap.add_argument("--backplate-margin", type=float, default=0.0, metavar="MM",
                    help="grow the backplate outline by MM (default: 0)")
    ap.add_argument("-s", "--scale", type=float, default=1.0,
                    help="scale factor applied to the SVG's physical size (default: 1)")
    ap.add_argument("--width", type=float, metavar="MM",
                    help="scale so the overall design is MM wide (overrides --scale)")
    ap.add_argument("--tolerance", type=float, default=0.05, metavar="MM",
                    help="max segment length when flattening curves (default: 0.05)")
    ap.add_argument("--no-occlusion", action="store_true",
                    help="don't cut shapes hidden under shapes painted on top of them")
    ap.add_argument("--list-colors", action="store_true",
                    help="only list the fill colors found and exit")
    args = ap.parse_args(argv)

    svg_file = args.svg.resolve()
    if not svg_file.is_file():
        raise SystemExit(f"file not found: {svg_file}")

    print(f"Reading {svg_file.name} ...")
    try:
        groups, everything = prepare(svg_file, args.scale, args.width,
                                     args.tolerance, not args.no_occlusion)
    except ValueError as e:
        raise SystemExit(str(e))

    _, _, maxx, maxy = everything.bounds
    print(f"Design size: {maxx:.2f} x {maxy:.2f} mm, "
          f"{len(groups)} color(s):")
    for color, geom in groups.items():
        print(f"  #{color}  {len(geom.geoms):4d} region(s)  {geom.area:10.2f} mm^2")
    if args.list_colors:
        return 0

    depths = parse_depths(args.depth)
    unknown = set(depths) - set(groups)
    for c in sorted(unknown):
        print(f"  warning: --depth given for #{c}, which is not in the SVG", file=sys.stderr)
    if args.interactive:
        print("Enter the extrusion depth (mm) for each color, 0 to skip:")
        for color in groups:
            depths[color] = ask_float(f"  #{color}", depths.get(color, args.default_depth))
        if args.backplate is None:
            bp = ask_float("  backplate (0 = none)", 0.0)
            args.backplate = bp if bp > 0 else None

    write_stls(svg_file, groups, everything, depths, args.default_depth,
               args.backplate, args.backplate_margin)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
