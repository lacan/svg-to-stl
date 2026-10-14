#!/usr/bin/env python3
"""
Assemble the static website (browser version of svg2stl) into _site/.

    python build_site.py            # build
    python build_site.py --serve    # build, then serve on http://127.0.0.1:8000/

The page (site/) runs svg2stl.py itself in the browser with Pyodide; it is
kept at the top level so it can also be imported and tested with plain Python.
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_site"


def build():
    # Empty the folder rather than deleting it: on Windows a folder that is
    # some process's working directory (e.g. a running server) can't be removed.
    OUT.mkdir(exist_ok=True)
    for old in OUT.iterdir():
        shutil.rmtree(old) if old.is_dir() else old.unlink()
    shutil.copytree(ROOT / "site", OUT, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "svg2stl.py", OUT / "svg2stl.py")
    (OUT / ".nojekyll").touch()  # serve files as-is on GitHub Pages
    print(f"built {OUT}")


if __name__ == "__main__":
    build()
    if "--serve" in sys.argv:
        import functools
        import webbrowser
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        class Handler(SimpleHTTPRequestHandler):
            def end_headers(self):
                self.send_header("Cache-Control", "no-store")  # always test the latest build
                super().end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 8000), functools.partial(Handler, directory=str(OUT)))
        print("serving http://127.0.0.1:8000/  (Ctrl+C to stop)")
        if "--no-browser" not in sys.argv:
            webbrowser.open("http://127.0.0.1:8000/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
