# JobFit AI

**Resume-aware job scanner that scores opportunities across two dimensions — how well you fit, and whether you'd actually want it.**

Live demo: https://jobfit-ai-production-d2fd.up.railway.app/

<!-- Screenshot: add after deployment -->

---

## What it does

Most job tools tell you "here are 500 openings." JobFit AI tells you "here are the 5 worth your weekend." Built specifically for students hunting Tech and Finance internships, it pulls from curated GitHub job lists, then uses Claude to score each posting on two things — how well it fits your background, and how well it matches what you're actually looking for. Pick a direction tag, upload your resume once, get an explainable ranked list. For any job that catches your eye, an integrated AI assistant tells you what's next.

---

## How it works

**Frontend** — A single `index.html` with React 18 loaded via CDN and `@babel/standalone` for JSX transpilation in-browser. No build step, no bundler. DOMPurify handles XSS for any rendered markdown. User preferences (role direction, location, score threshold) are persisted to `localStorage` so nothing resets between sessions.

**Backend** — FastAPI serves both the API and the static frontend from one Python process. Job lists are fetched with `urllib` (no headless browser needed — the sources are plain GitHub README markdown files). Claude Sonnet 4.6 scores each candidate against the resume; the scoring pipeline runs in a subprocess so long scans don't block the event loop. A `score_cache.json` keyed on URL + resume MD5 avoids re-scoring jobs on repeated scans.

**Deployment** — Railway, Python service. The frontend is served as a static file from FastAPI itself, so there's only one service to manage. Secrets (Anthropic API key, Gmail credentials) come from Railway environment variables.

---

## Design decisions

**Dual scoring instead of a single number.** A single "match score" conflates two different questions. You might be a perfect fit for a job you don't want, or want a job you're not qualified for yet. Splitting the score makes that tension visible and lets users sort by whichever axis matters more to them right now.

**Direction tags gated on job category.** Tech and finance interns want completely different options in that dropdown. Showing all tags at once was noisy, so the available directions update based on which job category is selected. Small thing, but it cuts the decision from "pick from 15" to "pick from 5."

**Global Claude API quota as the cost safety net.** Rate limiting per-user sounds cleaner in theory, but shared-IP networks (university WiFi, VPNs) would incorrectly penalize multiple legit users. A simple global daily cap is easier to reason about and sufficient for a demo-scale deployment.

**JD scraping is best-effort with explicit UI feedback.** The source repos don't always link to actual job descriptions — sometimes it's just a company name and a title. Rather than pretending Claude has full context, the UI shows a "Limited info" label when JD content couldn't be fetched. The score is still useful, just noisier.

---

## Roadmap

- [ ] Integrate `python-jobspy` to pull richer JD content for more grounded scoring
- [ ] Dockerfile-based deployment to get around Railway's runtime constraints (no Chromium, limited filesystem)
- [ ] Personalized email job alerts with a verification flow so the tool can notify without requiring a session

---

## Tech stack

Python · FastAPI · React · Tailwind · Claude API (Anthropic) · Railway

---

## Local development

```bash
# 1. Create and activate a Python 3.9+ venv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set required environment variables
export ANTHROPIC_API_KEY=sk-ant-...
# Optional: Gmail credentials for email notifications
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD=your-app-password

# 4. Start the server
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Then open http://localhost:8000. Upload your resume in the sidebar and run a scan.

> The frontend is served directly from FastAPI — no separate frontend dev server needed.
