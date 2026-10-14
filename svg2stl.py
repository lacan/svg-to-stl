#!/usr/bin/env python3
"""
svg2stl - convert an SVG into one STL per fill color for multi-color 3D printing.
Runs in the browser with Pyodide (see site/), so it only needs numpy, shapely
and svgelements.

Each filled shape in the SVG is grouped by its fill color and extruded upwards
(+Z) by a depth chosen per color. Two modes build on that:

- Extrude: optionally a "backplate" (the union of every shape) is extruded
  downwards (-Z). All parts share one coordinate system, so they line up when
  imported together into a slicer.
- Stamp: the design is mirrored and sits on a rectangular box with a blind
  threaded hole in its back; a knob handle with a matching threaded post
  screws into it.
"""

import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import shapely
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

def _triangulate(poly):
    """(n, 3, 2) array of counter-clockwise triangles exactly covering poly."""
    tris = [np.asarray(t.exterior.coords)[:3]
            for t in shapely.constrained_delaunay_triangles(poly).geoms]
    if not tris:
        return np.empty((0, 3, 2))
    tris = np.array(tris)
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    cw = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]) < 0
    tris[cw] = tris[cw][:, ::-1]
    return tris


def extrude_triangles(geom, height, z0):
    """
    Extrude into an (n, 3, 3) array of outward-facing triangles. Caps and
    walls share their vertices exactly, so the mesh is closed.
    """
    out = []
    z1 = z0 + height
    for poly in only_polygons(geom.simplify(1e-3)).geoms:
        poly = shapely.geometry.polygon.orient(poly)  # outer CCW, holes CW
        caps = _triangulate(poly)
        out += [_at_z(caps, z1), _at_z(caps[:, ::-1], z0), _walls(poly, z0, z1)]
    return np.concatenate(out) if out else np.empty((0, 3, 3))


def _at_z(tris, z):
    """(n, 3, 2) triangles -> (n, 3, 3) at height z."""
    return np.concatenate([tris, np.full((len(tris), 3, 1), z)], axis=2)


def _walls(poly, z0, z1):
    """Outward-facing side walls of an oriented polygon between z0 and z1."""
    out = []
    for ring in [poly.exterior, *poly.interiors]:
        a = np.asarray(ring.coords)[:-1]
        b = np.roll(a, -1, axis=0)
        lo = lambda p: np.column_stack([p, np.full(len(p), z0)])
        hi = lambda p: np.column_stack([p, np.full(len(p), z1)])
        out.append(np.stack([lo(a), lo(b), hi(b)], axis=1))
        out.append(np.stack([lo(a), hi(b), hi(a)], axis=1))
    return np.concatenate(out)


def stl_bytes(triangles):
    """Binary STL from an (n, 3, 3) triangle array."""
    tri = np.asarray(triangles, dtype=np.float64)
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, length, out=np.zeros_like(normals), where=length > 0)
    rec = np.zeros(len(tri), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["n"], rec["v"] = normals, tri
    header = b"svg2stl".ljust(80, b" ")
    return header + np.uint32(len(tri)).tobytes() + rec.tobytes()


def polygons_json(multipolygon):
    """Polygons as JSON-ready lists for the 3D preview."""
    def ring(r):
        # Rounding can merge neighbouring points; drop the repeats so the
        # browser's triangulator never sees a ring that collapsed to a point.
        pts = []
        for x, y in list(r.coords)[:-1]:
            p = [round(x, 4), round(y, 4)]
            if not pts or p != pts[-1]:
                pts.append(p)
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()
        return pts if len({tuple(p) for p in pts}) >= 3 else None
    out = []
    for p in multipolygon.geoms:
        outer = ring(p.exterior)
        if outer:
            out.append({"outer": outer, "holes": [h for h in map(ring, p.interiors) if h]})
    return out


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


# --------------------------------------------------------------------------
# Stamp: mirrored relief on a box, plus a knob handle that screws into it
# --------------------------------------------------------------------------
#
# Assembled coordinates: relief from Z=0 up, box from Z=-thickness to 0 with
# a blind threaded hole in its back (centered), handle below the box with its
# threaded post up inside the hole. The handle STL is exported upright for
# printing instead: flat top of the knob on the bed, post pointing up.

@dataclass
class StampOptions:
    margin: float = 3.0           # box beyond the design's bounding box
    corner_radius: float = 2.0
    box_thickness: float = 10.0
    knob_diameter: float = 0.0    # 0 = automatic
    knob_height: float = 30.0     # without the post
    thread_diameter: float = 16.0
    thread_pitch: float = 2.0
    thread_length: float = 8.0    # depth of the hole; the post is 0.5 mm shorter
    clearance: float = 0.3        # radial play between post and hole


THREAD_DEPTH = 0.5    # x pitch, radial depth of the thread profile
POST_GAP = 0.5        # mm between the post's tip and the bottom of the hole
HOLE_WALL = 1.0       # mm, minimum material around and above the hole
POST_FLARE = 0.7      # mm, 45 degree flare at the post's root (the hole's mouth is chamfered to match)


def stamp_layout(groups, everything, opts):
    """
    Mirror the design left-right (so stamped prints read correctly) and put
    it on the box. Returns (groups, box_outline) with the box's lower-left
    corner at the origin.
    """
    _, _, w, h = everything.bounds
    m = max(opts.margin, 0.0)
    flip = lambda g: shapely.affinity.affine_transform(g, [-1, 0, 0, 1, w + m, m])
    groups = OrderedDict((c, flip(g)) for c, g in groups.items())
    W, H = w + 2 * m, h + 2 * m
    box = shapely.box(0, 0, W, H)
    r = min(max(opts.corner_radius, 0.0), 0.49 * min(W, H))
    if r > 0:
        box = box.buffer(-r, join_style="mitre").buffer(r, quad_segs=8)
    return groups, box


def knob_diameter(opts, box):
    """The knob's widest diameter: as set, or sized to the box."""
    if opts.knob_diameter > 0:
        return opts.knob_diameter
    minx, miny, maxx, maxy = box.bounds
    auto = min(max(0.6 * min(maxx - minx, maxy - miny), 30.0), 45.0)
    return max(auto, _min_knob_diameter(opts))


def _min_knob_diameter(opts):
    # The foot (95% of the knob's radius) and the neck (72%) must both be
    # well wider than the post, or the knob snaps where the post meets it.
    major = opts.thread_diameter / 2
    return 2 * max((major + 3) / 0.95, (major + 2) / 0.72)


def check_stamp(opts, box):
    """Raise ValueError with a readable message if the parts can't be built."""
    t = opts
    if t.thread_pitch < 0.5:
        raise ValueError("thread pitch must be at least 0.5 mm")
    if t.thread_diameter / 2 - THREAD_DEPTH * t.thread_pitch < 1.5:
        raise ValueError("thread diameter is too small for its pitch")
    if t.thread_length < t.thread_pitch + POST_GAP:
        raise ValueError(f"thread length must be at least {t.thread_pitch + POST_GAP:g} mm")
    if t.box_thickness < t.thread_length + HOLE_WALL:
        raise ValueError(f"the box must be at least {t.thread_length + HOLE_WALL:g} mm thick "
                         f"for a {t.thread_length:g} mm deep thread")
    if t.knob_height < 5:
        raise ValueError("the handle must be at least 5 mm tall")
    if knob_diameter(t, box) < _min_knob_diameter(t) - 1e-9:
        raise ValueError(f"the knob must be at least {_min_knob_diameter(t):.1f} mm wide "
                         f"for a {t.thread_diameter:g} mm thread")
    c = box.centroid
    hole = shapely.Point(c.x, c.y).buffer(_mouth_radius(t) + HOLE_WALL)
    if not box.contains(hole):
        raise ValueError("the box is too small for the thread: increase the margin "
                         "or reduce the thread diameter")


def _mouth_radius(opts):
    return opts.thread_diameter / 2 + opts.clearance + POST_FLARE + 0.3


def _thread_radius(theta, z, major_r, pitch):
    """
    Radius of a right-handed trapezoidal thread (60 degree flanks) at angle
    theta and height z; broadcasts. Post and hole use the same helix.
    """
    depth = THREAD_DEPTH * pitch
    flank = depth * math.tan(math.radians(30)) / pitch
    crest = 0.125
    root = 1 - 2 * flank - crest
    u = np.mod(z / pitch - theta / (2 * math.pi), 1.0)
    rise = np.interp(u, [0, root, root + flank, root + flank + crest, 1], [0, 0, 1, 1, 0])
    return major_r - depth + depth * rise


def _revolve(r, z, theta, cx=0.0, cy=0.0):
    """(rows, cols, 3) vertex grid from per-row/col radii and heights."""
    r = np.broadcast_to(r, (len(z), len(theta)))
    z = np.broadcast_to(np.asarray(z)[:, None], r.shape)
    return np.stack([cx + r * np.cos(theta), cy + r * np.sin(theta), z], axis=-1)


def _grid_triangles(v):
    """
    Triangles of a vertex grid whose columns wrap around (angle). Facing out
    when rows go along a meridian from the bottom up the outside. Triangles
    collapsed at a pole (radius 0) are dropped.
    """
    a, d = v[:-1], v[1:]
    b, c = np.roll(a, -1, axis=1), np.roll(d, -1, axis=1)
    tris = np.concatenate([np.stack([a, b, c], axis=-2).reshape(-1, 3, 3),
                           np.stack([a, c, d], axis=-2).reshape(-1, 3, 3)])
    area = np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
    return tris[area > 1e-12]


def _rows(z0, z1, step):
    n = max(2, int(math.ceil(abs(z1 - z0) / step)) + 1)
    return np.linspace(z0, z1, n)


def box_triangles(box, opts, segments=128, rows_per_pitch=24):
    """The stamp's box with its blind threaded hole, as one closed mesh."""
    t = opts
    z0, z1 = -t.box_thickness, 0.0
    poly = shapely.geometry.polygon.orient(box.simplify(1e-3))
    c = box.centroid
    theta = np.linspace(0, 2 * math.pi, segments, endpoint=False)

    # Hole wall from the mouth (chamfered out to mouth_r, which the bottom
    # face's hole ring matches exactly) up to a plain ring at the floor.
    floor = z0 + t.thread_length
    z = _rows(z0, floor, t.thread_pitch / rows_per_pitch)
    major = t.thread_diameter / 2 + t.clearance
    mouth_r = _mouth_radius(t)
    r = _thread_radius(theta, z[:, None], major, t.thread_pitch)
    r = np.maximum(r, mouth_r - (z[:, None] - z0))        # 45 degree chamfer at the mouth
    r = np.maximum(r, major - (floor - z[:, None]))       # run out into the floor
    r = np.vstack([r, np.zeros((1, segments))])            # floor, closed at the axis
    hole = _revolve(r, np.append(z, floor), theta, c.x, c.y)
    hole_tris = _grid_triangles(hole)[:, ::-1]             # faces into the hole

    ring = hole[0, :, :2]
    back = shapely.geometry.polygon.orient(Polygon(poly.exterior.coords, [ring]))
    back_tris = _triangulate(back)[:, ::-1]
    return np.concatenate([_at_z(_triangulate(poly), z1), _at_z(back_tris, z0),
                           _walls(poly, z0, z1), hole_tris])


def _catmull_rom(points, per_segment=8):
    p = np.asarray(points, dtype=float)
    p = np.vstack([2 * p[0] - p[1], p, 2 * p[-1] - p[-2]])
    out = []
    for i in range(1, len(p) - 2):
        s = np.linspace(0, 1, per_segment, endpoint=False)[:, None]
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        out.append(0.5 * (2 * p1 + (p2 - p0) * s + (2 * p0 - 5 * p1 + 4 * p2 - p3) * s ** 2
                          + (3 * p1 - p0 - 3 * p2 + p3) * s ** 3))
    out.append(p[-2:-1])
    return np.vstack(out)


def handle_triangles(opts, diameter, segments=128, rows_per_pitch=24):
    """
    The knob with its threaded post, upright for printing: flat top of the
    knob on Z=0, post pointing up, centered on the Z axis.
    """
    t = opts
    R, H = diameter / 2, t.knob_height
    theta = np.linspace(0, 2 * math.pi, segments, endpoint=False)
    # Knob profile as (radius, height) from the flat top on the bed up to the
    # foot that sits against the box: bulb, neck, flared foot.
    profile = _catmull_rom([(0.75 * R, 0), (0.95 * R, 0.08 * H), (R, 0.2 * H), (0.9 * R, 0.35 * H),
                            (0.72 * R, 0.55 * H), (0.78 * R, 0.8 * H), (0.95 * R, H)])
    rows_r = [np.zeros(segments)] + [np.full(segments, r) for r, _ in profile]
    rows_z = [0.0] + [z for _, z in profile]

    major = t.thread_diameter / 2
    length = t.thread_length - POST_GAP
    tip_r = major - THREAD_DEPTH * t.thread_pitch - 0.5
    z = _rows(H, H + length, t.thread_pitch / rows_per_pitch)
    r = _thread_radius(theta, z[:, None], major, t.thread_pitch)
    r = np.maximum(r, major + POST_FLARE - (z[:, None] - H))  # flared root, against snapping
    r = np.minimum(r, tip_r + (H + length - z[:, None]))   # 45 degree chamfer at the tip
    rows_r += list(r) + [np.zeros(segments)]
    rows_z += list(z) + [H + length]
    return _grid_triangles(_revolve(np.array(rows_r), rows_z, theta))


def mesh_json(triangles, digits=3):
    """Flat vertex list for the 3D preview."""
    return np.round(np.asarray(triangles, dtype=float).reshape(-1), digits).tolist()
