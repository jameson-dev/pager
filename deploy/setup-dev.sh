#!/usr/bin/env bash
# pager local / non-root setup for Linux (and macOS).
#
# Creates a virtualenv in ./.venv, installs the Python dependencies, makes a
# writable local config + data dir, and prints how to run the web UI. No root,
# no systemd, no service user — for development or a quick manual run.
#
# System packages (poppler for PDF previews, optionally CUPS for printing) are
# NOT auto-installed here; this script will tell you what's missing and how to
# get it for your distro. Printing degrades gracefully if CUPS is absent.
#
# Usage:   bash deploy/setup-dev.sh
#          source .venv/bin/activate
#          PAGER_CONFIG="$PWD/config_local" uvicorn app.main:app --reload --port 8080
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

VENV_DIR="$REPO_DIR/.venv"
CONFIG_SRC="$REPO_DIR/config"
CONFIG_LOCAL="$REPO_DIR/config_local"
DATA_DIR="$REPO_DIR/data"

# --- python ------------------------------------------------------------------
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "!! python3 not found. Install Python 3.10+ and re-run (set PYTHON=... to override)." >&2
  exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo ">> Using $PY (Python $PYVER)"
# The app uses 3.10+ syntax (e.g. `str | None` unions); a lower version builds a
# venv fine but crashes at import. Fail early with a clear message instead.
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)'; then
  echo "!! Python 3.10+ is required (found $PYVER). Install a newer Python and" >&2
  echo "   re-run, e.g. PYTHON=python3.12 bash deploy/setup-dev.sh" >&2
  exit 1
fi

# --- system dep advisories (poppler / cups) ----------------------------------
suggest_install() {
  # $1 = human label, $2 = apt pkg, $3 = dnf pkg, $4 = pacman pkg
  if command -v apt-get >/dev/null 2>&1;   then echo "     sudo apt-get install -y $2"
  elif command -v dnf >/dev/null 2>&1;     then echo "     sudo dnf install -y $3"
  elif command -v pacman >/dev/null 2>&1;  then echo "     sudo pacman -S --noconfirm $4"
  elif command -v brew >/dev/null 2>&1;    then echo "     brew install ${5:-$1}"
  else echo "     (install '$1' with your package manager)"; fi
}

if ! command -v pdftoppm >/dev/null 2>&1; then
  echo "!! poppler (pdftoppm) not found — PDF template preview images will be unavailable:"
  suggest_install poppler poppler-utils poppler-utils poppler poppler
else
  echo ">> poppler OK ($(pdftoppm -v 2>&1 | head -1))"
fi

if ! command -v lp >/dev/null 2>&1 && ! command -v lpstat >/dev/null 2>&1; then
  echo "!! CUPS client not found — printing will be disabled (the app still runs):"
  suggest_install cups cups cups cups cups
fi

# --- virtualenv + deps -------------------------------------------------------
echo ">> Creating virtualenv at $VENV_DIR ..."
"$PY" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip

# pycups (in requirements.txt) needs CUPS dev headers to build. If they're not
# present, fall back to the Windows requirements file which omits pycups so the
# install still succeeds; printing is simply unavailable.
REQ="$REPO_DIR/requirements.txt"
if ! command -v cups-config >/dev/null 2>&1; then
  echo ">> CUPS dev headers (cups-config) not found — installing without pycups."
  echo "   (Printing disabled. Install libcups2-dev / cups-devel and re-run for printing.)"
  REQ="$REPO_DIR/requirements.windows.txt"
fi
echo ">> Installing Python deps from $(basename "$REQ") ..."
"$VENV_DIR/bin/pip" install -r "$REQ"

# --- local writable config + data --------------------------------------------
mkdir -p "$DATA_DIR/jobs"
if [[ ! -d "$CONFIG_LOCAL" ]]; then
  echo ">> Seeding writable config at $CONFIG_LOCAL ..."
  cp -r "$CONFIG_SRC" "$CONFIG_LOCAL"

  # Rewrite the production /opt and /var paths to repo-local writable ones so a
  # fresh clone runs with no manual editing. Uses the venv's PyYAML (just
  # installed) to avoid clobbering structure or comments-free round-tripping.
  echo ">> Pointing config_local paths at the local repo ..."
  REPO_DIR="$REPO_DIR" CONFIG_LOCAL="$CONFIG_LOCAL" DATA_DIR="$DATA_DIR" \
  "$VENV_DIR/bin/python" - <<'PY'
import os
from pathlib import Path
import yaml

cfg_path = Path(os.environ["CONFIG_LOCAL"]) / "config.yaml"
repo = Path(os.environ["REPO_DIR"])
data = Path(os.environ["DATA_DIR"])
local = Path(os.environ["CONFIG_LOCAL"])

conf = yaml.safe_load(cfg_path.read_text()) or {}
conf["log_file"] = str(data / "multimon.log")          # tail this; create it yourself
conf["output_dir"] = str(data / "jobs")
conf["database"] = str(data / "jobs.db")
conf["template_pdf"] = str(local / "template.pdf")
conf["templates"] = [{"name": "Default", "path": str(local / "template.pdf")}]
conf["active_template"] = "Default"
# Drop any committed session secret so this clone generates its own on first use
# (app.auth creates a fresh random secret when absent).
if isinstance(conf.get("auth"), dict):
    conf["auth"].pop("secret", None)
    if not conf["auth"]:
        conf.pop("auth")
cfg_path.write_text(yaml.safe_dump(conf, sort_keys=False, default_flow_style=False))
print(f"   wrote {cfg_path}")
PY
  # Make sure the watched log exists so the watcher has something to tail.
  touch "$DATA_DIR/multimon.log"
else
  echo ">> Existing $CONFIG_LOCAL kept (not overwritten)."
fi

cat <<EOF

>> Done. To run the web UI locally:

   source .venv/bin/activate
   export PAGER_CONFIG="$CONFIG_LOCAL"
   uvicorn app.main:app --reload --port 8080

   Then open http://localhost:8080

   Notes:
   - config_local paths were pointed at ./data (log_file, output_dir, database)
     so it runs self-contained. Feed it by appending lines to data/multimon.log,
     or via Jobs page -> Send test page.
   - Put a template PDF at $CONFIG_LOCAL/template.pdf (or set its path in Settings)
     to enable PDF stamping/preview.
   - Run the tests with: .venv/bin/python tests/test_pipeline.py
EOF
