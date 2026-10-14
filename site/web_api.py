"""
Glue between the static web page and svg2stl, run in the browser by Pyodide
(see worker.js). Takes the SVG's content and returns the preview geometry, or
a ZIP of STL files.

Every request has "mode": "extrude" (per-color parts + optional backplate) or
"stamp" (mirrored relief merged with its box + screw-on handle, see svg2stl).
In stamp mode, colors with a negative depth are engraved into the box.
"""

import contextlib
import hashlib
import io
import json
import re
import zipfile
from dataclasses import fields
from pathlib import Path

import numpy as np

import svg2stl

WORK = Path("/tmp/svg2stl")
WORK.mkdir(parents=True, exist_ok=True)
INPUT = WORK / "input.svg"
ZIP_OUT = WORK / "out.zip"

_svg = {"hash": None, "name": "design"}
_cache = {}


def set_svg(text, name):
    """Store the uploaded SVG; returns nothing, later calls use it."""
    data = text.encode("utf-8")
    INPUT.write_bytes(data)
    _svg["hash"] = hashlib.sha1(data).hexdigest()
    stem = re.sub(r"[^\w.-]+", "_", Path(name).stem).strip("._") or "design"
    _svg["name"] = stem


def _params(req):
    scale = float(req.get("scale") or 1.0)
    width = float(req.get("width") or 0) or None
    tolerance = max(float(req.get("tolerance") or 0.05), 0.005)
    occlude = bool(req.get("occlude", True))
    return scale, width, tolerance, occlude


def _geometry(req, warnings):
    if _svg["hash"] is None:
        raise ValueError("no SVG loaded")
    scale, width, tolerance, occlude = _params(req)
    key = (_svg["hash"], tolerance, scale)
    if key not in _cache:
        _cache.clear()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _cache[key] = (svg2stl.load_shapes(INPUT, tolerance / scale), err.getvalue())
    shapes, warn_text = _cache[key]
    warnings.extend(line.strip().removeprefix("warning: ")
                    for line in warn_text.splitlines() if line.strip())
    return svg2stl.prepare(INPUT, scale, width, tolerance, occlude, shapes)


def stamp_defaults():
    """StampOptions defaults as JSON, so the page's inputs have one source of truth."""
    return json.dumps({f.name: f.default for f in fields(svg2stl.StampOptions)})


def _depths(req):
    return {svg2stl.normalize_hex(c): float(d) for c, d in (req.get("depths") or {}).items()}


def _stamp(req, groups, everything, warnings):
    """
    Stamp mode: (mirrored groups, box outline, options, knob diameter,
    pockets), pockets being the (depth, geometry) engraved into the box.
    """
    given = req.get("stamp") or {}
    opts = svg2stl.StampOptions(**{f.name: float(given[f.name]) for f in fields(svg2stl.StampOptions)
                                   if given.get(f.name) not in (None, "")})
    groups, box = svg2stl.stamp_layout(groups, everything, opts)
    depths = _depths(req)
    pockets = [(-depths[c], g) for c, g in groups.items() if depths.get(c, 0.0) < 0]
    knob, stamp_warnings = svg2stl.check_stamp(opts, box, max((d for d, _ in pockets), default=0.0))
    warnings.extend(stamp_warnings)
    return groups, box, opts, knob, pockets


def _reply(fn):
    """Run fn and return JSON; errors become {"error": ...} for the page."""
    try:
        return json.dumps(fn())
    except Exception as e:
        return json.dumps({"error": str(e) or type(e).__name__})


def load(req_json):
    def run():
        req = json.loads(req_json)
        warnings = []
        groups, everything = _geometry(req, warnings)
        result = {"name": _svg["name"], "warnings": warnings, "mode": "extrude"}
        if req.get("mode") == "stamp":
            groups, box, opts, knob, pockets = _stamp(req, groups, everything, warnings)
            minx, miny, maxx, maxy = box.bounds
            c = box.centroid
            # Coarser threads for the preview keep it fast; the export uses full detail.
            handle = svg2stl.handle_triangles(opts, knob, segments=48, rows_per_pitch=6)
            handle += (c.x, c.y, -opts.box_thickness - opts.knob_height)
            result.update({
                "mode": "stamp",
                "size": [maxx - minx, maxy - miny],
                "box": svg2stl.mesh_json(svg2stl.box_triangles(box, opts, pockets, segments=48, rows_per_pitch=6)),
                "handle": svg2stl.mesh_json(handle),
                "boxThickness": opts.box_thickness,
                "knobHeight": opts.knob_height,
                "knobDiameter": knob,
            })
        else:
            _, _, w, h = everything.bounds
            margin = float(req.get("margin") or 0)
            result.update({
                "size": [w, h],
                "backplate": svg2stl.polygons_json(svg2stl.backplate_outline(everything, margin)),
            })
        result["colors"] = [{"hex": c, "area": g.area, "regions": len(g.geoms),
                             "polygons": svg2stl.polygons_json(g)} for c, g in groups.items()]
        return result
    return _reply(run)


def export(req_json):
    """Write every STL into ZIP_OUT; returns the file list as JSON."""
    def run():
        req = json.loads(req_json)
        groups, everything = _geometry(req, [])
        stamp = req.get("mode") == "stamp"
        if stamp:
            groups, box, opts, knob, pockets = _stamp(req, groups, everything, [])
        depths = _depths(req)
        stem = _svg["name"]
        parts = []  # (file name, triangles)
        if stamp:
            # Relief and box in one STL, so the slicer sees a single object.
            # The relief starts inside the box: overlapping shells merge
            # cleanly when sliced, merely touching ones may not.
            sink = svg2stl.RELIEF_SINK
            relief = [svg2stl.extrude_triangles(geom, depths[color] + sink, -sink)
                      for color, geom in groups.items() if depths.get(color, 0.0) > 0]
            if relief or pockets:
                parts.append((f"{stem}_stamp.stl",
                              np.concatenate([*relief, svg2stl.box_triangles(box, opts, pockets)])))
                parts.append((f"{stem}_handle.stl", svg2stl.handle_triangles(opts, knob)))
        else:
            for color, geom in groups.items():
                depth = depths.get(color, 0.0)
                if depth > 0:
                    parts.append((f"{stem}_{color}.stl", svg2stl.extrude_triangles(geom, depth, 0.0)))
            backplate = float(req.get("backplate") or 0)
            if backplate > 0:
                outline = svg2stl.backplate_outline(everything, float(req.get("margin") or 0))
                parts.append((f"{stem}_backplate.stl",
                              svg2stl.extrude_triangles(outline, backplate, -backplate)))
        if not parts:
            raise ValueError("nothing to export: every depth is 0")
        files = []
        with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED) as z:
            for name, tri in parts:
                z.writestr(name, svg2stl.stl_bytes(tri))
                files.append({"name": name, "triangles": len(tri)})
        zip_name = f"{stem}_stamp_stl.zip" if stamp else f"{stem}_stl.zip"
        return {"zip": str(ZIP_OUT), "zipName": zip_name, "files": files}
    return _reply(run)
