# Pager

Monitors a PagerMon / multimon-ng log file, extracts fields from pages on
selected capcodes, stamps them onto a template PDF at configurable positions,
and optionally prints to a network printer via CUPS. Includes a web UI for the
PDF layout, parsing rules, capcodes, printing toggles, and job history.

## How it works

```
rtl_fm → multimon-ng → tee → /var/log/pagermon/multimon.log
                                       │
                                  LogWatcher (tail -F)
                                       │
                 parse "Address: N  Alpha: TEXT"  →  capcode filter
                                       │
                  regex rules → fields {jobtype,address,mapref,units,…}
                                       │
            template.pdf + layout.json → overlay → data/jobs/job_*.pdf
                                       │
        print if (global ON) AND (capcode ON) AND (jobtype ON) → CUPS
                                       │
                              SQLite job history
```

## Install (Linux)

### Production (systemd service)

```bash
sudo bash deploy/install.sh
```

Works on **Debian/Ubuntu (apt), Fedora/RHEL/CentOS (dnf/yum), and Arch
(pacman)** — the script auto-detects your package manager and installs all
system dependencies (Python venv, CUPS, poppler, build tools). It then creates a
`pager` service user (added to the printer group for CUPS), installs Python
deps into a venv at `/opt/pager/venv`, installs a systemd unit + logrotate
config, and starts the web UI on **http://<host>:8080**.

Re-running is safe: an existing `/opt/pager/config` is preserved. Manage the
service with `systemctl status pager` and `journalctl -u pager -f`.

### Local / non-root (development or manual run)

```bash
bash deploy/setup-dev.sh
```

No root, no systemd. Creates `./.venv`, installs the Python deps (falling back
to a no-CUPS install if printing libraries aren't present), seeds a writable
`config_local/`, and prints the `uvicorn` command to start the UI. It also tells
you the exact package to install for poppler/CUPS on your distro if they're
missing.

### Hook into PagerMon

Add the `tee` line to your existing PagerMon `reader.sh` so multimon-ng's output
is mirrored to the watched log. See [reader/reader.sh](reader/reader.sh) — the
only added piece is:

```bash
| tee -a /var/log/pagermon/multimon.log \
```

Pager and PagerMon run side by side; Pager only *reads* the mirror file,
so your normal PagerMon server is unaffected.

## Web UI

| Page | What it does |
|------|--------------|
| **Jobs** | Every received page, open the PDF, **Reprint** any job. Auto-refreshes. |
| **PDF Layout** | Drag fields onto a preview of your template to set X/Y; set font/size/wrap width. |
| **Parsing Rules** | Define regex (named groups) that split messages into fields; test against a real page live. |
| **Settings & Printing** | Global print toggle, printer discovery/selection, per-capcode (+copies) and per-job-type print toggles, templates, alerts/watchdog, retention, paths. |

### Live alerts, watchdog & reliability

- **New-job alerts** — the Jobs page holds a live SSE connection; new pages appear
  instantly with an optional **sound + browser notification**. Toggle per browser
  (top of Jobs page); the server sets the default (`alert_enabled_default`).
- **Feed watchdog** — the header shows a `feed ok / feed STALE` pill. If the SDR /
  multimon-ng stops producing output for `watchdog_stale_seconds`, it goes red so a
  silently-dead decoder doesn't go unnoticed. `GET /api/health` exposes the detail.
- **Print failure + retry** — failed prints are flagged, a red banner shows the count,
  a background worker retries every 60s (up to 5 attempts), and **Retry all now** forces
  an immediate pass. Reprint still works per-job.
- **Test page** — inject a synthetic page (Jobs page → *Send test page*) to verify
  parsing/printing without waiting for a real dispatch.
- **PDF preview** — Layout page → *Open preview PDF* renders the current layout + rules +
  active template against a sample message, without storing or printing.
- **Templates** — define multiple template PDFs in Settings and pick the active one.
- **Retention** — `retention_days` auto-deletes old PDFs/rows daily; **Run cleanup now**
  triggers it on demand.

## Fields

Built-in fields always available for placement:
`date`, `time`, `datetime`, `capcode`, `message` (full raw text).

Any named group from your parsing rules (e.g. `jobtype`, `address`, `mapref`,
`units`, `crossstreet`, `details`) also becomes a placeable field.

## Printing model

A job prints only if **all three** are enabled:
1. Global printing (header pill / Settings toggle)
2. The job's **capcode** (Settings → Capcodes)
3. The job's **job type** (Settings → Job-type rules; unlisted types default to ON)

**Reprint** from the Jobs page bypasses these gates (manual action).

## Configuration files (`config/`)

- `config.yaml` — paths, capcodes, printer, global toggle, jobtype toggles
- `rules.yaml` — ordered regex extraction rules
- `layout.json` — field positions/fonts (edited via the Layout page)
- `template.pdf` — **you provide this**; the PDF text is stamped onto

## Local development (non-Linux)

CUPS is optional (printing is disabled and degrades gracefully). **poppler** is
needed for the PDF template-preview image; the Debian installer apt-installs it
automatically (`poppler-utils`), and on Windows the easiest way is:

```powershell
scoop install poppler      # or: choco install poppler
```

If poppler isn't on `PATH`, set its `bin` directory via the `poppler_path`
config key (or Settings → Paths). The app auto-detects common Windows install
locations too. To run the UI locally:

```bash
pip install -r requirements.txt
set PAGER_CONFIG=%CD%\config        # Windows
uvicorn app.main:app --reload --port 8080
```

## Tests

```bash
python tests/test_pipeline.py
```

Covers POCSAG parsing, rule extraction, PDF rendering, and print gating.
