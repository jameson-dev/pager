"""CUPS printer discovery and printing.

pycups talks to the local CUPS daemon. The Linux box should have CUPS installed
(`sudo apt install cups`) and the network printer added to CUPS (the web UI's
discovery uses CUPS' own backend enumeration, which finds network printers).
"""
from __future__ import annotations

import logging

log = logging.getLogger("pager.printing")

try:
    import cups  # type: ignore
    _HAVE_CUPS = True
except ImportError:  # e.g. when developing on Windows/macOS
    cups = None  # type: ignore
    _HAVE_CUPS = False


def cups_available() -> bool:
    return _HAVE_CUPS


def list_printers() -> list[dict]:
    """Installed CUPS print queues."""
    if not _HAVE_CUPS:
        return []
    try:
        conn = cups.Connection()
        printers = conn.getPrinters()
        return [
            {
                "name": name,
                "info": p.get("printer-info", ""),
                "location": p.get("printer-location", ""),
                "state": p.get("printer-state", 0),
                "uri": p.get("device-uri", ""),
            }
            for name, p in printers.items()
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to list printers: %s", exc)
        return []


def discover_network_printers() -> list[dict]:
    """
    Ask CUPS to enumerate available devices (network + USB) that are NOT yet
    set up as queues. Lets the UI add a printer it finds on the network.
    """
    if not _HAVE_CUPS:
        return []
    try:
        conn = cups.Connection()
        devices = conn.getDevices()
        out = []
        for uri, dev in devices.items():
            out.append(
                {
                    "uri": uri,
                    "info": dev.get("device-info", ""),
                    "make_model": dev.get("device-make-and-model", ""),
                    "device_class": dev.get("device-class", ""),
                }
            )
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("Device discovery failed: %s", exc)
        return []


def add_printer(name: str, uri: str, ppd_make_model: str | None = None) -> tuple[bool, str]:
    """Create a CUPS queue for a discovered network printer."""
    if not _HAVE_CUPS:
        return False, "CUPS not available on this host"
    try:
        conn = cups.Connection()
        # Use a generic PostScript/everywhere driver if no PPD chosen.
        conn.addPrinter(name, device=uri, info=name)
        conn.enablePrinter(name)
        conn.acceptJobs(name)
        return True, "Printer added"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def print_pdf(printer_name: str, pdf_path: str, title: str = "PagerMon Job") -> tuple[bool, str | None]:
    """
    Print a PDF to the named CUPS printer (or system default if empty).
    Returns (success, error_message).
    """
    if not _HAVE_CUPS:
        return False, "CUPS not available on this host"
    try:
        conn = cups.Connection()
        target = printer_name or conn.getDefault()
        if not target:
            return False, "No printer specified and no system default set"
        conn.printFile(target, pdf_path, title, {})
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
