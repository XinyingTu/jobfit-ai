#!/usr/bin/env python3
"""
Daily scan entry point — called by cron at 9 am.

Loads scan_config.json, runs the scan pipeline, then sends an email
digest of newly appeared high-score jobs via notify.py.

Usage:
    python daily_scan.py               # uses scan_config.json
    python daily_scan.py --notify-only # skip scan, just (re-)send digest
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "scan_config.json"
VENV_PYTHON = Path(__file__).parent / ".venv" / "bin" / "python"


def _load_dotenv() -> None:
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _python() -> str:
    """Return the venv python if present, else fall back to sys.executable."""
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def main() -> int:
    _load_dotenv()

    notify_only = "--notify-only" in sys.argv

    if not CONFIG_FILE.exists():
        print(f"[daily_scan] {CONFIG_FILE} not found. Copy and fill in scan_config.json first.")
        return 1

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    scan_cfg = config.get("scan", {})

    # ── Run scan ───────────────────────────────────────────────────
    if not notify_only:
        cmd = [_python(), str(Path(__file__).parent / "main.py"), "scan"]

        if scan_cfg.get("role"):
            cmd += ["--role", scan_cfg["role"]]
        if scan_cfg.get("sources"):
            cmd += ["--sources", scan_cfg["sources"]]
        if scan_cfg.get("threshold"):
            cmd += ["--threshold", str(scan_cfg["threshold"])]
        if scan_cfg.get("location"):
            cmd += ["--location", scan_cfg["location"]]
        if scan_cfg.get("work_model"):
            cmd += ["--work-model", scan_cfg["work_model"]]
        if scan_cfg.get("resume_pdf"):
            cmd += ["--resume-pdf", scan_cfg["resume_pdf"]]

        print(f"[daily_scan] {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(Path(__file__).parent))
        if result.returncode != 0:
            print(f"[daily_scan] Scan exited with code {result.returncode} — aborting notification.")
            return result.returncode

    # ── Send notification ──────────────────────────────────────────
    # Import here so dotenv is loaded first
    sys.path.insert(0, str(Path(__file__).parent))
    from notify import send_digest
    send_digest(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
