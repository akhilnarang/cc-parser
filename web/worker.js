/* Pyodide Web Worker for cc-parser.
 *
 * Loads the Python runtime, installs PDF/parsing packages,
 * mounts the cc_parser source tree, and exposes parse_pdf().
 *
 * Message protocol (main thread ↔ worker):
 *   → { type: "init" }                          start Pyodide
 *   ← { type: "status", text }                  progress updates
 *   ← { type: "ready" }                         init complete
 *   → { type: "parse", payload: { pdfBuffer, filename, password, bank } }
 *   ← { type: "result", data }                  parsed statement dict
 *   ← { type: "encrypted", text }               PDF needs a password
 *   ← { type: "error", text }                   unrecoverable error
 */

/* global importScripts, loadPyodide */

// Self-hosted Pyodide runtime on R2.
const PYODIDE_BASE = "https://files.akhilnarang.dev/cdn/pyodide/v0.29.3";
const PYODIDE_JS = `${PYODIDE_BASE}/pyodide.js`;

let pyodide = null;
let parsePdfFn = null; // cached Python callable

function post(type, extra) {
  self.postMessage(Object.assign({ type }, extra));
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

async function initPyodide() {
  // Guard against double init (e.g. if main thread retries after error).
  if (pyodide !== null) {
    post("ready");
    return;
  }

  post("status", { text: "Loading Python runtime..." });
  importScripts(PYODIDE_JS);
  pyodide = await loadPyodide({ indexURL: `${PYODIDE_BASE}/` });

  // Load Pyodide built-in packages (native WASM wheels) first.
  // pydantic + pydantic-core and Pillow are pre-built in Pyodide —
  // using loadPackage avoids version conflicts with micropip.
  post("status", { text: "Installing packages..." });
  await pyodide.loadPackage(["micropip", "pydantic", "Pillow"]);

  // Install pure-Python packages via micropip.
  // pdfplumber is installed with deps=False because it pins an exact
  // pdfminer.six version (older than what micropip resolves) and
  // depends on pypdfium2 (native C, no WASM wheel — not needed since
  // we never call page.to_image()).
  await pyodide.runPythonAsync(`
import micropip
await micropip.install(["pypdf", "pdfminer.six"])
await micropip.install("pdfplumber", deps=False)
`);

  // Mount cc_parser source files into the Pyodide virtual filesystem.
  post("status", { text: "Loading parser modules..." });
  await mountCcParser();

  // Pre-import and cache the parse function so first parse is faster.
  // Use .to_py() for proper Pyodide typed-array → Python bytes conversion
  // instead of bytes() which falls back to slow element-by-element iteration.
  parsePdfFn = pyodide.runPython(`
import sys, json
if "/home/pyodide" not in sys.path:
    sys.path.insert(0, "/home/pyodide")

from cc_parser.browser import parse_pdf as _parse_pdf

def _parse_wrapper(pdf_bytes, filename, password, bank):
    raw = pdf_bytes.to_py() if hasattr(pdf_bytes, 'to_py') else bytes(pdf_bytes)
    result = _parse_pdf(bytes(raw), filename, password or None, bank)
    return json.dumps(result, ensure_ascii=True)

_parse_wrapper
`);

  post("ready");
}

async function mountCcParser() {
  // Fetch the build-generated manifest instead of using a hardcoded file list.
  // In dev (serving from project root), manifest is at ../cc_parser_manifest.json.
  // In production (build.sh output), it's at ./cc_parser_manifest.json.
  let manifestUrl = "./cc_parser_manifest.json";
  let baseUrl = "./cc_parser";
  let resp = await fetch(manifestUrl);
  if (!resp.ok) {
    // Dev fallback: serving from project root, worker is at /web/worker.js
    manifestUrl = "../cc_parser_manifest.json";
    baseUrl = "../cc_parser";
    resp = await fetch(manifestUrl);
  }
  if (!resp.ok) throw new Error(`Failed to fetch manifest: ${resp.status}`);
  const files = await resp.json();

  // Create directory structure
  const dirs = new Set();
  for (const f of files) {
    const parts = f.split("/");
    for (let i = 1; i < parts.length; i++) {
      dirs.add("/home/pyodide/cc_parser/" + parts.slice(0, i).join("/"));
    }
  }
  dirs.add("/home/pyodide/cc_parser");
  for (const d of [...dirs].sort()) {
    try { pyodide.FS.mkdirTree(d); } catch {}
  }

  // Fetch and mount all files in parallel
  await Promise.all(files.map(async (relPath) => {
    const r = await fetch(`${baseUrl}/${relPath}`);
    if (!r.ok) throw new Error(`Failed to fetch ${baseUrl}/${relPath}: ${r.status}`);
    pyodide.FS.writeFile(`/home/pyodide/cc_parser/${relPath}`, await r.text());
  }));
}

// ---------------------------------------------------------------------------
// PDF parsing
// ---------------------------------------------------------------------------

function parsePdf(pdfBuffer, filename, password, bank) {
  const uint8 = new Uint8Array(pdfBuffer);
  const jsonStr = parsePdfFn(uint8, filename, password || "", bank);
  return JSON.parse(jsonStr);
}

// ---------------------------------------------------------------------------
// Message handler
// ---------------------------------------------------------------------------

self.onmessage = async function (event) {
  const { type, payload } = event.data;

  switch (type) {
    case "init":
      try {
        await initPyodide();
      } catch (err) {
        post("error", { text: `Initialisation failed: ${err.message}` });
      }
      break;

    case "parse":
      if (!parsePdfFn) {
        post("error", { text: "Runtime not ready yet." });
        return;
      }
      try {
        post("status", { text: "Parsing PDF..." });
        const result = parsePdf(
          payload.pdfBuffer,
          payload.filename,
          payload.password,
          payload.bank,
        );
        post("result", { data: result });
      } catch (err) {
        const msg = err.message || String(err);
        if (/Failed to decrypt/i.test(msg)) {
          post("wrong_password", { text: msg });
        } else if (/Password is required/i.test(msg) || /PDF is encrypted/i.test(msg)) {
          post("encrypted", { text: msg });
        } else {
          post("error", { text: msg });
        }
      }
      break;
  }
};
