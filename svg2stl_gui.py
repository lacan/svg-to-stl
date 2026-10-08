#!/usr/bin/env python3
"""
svg2stl web UI - a local-only server that previews the extrusions in the
browser and exports the STLs with the same code as the command line tool.

    svg2stl_gui.bat [file.svg] [--no-browser]

Opens http://127.0.0.1:<port>/ in the default browser. Nothing is sent
anywhere: the server only listens on localhost.
"""

import json
import mimetypes
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import svg2stl

WEB_DIR = Path(__file__).resolve().parent / "web"
mimetypes.add_type("text/javascript", ".js")

# load_shapes() is the slow part; cache it per (file, mtime, tolerance, scale)
_shape_cache = {}
_cache_lock = threading.Lock()


def get_shapes(path, tolerance, scale):
    key = (str(path), path.stat().st_mtime_ns, tolerance, scale)
    with _cache_lock:
        if key not in _shape_cache:
            _shape_cache.clear()
            _shape_cache[key] = svg2stl.load_shapes(path, tolerance / scale)
        return _shape_cache[key]


def geometry_params(req):
    path = Path(req.get("path", "")).expanduser()
    if not path.is_file():
        raise ValueError(f"file not found: {path}")
    if path.suffix.lower() != ".svg":
        raise ValueError("please choose an .svg file")
    scale = float(req.get("scale") or 1.0)
    width = float(req.get("width") or 0) or None
    tolerance = max(float(req.get("tolerance") or 0.05), 0.005)
    occlude = bool(req.get("occlude", True))
    shapes = get_shapes(path, tolerance, scale)
    groups, everything = svg2stl.prepare(path, scale, width, tolerance, occlude, shapes)
    return path, groups, everything


def pick_file(initial):
    """Native file dialog, run in a subprocess so Tk owns its own main thread."""
    code = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
        "p = filedialog.askopenfilename(title='Choose an SVG', initialdir=sys.argv[1] or None,\n"
        "    filetypes=[('SVG files', '*.svg'), ('All files', '*.*')])\n"
        "print(p or '', end='')\n"
    )
    init_dir = str(Path(initial).parent) if initial and Path(initial).parent.is_dir() else ""
    out = subprocess.run([sys.executable, "-c", code, init_dir],
                         capture_output=True, text=True, encoding="utf-8")
    return out.stdout.strip()


class Handler(BaseHTTPRequestHandler):
    initial_file = ""

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/initial":
            return self.send_json({"path": self.initial_file})
        if self.path.startswith("/api/svg?"):
            from urllib.parse import parse_qs, urlparse
            p = Path(parse_qs(urlparse(self.path).query).get("path", [""])[0])
            if p.suffix.lower() != ".svg" or not p.is_file():
                return self.send_error(404)
            return self.send_file(p, "image/svg+xml")
        rel = "index.html" if self.path in ("/", "") else self.path.lstrip("/").split("?")[0]
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR not in target.parents or not target.is_file():
            return self.send_error(404)
        self.send_file(target, mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    def send_file(self, path, ctype):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        # Only accept same-origin requests from the page we serve.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self.send_error(403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/pick":
                return self.send_json({"path": pick_file(req.get("current", ""))})
            if self.path == "/api/load":
                return self.send_json(self.load(req))
            if self.path == "/api/export":
                return self.send_json(self.export(req))
            if self.path == "/api/reveal":
                out = svg2stl.output_dir(Path(req.get("path", "")))
                if not out.is_dir():
                    raise ValueError(f"folder not found: {out}")
                os.startfile(out)
                return self.send_json({"ok": True})
            self.send_error(404)
        except Exception as e:  # report every failure to the page
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 400)

    def load(self, req):
        path, groups, everything = geometry_params(req)
        margin = float(req.get("margin") or 0)
        _, _, w, h = everything.bounds
        return {
            "path": str(path),
            "name": path.stem,
            "outDir": str(svg2stl.output_dir(path)),
            "size": [w, h],
            "colors": [{"hex": c, "area": g.area, "regions": len(g.geoms),
                        "polygons": svg2stl.polygons_json(g)} for c, g in groups.items()],
            "backplate": svg2stl.polygons_json(svg2stl.backplate_outline(everything, margin)),
        }

    def export(self, req):
        path, groups, everything = geometry_params(req)
        depths = {svg2stl.normalize_hex(c): float(d) for c, d in req.get("depths", {}).items()}
        lines = []
        written = svg2stl.write_stls(
            path, groups, everything, depths, default_depth=0.0,
            backplate=float(req.get("backplate") or 0) or None,
            backplate_margin=float(req.get("margin") or 0), log=lines.append)
        return {"files": [str(p) for p in written], "log": lines}


def main():
    args = [a for a in sys.argv[1:] if a != "--no-browser"]
    initial = os.path.abspath(args[0]) if args else ""
    Handler.initial_file = initial
    server = None
    for port in range(8765, 8800):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("no free port between 8765 and 8799")
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"svg2stl UI running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
