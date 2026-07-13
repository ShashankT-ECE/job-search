"""
jobsearch.ranker — Score scraped jobs against your master resume using DeepSeek.

Usage:
    python -m jobsearch.ranker
    python -m jobsearch.ranker --limit 5   # test mode: only score N jobs
    python -m jobsearch.ranker --dry-run   # validate setup without API calls

Environment:
    DEEPSEEK_API_KEY in .env file (or export DEEPSEEK_API_KEY=...)
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

# DeepSeek model (OpenAI-compatible API)
MODEL_NAME = "deepseek-v4-flash"

# Rate limit: DeepSeek has generous limits, 1s delay is polite
RATE_LIMIT_DELAY = 1.0

# JSON output instruction appended to the prompt
JSON_FORMAT_INSTRUCTION = (
    "You MUST respond with ONLY a valid JSON object. "
    "No markdown, no code fences, no extra text. "
    'The JSON object must have exactly these keys: {"score": integer, "reason": string}. '
    "Score is an integer from 0 to 100.\n"
)


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


def init_deepseek():
    """Load API key, create OpenAI client pointed at DeepSeek."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY not found.")
        print("  1. Create a .env file: cp .env.example .env")
        print("  2. Add your key: DEEPSEEK_API_KEY=sk-...")
        print("  3. Get a key at: https://platform.deepseek.com/api_keys")
        sys.exit(1)

    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )
    return client


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
def score_job(client, resume_text: str, job: dict) -> tuple[int, str]:
    """Call DeepSeek to score a single job. Returns (score, reason)."""
    prompt = RANKING_PROMPT.format(
        resume=resume_text,
        title=job.get("title", "Unknown"),
        company=job.get("company", "Unknown"),
        location=job.get("location", "Unknown"),
        description=job.get("description") or "(No description provided)",
    )
    # Prepend JSON format instruction
    prompt = JSON_FORMAT_INSTRUCTION + "\n" + prompt

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )

    # Parse JSON response
    content = response.choices[0].message.content
    result = json.loads(content)
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

    # Init DeepSeek
    print("Initializing DeepSeek...")
    client = init_deepseek()
    print(f"  Model: {MODEL_NAME} (JSON mode)\n")

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
                score, reason = score_job(client, resume_text, job_dict)
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

                # If we hit repeated errors, don't keep hammering the API
                if "429" in exc_msg or "Rate" in exc_name or "quota" in exc_msg.lower():
                    if errors >= 3:
                        print("\n  ⚠️  Repeated rate-limit errors — stopping.")
                        print("  → Check your DeepSeek account balance at https://platform.deepseek.com")
                        print("  → Resuming later will skip already-scored jobs.\n")
                        conn.commit()
                        break

                # Still commit progress so far
                conn.commit()

            # Polite delay between calls
            if idx < total_unscored - 1:
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
        description="Score scraped jobs against your master resume using DeepSeek"
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"Path to SQLite database (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--resume", type=Path, default=DEFAULT_RESUME,
        help=f"Path to master resume YAML (default: {DEFAULT_RESUME})",
    )
    parser.add_argument(
        "--limit", "-n", type=int, default=None,
        help="Only score N jobs (useful for testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate setup without making API calls",
    )
    args = parser.parse_args()

    stats = run_ranking(
        db_path=args.db,
        resume_path=args.resume,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    print(f"\n{'='*60}")
    print("RANKING COMPLETE")
    print(f"{'='*60}")
    print(f"  Total unscored (start): {stats['total_unscored']}")
    print(f"  Scored successfully    : {stats['scored']}")
    print(f"  Errors                 : {stats['errors']}")
    print(f"  Database               : {args.db.resolve()}")

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
