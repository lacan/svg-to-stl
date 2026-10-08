"""
Glue between the static web page and svg2stl, run in the browser by Pyodide
(see worker.js). Mirrors the /api/load and /api/export endpoints of the local
UI, but takes the SVG's content instead of a path and returns a ZIP instead of
writing files.
"""

import contextlib
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

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
        _, _, w, h = everything.bounds
        margin = float(req.get("margin") or 0)
        return {
            "name": _svg["name"],
            "size": [w, h],
            "warnings": warnings,
            "colors": [{"hex": c, "area": g.area, "regions": len(g.geoms),
                        "polygons": svg2stl.polygons_json(g)} for c, g in groups.items()],
            "backplate": svg2stl.polygons_json(svg2stl.backplate_outline(everything, margin)),
        }
    return _reply(run)


def export(req_json):
    """Write every STL into ZIP_OUT; returns the file list as JSON."""
    def run():
        req = json.loads(req_json)
        groups, everything = _geometry(req, [])
        depths = {svg2stl.normalize_hex(c): float(d) for c, d in req.get("depths", {}).items()}
        backplate = float(req.get("backplate") or 0)
        stem = _svg["name"]
        files = []
        with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED) as z:
            for color, geom in groups.items():
                depth = depths.get(color, 0.0)
                if depth <= 0:
                    continue
                tri = svg2stl.extrude_triangles(geom, depth, 0.0)
                name = f"{stem}_{color}.stl"
                z.writestr(name, svg2stl.stl_bytes(tri))
                files.append({"name": name, "triangles": len(tri)})
            if backplate > 0:
                outline = svg2stl.backplate_outline(everything, float(req.get("margin") or 0))
                tri = svg2stl.extrude_triangles(outline, backplate, -backplate)
                name = f"{stem}_backplate.stl"
                z.writestr(name, svg2stl.stl_bytes(tri))
                files.append({"name": name, "triangles": len(tri)})
        if not files:
            raise ValueError("nothing to export: every depth is 0")
        return {"zip": str(ZIP_OUT), "zipName": f"{stem}_stl.zip", "files": files}
    return _reply(run)
