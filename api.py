#!/usr/bin/env python3
"""FastAPI server — exposes scan API and serves the React frontend."""

import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
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
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import claude_budget

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB

# UUID v4 pattern — prevents path traversal in session IDs
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

app = FastAPI(title="Job Agent API")

_VALID_DIRECTION_TAGS: set = {
    # tech-intern
    "AI/ML", "Frontend", "Backend", "Fullstack", "Data", "DevOps", "Mobile", "Security",
    # finance-intern
    "Quant", "Trading", "Fin Eng", "Risk", "Investment Banking Tech",
}

# Admin token for crontab-mutating endpoints. Read from .env; never sent to the
# frontend. If unset, the admin endpoints are disabled rather than left open.
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()


def _check_claude_quota() -> None:
    """Raise 503 if the shared daily Claude-call budget is exhausted."""
    try:
        claude_budget.check()
    except claude_budget.ClaudeBudgetExceeded:
        raise HTTPException(
            status_code=503, detail="Today's quota is full. Please try tomorrow!"
        )


def _require_admin(token: Optional[str]) -> None:
    """Gate an endpoint behind the X-Admin-Token header (constant-time compare)."""
    if not _ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoint disabled: ADMIN_TOKEN is not configured.",
        )
    if not token or not hmac.compare_digest(token, _ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")


_sessions: dict = {}          # session_id → {"state": {...}, "lock": Lock}
_sessions_lock = threading.Lock()  # guards _sessions dict itself


def _get_session(session_id: str) -> tuple:
    """Return (state_dict, lock) for the given session, creating it if needed."""
    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = {
                "state": {"running": False, "lines": [], "error": None, "done": True, "proc": None},
                "lock": threading.Lock(),
            }
        entry = _sessions[session_id]
    return entry["state"], entry["lock"]


def _validate_session_id(sid: Optional[str]) -> str:
    """Validate that sid is a well-formed UUID v4; raise 400 otherwise."""
    if not sid or not _SESSION_ID_RE.match(sid.strip().lower()):
        raise HTTPException(status_code=400, detail="Missing or invalid X-Session-ID header.")
    return sid.strip().lower()


def _session_data_dir(session_id: str) -> Path:
    """Return (and create) the per-session data directory."""
    p = DATA_DIR / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _run(cmd: list, state: dict, lock: threading.Lock) -> None:
    print(f"[subprocess] starting: {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(Path(__file__).parent),
            env=os.environ.copy(),
        )
        with lock:
            state["proc"] = proc
        for line in iter(proc.stdout.readline, ""):
            stripped = line.rstrip()
            if stripped:
                print(stripped, flush=True)  # tee to Railway Deploy Logs
                with lock:
                    state["lines"].append(stripped)
        proc.wait()
        print(f"[subprocess] exited with code {proc.returncode}", flush=True)
        if proc.returncode != 0:
            with lock:
                state["error"] = f"Process exited with code {proc.returncode}"
    except Exception as exc:
        print(f"[subprocess] ERROR launching process: {exc}", flush=True)
        with lock:
            state["error"] = str(exc)
    finally:
        with lock:
            state["running"] = False
            state["done"] = True
            state["proc"] = None


@app.post("/api/scan")
async def start_scan(
    resume_pdf: UploadFile = File(...),
    role: str = Form("Software Engineer"),
    sources: str = Form(""),
    threshold: int = Form(70),
    location: str = Form(""),
    work_model: str = Form(""),
    direction_tags: str = Form(""),
    x_session_id: Optional[str] = Header(None),
):
    session_id = _validate_session_id(x_session_id)
    state, lock = _get_session(session_id)

    with lock:
        if state["running"]:
            return JSONResponse({"error": "A scan is already running."}, status_code=409)

    # Refuse before doing any work if the shared daily Claude budget is already
    # full. Per-call enforcement (resume parse + each scoring call) happens in
    # main.py so the run also stops mid-scan when the cap is hit.
    _check_claude_quota()

    content = await resume_pdf.read(MAX_PDF_BYTES + 1)
    if len(content) > MAX_PDF_BYTES:
        return JSONResponse({"error": "Resume must be under 10 MB."}, status_code=400)
    if not content.startswith(b"%PDF"):
        return JSONResponse({"error": "Only PDF files are accepted."}, status_code=400)
    pdf_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
    pdf_path.write_bytes(content)

    data_dir = _session_data_dir(session_id)
    cmd = [
        sys.executable, "main.py", "scan",
        "--resume-pdf", str(pdf_path),
        "--role", role,
        "--threshold", str(threshold),
        "--data-dir", str(data_dir),
    ]
    if sources.strip():
        cmd += ["--sources", sources.strip()]
    if location.strip():
        cmd += ["--location", location.strip()]
    if work_model.strip():
        cmd += ["--work-model", work_model.strip()]

    try:
        raw_tags = json.loads(direction_tags) if direction_tags.strip() else []
        if not isinstance(raw_tags, list):
            raw_tags = []
    except (json.JSONDecodeError, ValueError):
        raw_tags = []
    valid_tags = [t for t in raw_tags if isinstance(t, str) and t in _VALID_DIRECTION_TAGS]
    if valid_tags:
        cmd += ["--direction-tags", json.dumps(valid_tags)]

    with lock:
        state.update({"running": True, "lines": [], "error": None, "done": False})

    threading.Thread(target=_run, args=(cmd, state, lock), daemon=True).start()
    return {"status": "started"}


@app.get("/api/status")
def get_status(x_session_id: Optional[str] = Header(None)):
    session_id = _validate_session_id(x_session_id)
    state, lock = _get_session(session_id)
    with lock:
        return {k: v for k, v in state.items() if k != "proc"}


@app.post("/api/stop")
def stop_scan(x_session_id: Optional[str] = Header(None)):
    """Terminate the currently running scan process."""
    session_id = _validate_session_id(x_session_id)
    state, lock = _get_session(session_id)
    with lock:
        if not state["running"]:
            return JSONResponse({"error": "No scan is running."}, status_code=400)
        proc = state.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"status": "stopping"}


@app.post("/api/reset")
def reset_state(x_session_id: Optional[str] = Header(None)):
    """Clear a stuck scan state."""
    session_id = _validate_session_id(x_session_id)
    state, lock = _get_session(session_id)
    with lock:
        state.update({"running": False, "lines": [], "error": None, "done": True})
    return {"status": "reset"}


@app.post("/api/score-more")
def score_more(x_session_id: Optional[str] = Header(None)):
    """Score the next batch of pending candidates from the last scan."""
    # Refuse before starting if the shared daily Claude budget is already full;
    # per-call enforcement continues inside main.py for mid-run cutoff.
    _check_claude_quota()

    session_id = _validate_session_id(x_session_id)
    state, lock = _get_session(session_id)
    data_dir = _session_data_dir(session_id)

    with lock:
        if state["running"]:
            return JSONResponse({"error": "A scan is already running."}, status_code=409)
        if not (data_dir / "pending_candidates.json").exists():
            return JSONResponse({"error": "No pending candidates."}, status_code=400)
        state.update({"running": True, "lines": [], "error": None, "done": False})

    cmd = [sys.executable, "main.py", "score-more", "--data-dir", str(data_dir)]
    threading.Thread(target=_run, args=(cmd, state, lock), daemon=True).start()
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

@app.post("/api/chat")
async def chat(req: _ChatReq, x_session_id: Optional[str] = Header(None)):
    _check_claude_quota()
    session_id = _validate_session_id(x_session_id)
    data_dir = _session_data_dir(session_id)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not set."}, status_code=500)

    # Build results context from last scan
    results_summary = "No scan results available yet."
    last_run_path = data_dir / "last_run.json"
    if last_run_path.exists():
        data = json.loads(last_run_path.read_text(encoding="utf-8"))
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
    resume_text_path = data_dir / "last_resume_text.txt"
    if resume_text_path.exists():
        resume_text = resume_text_path.read_text(encoding="utf-8")

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
    claude_budget.record()
    return {"reply": resp.content[0].text}


@app.post("/api/chat-job")
async def chat_job(req: _JobChatReq, x_session_id: Optional[str] = Header(None)):
    _check_claude_quota()
    session_id = _validate_session_id(x_session_id)
    data_dir = _session_data_dir(session_id)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not set."}, status_code=500)

    resume_text = ""
    resume_text_path = data_dir / "last_resume_text.txt"
    if resume_text_path.exists():
        resume_text = resume_text_path.read_text(encoding="utf-8").strip()

    if resume_text:
        resume_section = f"## Candidate's Resume\n{resume_text}"
    else:
        resume_section = (
            "## Candidate's Resume\n"
            "No resume on file. The chat interface has no file upload — do NOT ask the user "
            "to share their resume. Base your response solely on the job title and description above."
        )

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
        f"{resume_section}"
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = None
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=system,
                messages=[{"role": m.role, "content": m.content} for m in req.messages],
            )
            break
        except anthropic.APIConnectionError as e:
            print(
                f"[chat-job] APIConnectionError attempt {attempt + 1}/2: {repr(e)} | cause: {repr(e.__cause__)}",
                flush=True,
            )
            if attempt == 0:
                time.sleep(1)
    if resp is None:
        return JSONResponse({"error": "AI 助手暂时连接失败，请重试"}, status_code=503)
    claude_budget.record()
    return {"reply": resp.content[0].text}


@app.get("/api/resume-text")
def get_resume_text(x_session_id: Optional[str] = Header(None)):
    session_id = _validate_session_id(x_session_id)
    data_dir = _session_data_dir(session_id)
    p = data_dir / "last_resume_text.txt"
    return {"text": p.read_text(encoding="utf-8") if p.exists() else ""}


@app.get("/api/results")
def get_results(x_session_id: Optional[str] = Header(None)):
    session_id = _validate_session_id(x_session_id)
    data_dir = _session_data_dir(session_id)
    p = data_dir / "last_run.json"
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


@app.get("/api/email-schedule")
def get_email_schedule(x_admin_token: Optional[str] = Header(None)):
    """Return current daily scan crontab state and email recipient (admin only)."""
    _require_admin(x_admin_token)
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


@app.post("/api/email-schedule")
def set_email_schedule(req: _ScheduleReq, x_admin_token: Optional[str] = Header(None)):
    """Add, update, or remove the daily scan crontab entry (admin only)."""
    _require_admin(x_admin_token)
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
