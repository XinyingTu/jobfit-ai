#!/usr/bin/env python3
"""Send an HTML email digest of new high-score jobs since the last notification."""

import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

LAST_NOTIFIED_FILE = Path("last_notified.json")
LAST_RUN_FILE = Path("last_run.json")
CONFIG_FILE = Path("scan_config.json")


def _load_notified_urls() -> set:
    if not LAST_NOTIFIED_FILE.exists():
        return set()
    return set(json.loads(LAST_NOTIFIED_FILE.read_text(encoding="utf-8")).get("urls", []))


def _save_notified_urls(urls: set) -> None:
    LAST_NOTIFIED_FILE.write_text(
        json.dumps({"urls": sorted(urls), "updated": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )


def _score_color(score: int) -> str:
    if score >= 80:
        return "#10b981"
    if score >= 60:
        return "#f59e0b"
    return "#ef4444"


def _build_email_html(jobs: list, threshold: int) -> str:
    now = datetime.now()
    date_str = now.strftime(f"%B {now.day}, %Y")

    cards = ""
    for job in jobs:
        score = job.get("score", 0)
        color = _score_color(score)
        loc = job.get("location", "")
        wm = job.get("work_model", "")
        meta_parts = [p for p in [loc, wm] if p]
        meta = " &nbsp;·&nbsp; ".join(meta_parts)
        reason = job.get("reason", "")
        link = job.get("link", "#")

        cards += f"""
    <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;
                padding:20px 22px;margin-bottom:14px;">
      <div style="display:flex;align-items:center;gap:14px;margin-bottom:8px;">
        <span style="font-size:24px;font-weight:800;color:{color};min-width:32px;">{score}</span>
        <div>
          <div style="font-weight:700;font-size:15px;color:#0f172a;line-height:1.3;">
            {job.get('job_title', '')}
          </div>
          <div style="color:#64748b;font-size:13px;margin-top:2px;">
            {job.get('company_name', '')}
          </div>
        </div>
      </div>
      {f'<div style="font-size:12px;color:#94a3b8;margin-bottom:8px;">{meta}</div>' if meta else ''}
      {f'<div style="font-size:13px;color:#475569;margin-bottom:12px;border-left:3px solid {color};padding-left:10px;">{reason}</div>' if reason else ''}
      <a href="{link}"
         style="display:inline-block;padding:8px 18px;background:#0f172a;color:#fff;
                border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;">
        Apply →
      </a>
    </div>"""

    count = len(jobs)
    label = "match" if count == 1 else "matches"
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             background:#f1f5f9;margin:0;padding:32px 16px;">
  <div style="max-width:620px;margin:0 auto;">
    <div style="background:#0f172a;border-radius:12px;padding:24px 28px;margin-bottom:20px;">
      <div style="font-size:20px;font-weight:800;color:#f8fafc;">
        Job<span style="color:#3b82f6;">Agent</span> Daily Digest
      </div>
      <div style="color:#94a3b8;font-size:13px;margin-top:6px;">
        {count} new {label} scoring &ge; {threshold} &nbsp;&middot;&nbsp; {date_str}
      </div>
    </div>
    {cards}
    <div style="text-align:center;color:#94a3b8;font-size:11px;margin-top:20px;">
      To stop these emails, set &ldquo;enabled&rdquo;: false in scan_config.json
    </div>
  </div>
</body>
</html>"""


def send_digest(config: Optional[dict] = None) -> int:
    """Detect new high-score jobs and email a digest. Returns count of jobs sent."""
    if config is None:
        if not CONFIG_FILE.exists():
            print("[notify] scan_config.json not found — skipping.")
            return 0
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    notify_cfg = config.get("notify", {})
    if not notify_cfg.get("enabled", False):
        print("[notify] Notifications disabled in scan_config.json.")
        return 0

    if not LAST_RUN_FILE.exists():
        print("[notify] last_run.json not found — nothing to notify.")
        return 0

    run_data = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
    results = run_data.get("results") or []
    min_score = int(notify_cfg.get("min_score", config.get("scan", {}).get("threshold", 70)))

    notified_urls = _load_notified_urls()
    new_jobs = [
        r for r in results
        if r.get("score") is not None
        and r["score"] >= min_score
        and r.get("link") not in notified_urls
    ]

    if not new_jobs:
        print("[notify] No new qualifying jobs — skipping email.")
        return 0

    new_jobs.sort(key=lambda r: -(r.get("score") or 0))

    smtp_host = notify_cfg.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(notify_cfg.get("smtp_port", 587))
    smtp_user = notify_cfg.get("smtp_user", "")
    smtp_password = (os.environ.get("SMTP_PASSWORD") or notify_cfg.get("smtp_password", "")).strip()
    from_addr = notify_cfg.get("from_addr") or smtp_user
    to_addr = notify_cfg.get("to_addr", "")

    if not smtp_user or not smtp_password or not to_addr:
        print("[notify] Missing smtp_user / smtp_password / to_addr in scan_config.json.")
        return 0

    now = datetime.now()
    subject = (
        f"Job Agent: {len(new_jobs)} new {'match' if len(new_jobs) == 1 else 'matches'}"
        f" · {now.strftime(f'%b {now.day}')}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(_build_email_html(new_jobs, min_score), "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(from_addr, to_addr, msg.as_string())
        print(f"[notify] ✓ Sent {len(new_jobs)} job(s) → {to_addr}")
        _save_notified_urls(notified_urls | {r["link"] for r in new_jobs if r.get("link")})
        return len(new_jobs)
    except smtplib.SMTPAuthenticationError:
        print("[notify] ✗ SMTP auth failed — check smtp_user / smtp_password in scan_config.json.")
        print("         For Gmail: use an App Password (myaccount.google.com/apppasswords).")
        return 0
    except Exception as e:
        print(f"[notify] ✗ Failed to send email: {e}")
        return 0


if __name__ == "__main__":
    sent = send_digest()
    raise SystemExit(0 if sent >= 0 else 1)
