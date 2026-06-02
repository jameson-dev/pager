#!/usr/bin/env bash
# pager production installer for Linux (systemd + CUPS).
#
# Supports Debian/Ubuntu (apt), Fedora/RHEL/CentOS (dnf), and Arch (pacman).
# Installs system deps, a venv, a service user, a systemd unit and logrotate,
# then starts the web UI on http://<host>:8080.
#
# Usage:   sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/pager
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# The PagerMon reader tees decoded pages here; the pager service reads them.
PAGER_MIRROR_LOG=/var/log/pagermon/multimon.log
PAGER_MIRROR_LOG_DIR="$(dirname "$PAGER_MIRROR_LOG")"

# --- run as root -------------------------------------------------------------
# We re-exec under sudo if not already root, so the script works whether the
# user typed `sudo bash ...` or just `bash ...`.
if [[ "${EUID}" -ne 0 ]]; then
  echo ">> Re-running with sudo..."
  exec sudo -E bash "$0" "$@"
fi

# --- detect package manager --------------------------------------------------
PKG=""
for cand in apt-get dnf yum pacman; do
  if command -v "$cand" >/dev/null 2>&1; then PKG="$cand"; break; fi
done
if [[ -z "$PKG" ]]; then
  echo "!! No supported package manager found (apt-get/dnf/yum/pacman)." >&2
  echo "   Install these manually, then re-run: python3 venv, CUPS dev headers," >&2
  echo "   poppler-utils, gcc." >&2
  exit 1
fi
echo ">> Using package manager: $PKG"

echo ">> Installing system dependencies (Python venv, CUPS, poppler, build tools)..."
case "$PKG" in
  apt-get)
    apt-get update
    apt-get install -y python3 python3-venv python3-dev cups poppler-utils libcups2-dev gcc
    ;;
  dnf|yum)
    "$PKG" install -y python3 python3-virtualenv python3-devel cups cups-devel poppler-utils gcc
    ;;
  pacman)
    pacman -Sy --noconfirm python cups poppler gcc
    ;;
esac

# poppler-utils provides pdftoppm, which pdf2image needs for template previews.
if ! command -v pdftoppm >/dev/null 2>&1; then
  echo "!! WARNING: pdftoppm not found after install — the PDF template preview"
  echo "   image will be unavailable. Install the poppler-utils/poppler package."
else
  echo ">> poppler OK ($(pdftoppm -v 2>&1 | head -1))"
fi

# --- service user (member of the printer group) ------------------------------
# CUPS uses the 'lp' group on Debian/Fedora; some distros call it 'sys'. We add
# whichever exists so the service can submit print jobs.
PRINT_GROUP=""
for g in lp sys; do
  if getent group "$g" >/dev/null 2>&1; then PRINT_GROUP="$g"; break; fi
done

echo ">> Creating service user 'pager'..."
# nologin lives in different places across distros (/usr/sbin vs /sbin).
NOLOGIN="$(command -v nologin || true)"
for cand in /usr/sbin/nologin /sbin/nologin; do
  [[ -n "$NOLOGIN" ]] && break
  [[ -x "$cand" ]] && NOLOGIN="$cand"
done
NOLOGIN="${NOLOGIN:-/bin/false}"
id -u pager &>/dev/null || useradd --system --home "$APP_DIR" --shell "$NOLOGIN" pager
if [[ -n "$PRINT_GROUP" ]]; then
  usermod -aG "$PRINT_GROUP" pager   # allow printing via CUPS
  echo ">> Added pager to '$PRINT_GROUP' group for printing."
fi

echo ">> Copying app to $APP_DIR..."
mkdir -p "$APP_DIR"
cp -r "$REPO_DIR/app" "$APP_DIR/"
# Don't clobber an admin's edited config on re-install; seed it once.
if [[ ! -d "$APP_DIR/config" ]]; then
  cp -r "$REPO_DIR/config" "$APP_DIR/"
else
  echo ">> Existing $APP_DIR/config kept (not overwritten)."
fi
mkdir -p "$APP_DIR/data/jobs"
# The mirror log dir (/var/log/pagermon) is owned/permissioned later, when we
# know which user runs the PagerMon reader (it writes the log; pager reads it).
mkdir -p "$PAGER_MIRROR_LOG_DIR"

echo ">> Creating virtualenv and installing Python deps..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo ">> Setting ownership..."
chown -R pager:pager "$APP_DIR"
# NOTE: /var/log/pagermon is intentionally NOT chowned to pager here — the
# PagerMon reader (a different user) needs to write it. Ownership/permissions
# for the mirror log are set when we patch the reader (see patch_reader), so
# the writer can create it and pager can still read it.

echo ">> Installing systemd unit and logrotate config..."
cp "$REPO_DIR/deploy/pager.service" /etc/systemd/system/
# Point the unit's print group at whatever this distro uses.
if [[ -n "$PRINT_GROUP" ]]; then
  sed -i "s/^SupplementaryGroups=.*/SupplementaryGroups=$PRINT_GROUP/" /etc/systemd/system/pager.service
fi
cp "$REPO_DIR/deploy/logrotate.conf" /etc/logrotate.d/pagermon-multimon
systemctl daemon-reload
systemctl enable --now pager

echo
echo ">> Done. Web UI: http://<this-host>:8080"
echo ">> Service status:  systemctl status pager"
echo ">> Logs:            journalctl -u pager -f"

# --- offer to patch the PagerMon reader.sh -----------------------------------
# pager only reads the mirror log; PagerMon must `tee` multimon-ng's output to
# it. We can add that line to the user's reader.sh automatically (with a backup),
# but only when we can actually prompt — a piped/non-interactive run is skipped.

# Make the mirror log writable by the user that runs the reader, and readable by
# pager. The reader runs as whoever owns reader.sh; we chown the dir+file to that
# user with group=pager and mode 0750 (writer: rwx, pager: r-x via group).
setup_mirror_log_perms() {
  local reader="$1" owner
  owner="$(stat -c '%U' "$reader" 2>/dev/null || true)"
  if [[ -z "$owner" || "$owner" == "UNKNOWN" ]]; then
    echo "!! Couldn't determine the owner of $reader; leaving $PAGER_MIRROR_LOG_DIR as-is." >&2
    echo "   If the reader can't write the log, run:" >&2
    echo "       sudo chown -R <reader-user>:pager $PAGER_MIRROR_LOG_DIR" >&2
    return 1
  fi
  mkdir -p "$PAGER_MIRROR_LOG_DIR"
  touch "$PAGER_MIRROR_LOG"                       # create now so tee -a + reads work immediately
  chown "$owner:pager" "$PAGER_MIRROR_LOG_DIR" "$PAGER_MIRROR_LOG"
  chmod 0750 "$PAGER_MIRROR_LOG_DIR"             # owner rwx, group (pager) r-x, others none
  chmod 0640 "$PAGER_MIRROR_LOG"                 # owner rw, group (pager) r, others none
  echo ">> Mirror log owned by '$owner' (writer) with group 'pager' (reader): $PAGER_MIRROR_LOG"
}

patch_reader() {
  local reader="$1"

  if [[ ! -f "$reader" ]]; then
    echo "!! $reader not found — skipping. Add the tee line by hand (see reader/reader.sh)." >&2
    return 1
  fi

  # Idempotent: if it already mirrors to our log, there's nothing to do.
  if grep -qF "tee -a $PAGER_MIRROR_LOG" "$reader"; then
    echo ">> $reader already mirrors to $PAGER_MIRROR_LOG — no change needed."
    return 0
  fi

  # Find the line that *runs* PagerMon's reader.js. It's invoked as `node
  # reader.js`, `./reader.js`, an absolute path, etc.; we insert a tee just
  # before it so multimon-ng's output is mirrored, then forwarded on. Skip
  # comment lines (a stock reader.sh documents the chain in `# node reader.js`
  # comments — matching those would patch the wrong place).
  local anchor
  anchor="$(grep -nE 'reader\.js' "$reader" | grep -vE '^[0-9]+:[[:space:]]*#' | head -1 | cut -d: -f1)"
  if [[ -z "$anchor" ]]; then
    echo "!! Couldn't find a line running 'reader.js' in $reader." >&2
    echo "   Add a tee into the pipeline by hand (see reader/reader.sh)." >&2
    return 1
  fi

  local backup="$reader.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$reader" "$backup"
  echo ">> Backed up original to $backup"

  # Insert a tee just before the reader.js line, matching the pipeline style:
  #   - reader.js line starts with `|`  (leading-pipe / backslash-continued):
  #         | tee -a LOG \
  #   - otherwise the previous line carried a trailing `|`:
  #         tee -a LOG |
  # We pass the line number (not a regex) to awk so nothing needs escaping.
  local tmp="$reader.pager.tmp"
  if awk -v at="$anchor" -v mlog="$PAGER_MIRROR_LOG" '
        NR == at {
          if ($0 ~ /^[[:space:]]*\|/) print "  | tee -a " mlog " \\"
          else                        print "tee -a " mlog " |"
        }
        { print }
      ' "$reader" > "$tmp" && bash -n "$tmp" 2>/dev/null; then
    cat "$tmp" > "$reader"   # preserve original perms/owner; just rewrite content
    rm -f "$tmp"
  else
    echo "!! Patch produced a broken script; left $reader untouched (backup: $backup)." >&2
    rm -f "$tmp"
    return 1
  fi

  # Make sure the reader's user can write the log and pager can read it.
  setup_mirror_log_perms "$reader" || true
  echo ">> Added mirror line to $reader. Restart PagerMon's reader to apply."
  return 0
}

READER_PATCHED=0
DEFAULT_READER=/opt/pagermon/reader/reader.sh
if [[ -t 0 ]]; then
  echo
  read -r -p ">> Patch your PagerMon reader.sh to mirror pages to $PAGER_MIRROR_LOG? [y/N] " reply
  if [[ "$reply" =~ ^[Yy] ]]; then
    read -r -p "   Path to reader.sh [$DEFAULT_READER]: " reader_path
    reader_path="${reader_path:-$DEFAULT_READER}"
    patch_reader "$reader_path" && READER_PATCHED=1
  fi
else
  echo ">> Non-interactive run: skipping reader.sh patch (edit it by hand — see reader/reader.sh)."
fi

# Open the UI port on firewalld-based distros (Fedora/RHEL), which block it by
# default. Best-effort: skip silently if firewalld isn't in use.
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state &>/dev/null; then
  echo ">> Opening port 8080 in firewalld..."
  firewall-cmd --permanent --add-port=8080/tcp >/dev/null && firewall-cmd --reload >/dev/null || true
fi

echo
echo ">> Next steps:"
echo "   1. Put your template PDF at $APP_DIR/config/template.pdf (or set path in Settings)."
if [[ "$READER_PATCHED" -eq 1 ]]; then
  echo "   2. Restart your PagerMon reader so the new 'tee' line takes effect."
else
  echo "   2. Edit your PagerMon reader.sh to add the 'tee -a $PAGER_MIRROR_LOG' line"
  echo "      (see reader/reader.sh in this repo for the exact change)."
fi
echo "   3. Add a network printer + capcodes in the web UI."
