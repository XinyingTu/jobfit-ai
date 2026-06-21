# Job Agent — CLAUDE.md

## Project Overview

Super Scanner: scrape Hanzilla GitHub README repos → keyword pre-filter (free) → Claude scoring (paid) → daily_digest.md + email notification. No apply/form-fill automation.

**Dev server:** `.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000 --reload &`
Always start with `--reload` so code changes apply without manual restarts.

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI (`api.py`), Python 3.9 venv |
| CLI | Typer (`main.py`) — `scan`, `score-more` commands |
| Frontend | Single-file `frontend/index.html` — React 18 via CDN, `@babel/standalone@7` (PINNED — v8 breaks), marked.js |
| AI | `claude-sonnet-4-6` for scoring, `claude-haiku-4-5-20251001` for chat |
| Email | Gmail SMTP via `smtplib` + STARTTLS |
| Cron | macOS crontab → `daily_scan.py` at 9am |

---

## Architecture

### Scan Pipeline (`main.py`)
```
Scrape all rows (free, no limit)
  → _CLOSED_SIGNAL filter (per-row, before cell parsing)
  → dedup by URL
  → keyword pre-filter: _passes_keyword_filter(title, role_keywords) — free, title-only
  → cap at SCORE_BATCH_SIZE=50 (most recent first)
  → save remainder to pending_candidates.json
  → Claude scoring (paid) for top 50
  → write last_run.json (includes pending_count field)
  → write daily_digest.md
```

### Load More (`score-more` command)
- `main.py score-more` reads `pending_candidates.json`, scores next 50, **merges** into existing `last_run.json`
- Backend: `POST /api/score-more` → same `_run()` + `_state` threading as `/api/scan`
- Frontend polls `/api/status` until done, then refreshes `/api/results`

### Sources (`_ALL_SOURCES` in main.py)
Currently only two sources:
```python
_ALL_SOURCES = [
    (_scrape_github, GITHUB_README,          "tech-intern"),   # canada_sde_intern_position
    (_scrape_github, GITHUB_FINANCE_INTERNS, "finance-intern"), # FINANCE_INTERNS.md
]
```
Many more repos are defined (format_a/b/c scrapers) but not wired into `_ALL_SOURCES` yet.

### Four Scraper Formats
- `_scrape_github` — 7-column: Title | Company | Role | Info | Details | Location | Apply
- `_scrape_github_format_a` — 5-column: Company | Job Title | Location | Work Model | Date (Apply URL in title cell)
- `_scrape_github_format_b` — 5-column with HTML `<strong>`/`<a>` tags
- `_scrape_github_format_c` — 5-column: Company | Role | Location | Apply badge | Date

---

## Key Files

| File | Purpose |
|---|---|
| `api.py` | FastAPI server — scan, score-more, chat, results endpoints |
| `main.py` | CLI — scan pipeline, score-more pipeline, scrapers |
| `notify.py` | Gmail SMTP digest sender |
| `daily_scan.py` | Cron entry point — runs scan + notify |
| `scan_config.json` | Scan settings + SMTP credentials |
| `frontend/index.html` | Single-file React frontend (no build step) |
| `last_run.json` | Latest scan results + metadata + pending_count |
| `pending_candidates.json` | Unscored batch queue for Load more |
| `score_cache.json` | Keyed by URL + MD5 resume hash |
| `last_resume_text.txt` | Cached resume text for chat context |
| `last_notified.json` | Already-emailed job URLs (dedup for notifications) |

---

## Important Constraints

### Security / Data
- **NEVER fabricate resume content.** All resume content comes from the resume PDF/TXT files on disk only.
- **NEVER add apply/form-fill automation.** Out of scope by design.

### Python 3.9 Compatibility
The venv is Python 3.9. Use `Optional[dict]` from `typing` instead of `dict | None` (union syntax requires 3.10+).

### Frontend: Babel Version
`@babel/standalone` MUST stay pinned at `@7`. v8 has a breaking syntax change that causes blank page with `SyntaxError: Unexpected token '{'`.

### Closed-Job Filter (`_CLOSED_SIGNAL`)
```python
_CLOSED_SIGNAL = re.compile(
    r"🔒|\bclosed\b|no longer accepting|position filled|not accepting|\bapplication\b.*\bclosed\b",
    re.IGNORECASE,
)
```
- `\bclosed\b` (word boundary) is **required** — bare `closed` matches `"dis`**`closed`**`"` inside `"Not disclosed"` (common salary field), incorrectly dropping open jobs.
- The filter is applied to the full raw table row before cell splitting.
- The current `tech-intern` and `finance-intern` repos don't use explicit closed markers — they delete rows when jobs close. The filter matters for `canada-intern` (format_c) which uses `Closed🔒`.

### `\|` Escape in Markdown Tables
Some rows have `\|` in bilingual company/job names (e.g. `Veolia \| North America`). This causes off-by-one cell misalignment — the apply cell ends up being a location string instead of a URL, so those rows are naturally skipped by the `if not m: continue` URL check. Not a bug to fix, just a known limitation (~3-4 rows per repo).

---

## Runtime State (`api.py`)

```python
_state = {"running": False, "lines": [], "error": None, "done": True}
_lock  = threading.Lock()
```
- `_run(cmd)` — shared subprocess runner for both scan and score-more
- `/api/reset` — clears stuck state (shown in UI when "already running" error appears)

---

## Chat System Prompts

### `/api/chat-job` (job sidebar chat)
Key rules enforced in system prompt:
- **Resume tips:** 3–5 bullet points only, NO full resume rewrite
- **Cover letters:** opening + 2-3 body + closing paragraph, no bullets
- **No meta-sections:** no "Application Checklist", "Next Steps", "How to Use This", "Summary"
- **No PDF instructions:** app handles copy/export, Claude should just produce content

`stripMetaSections(md)` in frontend also strips meta headings from clipboard content as a second layer.

---

## Email Notification

Config in `scan_config.json` under `"notify"`:
- SMTP: `smtp.gmail.com:587` with STARTTLS
- `smtp_user`: `your_email@gmail.com` (Gmail App Password)
- `to_addr`: `your_email@example.com`
- Dedup via `last_notified.json` — only new URLs are emailed
- `daily_scan.py --notify-only` to re-send without re-scraping

---

## Current State (as of 2026-06-17)

- Last scan: role=`AI Engineer`, location=`Vancouver`, sources=`tech-intern`, threshold=70
- 307 scraped → 234 keyword-matched → 50 scored → 11 qualifying
- `pending_count: 184` (unscored remainder in `pending_candidates.json`)
- Server restarted with `--reload` — was running without it since Sunday, causing `/api/score-more` to return 405

---

## Known Issues / Watch Out

1. **score-more merge bug (suspected):** After score-more completes, `/api/results` briefly shows the merged count (100), but `last_run.json` on disk may revert to the pre-merge state (50 results). Needs investigation — check `_score_more_pipeline()` write path in `main.py`.

2. **score-more 400 when no pending file:** If `pending_candidates.json` is missing, `/api/score-more` returns 400. Frontend `handleLoadMore()` silently sets `loadingMore=false` and returns — button appears to do nothing. Add error toast if this happens.

3. **Expired apply URLs:** Repos remove closed jobs by deleting rows, not marking them. Jobs in results may have expired LinkedIn/company URLs — can't detect without fetching each link.
