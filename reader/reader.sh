#!/bin/bash
# Modified PagerMon reader.sh
# -----------------------------------------------------------------------------
# This is the standard PagerMon reader chain with ONE addition: the raw
# multimon-ng output is `tee`'d to a flat log file that pager watches.
#
# Adjust the rtl_fm device/frequency and the multimon-ng decoder to match your
# existing PagerMon setup. The only pager-specific line is the `tee`.
# -----------------------------------------------------------------------------

set -euo pipefail

# --- pager: where to mirror decoded lines ------------------------------------
PAGER_LOG="${PAGER_LOG:-/var/log/pagermon/multimon.log}"
mkdir -p "$(dirname "$PAGER_LOG")"

# --- your existing PagerMon reader settings ----------------------------------
RTL_DEVICE="${RTL_DEVICE:-0}"
FREQ="${FREQ:-148.5875M}"
SAMPLE_RATE="${SAMPLE_RATE:-22050}"
DECODER="${DECODER:-POCSAG512 -a POCSAG1200 -a POCSAG2400}"

# rtl_fm -> multimon-ng -> tee(log) -> PagerMon reader.js
rtl_fm -d "$RTL_DEVICE" -E dc -F 0 -A fast -f "$FREQ" -s "$SAMPLE_RATE" - \
  | multimon-ng -q -b1 -c -a $DECODER -f alpha -t raw /dev/stdin \
  | tee -a "$PAGER_LOG" \
  | node /opt/pagermon/reader/reader.js
