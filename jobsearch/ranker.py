"""
jobsearch.ranker — Score scraped jobs against your master resume using Gemini.

Usage:
    python -m jobsearch.ranker
    python -m jobsearch.ranker --limit 5   # test mode: only score N jobs
    python -m jobsearch.ranker --dry-run   # validate setup without API calls

Environment:
    GEMINI_API_KEY in .env file (or export GEMINI_API_KEY=...)

Rate limit:
    Free tier: 15 RPM. We sleep 4.1s between calls (~14.6 RPM).
    On 429 errors, tenacity retries with exponential backoff.
"""

import argparse
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from jobsearch.db import get_connection

# ──────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "outputs" / "jobs.db"
DEFAULT_RESUME = PROJECT_ROOT / "resume" / "master_cv.yaml"
DEFAULT_ENV = PROJECT_ROOT / ".env"

# Model to use — must support structured JSON output
# See https://ai.google.dev/gemini-api/docs/models for current availability
MODEL_NAME = "gemini-2.0-flash"

# Rate limit: 15 RPM free tier → 4.1s between calls (~14.6 RPM)
RATE_LIMIT_DELAY = 4.1

# ──────────────────────────────────────────────────────────────────────
# Schema for Gemini structured output
# ──────────────────────────────────────────────────────────────────────
SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "Match score from 0 (no fit) to 100 (perfect fit)",
        },
        "reason": {
            "type": "string",
            "description": "One-sentence justification for the score, citing specific skill overlaps or gaps",
        },
    },
    "required": ["score", "reason"],
}


# ──────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────

def load_resume_text(resume_path: Path = None) -> str:
    """Load the master resume and return it as a compact text summary."""
    path = resume_path or DEFAULT_RESUME
    if not path.exists():
        print(f"ERROR: Resume file not found: {path}")
        sys.exit(1)

    with open(path) as f:
        data = yaml.safe_load(f)

    if data is None:
        print(f"ERROR: Resume file is empty: {path}")
        sys.exit(1)

    # Build a compact profile from the bullet bank
    lines = []

    # Personal info
    personal = data.get("cv", data.get("personal_info", {}))
    if personal.get("name"):
        lines.append(f"Name: {personal['name']}")
    if personal.get("location"):
        lines.append(f"Location: {personal['location']}")

    # Summary
    summary_bank = data.get("summary_bank", {})
    summary_text = None
    if isinstance(summary_bank, list):
        for item in summary_bank:
            if isinstance(item, dict) and item.get("id") == "sum_digital_verification":
                summary_text = item.get("text", "")
                break
    elif isinstance(summary_bank, dict):
        summary_text = summary_bank.get("sum_digital_verification", "")
    if summary_text:
        lines.append(f"Summary: {summary_text}")

    # Skills (compact)
    skills = data.get("technical_skills", data.get("skills", []))
    if skills:
        skill_labels = []
        for s in skills:
            if isinstance(s, dict):
                skill_labels.append(f"{s.get('label', '')}: {s.get('details', '')}")
        lines.append("Skills: " + " | ".join(skill_labels))

    # Experience (titles + companies only)
    experience = data.get("experience", [])
    if experience:
        lines.append("Experience:")
        for exp in experience:
            company = exp.get("company", "")
            position = exp.get("position", "")
            lines.append(f"  - {position} at {company}")

    # Education
    education = data.get("education", [])
    if education:
        lines.append("Education:")
        for edu in education:
            institution = edu.get("institution", "")
            degree = edu.get("degree", "")
            area = edu.get("area", "")
            lines.append(f"  - {degree} in {area} from {institution}")

    # Projects
    projects = data.get("projects", [])
    if projects:
        lines.append("Projects:")
        for proj in projects:
            name = proj.get("name", "")
            lines.append(f"  - {name}")

    return "\n".join(lines)


def init_gemini():
    """Load API key, configure the SDK, and return a client + config tuple."""
    # Load .env
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not found.")
        print("  1. Create a .env file: cp .env.example .env")
        print("  2. Add your API key: GEMINI_API_KEY=your_key_here")
        print("  3. Get a key at: https://aistudio.google.com/apikey")
        sys.exit(1)

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=SCORE_SCHEMA,
        temperature=0.3,
    )

    return client, config


# ──────────────────────────────────────────────────────────────────────
# Scoring logic
# ──────────────────────────────────────────────────────────────────────

RANKING_PROMPT = textwrap.dedent("""\
    You are a strict technical recruiter specializing in VLSI, ASIC, FPGA, and
    embedded systems roles. Evaluate the job description below against the
    candidate's resume and output a match score from 0 to 100.

    Scoring guidelines:
    - 90-100: Exceptional fit — job requirements align closely with the candidate's
      exact skills, tools, methodologies, and experience level.
    - 70-89: Strong fit — most core requirements match; minor gaps in specific
      tools, seniority, or domain.
    - 50-69: Partial fit — some overlap in domain or skills, but significant gaps
      in tools, experience level, or specialization.
    - 30-49: Weak fit — tangential overlap; the candidate has adjacent skills but
      would need substantial ramp-up.
    - 0-29: Poor fit — little to no alignment between the job and the candidate's
      background.

    Consider:
    1. Technical skill overlap (tools, languages, methodologies)
    2. Domain match (RTL, verification, physical design, analog, FPGA, embedded, etc.)
    3. Seniority/experience level match (intern vs entry-level vs senior)
    4. Overall viability — can this candidate realistically land this role?

    Output a JSON object with:
    - "score": integer from 0 to 100
    - "reason": one concise sentence explaining the score in plain English.
      Cite specific matches or gaps (e.g., "Strong SystemVerilog/UVM overlap but
      role requires 3+ years experience vs candidate's internship level").

    --- CANDIDATE RESUME ---
    {resume}

    --- JOB DESCRIPTION ---
    Title: {title}
    Company: {company}
    Location: {location}

    {description}
    """)


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=3, min=5, max=60),
    retry_error_callback=lambda retry_state: (0, f"Retry exhausted: {retry_state.outcome.exception()}"),
    reraise=False,
)
def score_job(client, config, resume_text: str, job: dict) -> tuple[int, str]:
    """Call Gemini to score a single job. Returns (score, reason)."""
    prompt = RANKING_PROMPT.format(
        resume=resume_text,
        title=job.get("title", "Unknown"),
        company=job.get("company", "Unknown"),
        location=job.get("location", "Unknown"),
        description=job.get("description") or "(No description provided)",
    )

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=config,
    )

    # Parse structured JSON response
    result = json.loads(response.text)
    score = int(result.get("score", 0))
    reason = str(result.get("reason", ""))
    # Clamp score to valid range
    score = max(0, min(100, score))
    return score, reason


# ──────────────────────────────────────────────────────────────────────
# Main orchestrator
# ──────────────────────────────────────────────────────────────────────

def run_ranking(
    db_path: Path = None,
    resume_path: Path = None,
    limit: int = None,
    dry_run: bool = False,
) -> dict:
    """Score all unscored jobs in the database.

    Returns stats dict with keys: total_unscored, scored, errors, skipped.
    """
    db_path = Path(db_path or DEFAULT_DB)
    resume_path = Path(resume_path or DEFAULT_RESUME)

    # Load resume once
    print("Loading resume...")
    resume_text = load_resume_text(resume_path)
    print(f"  Resume loaded: {len(resume_text)} chars\n")

    # Count unscored jobs
    conn = get_connection(db_path)
    try:
        total_unscored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE match_score IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    if total_unscored == 0:
        print("All jobs are already scored — nothing to do.")
        return {"total_unscored": 0, "scored": 0, "errors": 0, "skipped": 0}

    print(f"Jobs to score: {total_unscored}")
    if limit and limit < total_unscored:
        print(f"  (limited to {limit} by --limit flag)")
        total_unscored = limit

    if dry_run:
        print("DRY RUN — skipping API calls. Setup OK.\n")
        return {"total_unscored": total_unscored, "scored": 0, "errors": 0, "skipped": 0}

    # Init Gemini
    print("Initializing Gemini...")
    client, config = init_gemini()
    print(f"  Model: {MODEL_NAME} (structured JSON output)\n")

    # Iterate and score
    scored = 0
    errors = 0
    skipped = 0

    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, title, company, location, description "
            "FROM jobs WHERE match_score IS NULL "
            "ORDER BY id"
        )

        for idx, row in enumerate(cursor):
            if limit and idx >= limit:
                break

            job_id = row["id"]
            title = row["title"] or "Unknown"
            company = row["company"] or "Unknown"

            print(
                f"[{idx + 1}/{total_unscored}] "
                f"{title[:70]} @ {company[:40]} ... ",
                end="",
                flush=True,
            )

            job_dict = {
                "title": title,
                "company": company,
                "location": row["location"],
                "description": row["description"],
            }

            try:
                score, reason = score_job(client, config, resume_text, job_dict)
                conn.execute(
                    "UPDATE jobs SET match_score = ? WHERE id = ?",
                    (score, job_id),
                )
                conn.commit()
                scored += 1
                print(f"Score: {score}")
                if reason:
                    print(f"       Reason: {reason[:120]}")

            except Exception as exc:
                errors += 1
                exc_name = type(exc).__name__
                exc_msg = str(exc)[:120]
                print(f"ERROR: {exc_name}: {exc_msg}")

                # If we hit a quota wall, don't keep banging on the API
                if "429" in exc_msg or "ResourceExhausted" in exc_name or "quota" in exc_msg.lower():
                    if errors >= 3:
                        print("\n  ⚠️  Repeated quota errors — API key may have exhausted its daily limit.")
                        print("  → Verify your key at https://aistudio.google.com/apikey")
                        print("  → Free tier: 1,500 requests/day. Billing may need to be enabled.")
                        print("  → Resuming later will skip already-scored jobs.\n")
                        conn.commit()
                        break

                # Still commit progress so far
                conn.commit()

            # Rate limit: sleep between every call
            if idx < total_unscored - 1:  # no need to sleep after the last one
                time.sleep(RATE_LIMIT_DELAY)

    finally:
        conn.close()

    return {
        "total_unscored": total_unscored,
        "scored": scored,
        "errors": errors,
        "skipped": skipped,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Score scraped jobs against your master resume using Gemini"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to SQLite database (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--resume",
        type=Path,
        default=DEFAULT_RESUME,
        help=f"Path to master resume YAML (default: {DEFAULT_RESUME})",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Only score N jobs (useful for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate setup without making API calls",
    )
    args = parser.parse_args()

    # ── Run ──────────────────────────────────────────────────────────
    stats = run_ranking(
        db_path=args.db,
        resume_path=args.resume,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("RANKING COMPLETE")
    print(f"{'='*60}")
    print(f"  Total unscored (start): {stats['total_unscored']}")
    print(f"  Scored successfully    : {stats['scored']}")
    print(f"  Errors                 : {stats['errors']}")
    print(f"  Database               : {args.db.resolve()}")

    # Show score distribution if any jobs scored
    if stats["scored"] > 0:
        conn = get_connection(args.db)
        try:
            dist = conn.execute(
                "SELECT "
                "  COUNT(CASE WHEN match_score >= 90 THEN 1 END) as excellent,"
                "  COUNT(CASE WHEN match_score >= 70 AND match_score < 90 THEN 1 END) as strong,"
                "  COUNT(CASE WHEN match_score >= 50 AND match_score < 70 THEN 1 END) as partial,"
                "  COUNT(CASE WHEN match_score >= 30 AND match_score < 50 THEN 1 END) as weak,"
                "  COUNT(CASE WHEN match_score < 30 THEN 1 END) as poor "
                "FROM jobs WHERE match_score IS NOT NULL"
            ).fetchone()
            print(f"\n  Score distribution (all scored jobs):")
            print(f"    90-100 (Excellent) : {dist['excellent']}")
            print(f"    70-89  (Strong)    : {dist['strong']}")
            print(f"    50-69  (Partial)   : {dist['partial']}")
            print(f"    30-49  (Weak)      : {dist['weak']}")
            print(f"     0-29  (Poor)      : {dist['poor']}")
        finally:
            conn.close()

    print(f"{'='*60}")


if __name__ == "__main__":
    main()
