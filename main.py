#!/usr/bin/env python3
"""Super Scanner — scrape jobs, score against your resume, write daily_digest.md."""

import asyncio
import base64
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional

import anthropic
import typer
from playwright.async_api import async_playwright

import claude_budget

app = typer.Typer(
    name="job-agent",
    help="Super Scanner: scrape jobs, score against your resume, write daily_digest.md.",
    no_args_is_help=True,
)

BUILTIN_BASE = "https://builtin.com"
GITHUB_README              = "https://raw.githubusercontent.com/hanzili/canada_sde_intern_position/main/README.md"
GITHUB_FINANCE_INTERNS     = "https://raw.githubusercontent.com/hanzili/canada_sde_junior_new_grad_position/main/FINANCE_INTERNS.md"
GITHUB_ACCOUNT_INTERN      = "https://raw.githubusercontent.com/hanzili/2026-Account-Internship/master/README.md"
GITHUB_ACCOUNT_NEWGRAD     = "https://raw.githubusercontent.com/hanzili/2026-Account-New-Grad/master/README.md"
GITHUB_SWE_INTERN          = "https://raw.githubusercontent.com/hanzili/2026-Software-Engineer-Internship/master/README.md"
GITHUB_SWE_NEWGRAD         = "https://raw.githubusercontent.com/hanzili/2026-Software-Engineer-New-Grad/master/README.md"
GITHUB_DATA_INTERN         = "https://raw.githubusercontent.com/hanzili/2026-Data-Analysis-Internship/master/README.md"
GITHUB_DATA_NEWGRAD        = "https://raw.githubusercontent.com/hanzili/2026-Data-Analysis-New-Grad/master/README.md"
GITHUB_ENG_INTERN          = "https://raw.githubusercontent.com/hanzili/2026-Engineer-Internship/master/README.md"
GITHUB_BA_INTERN           = "https://raw.githubusercontent.com/hanzili/2026-Business-Analyst-Internship/master/README.md"
GITHUB_CONSULT_INTERN      = "https://raw.githubusercontent.com/hanzili/2026-Consultant-Internship/master/README.md"
GITHUB_PM_INTERN           = "https://raw.githubusercontent.com/hanzili/2026-Product-Management-Internship/master/README.md"
GITHUB_MKT_INTERN          = "https://raw.githubusercontent.com/hanzili/2026-Marketing-Internship/master/README.md"
GITHUB_HR_INTERN           = "https://raw.githubusercontent.com/hanzili/2026-HR-Internship/master/README.md"
GITHUB_DESIGN_INTERN       = "https://raw.githubusercontent.com/hanzili/2026-Design-Internship/master/README.md"
GITHUB_SALES_INTERN        = "https://raw.githubusercontent.com/hanzili/2026-Sales-Internship/master/README.md"
GITHUB_LEGAL_INTERN        = "https://raw.githubusercontent.com/hanzili/2026-Legal-Internship/master/README.md"
GITHUB_LEGAL_NEWGRAD       = "https://raw.githubusercontent.com/hanzili/2026-Legal-New-Grad/master/README.md"
GITHUB_EDU_INTERN          = "https://raw.githubusercontent.com/hanzili/2026-Education-Internship/master/README.md"
GITHUB_EDU_NEWGRAD         = "https://raw.githubusercontent.com/hanzili/2026-Education-New-Grad/master/README.md"
GITHUB_GOV_INTERN          = "https://raw.githubusercontent.com/hanzili/2026-Public-Sector-Internship/master/README.md"
GITHUB_GOV_NEWGRAD         = "https://raw.githubusercontent.com/hanzili/2026-Public-Sector-New-Grad/master/README.md"
GITHUB_SUPPORT_INTERN      = "https://raw.githubusercontent.com/hanzili/2026-Support-Internship/master/README.md"
GITHUB_SUPPORT_NEWGRAD     = "https://raw.githubusercontent.com/hanzili/2026-Support-New-Grad/master/README.md"
GITHUB_ART_INTERN          = "https://raw.githubusercontent.com/hanzili/2026-Art-Internship/master/README.md"
GITHUB_ART_NEWGRAD         = "https://raw.githubusercontent.com/hanzili/2026-Art-New-Grad/master/README.md"
GITHUB_MGMT_INTERN         = "https://raw.githubusercontent.com/hanzili/2026-Management-Internship/master/README.md"
GITHUB_AI_COLLEGE          = "https://raw.githubusercontent.com/hanzili/2026-AI-College-Jobs/main/README.md"
GITHUB_SWE_COLLEGE         = "https://raw.githubusercontent.com/hanzili/2026-SWE-College-Jobs/main/README.md"
GITHUB_CANADA_INTERN       = "https://raw.githubusercontent.com/hanzili/Canadian-Tech-Internships-2026-hanzilla/main/README.md"
RESUME_TXT        = Path("Xinying Tu resume.txt")
RESUME_PDF        = Path("Xinying Tu resume.pdf")
DIGEST_FILE       = Path("daily_digest.md")
SCORE_CACHE_FILE  = Path("score_cache.json")

# Pre-filter senior/off-track titles before Claude is called.
_SENIOR_FILTER = re.compile(
    r"\b(senior|sr\.|staff|principal|lead|director|manager|head of|vp |vice president|"
    r"executive|president|cto|ceo|coo|svp|evp|partner|consultant)\b",
    re.IGNORECASE,
)

# Metro Vancouver area — keep jobs explicitly in this region.
_VANCOUVER_TERMS = re.compile(
    r"\b(vancouver|north vancouver|west vancouver|burnaby|richmond|new westminster|"
    r"coquitlam|port coquitlam|port moody|surrey|metro vancouver|greater vancouver|"
    r"lower mainland)\b",
    re.IGNORECASE,
)

# Remote signals — keep jobs that are genuinely remote/global.
_REMOTE_SIGNAL = re.compile(
    r"\b(remote|work from home|wfh|fully remote|100%\s*remote|entirely remote|"
    r"work from anywhere|remote.first|anywhere in canada|remote across canada|"
    r"worldwide|global|distributed team)\b",
    re.IGNORECASE,
)

# Hybrid signals — explicitly split between office and home.
_HYBRID_SIGNAL = re.compile(
    r"\b(hybrid|partially remote|semi.?remote|flexible.?work|mix of remote|"
    r"remote.hybrid|hybrid.remote|few days.*(office|home)|days?.*(office|home))\b",
    re.IGNORECASE,
)

# On-site signals — explicitly in-person / office-based.
_ONSITE_SIGNAL = re.compile(
    r"\b(on.?site|on.?location|in.?person|in.?office|office.?based|"
    r"must.*commute|reporting.*office|fully in.?office)\b",
    re.IGNORECASE,
)

# Closed-listing signals — job is no longer accepting applications.
_CLOSED_SIGNAL = re.compile(
    r"🔒|\bclosed\b|no longer accepting|position filled|not accepting|\bapplication\b.*\bclosed\b",
    re.IGNORECASE,
)

# Role → synonym/abbreviation expansions for the keyword pre-filter.
# Scraping is free; only keyword-matched titles proceed to Claude scoring.
_ROLE_EXPANSIONS: dict[str, list[str]] = {
    "software":    ["software", "developer", "engineer", "sde", "swe", "programmer"],
    "engineer":    ["engineer", "developer", "sde", "swe"],
    "developer":   ["developer", "engineer", "programmer", "software"],
    "ai":          ["ai", "artificial intelligence", "machine learning", "ml",
                    "deep learning", "llm", "nlp", "generative", "gen ai"],
    "ml":          ["ml", "machine learning", "ai", "deep learning",
                    "artificial intelligence", "llm", "nlp"],
    "machine":     ["machine learning", "ml", "ai", "deep learning"],
    "data":        ["data", "analyst", "analytics", "science", "scientist",
                    "business intelligence", "bi"],
    "financial":   ["financial", "finance", "analyst", "accounting",
                    "quant", "investment", "banking"],
    "finance":     ["financial", "finance", "analyst", "accounting",
                    "quant", "investment", "banking"],
    "analyst":     ["analyst", "analytics", "analysis", "research", "reporting"],
    "product":     ["product", "pm", "product manager", "product management",
                    "product owner"],
    "design":      ["design", "designer", "ux", "ui", "user experience",
                    "user interface", "visual"],
    "backend":     ["backend", "back-end", "server", "api", "infrastructure",
                    "platform"],
    "frontend":    ["frontend", "front-end", "react", "vue", "angular",
                    "web developer", "ui developer"],
    "fullstack":   ["fullstack", "full-stack", "full stack"],
    "devops":      ["devops", "sre", "infrastructure", "platform", "cloud",
                    "kubernetes", "docker"],
    "cloud":       ["cloud", "aws", "azure", "gcp", "infrastructure", "devops", "sre"],
    "security":    ["security", "cybersecurity", "cyber", "infosec", "appsec"],
    "mobile":      ["mobile", "ios", "android", "react native", "flutter"],
    "web":         ["web", "frontend", "full stack", "javascript", "react"],
    "marketing":   ["marketing", "growth", "seo", "content", "brand", "digital"],
    "hr":          ["hr", "human resources", "people", "talent", "recruiting"],
    "legal":       ["legal", "law", "counsel", "compliance", "regulatory"],
    "consulting":  ["consulting", "consultant", "strategy", "advisory"],
    "management":  ["management", "operations", "program manager", "project manager"],
    "sales":       ["sales", "account", "business development", "revenue"],
    "research":    ["research", "researcher", "scientist", "r&d"],
    "accounting":  ["accounting", "accountant", "finance", "cpa", "financial"],
    "business":    ["business", "analyst", "operations", "strategy", "consulting"],
}


def _build_role_keywords(role: str) -> list[str]:
    """Return deduplicated keywords for title-level pre-filter based on the role string."""
    role_lower = role.lower().strip()
    keywords: set[str] = set()
    for word in re.split(r"\W+", role_lower):
        if len(word) >= 2:
            keywords.add(word)
    for trigger, expansions in _ROLE_EXPANSIONS.items():
        if trigger in role_lower:
            keywords.update(expansions)
    return sorted(keywords)


def _passes_keyword_filter(title: str, keywords: list[str]) -> bool:
    """True if job title contains any role keyword (case-insensitive substring)."""
    if not keywords:
        return True
    t = title.lower()
    return any(kw in t for kw in keywords)


def _detect_work_model(location: str, jd: str) -> str:
    """
    Detect work model from the job location field and JD text.
    Returns 'Remote', 'Hybrid', 'On Site', or '' (unknown).
    Hybrid is checked first because hybrid postings often mention remote too.
    """
    text = location + " " + jd
    if _HYBRID_SIGNAL.search(text):
        return "Hybrid"
    if _REMOTE_SIGNAL.search(text):
        return "Remote"
    if _ONSITE_SIGNAL.search(text):
        return "On Site"
    return ""


# Student-friendly signals — role welcomes interns / entry-level / part-time.
_STUDENT_SIGNAL = re.compile(
    r"\b(intern|internship|co.op|coop|new.grad|new graduate|entry.level|entry level|"
    r"0.1 year|0.2 year|no experience required|student|part.time|part time|"
    r"junior|undergraduate|recent graduate|fresh graduate|graduate student|"
    r"summer student|practicum)\b",
    re.IGNORECASE,
)

# Hard experience gate — role explicitly requires 3+ years of professional experience.
_EXPERIENCE_GATE = re.compile(
    r"\b([3-9]|\d{2})\+?\s*years?\s+(of\s+)?(professional|industry|work|software|"
    r"development|engineering|relevant)?\s*(experience|exp\.)\b",
    re.IGNORECASE,
)

_SCORING_PROMPT_TEMPLATE = """\
You are a job-match evaluator. Score a job against a candidate on TWO independent dimensions.

{{CANDIDATE_PROFILE}}

DIMENSION 1 — FIT SCORE (0-10): How well does the candidate meet the job's hard requirements?
1. Start with a raw fit (0-6) based on how the candidate's skills, coursework, and experience \
   align with the JD's stated requirements.
2. TECHNICAL PRIORITY BONUS (+1.5): If the job title or description prominently features \
   "Backend", "Fullstack", "AI Engineer", or "Machine Learning", add 1.5.
3. ACADEMIC BASELINE BONUS (+2): If the candidate's GPA is strong (3.5+/4.0, 85%+/100, or \
   equivalent), add 2 to reflect strong CS fundamentals that transfer across roles.
4. STUDENT-FRIENDLY BOOST (+0.5): If the role is explicitly internship, co-op, new-grad, \
   entry-level, or junior, add 0.5.
5. EXPERIENCE PENALTY (-1.5): If the role requires 3+ years of professional experience and \
   is NOT tagged intern/entry-level, subtract 1.5 from the raw fit before applying bonuses.
6. For Product or UI/UX roles, apply the academic bonus but NOT the technical priority bonus.
7. Clamp final fit_score to [0, 10].

DIMENSION 2 — PREFERENCE SCORE (0-10): How well does this job match the candidate's direction preferences?
{{DIRECTION_SECTION}}

OUTPUT FORMAT — return ONLY valid JSON, no markdown fences, no extra keys:
{
  "fit_score": <integer 0-10>,
  "fit_reason": "<one concise sentence: which hard requirements match or mismatch the resume>",
  "preference_score": <integer 0-10>,
  "preference_reason": "<one concise sentence: how this job aligns with the candidate's stated preferences>",
  "priority": <"AI/SWE" if technical priority bonus was applied, else null>
}"""


def _extract_json(text: str) -> dict:
    """Parse the first JSON object in a model response, ignoring any surrounding text."""
    raw = text.strip()
    raw = re.sub(r"^```[a-z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    obj, _ = json.JSONDecoder().raw_decode(raw, raw.index("{"))
    return obj


def _parse_resume_pdf(client: anthropic.Anthropic, pdf_path: Path) -> tuple[dict, str]:
    """
    Call Claude once at startup to extract a structured profile and full text from the resume PDF.
    Returns (profile_dict, resume_text).
    """
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")
    claude_budget.check()  # raises ClaudeBudgetExceeded if the daily cap is hit
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Read this resume and return ONLY valid JSON with exactly two top-level keys:\n"
                        '{"profile": {"name": "", "school": "", "major": "", "gpa": "", '
                        '"skills": ["..."], "experience": ["..."], "looking_for": ""}, '
                        '"text": "<complete resume text verbatim>"}\n\n'
                        "profile.skills: programming languages, frameworks, and tools\n"
                        "profile.experience: internships, notable projects, or relevant coursework "
                        "(each as a short phrase, max 10 items)\n"
                        "profile.looking_for: the type of role the candidate is targeting\n"
                        "text: the full resume content as plain text, preserving all details"
                    ),
                },
            ],
        }],
    )
    claude_budget.record()  # count only after a successful call
    result = _extract_json(response.content[0].text)
    return result["profile"], result["text"]


def _build_scoring_prompt(profile: dict, direction_tags: Optional[list] = None) -> str:
    """Build the scoring system prompt with the candidate profile injected from the parsed PDF."""
    skills = ", ".join(profile.get("skills") or []) or "Not specified"
    exp_lines = "\n".join(f"  - {e}" for e in (profile.get("experience") or [])) or "  - Not specified"
    looking_for = profile.get("looking_for", "Software Engineering roles")
    candidate_section = (
        "CANDIDATE PROFILE:\n"
        f"- Name: {profile.get('name', 'Unknown')}\n"
        f"- School: {profile.get('school', 'Unknown')}, {profile.get('major', 'Unknown')}\n"
        f"- GPA: {profile.get('gpa', 'Not specified')}\n"
        f"- Skills: {skills}\n"
        f"- Experience / Projects:\n{exp_lines}\n"
        f"- Looking for: {looking_for}"
    )
    if direction_tags:
        tags_str = ", ".join(direction_tags)
        direction_section = (
            f"The candidate prefers jobs in these directions: [{tags_str}].\n"
            "Score 9-10: job title/description clearly matches one of these directions.\n"
            "Score 6-8: related to the preferred directions but not an exact match.\n"
            "Score 3-5: neutral — different area but not a clear conflict.\n"
            "Score 0-2: clearly conflicts with or is very different from the stated preferences.\n"
            f"General role preference (additional context): {looking_for}."
        )
    else:
        direction_section = (
            f"No direction preferences selected. Score based on the candidate's general role preference: {looking_for}.\n"
            "Score 9-10: job matches the stated preference exactly.\n"
            "Score 6-8: related but not the primary preference.\n"
            "Score 3-5: adjacent area with some relevance.\n"
            "Score 0-2: clearly different from the stated preferences."
        )
    return (
        _SCORING_PROMPT_TEMPLATE
        .replace("{{CANDIDATE_PROFILE}}", candidate_section)
        .replace("{{DIRECTION_SECTION}}", direction_section)
    )


@app.callback()
def _root():
    """Super Scanner — scrape jobs, score against your resume, write daily_digest.md."""


# ---------------------------------------------------------------------------
# BuiltIn listing scraper  (search-builtin command)
# ---------------------------------------------------------------------------

async def _scrape_builtin(
    role: str,
    limit: int,
    internship: bool = False,
    remote: bool = False,
    vancouver: bool = False,
) -> list[dict]:
    query_parts = [role]
    if vancouver:
        query_parts.append("Vancouver BC")
    query = " ".join(query_parts)

    path = "remote-jobs" if remote else "jobs"
    experience_param = "&experience=internship" if internship else ""
    url = f"{BUILTIN_BASE}/{path}?q={query.replace(' ', '+')}{experience_param}"

    filters = []
    if internship:
        filters.append("internship")
    if remote:
        filters.append("remote")
    if vancouver:
        filters.append("vancouver")
    label = f"{role!r}" + (f" [{', '.join(filters)}]" if filters else "")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        typer.echo(f"[*] Searching BuiltIn for: {label}", err=True)
        typer.echo(f"[*] URL: {url}", err=True)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3_500)

        jobs = await _extract_nextjs_jobs(page, limit)
        if not jobs:
            typer.echo("[*] Next.js island empty — falling back to DOM scraping…", err=True)
            jobs = await _extract_dom_jobs(page, limit)

        await browser.close()
    return jobs[:limit]


async def _extract_nextjs_jobs(page, limit: int) -> list[dict]:
    raw = await page.evaluate("""
        () => {
            const el = document.getElementById('__NEXT_DATA__');
            return el ? el.textContent : null;
        }
    """)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    jobs: list[dict] = []
    _walk(data, jobs, limit)
    return jobs


def _walk(node, results: list, limit: int, depth: int = 0) -> None:
    if len(results) >= limit or depth > 12:
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, results, limit, depth + 1)
    elif isinstance(node, dict):
        title   = node.get("title") or node.get("jobTitle") or node.get("job_title")
        company = node.get("company") or node.get("companyName") or node.get("company_name")
        if isinstance(company, dict):
            company = company.get("name") or company.get("title") or ""
        slug = (
            node.get("url") or node.get("slug") or node.get("link")
            or node.get("applyUrl") or ""
        )
        if title and (company or slug):
            link = str(slug) if str(slug).startswith("http") else f"{BUILTIN_BASE}{slug}"
            results.append({
                "job_title":    str(title).strip(),
                "company_name": str(company or "").strip(),
                "link":         link,
            })
            if len(results) >= limit:
                return
        for value in node.values():
            _walk(value, results, limit, depth + 1)


async def _extract_dom_jobs(page, limit: int) -> list[dict]:
    return await page.evaluate(
        """
        ([limit, base]) => {
            const jobs = [];
            const companyMap = {};
            for (const el of document.querySelectorAll('a[data-id="company-title"]')) {
                const jid = el.getAttribute('data-builtin-track-job-id');
                if (jid) companyMap[jid] = (el.innerText || el.textContent || '').trim();
            }
            const locationMap = {};
            for (const myItem of document.querySelectorAll('my-item[entity-type="job"]')) {
                const jid  = myItem.getAttribute('entity-id');
                const card = myItem.parentElement;
                if (!jid || !card) continue;
                const section = card.querySelector('.bounded-attribute-section');
                if (!section) continue;
                const parts = [];
                const houseIcon = section.querySelector('i.fa-house-building');
                if (houseIcon) {
                    const row  = houseIcon.closest('.d-flex.align-items-start');
                    const span = row && row.querySelector('span.font-barlow');
                    if (span) parts.push((span.textContent || '').trim());
                }
                const locIcon = section.querySelector('i.fa-location-dot');
                if (locIcon) {
                    const row  = locIcon.closest('.d-flex.align-items-start');
                    const span = row && row.querySelector('span.font-barlow');
                    if (span) {
                        const tooltip = span.getAttribute('data-bs-title');
                        if (tooltip) {
                            const tmp = document.createElement('div');
                            tmp.innerHTML = tooltip;
                            const locs = Array.from(tmp.querySelectorAll('div'))
                                .map(d => d.textContent.trim()).filter(Boolean);
                            if (locs.length) parts.push(locs.join(' / '));
                        } else {
                            const txt = (span.textContent || '').trim();
                            if (txt) parts.push(txt);
                        }
                    }
                }
                locationMap[jid] = parts.join(' — ');
            }
            const seen = new Set();
            for (const anchor of document.querySelectorAll('a[data-id="job-card-title"]')) {
                if (jobs.length >= limit) break;
                const href = anchor.getAttribute('href') || '';
                if (seen.has(href) || !href) continue;
                seen.add(href);
                const title = (anchor.innerText || anchor.textContent || '').trim();
                if (!title) continue;
                const jid = anchor.getAttribute('data-builtin-track-job-id') || '';
                jobs.push({
                    job_title:    title,
                    company_name: companyMap[jid]  || '',
                    location:     locationMap[jid] || '',
                    link: href.startsWith('http') ? href : base + href,
                });
            }
            return jobs;
        }
        """,
        [limit, BUILTIN_BASE],
    )


@app.command(name="search-builtin")
def search_builtin(
    role: str = typer.Option(
        "Software Engineer", "--role", "-r",
        help="Role to search for, e.g. 'Product Manager'",
    ),
    limit: int = typer.Option(5, "--limit", "-n", help="Number of listings to return"),
    internship: bool = typer.Option(
        False, "--internship/--no-internship", "--intern/--no-intern",
        help="Filter for internship positions only",
    ),
    remote: bool = typer.Option(False, "--remote/--no-remote", help="Remote positions only"),
    vancouver: bool = typer.Option(
        False, "--vancouver/--no-vancouver",
        help="Vancouver, BC on-site / hybrid positions",
    ),
    output: Optional[str] = typer.Option(
        None, "--output", "-o",
        help="Save results to this file path (e.g. jobs.json)",
    ),
):
    """
    Scrape job listings from BuiltIn.com and print them as JSON.

    Examples:
        python main.py search-builtin
        python main.py search-builtin --role "Software Engineer" --intern --vancouver --output jobs.json
        python main.py search-builtin --role "Data Engineer" --remote --limit 10
    """
    if remote and vancouver:
        typer.echo(
            "[!] --remote and --vancouver are contradictory. "
            "Showing remote jobs that mention Vancouver.",
            err=True,
        )
    jobs = asyncio.run(
        _scrape_builtin(role, limit, internship=internship, remote=remote, vancouver=vancouver)
    )
    if not jobs:
        typer.echo("[!] No listings found. BuiltIn may have changed their page structure.", err=True)
        raise typer.Exit(code=1)

    active_filters = [
        f for f, on in [("internship", internship), ("remote", remote), ("vancouver", vancouver)] if on
    ]
    payload = {
        "query":       role,
        "filters":     active_filters,
        "source":      "builtin.com",
        "total_found": len(jobs),
        "jobs":        jobs,
    }
    serialised = json.dumps(payload, indent=2, ensure_ascii=False)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(serialised + "\n")
        typer.echo(f"[✓] Saved {len(jobs)} listings to {output}", err=True)
    else:
        typer.echo(serialised)


# ---------------------------------------------------------------------------
# Indeed Canada scraper
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GitHub README scraper  (hanzili/canada_sde_intern_position)
# ---------------------------------------------------------------------------

def _scrape_github(limit: int, url: str = GITHUB_README, source: str = "hanzilla-intern") -> list[dict]:
    """
    Fetch a Hanzilla GitHub README and parse its markdown tables.
    Returns up to `limit` job dicts — no Playwright, pure HTTP.
    """
    import urllib.request

    typer.echo(f"[*] Fetching GitHub: {url}", err=True)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            status = getattr(resp, "status", "?")
            raw = resp.read().decode("utf-8")
        typer.echo(f"[*] HTTP {status} — {len(raw)} bytes from {url}", err=True)
    except Exception as e:
        import traceback
        typer.echo(f"[!] Failed to fetch {url}: {type(e).__name__}: {e}", err=True)
        typer.echo(traceback.format_exc(), err=True)
        return []

    if not raw.strip():
        typer.echo(f"[!] Empty response body from {url}", err=True)
        return []

    # Regex to extract apply URL from [Apply](<URL>) or [Apply](URL)
    _apply_url = re.compile(r'\[Apply\]\(<?(https?://[^>)\s]+)>?\)', re.IGNORECASE)
    # Strip HTML comments like <!--id:700750547-->
    _html_comment = re.compile(r'<!--.*?-->', re.DOTALL)
    # Strip leading emoji / non-ASCII characters from titles
    _leading_junk = re.compile(r'^[\s\U0001F300-\U0001FFFF🆕🔥💤]+')

    jobs: list[dict] = []
    seen_links: set[str] = set()
    n_table_rows = 0
    n_closed = 0
    n_no_url = 0

    for line in raw.splitlines():
        if len(jobs) >= limit:
            break

        line = line.strip()
        # Must be a table row (starts and ends with |) but not a separator row
        if not (line.startswith("|") and line.endswith("|")):
            continue
        if re.match(r'^\|[\s\-:]+\|', line):
            continue

        n_table_rows += 1

        if _CLOSED_SIGNAL.search(line):
            n_closed += 1
            continue

        cells = [c.strip() for c in line.split("|")]
        # Drop the empty strings from the leading/trailing pipes
        cells = [c for c in cells if c != ""]

        # Need at least 7 columns: Title Company Role Info Details Location Apply
        if len(cells) < 7:
            continue

        raw_title   = cells[0]
        company     = cells[1]
        location    = cells[5]
        apply_cell  = cells[6]

        # Skip the header row
        if raw_title.lower() in ("title", "**title**"):
            n_table_rows -= 1
            continue

        # Clean title
        title = _html_comment.sub("", raw_title)
        title = _leading_junk.sub("", title).strip()
        if not title:
            continue

        # Extract apply URL
        m = _apply_url.search(apply_cell)
        if not m:
            n_no_url += 1
            continue
        link = m.group(1)
        if link in seen_links:
            continue
        seen_links.add(link)

        jobs.append({
            "job_title":    title,
            "company_name": company,
            "location":     location,
            "link":         link,
            "source":       source,
        })

    typer.echo(
        f"[*] Parsed {len(jobs)} listings from {url.split('/')[-1]} "
        f"(rows={n_table_rows}, closed={n_closed}, no_url={n_no_url}).",
        err=True,
    )
    if n_table_rows == 0:
        typer.echo(f"[!] WARNING: 0 table rows found — content may not be markdown or URL returned wrong file.", err=True)
        typer.echo(f"[!] First 500 chars: {raw[:500]!r}", err=True)
    return jobs[:limit]


def _scrape_github_format_a(limit: int, url: str, source: str) -> list[dict]:
    """
    Parse Hanzilla repos with Format A: Company | Job Title | Location | Work Model | Date Posted.
    Apply URL is embedded in the Job Title cell as **[Title](URL)**.
    Company column may use ↳ to indicate same company as previous row.
    """
    import urllib.request

    typer.echo(f"[*] Fetching GitHub: {url}", err=True)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            status = getattr(resp, "status", "?")
            raw = resp.read().decode("utf-8")
        typer.echo(f"[*] HTTP {status} — {len(raw)} bytes", err=True)
    except Exception as e:
        import traceback
        typer.echo(f"[!] Failed to fetch {url}: {type(e).__name__}: {e}", err=True)
        typer.echo(traceback.format_exc(), err=True)
        return []

    if not raw.strip():
        typer.echo(f"[!] Empty response body from {url}", err=True)
        return []

    _md_link = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')

    jobs: list[dict] = []
    seen_links: set[str] = set()
    last_company = ""

    for line in raw.splitlines():
        if len(jobs) >= limit:
            break

        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        if re.match(r'^\|[\s\-:]+\|', line):
            continue
        if _CLOSED_SIGNAL.search(line):
            continue

        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c != ""]

        if len(cells) < 4:
            continue

        company_cell = cells[0]
        title_cell   = cells[1]
        location     = cells[2] if len(cells) > 2 else ""
        work_model   = cells[3] if len(cells) > 3 else ""

        # Skip header row
        if company_cell.lower().strip("* ") in ("company",):
            continue

        # Handle ↳ continuation (same company as previous row)
        if company_cell.strip() == "↳":
            company = last_company
        else:
            m = _md_link.search(company_cell)
            company = m.group(1) if m else company_cell.strip("* ")
            last_company = company

        # Extract title and apply URL from title cell
        m = _md_link.search(title_cell)
        if not m:
            continue

        title = m.group(1)
        link  = m.group(2)

        if link in seen_links:
            continue
        seen_links.add(link)

        jobs.append({
            "job_title":    title,
            "company_name": company,
            "location":     location,
            "work_model":   work_model,
            "link":         link,
            "source":       source,
        })

    repo_name = url.split("/")[-3] if url.count("/") >= 3 else url
    typer.echo(f"[*] Parsed {len(jobs)} listings from {repo_name}.", err=True)
    return jobs[:limit]


def _scrape_github_format_b(limit: int, url: str, source: str) -> list[dict]:
    """
    Parse hanzili repos with Format B: Company | Position | Location | Salary | Posting | Age
    Company uses HTML <a href="..."><strong>Name</strong></a>.
    Apply URL is in the Posting cell (index 4) as <a href="apply_url"><img .../></a>.
    """
    import urllib.request

    typer.echo(f"[*] Fetching GitHub: {url}", err=True)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            status = getattr(resp, "status", "?")
            raw = resp.read().decode("utf-8")
        typer.echo(f"[*] HTTP {status} — {len(raw)} bytes", err=True)
    except Exception as e:
        import traceback
        typer.echo(f"[!] Failed to fetch {url}: {type(e).__name__}: {e}", err=True)
        typer.echo(traceback.format_exc(), err=True)
        return []

    if not raw.strip():
        typer.echo(f"[!] Empty response body from {url}", err=True)
        return []

    _strong = re.compile(r'<strong>([^<]+)</strong>')
    _href   = re.compile(r'<a\s+href="([^"]+)"')

    jobs: list[dict] = []
    seen_links: set[str] = set()

    for line in raw.splitlines():
        if len(jobs) >= limit:
            break

        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        if re.match(r'^\|[\s\-:]+\|', line):
            continue
        if _CLOSED_SIGNAL.search(line):
            continue

        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c != ""]

        if len(cells) < 5:
            continue

        company_cell = cells[0]
        position     = cells[1].strip()
        location     = cells[2].strip() if len(cells) > 2 else ""
        apply_cell   = cells[4] if len(cells) > 4 else ""

        # Skip header row
        if position.lower() in ("position",):
            continue

        # Extract company name from <strong>
        m = _strong.search(company_cell)
        if not m:
            continue
        company = m.group(1)

        # Extract apply URL from Posting cell
        m = _href.search(apply_cell)
        if not m:
            continue
        link = m.group(1)

        if link in seen_links:
            continue
        seen_links.add(link)

        jobs.append({
            "job_title":    position,
            "company_name": company,
            "location":     location,
            "link":         link,
            "source":       source,
        })

    repo_name = url.split("/")[-3] if url.count("/") >= 3 else url
    typer.echo(f"[*] Parsed {len(jobs)} listings from {repo_name}.", err=True)
    return jobs[:limit]


def _scrape_github_format_c(limit: int, url: str, source: str) -> list[dict]:
    """
    Parse Canadian-Tech-Internships-2026-hanzilla: Company | Role | Location | Apply | Date Posted
    Apply URL is [![Apply](badge)](apply_url) in column 3. Company may use ↳ for continuation.
    """
    import urllib.request

    typer.echo(f"[*] Fetching GitHub: {url}", err=True)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            status = getattr(resp, "status", "?")
            raw = resp.read().decode("utf-8")
        typer.echo(f"[*] HTTP {status} — {len(raw)} bytes", err=True)
    except Exception as e:
        import traceback
        typer.echo(f"[!] Failed to fetch {url}: {type(e).__name__}: {e}", err=True)
        typer.echo(traceback.format_exc(), err=True)
        return []

    if not raw.strip():
        typer.echo(f"[!] Empty response body from {url}", err=True)
        return []

    # Matches [![inner_text](inner_url)](outer_url) — captures apply_url
    _badge_link = re.compile(r'\[!\[.*?\]\(.*?\)\]\((https?://[^)]+)\)')

    jobs: list[dict] = []
    seen_links: set[str] = set()
    last_company = ""

    for line in raw.splitlines():
        if len(jobs) >= limit:
            break

        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        if re.match(r'^\|[\s\-:]+\|', line):
            continue
        if _CLOSED_SIGNAL.search(line):
            continue

        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c != ""]

        if len(cells) < 4:
            continue

        company_cell = cells[0]
        role         = cells[1].strip()
        location     = cells[2].strip() if len(cells) > 2 else ""
        apply_cell   = cells[3] if len(cells) > 3 else ""

        # Skip header row
        if role.lower() in ("role",):
            continue

        # Handle ↳ continuation
        if company_cell.strip() == "↳":
            company = last_company
        else:
            company = company_cell.strip()
            last_company = company

        # Extract apply URL from badge link
        m = _badge_link.search(apply_cell)
        if not m:
            continue
        link = m.group(1)

        if link in seen_links:
            continue
        seen_links.add(link)

        jobs.append({
            "job_title":    role,
            "company_name": company,
            "location":     location,
            "link":         link,
            "source":       source,
        })

    repo_name = url.split("/")[-3] if url.count("/") >= 3 else url
    typer.echo(f"[*] Parsed {len(jobs)} listings from {repo_name}.", err=True)
    return jobs[:limit]


# ---------------------------------------------------------------------------
# JD extraction
# ---------------------------------------------------------------------------

async def _scrape_jd(url: str) -> str:
    """Return the full job description text from a BuiltIn or Indeed job page."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_500)
        jd = await page.evaluate("""
            () => {
                const candidates = [
                    '#jobDescriptionText',
                    '[class*="jobsearch-jobDescriptionText"]',
                    '.jobsearch-JobComponent-description',
                    '[data-id="job-description"]',
                    '[class*="job-description"]',
                    '[class*="jobDescription"]',
                    'article',
                    '[class*="description"]',
                ];
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el && (el.innerText || '').length > 200)
                        return el.innerText.trim();
                }
                const main = document.querySelector('main') || document.body;
                return (main.innerText || '').slice(0, 10000);
            }
        """)
        await browser.close()
    return (jd or "").strip()


# ---------------------------------------------------------------------------
# Claude match scoring  (resume text is prompt-cached across all calls)
# ---------------------------------------------------------------------------

def _score_match(
    client: anthropic.Anthropic,
    job_title: str,
    jd: str,
    resume: str,
    system_prompt: str,
) -> dict:
    """Score how well the resume fits the job. Returns dict with fit_score, preference_score, reasons, priority."""
    claude_budget.check()  # raises ClaudeBudgetExceeded if the daily cap is hit
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=[
            {
                "type": "text",
                "text": system_prompt,
            },
            {
                "type": "text",
                "text": f"<resume>\n{resume}\n</resume>",
                # Resume is constant across all calls — cache it to save tokens.
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"<job_title>{job_title}</job_title>\n\n"
                    f"<job_description>\n{jd}\n</job_description>\n\n"
                    "Score this match."
                ),
            }
        ],
    )
    claude_budget.record()  # count only after a successful call
    parsed = _extract_json(response.content[0].text)
    fit_score        = min(10, max(0, int(parsed["fit_score"])))
    preference_score = min(10, max(0, int(parsed["preference_score"])))
    total            = min(100, (fit_score + preference_score) * 5)
    fit_reason       = str(parsed.get("fit_reason", ""))
    return {
        "score":             total,
        "fit_score":         fit_score,
        "fit_reason":        fit_reason,
        "preference_score":  preference_score,
        "preference_reason": str(parsed.get("preference_reason", "")),
        "reason":            fit_reason,  # kept for _write_digest compat
        "priority":          parsed.get("priority"),
    }


# ---------------------------------------------------------------------------
# Location filter
# ---------------------------------------------------------------------------

def _passes_location_filter(job: dict, jd: str, location_pref: str) -> bool:
    """
    If location_pref is empty, all jobs pass.
    Otherwise, only pass jobs whose location matches the preference (case-insensitive
    substring) OR whose location/JD contains a remote signal.
    """
    if not location_pref.strip():
        return True

    location_str = job.get("location", "")

    if _REMOTE_SIGNAL.search(location_str):
        return True
    if not location_str and _REMOTE_SIGNAL.search(jd):
        return True

    return location_pref.strip().lower() in location_str.lower()


def _passes_combined_filter(
    detected_wm: str, location_str: str, city: str, wm_set: set
) -> bool:
    """
    Unified city + work model filter.
    - city: lowercase city preference (empty = no city filter).
    - wm_set: set of selected work models e.g. {'Remote', 'Hybrid'}. Empty = no filter.
    - Remote always bypasses the city constraint.
    - Onsite/Hybrid must be in wm_set and match city (if set).
    - Unknown work model passes wm_set check; city filter still applies.
    """
    if not wm_set:
        if not city:
            return True
        if detected_wm == "Remote":
            return True
        return city in location_str.lower()

    if detected_wm == "Remote":
        return "Remote" in wm_set

    if detected_wm in ("Hybrid", "On Site"):
        if detected_wm not in wm_set:
            return False
        return not city or city in location_str.lower()

    # Unknown work model: passes wm check; city filter still applies
    return not city or city in location_str.lower()


def _load_cache() -> dict:
    if SCORE_CACHE_FILE.exists():
        try:
            return json.loads(SCORE_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    SCORE_CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Student filter
# ---------------------------------------------------------------------------

def _passes_student_filter(jd: str) -> bool:
    """
    Strict require: JD must explicitly signal intern / part-time / entry-level / co-op.
    Ambiguous roles (no signal, no gate) are rejected.
    """
    return bool(_STUDENT_SIGNAL.search(jd))


# ---------------------------------------------------------------------------
# Digest writer
# ---------------------------------------------------------------------------

def _write_digest(
    qualifying: list[dict],
    also_reviewed: list[dict],
    total_scraped: int,
    location_passed: int,
    role: str,
    threshold: int,
) -> None:
    """Write qualifying jobs (and below-threshold reviewed jobs) to daily_digest.md."""
    today = datetime.date.today().strftime("%B %d, %Y")
    now   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# Job Digest — {today}",
        "",
        f"> **Role:** {role} &nbsp;|&nbsp; "
        f"**Scraped:** {total_scraped} &nbsp;|&nbsp; "
        f"**Location passed:** {location_passed} &nbsp;|&nbsp; "
        f"**Qualifying (score >= {threshold}):** {len(qualifying)}",
        "",
        "---",
        "",
    ]

    if not qualifying:
        lines += [
            "## No qualifying jobs found today",
            "",
            "Try a different role, lower the threshold, or run again tomorrow.",
            "",
        ]
    else:
        lines += ["## Qualifying Matches", ""]
        for rank, job in enumerate(qualifying, 1):
            title    = job.get("job_title",    "Unknown")
            company  = job.get("company_name", "Unknown")
            location = job.get("location")     or "Not specified"
            score    = job.get("score",        0)
            reason   = job.get("reason",       "")
            priority = job.get("priority")
            link     = job.get("link",         "")
            source   = job.get("source",       "")

            priority_badge = f" &nbsp;`{priority}`" if priority else ""
            score_bar      = "█" * (score // 10) + "░" * (10 - score // 10)

            lines += [
                f"### {rank}. [{title} @ {company}]({link})",
                "",
                f"**Score:** {score}/100{priority_badge} &nbsp; `{score_bar}`  ",
                f"**Location:** {location}  ",
                f"**Source:** {source}  ",
                f"**Why:** {reason}",
                "",
                "---",
                "",
            ]

    if also_reviewed:
        lines += [
            f"## Also Reviewed (score < {threshold})",
            "",
        ]
        for job in also_reviewed:
            title    = job.get("job_title",    "Unknown")
            company  = job.get("company_name", "Unknown")
            score    = job.get("score",        0)
            location = (job.get("location") or "—").split("\n")[0]
            link     = job.get("link",         "")
            lines.append(f"- **{score}/100** — [{title} @ {company}]({link}) &nbsp; _{location}_")

        lines += ["", "---", ""]

    lines += [f"_Generated at {now}_", ""]

    DIGEST_FILE.write_text("\n".join(lines), encoding="utf-8")
    typer.echo(f"[✓] Digest written to {DIGEST_FILE}  ({len(qualifying)} qualifying jobs)", err=True)


# ---------------------------------------------------------------------------
# All sources — (parser_fn, url, source_name)
# Defined here so all scraper functions are in scope.
# ---------------------------------------------------------------------------

_ALL_SOURCES = [
    (_scrape_github, GITHUB_README,          "tech-intern"),
    (_scrape_github, GITHUB_FINANCE_INTERNS, "finance-intern"),
]


# ---------------------------------------------------------------------------
# scan command
# ---------------------------------------------------------------------------

@app.command(name="scan")
def scan(
    role: str = typer.Option(
        "Software Engineer", "--role", "-r",
        help="Role to search for, e.g. 'Financial Analyst'",
    ),
    sources: str = typer.Option(
        "", "--sources", "-s",
        help="Comma-separated source names to include (default: all). e.g. 'swe-intern,swe-newgrad,canada-intern'",
    ),
    threshold: int = typer.Option(
        80, "--threshold", "-t",
        help="Minimum score to include in digest (default: 80)",
    ),
    location: str = typer.Option(
        "", "--location", "-l",
        help="Location filter — only jobs matching this location or Remote are scored. Leave empty to disable.",
    ),
    work_model: str = typer.Option(
        "", "--work-model", "-w",
        help="Comma-separated work models to include, e.g. 'Remote,Hybrid'. Leave empty for all.",
    ),
    resume_pdf: str = typer.Option(
        str(RESUME_PDF), "--resume-pdf",
        help="Path to resume PDF (default: 'Xinying Tu resume.pdf')",
    ),
    direction_tags: str = typer.Option(
        "", "--direction-tags",
        help='JSON array of direction tags, e.g. \'["AI/ML","Backend"]\'',
    ),
):
    """
    Super Scanner: scrape Hanzilla → filter → score → write daily_digest.md.

    Scrapes 30 listings per selected source from the 17 hanzili/2026-Internship categories.
    Jobs scoring >= threshold are written to daily_digest.md.
    Already-scored jobs are read from score_cache.json (keyed by URL + resume hash).

    Requires env var ANTHROPIC_API_KEY.

    Examples:
        python main.py scan
        python main.py scan --sources "swe-intern,data-intern" --work-model "Remote"
        python main.py scan --role "Financial Analyst" --location "Toronto"
    """
    asyncio.run(_scan_pipeline(role, sources, threshold, location, work_model, Path(resume_pdf), direction_tags))


async def _scan_pipeline(
    role: str,
    sources: str = "",
    threshold: int = 80,
    location: str = "",
    work_model: str = "",
    resume_pdf: Path = RESUME_PDF,
    direction_tags: str = "",
) -> None:
    # Preflight
    if not resume_pdf.exists():
        typer.echo(f"[!] '{resume_pdf}' not found.", err=True)
        raise typer.Exit(1)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        typer.echo(
            "[!] ANTHROPIC_API_KEY is not set or is empty — "
            "set it as an environment variable and restart the server.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"[*] ANTHROPIC_API_KEY present (length={len(api_key)})", err=True)

    client = anthropic.Anthropic(api_key=api_key)

    # Parse resume PDF once — extracts structured profile + full text
    typer.echo(f"[*] Parsing resume PDF: {resume_pdf.name}…", err=True)
    try:
        profile, resume_text = _parse_resume_pdf(client, resume_pdf)
    except claude_budget.ClaudeBudgetExceeded:
        typer.echo("[!] Daily Claude quota reached — try again tomorrow.", err=True)
        raise typer.Exit(1)
    except Exception as e:
        import traceback
        typer.echo(
            f"[!] Failed to parse resume PDF: {type(e).__name__}: {e}\n"
            + traceback.format_exc(),
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"[✓] Profile: {profile.get('name')} | {profile.get('school')} | GPA {profile.get('gpa')}", err=True)
    typer.echo(f"    Skills: {', '.join(profile.get('skills') or [])}", err=True)
    Path("last_resume_text.txt").write_text(resume_text, encoding="utf-8")

    try:
        dtags: list = json.loads(direction_tags) if direction_tags.strip() else []
        if not isinstance(dtags, list):
            dtags = []
    except (json.JSONDecodeError, ValueError):
        dtags = []
    scoring_prompt = _build_scoring_prompt(profile, dtags)

    # Resume hash + direction-tags hash for cache invalidation
    resume_hash = hashlib.md5(resume_pdf.read_bytes()).hexdigest()[:16]
    tags_hash   = hashlib.md5(json.dumps(sorted(dtags)).encode()).hexdigest()[:8]
    cache = _load_cache()
    cache_hits = 0

    # Resolve active sources
    wm_filter = work_model.strip()
    wm_set: set[str] = {wm.strip() for wm in wm_filter.split(",") if wm.strip()} if wm_filter else set()
    city = location.strip().lower()
    if sources.strip():
        selected = {s.strip() for s in sources.split(",") if s.strip()}
        active_sources = [(fn, url, src) for fn, url, src in _ALL_SOURCES if src in selected]
        if not active_sources:
            typer.echo(f"[!] No matching sources for: {sources!r} — scanning all.", err=True)
            active_sources = list(_ALL_SOURCES)
    else:
        active_sources = list(_ALL_SOURCES)

    loc_display = f"  |  location={location!r}" if location else ""
    wm_display  = f"  |  work_model={sorted(wm_set)}" if wm_set else ""
    typer.echo(f"\n{'='*60}", err=True)
    typer.echo(
        f"  Super Scanner  |  role={role!r}{loc_display}{wm_display}"
        f"  |  sources={len(active_sources)}  |  threshold={threshold}",
        err=True,
    )
    typer.echo(f"{'='*60}\n", err=True)

    # Stage 1 — scrape all listings (no limit; scraping is free)
    jobs: list[dict] = []
    for fn, url, src in active_sources:
        before = len(jobs)
        jobs += fn(9999, url, src)
        typer.echo(f"[*] Source {src!r}: {len(jobs) - before} listings.", err=True)

    # Deduplicate by link
    seen: set[str] = set()
    unique: list[dict] = []
    for j in jobs:
        if j["link"] not in seen:
            seen.add(j["link"])
            unique.append(j)
    jobs = unique
    n_scraped = len(jobs)

    if not jobs:
        typer.echo("[!] No listings found.", err=True)
        _write_digest([], [], 0, 0, role, threshold)
        return

    typer.echo(f"[*] {n_scraped} listings scraped (closed filtered out).", err=True)

    # Stage 2 — keyword pre-filter (title only, zero API cost)
    role_keywords = _build_role_keywords(role)
    jobs = [j for j in jobs if _passes_keyword_filter(j.get("job_title", ""), role_keywords)]
    kw_preview = ", ".join(role_keywords[:8]) + ("…" if len(role_keywords) > 8 else "")
    typer.echo(
        f"[*] Keyword pre-filter: {len(jobs)}/{n_scraped} matched"
        f" '{role}' [{kw_preview}]",
        err=True,
    )

    if not jobs:
        typer.echo("[!] No listings matched keyword pre-filter — try a broader role name.\n", err=True)
        _write_digest([], [], n_scraped, 0, role, threshold)
        return

    # Cap at 50 for Claude scoring (repos list newest first, so head = most recent)
    SCORE_BATCH_SIZE = 50
    pending_candidates = jobs[SCORE_BATCH_SIZE:]
    jobs = jobs[:SCORE_BATCH_SIZE]

    if pending_candidates:
        Path("pending_candidates.json").write_text(
            json.dumps({
                "role": role, "threshold": threshold,
                "location": location, "work_model": work_model,
                "resume_pdf": str(resume_pdf),
                "direction_tags": dtags,
                "candidates": pending_candidates,
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        typer.echo(
            f"[*] Scoring top {len(jobs)} (most recent); "
            f"{len(pending_candidates)} queued for 'Load more'.\n",
            err=True,
        )
    else:
        Path("pending_candidates.json").unlink(missing_ok=True)
        typer.echo(f"[*] Sending {len(jobs)} candidates to scoring pipeline…\n", err=True)

    results: list[dict] = []
    location_passed = 0

    # Stage 2 — filter and score
    for i, job in enumerate(jobs, 1):
        title   = job.get("job_title",    "Unknown")
        company = job.get("company_name", "Unknown")
        url     = job.get("link",         "")

        typer.echo(f"[{i:02d}/{len(jobs):02d}] {title} @ {company}", err=True)

        record: dict = {
            "job_title":         title,
            "company_name":      company,
            "location":          job.get("location", ""),
            "work_model":        job.get("work_model", ""),
            "link":              url,
            "source":            job.get("source", "hanzilla"),
            "score":             None,
            "fit_score":         None,
            "fit_reason":        None,
            "preference_score":  None,
            "preference_reason": None,
            "reason":            None,
            "priority":          None,
            "action":            None,
        }

        # Senior pre-filter
        if _SENIOR_FILTER.search(title):
            typer.echo("  [Skip] Senior/off-track title.\n", err=True)
            record["action"] = "skipped_prefilter"
            results.append(record)
            continue

        # Check score cache before fetching JD
        cache_key = f"{url}|{resume_hash}|{tags_hash}"
        cached = cache.get(cache_key)
        if cached and cached.get("resume_hash") == resume_hash:
            cached_wm = cached.get("work_model", "")
            if cached_wm:
                record["work_model"] = cached_wm
            if not _passes_combined_filter(cached_wm, job.get("location", ""), city, wm_set):
                typer.echo(f"  [Skip] Filtered (cached wm={cached_wm!r}).\n", err=True)
                record["action"] = "filtered_location"
                results.append(record)
                continue
            cache_hits += 1
            priority_tag = f" [{cached.get('priority')}]" if cached.get("priority") else ""
            typer.echo(f"  [Cache] Score {cached['score']}/100{priority_tag} — {cached.get('fit_reason','')}\n", err=True)
            record.update({
                "score":             cached["score"],
                "fit_score":         cached.get("fit_score"),
                "fit_reason":        cached.get("fit_reason", ""),
                "preference_score":  cached.get("preference_score"),
                "preference_reason": cached.get("preference_reason", ""),
                "reason":            cached.get("reason", ""),
                "priority":          cached.get("priority"),
                "action":            "qualifying" if cached["score"] >= threshold else "scored_below_threshold",
            })
            results.append(record)
            location_passed += 1
            continue

        # Fetch JD — needed for location filter, work model detection, and scoring
        jd = await _scrape_jd(url)
        if not jd:
            typer.echo("  [Skip] Could not fetch job description.\n", err=True)
            record["action"] = "skipped_no_jd"
            results.append(record)
            continue

        # Detect work model and apply combined location + work model filter
        detected_wm = _detect_work_model(job.get("location", ""), jd)
        if detected_wm:
            record["work_model"] = detected_wm
        if not _passes_combined_filter(detected_wm, job.get("location", ""), city, wm_set):
            typer.echo(f"  [Skip] Filtered (wm={detected_wm!r}, city={city!r}).\n", err=True)
            record["action"] = "filtered_location"
            results.append(record)
            continue

        location_passed += 1

        # Claude scoring
        try:
            scored = _score_match(client, title, jd, resume_text, scoring_prompt)
        except claude_budget.ClaudeBudgetExceeded:
            typer.echo("  [Stop] Daily Claude quota reached — stopping scan.\n", err=True)
            break
        except Exception as e:
            typer.echo(f"  [Skip] Scoring error: {e}\n", err=True)
            record["action"] = "skipped_scoring_error"
            results.append(record)
            continue

        record.update({
            "score":             scored["score"],
            "fit_score":         scored["fit_score"],
            "fit_reason":        scored["fit_reason"],
            "preference_score":  scored["preference_score"],
            "preference_reason": scored["preference_reason"],
            "reason":            scored["reason"],
            "priority":          scored["priority"],
        })

        # Save to cache keyed by url|resume_hash|tags_hash
        cache[cache_key] = {
            "score":             scored["score"],
            "fit_score":         scored["fit_score"],
            "fit_reason":        scored["fit_reason"],
            "preference_score":  scored["preference_score"],
            "preference_reason": scored["preference_reason"],
            "reason":            scored["reason"],
            "priority":          scored["priority"],
            "resume_hash":       resume_hash,
            "work_model":        detected_wm,
        }
        _save_cache(cache)

        priority_tag = f" [{scored['priority']}]" if scored["priority"] else ""
        if scored["score"] >= threshold:
            typer.echo(f"  [QUALIFYING] Score {scored['score']}/100{priority_tag} — {scored['fit_reason']}\n", err=True)
            record["action"] = "qualifying"
        else:
            typer.echo(f"  Score {scored['score']}/100{priority_tag} — {scored['fit_reason']}\n", err=True)
            record["action"] = "scored_below_threshold"

        results.append(record)

    # Save last_run.json (all results, scored first sorted by score desc)
    scored   = sorted(
        [r for r in results if r["score"] is not None],
        key=lambda r: r["score"], reverse=True,
    )
    unscored = [r for r in results if r["score"] is None]
    last_run = {
        "role":             role,
        "source":           "hanzilla",
        "threshold":        threshold,
        "total_scraped":    n_scraped,
        "location_passed":  location_passed,
        "pending_count":    len(pending_candidates),
        "results":          scored + unscored,
    }
    with open("last_run.json", "w", encoding="utf-8") as f:
        json.dump(last_run, f, indent=2, ensure_ascii=False)
    typer.echo("[✓] Full results saved to last_run.json\n", err=True)

    # Write digest
    qualifying     = [r for r in scored if r["score"] >= threshold]
    also_reviewed  = [r for r in scored if r["score"] < threshold]
    _write_digest(qualifying, also_reviewed, n_scraped, location_passed, role, threshold)

    typer.echo(f"\n{'='*60}", err=True)
    typer.echo(
        f"  Done — {len(qualifying)} qualifying  |  "
        f"{location_passed} scored  |  "
        f"{cache_hits} from cache  |  "
        f"{len(jobs)} keyword-matched / {n_scraped} scraped",
        err=True,
    )
    typer.echo(f"{'='*60}\n", err=True)


@app.command(name="score-more")
def score_more_cmd():
    """Score the next batch of pending candidates from the last scan."""
    asyncio.run(_score_more_pipeline())


async def _score_more_pipeline() -> None:
    pending_file = Path("pending_candidates.json")
    if not pending_file.exists():
        typer.echo("[!] No pending candidates found.", err=True)
        return

    data = json.loads(pending_file.read_text(encoding="utf-8"))
    all_pending = data.get("candidates", [])
    if not all_pending:
        typer.echo("[!] Pending list is empty.", err=True)
        pending_file.unlink(missing_ok=True)
        return

    role          = data.get("role", "Software Engineer")
    threshold     = int(data.get("threshold", 80))
    location      = data.get("location", "")
    work_model    = data.get("work_model", "")
    resume_pdf    = Path(data.get("resume_pdf", str(RESUME_PDF)))
    dtags: list   = data.get("direction_tags", [])

    SCORE_BATCH_SIZE = 50
    batch     = all_pending[:SCORE_BATCH_SIZE]
    remaining = all_pending[SCORE_BATCH_SIZE:]

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        typer.echo("[!] ANTHROPIC_API_KEY not set.", err=True)
        raise typer.Exit(1)
    if not resume_pdf.exists():
        typer.echo(f"[!] '{resume_pdf}' not found.", err=True)
        raise typer.Exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    typer.echo(f"[*] Parsing resume PDF: {resume_pdf.name}…", err=True)
    try:
        profile, resume_text = _parse_resume_pdf(client, resume_pdf)
    except claude_budget.ClaudeBudgetExceeded:
        typer.echo("[!] Daily Claude quota reached — try again tomorrow.", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"[!] Failed to parse resume: {e}", err=True)
        raise typer.Exit(1)
    Path("last_resume_text.txt").write_text(resume_text, encoding="utf-8")
    scoring_prompt = _build_scoring_prompt(profile, dtags)
    resume_hash = hashlib.md5(resume_pdf.read_bytes()).hexdigest()[:16]
    tags_hash   = hashlib.md5(json.dumps(sorted(dtags)).encode()).hexdigest()[:8]
    cache = _load_cache()
    cache_hits = 0

    wm_set = {wm.strip() for wm in work_model.split(",") if wm.strip()} if work_model.strip() else set()
    city = location.strip().lower()

    typer.echo(f"\n{'='*60}", err=True)
    typer.echo(
        f"  Score More  |  {len(batch)} candidates  |  "
        f"{len(remaining)} still pending  |  threshold={threshold}",
        err=True,
    )
    typer.echo(f"{'='*60}\n", err=True)

    new_results: list[dict] = []
    location_passed = 0

    for i, job in enumerate(batch, 1):
        title   = job.get("job_title", "Unknown")
        company = job.get("company_name", "Unknown")
        url     = job.get("link", "")

        typer.echo(f"[{i:02d}/{len(batch):02d}] {title} @ {company}", err=True)

        record: dict = {
            "job_title":         title,
            "company_name":      company,
            "location":          job.get("location", ""),
            "work_model":        job.get("work_model", ""),
            "link":              url,
            "source":            job.get("source", "hanzilla"),
            "score":             None,
            "fit_score":         None,
            "fit_reason":        None,
            "preference_score":  None,
            "preference_reason": None,
            "reason":            None,
            "priority":          None,
            "action":            None,
        }

        if _SENIOR_FILTER.search(title):
            typer.echo("  [Skip] Senior/off-track title.\n", err=True)
            record["action"] = "skipped_prefilter"
            new_results.append(record)
            continue

        cache_key = f"{url}|{resume_hash}|{tags_hash}"
        cached = cache.get(cache_key)
        if cached and cached.get("resume_hash") == resume_hash:
            cached_wm = cached.get("work_model", "")
            if cached_wm:
                record["work_model"] = cached_wm
            if not _passes_combined_filter(cached_wm, job.get("location", ""), city, wm_set):
                record["action"] = "filtered_location"
                new_results.append(record)
                continue
            cache_hits += 1
            priority_tag = f" [{cached.get('priority')}]" if cached.get("priority") else ""
            typer.echo(f"  [Cache] Score {cached['score']}/100{priority_tag} — {cached.get('fit_reason','')}\n", err=True)
            record.update({
                "score":             cached["score"],
                "fit_score":         cached.get("fit_score"),
                "fit_reason":        cached.get("fit_reason", ""),
                "preference_score":  cached.get("preference_score"),
                "preference_reason": cached.get("preference_reason", ""),
                "reason":            cached.get("reason", ""),
                "priority":          cached.get("priority"),
                "action":            "qualifying" if cached["score"] >= threshold else "scored_below_threshold",
            })
            new_results.append(record)
            location_passed += 1
            continue

        jd = await _scrape_jd(url)
        if not jd:
            typer.echo("  [Skip] Could not fetch JD.\n", err=True)
            record["action"] = "skipped_no_jd"
            new_results.append(record)
            continue

        detected_wm = _detect_work_model(job.get("location", ""), jd)
        if detected_wm:
            record["work_model"] = detected_wm
        if not _passes_combined_filter(detected_wm, job.get("location", ""), city, wm_set):
            typer.echo(f"  [Skip] Filtered (wm={detected_wm!r}).\n", err=True)
            record["action"] = "filtered_location"
            new_results.append(record)
            continue
        location_passed += 1

        try:
            scored = _score_match(client, title, jd, resume_text, scoring_prompt)
        except claude_budget.ClaudeBudgetExceeded:
            typer.echo("  [Stop] Daily Claude quota reached — stopping.\n", err=True)
            break
        except Exception as e:
            typer.echo(f"  [Skip] Scoring error: {e}\n", err=True)
            record["action"] = "skipped_scoring_error"
            new_results.append(record)
            continue

        record.update({
            "score":             scored["score"],
            "fit_score":         scored["fit_score"],
            "fit_reason":        scored["fit_reason"],
            "preference_score":  scored["preference_score"],
            "preference_reason": scored["preference_reason"],
            "reason":            scored["reason"],
            "priority":          scored["priority"],
        })
        cache[cache_key] = {
            "score":             scored["score"],
            "fit_score":         scored["fit_score"],
            "fit_reason":        scored["fit_reason"],
            "preference_score":  scored["preference_score"],
            "preference_reason": scored["preference_reason"],
            "reason":            scored["reason"],
            "priority":          scored["priority"],
            "resume_hash":       resume_hash,
            "work_model":        detected_wm,
        }
        _save_cache(cache)

        priority_tag = f" [{scored['priority']}]" if scored["priority"] else ""
        if scored["score"] >= threshold:
            typer.echo(f"  [QUALIFYING] Score {scored['score']}/100{priority_tag} — {scored['fit_reason']}\n", err=True)
            record["action"] = "qualifying"
        else:
            typer.echo(f"  Score {scored['score']}/100{priority_tag} — {scored['fit_reason']}\n", err=True)
            record["action"] = "scored_below_threshold"

        new_results.append(record)

    # Merge into last_run.json
    last_run_file = Path("last_run.json")
    last_run = json.loads(last_run_file.read_text(encoding="utf-8")) if last_run_file.exists() else {
        "role": role, "threshold": threshold, "total_scraped": 0,
    }
    existing = last_run.get("results", [])
    existing_urls = {r.get("link") for r in existing}
    merged = existing + [r for r in new_results if r.get("link") not in existing_urls]
    scored   = sorted([r for r in merged if r["score"] is not None], key=lambda r: r["score"], reverse=True)
    unscored = [r for r in merged if r["score"] is None]

    last_run["results"]       = scored + unscored
    last_run["pending_count"] = len(remaining)
    last_run_file.write_text(json.dumps(last_run, indent=2, ensure_ascii=False), encoding="utf-8")
    typer.echo("[✓] Results merged into last_run.json\n", err=True)

    if remaining:
        data["candidates"] = remaining
        pending_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        typer.echo(f"[*] {len(remaining)} candidates still pending.", err=True)
    else:
        pending_file.unlink(missing_ok=True)
        typer.echo("[✓] All candidates scored.", err=True)

    qualifying = [r for r in scored if r["score"] >= threshold]
    typer.echo(f"\n{'='*60}", err=True)
    typer.echo(
        f"  Done — {len(qualifying)} qualifying  |  {location_passed} scored  |  {cache_hits} from cache",
        err=True,
    )
    typer.echo(f"{'='*60}\n", err=True)


if __name__ == "__main__":
    app()
