// Runs svg2stl (Python) in the browser with Pyodide, off the main thread.
// Messages in:  {id, cmd: "load" | "export", svg?, name?, req}
// Messages out: {type: "progress", msg} | {type: "ready"} | {id, result} | {id, error} | {id, skipped}

// Loaded as a module worker: new Worker("worker.js", { type: "module" }).
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";

const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";
const SVGELEMENTS_WHEEL = "vendor/svgelements-1.9.6-py2.py3-none-any.whl";

const progress = (msg) => postMessage({ type: "progress", msg });
let py, api;

async function init() {
  progress("Downloading the converter…");
  py = await loadPyodide({ indexURL: PYODIDE_URL });
  progress("Loading geometry libraries…");
  await py.loadPackage(["numpy", "shapely", new URL(SVGELEMENTS_WHEEL, self.location).href]);
  py.FS.mkdirTree("/app");
  for (const f of ["svg2stl.py", "web_api.py"]) {
    const res = await fetch(f, { cache: "no-cache" });
    if (!res.ok) throw new Error(`could not fetch ${f} (HTTP ${res.status})`);
    py.FS.writeFile("/app/" + f, await res.text());
  }
  py.runPython("import sys; sys.path.insert(0, '/app')");
  api = py.pyimport("web_api");
  postMessage({ type: "ready" });
}
const ready = init();
ready.catch((e) => postMessage({ type: "fatal", msg: String(e.message || e) }));

let latestLoad = 0;
onmessage = async ({ data }) => {
  const { id, cmd } = data;
  try {
    await ready;
    if (cmd === "load") {
      latestLoad = id;
      // Let queued messages arrive; only compute the newest preview request.
      await new Promise((r) => setTimeout(r, 0));
      if (id !== latestLoad && data.svg == null) return postMessage({ id, skipped: true });
      if (data.svg != null) api.set_svg(data.svg, data.name);
      const result = JSON.parse(api.load(JSON.stringify(data.req)));
      if (result.error) throw new Error(result.error);
      postMessage({ id, result });
    } else if (cmd === "export") {
      const result = JSON.parse(api.export(JSON.stringify(data.req)));
      if (result.error) throw new Error(result.error);
      const zip = py.FS.readFile(result.zip);
      postMessage({ id, result, zip }, [zip.buffer]);
    } else {
      throw new Error("unknown command " + cmd);
    }
  } catch (e) {
    postMessage({ id, error: String(e.message || e).trim().split("\n").pop() });
  }
};
