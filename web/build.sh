#!/usr/bin/env bash
# Build a self-contained dist/ folder for static hosting.
# Usage: bash web/build.sh
# Usage: bash web/build.sh --dev-manifest   (generate manifest for local dev only)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"

if [[ "${1:-}" == "--dev-manifest" ]]; then
  (cd "$ROOT/cc_parser" && find . -name '*.py' ! -name 'cli.py' ! -name 'extractor.py' | sed 's|^\./||' | sort | python3 -c "
import sys, json
print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))
") > "$ROOT/cc_parser_manifest.json"
  echo "Generated cc_parser_manifest.json for local dev"
  exit 0
fi

rm -rf "$DIST"
mkdir -p "$DIST/cc_parser/parsers"

# Copy web assets
cp "$ROOT/web/index.html" "$ROOT/web/app.js" "$ROOT/web/storage.js" "$DIST/"

# Copy worker.js with adjusted fetch path (../cc_parser → ./cc_parser)
sed 's|\.\./cc_parser|./cc_parser|g' "$ROOT/web/worker.js" > "$DIST/worker.js"

# Copy only the Python files the browser path needs (no cli.py, no extractor.py)
cp "$ROOT/cc_parser/__init__.py" "$ROOT/cc_parser/browser.py" "$DIST/cc_parser/"
cp "$ROOT/cc_parser/parsers/"*.py "$DIST/cc_parser/parsers/"

# Generate manifest of all .py files for the Pyodide worker
(cd "$DIST/cc_parser" && find . -name '*.py' | sed 's|^\./||' | sort | python3 -c "
import sys, json
print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))
") > "$DIST/cc_parser_manifest.json"

# Also generate manifest at project root for local dev serving
(cd "$ROOT/cc_parser" && find . -name '*.py' ! -name 'cli.py' ! -name 'extractor.py' | sed 's|^\./||' | sort | python3 -c "
import sys, json
print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))
") > "$ROOT/cc_parser_manifest.json"

# COOP/COEP headers for Pyodide SharedArrayBuffer support
cat > "$DIST/_headers" << 'HEADERS'
/*
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: credentialless
HEADERS

echo "Built → $DIST/ ($(find "$DIST" -type f | wc -l) files)"
echo "Deploy: drag-drop dist/ to Cloudflare Pages, or run:"
echo "  npx wrangler pages deploy dist/"
echo ""
echo "For local dev, run: bash web/build.sh --dev-manifest"
