#!/usr/bin/env python3
"""FastAPI server — exposes scan API and serves the React frontend."""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from .env into os.environ (no-op if file absent)."""
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


_load_dotenv()

import anthropic
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB

app = FastAPI(title="Job Agent API")

_ACCESS_CODE = os.environ.get("APP_ACCESS_CODE", "")
_rate_buckets: dict = defaultdict(deque)
_RATE_WINDOW = 60   # seconds
_RATE_LIMIT   = 30  # requests per IP per window


def _check_access(
    request: Request,
    x_access_code: Optional[str] = Header(None),
) -> None:
    if _ACCESS_CODE and x_access_code != _ACCESS_CODE:
        raise HTTPException(status_code=401, detail="Invalid access code.")
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _rate_buckets[ip]
    while bucket and now - bucket[0] > _RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    bucket.append(now)


_state: dict = {"running": False, "lines": [], "error": None, "done": True, "proc": None}
_lock = threading.Lock()


def _run(cmd: list) -> None:
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(Path(__file__).parent),
            env=os.environ.copy(),
        )
        with _lock:
            _state["proc"] = proc
        for line in iter(proc.stdout.readline, ""):
            stripped = line.rstrip()
            if stripped:
                with _lock:
                    _state["lines"].append(stripped)
        proc.wait()
        if proc.returncode != 0:
            with _lock:
                _state["error"] = f"Process exited with code {proc.returncode}"
    except Exception as exc:
        with _lock:
            _state["error"] = str(exc)
    finally:
        with _lock:
            _state["running"] = False
            _state["done"] = True
            _state["proc"] = None


@app.post("/api/scan", dependencies=[Depends(_check_access)])
async def start_scan(
    resume_pdf: UploadFile = File(...),
    role: str = Form("Software Engineer"),
    sources: str = Form(""),
    threshold: int = Form(70),
    location: str = Form(""),
    work_model: str = Form(""),
):
    with _lock:
        if _state["running"]:
            return JSONResponse({"error": "A scan is already running."}, status_code=409)

    content = await resume_pdf.read(MAX_PDF_BYTES + 1)
    if len(content) > MAX_PDF_BYTES:
        return JSONResponse({"error": "Resume must be under 10 MB."}, status_code=400)
    if not content.startswith(b"%PDF"):
        return JSONResponse({"error": "Only PDF files are accepted."}, status_code=400)
    pdf_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
    pdf_path.write_bytes(content)

    cmd = [
        sys.executable, "main.py", "scan",
        "--resume-pdf", str(pdf_path),
        "--role", role,
        "--threshold", str(threshold),
    ]
    if sources.strip():
        cmd += ["--sources", sources.strip()]
    if location.strip():
        cmd += ["--location", location.strip()]
    if work_model.strip():
        cmd += ["--work-model", work_model.strip()]

    with _lock:
        _state.update({"running": True, "lines": [], "error": None, "done": False})

    threading.Thread(target=_run, args=(cmd,), daemon=True).start()
    return {"status": "started"}


@app.get("/api/status")
def get_status():
    with _lock:
        return {k: v for k, v in _state.items() if k != "proc"}


@app.post("/api/stop")
def stop_scan():
    """Terminate the currently running scan process."""
    with _lock:
        if not _state["running"]:
            return JSONResponse({"error": "No scan is running."}, status_code=400)
        proc = _state.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"status": "stopping"}


@app.post("/api/reset")
def reset_state():
    """Clear a stuck scan state."""
    with _lock:
        _state.update({"running": False, "lines": [], "error": None, "done": True})
    return {"status": "reset"}


@app.post("/api/score-more", dependencies=[Depends(_check_access)])
def score_more():
    """Score the next batch of pending candidates from the last scan."""
    with _lock:
        if _state["running"]:
            return JSONResponse({"error": "A scan is already running."}, status_code=409)
        if not Path("pending_candidates.json").exists():
            return JSONResponse({"error": "No pending candidates."}, status_code=400)
        _state.update({"running": True, "lines": [], "error": None, "done": False})

    threading.Thread(target=_run, args=([sys.executable, "main.py", "score-more"],), daemon=True).start()
    return {"status": "started"}


class _ChatMsg(BaseModel):
    role: str
    content: str

class _ChatReq(BaseModel):
    messages: list[_ChatMsg]

class _JobChatReq(BaseModel):
    messages: list[_ChatMsg]
    job_title: str = ""
    company: str = ""
    jd: str = ""

@app.post("/api/chat", dependencies=[Depends(_check_access)])
async def chat(req: _ChatReq):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not set."}, status_code=500)

    # Build results context from last scan
    results_summary = "No scan results available yet."
    if Path("last_run.json").exists():
        data = json.loads(Path("last_run.json").read_text(encoding="utf-8"))
        lines = []
        for r in (data.get("results") or []):
            if r.get("score") is not None:
                link = r.get("link", "")
                line = (
                    f"[{r['score']}/100] {r.get('job_title','?')} @ {r.get('company_name','?')}"
                    f"  |  {r.get('location','?')}  |  {r.get('work_model','?')}"
                )
                if r.get("reason"):
                    line += f"\n  Reason: {r['reason']}"
                if link:
                    line += f"\n  Link: {link}"
                lines.append(line)
        if lines:
            results_summary = "\n".join(lines[:30])

    resume_text = ""
    if Path("last_resume_text.txt").exists():
        resume_text = Path("last_resume_text.txt").read_text(encoding="utf-8")

    system = (
        "You are a job search assistant helping the user with their job applications.\n\n"
        f"## User's Resume\n{resume_text or 'Not available.'}\n\n"
        f"## Recent Scan Results (scored jobs)\n{results_summary}"
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=system,
        messages=[{"role": m.role, "content": m.content} for m in req.messages],
    )
    return {"reply": resp.content[0].text}


@app.post("/api/chat-job", dependencies=[Depends(_check_access)])
async def chat_job(req: _JobChatReq):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not set."}, status_code=500)

    resume_text = ""
    if Path("last_resume_text.txt").exists():
        resume_text = Path("last_resume_text.txt").read_text(encoding="utf-8")

    system = (
        "You are a job application assistant embedded in a web app. "
        "The app has jsPDF built in and handles all PDF export automatically — "
        "never explain how to convert text to PDF or suggest tools for it; "
        "just produce the document content and the app takes care of the rest.\n\n"
        "## Guidelines\n"
        "- **Resume tips:** When asked for resume tips, do NOT rewrite the resume. "
        "Instead give exactly 3–5 short, specific, actionable bullet points telling the user "
        "what to change or emphasize in their existing resume to better match this role. "
        "No full resume output. No 'Key Changes' section. Just the bullets.\n"
        "- **Cover letters:** Use clean professional formatting — an opening paragraph, "
        "2–3 body paragraphs, and a closing paragraph. No bullet points in cover letters.\n"
        "- **All responses:** Output only the requested content — no preamble, no closing remarks, "
        "no meta-sections such as 'Application Checklist', 'Next Steps', 'How to Use This', "
        "'Summary', or 'Tips for Applying'. The user will copy the response directly; "
        "it must be immediately usable with no editing needed. "
        "Use clean markdown: headers, bullet points, bold text where appropriate. "
        "Never output raw asterisks or pound signs as literal characters.\n\n"
        f"## Target Role\n{req.job_title} at {req.company}\n\n"
        f"## Job Description\n{req.jd or 'Not provided — base your answer on the role title.'}\n\n"
        f"## User's Resume\n{resume_text or 'Not available.'}"
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=system,
        messages=[{"role": m.role, "content": m.content} for m in req.messages],
    )
    return {"reply": resp.content[0].text}


@app.get("/api/resume-text", dependencies=[Depends(_check_access)])
def get_resume_text():
    p = Path("last_resume_text.txt")
    return {"text": p.read_text(encoding="utf-8") if p.exists() else ""}


@app.get("/api/results")
def get_results():
    p = Path("last_run.json")
    if not p.exists():
        return {"results": [], "total_scraped": 0, "threshold": 70}
    return json.loads(p.read_text(encoding="utf-8"))


_PROJECT_DIR = Path(__file__).parent
_VENV_PYTHON = _PROJECT_DIR / ".venv" / "bin" / "python"
_DAILY_SCAN_SCRIPT = _PROJECT_DIR / "daily_scan.py"


class _ScheduleReq(BaseModel):
    scheduled: bool
    hour: int = 9
    minute: int = 0


@app.get("/api/email-schedule", dependencies=[Depends(_check_access)])
def get_email_schedule():
    """Return current daily scan crontab state and email recipient."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    scheduled = False
    hour, minute = 9, 0
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if "daily_scan.py" in line and not line.strip().startswith("#"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        minute = int(parts[0])
                        hour = int(parts[1])
                        scheduled = True
                    except ValueError:
                        pass
                break

    to_addr = ""
    config_path = _PROJECT_DIR / "scan_config.json"
    if config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        to_addr = cfg.get("notify", {}).get("to_addr", "")

    return {"scheduled": scheduled, "hour": hour, "minute": minute, "to_addr": to_addr}


@app.post("/api/email-schedule", dependencies=[Depends(_check_access)])
def set_email_schedule(req: _ScheduleReq):
    """Add, update, or remove the daily scan crontab entry."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing_lines = result.stdout.splitlines() if result.returncode == 0 else []

    filtered = [l for l in existing_lines if "daily_scan.py" not in l]

    if req.scheduled:
        python_bin = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable
        log_path = str(_PROJECT_DIR / "daily_scan.log")
        entry = (
            f"{req.minute} {req.hour} * * * "
            f"{python_bin} {_DAILY_SCAN_SCRIPT} "
            f">> {log_path} 2>&1"
        )
        filtered.append(entry)

    new_crontab = "\n".join(filtered)
    if filtered:
        new_crontab += "\n"

    write_proc = subprocess.run(
        ["crontab", "-"], input=new_crontab, text=True, capture_output=True
    )
    if write_proc.returncode != 0:
        return JSONResponse(
            {"error": write_proc.stderr or "Failed to update crontab."},
            status_code=500,
        )

    config_path = _PROJECT_DIR / "scan_config.json"
    if config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if "notify" not in cfg:
            cfg["notify"] = {}
        cfg["notify"]["enabled"] = req.scheduled
        config_path.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    return {"status": "ok", "scheduled": req.scheduled, "hour": req.hour, "minute": req.minute}


# Serve React frontend — must be mounted last so /api/* routes take precedence
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
