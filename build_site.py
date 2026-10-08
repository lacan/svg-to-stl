#!/usr/bin/env python3
"""
Assemble the static website (browser version of svg2stl) into _site/.

    python build_site.py            # build
    python build_site.py --serve    # build, then serve on http://127.0.0.1:8000/

The page runs svg2stl.py itself in the browser (Pyodide), so the converter
code is shared with the command line tool; only the UI differs (site/).
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
    shutil.copytree(ROOT / "site", OUT, dirs_exist_ok=True)
    shutil.copy2(ROOT / "svg2stl.py", OUT / "svg2stl.py")
    for f in (ROOT / "web" / "vendor").iterdir():  # three.js
        shutil.copy2(f, OUT / "vendor" / f.name)
    (OUT / ".nojekyll").touch()  # serve files as-is on GitHub Pages
    print(f"built {OUT}")


if __name__ == "__main__":
    build()
    if "--serve" in sys.argv:
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(OUT))
        print("serving http://127.0.0.1:8000/  (Ctrl+C to stop)")
        ThreadingHTTPServer(("127.0.0.1", 8000), handler).serve_forever()
