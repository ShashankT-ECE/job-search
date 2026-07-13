"""
jobsearch.db — Lightweight SQLite database for job listings.

Schema:
  jobs
    id                  INTEGER PRIMARY KEY AUTOINCREMENT
    site                TEXT    — source site (linkedin, indeed, glassdoor, ...)
    job_url             TEXT    — UNIQUE, prevents duplicate listings
    title               TEXT
    company             TEXT
    location            TEXT
    date_posted         TEXT    — as reported by the job board
    description         TEXT    — full job description (markdown)
    scraped_at          TEXT    — ISO 8601 timestamp of when we scraped it
    match_score         INTEGER — NULL until the LLM ranker runs
    tailored_resume_path TEXT   — NULL until Gemini tailors a resume

Usage:
    from jobsearch.db import init_db, insert_jobs, get_stats
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Default database location
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "outputs" / "jobs.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    site                 TEXT    NOT NULL,
    job_url              TEXT    NOT NULL UNIQUE,
    title                TEXT,
    company              TEXT,
    location             TEXT,
    date_posted          TEXT,
    description          TEXT,
    scraped_at           TEXT    NOT NULL,
    match_score          INTEGER DEFAULT NULL,
    tailored_resume_path TEXT    DEFAULT NULL
);
"""

# Index for fast lookups by URL (used by UPSERT logic)
CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(job_url);
"""

# Index for the ranker: filter unscored jobs
CREATE_INDEX_SCORE_SQL = """
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(match_score);
"""


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Return a connection to the jobs database."""
    path = str(db_path or DEFAULT_DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Optional[Path] = None) -> Path:
    """Create the database and jobs table if they don't exist.

    Returns the resolved path to the database file.
    """
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_INDEX_SQL)
        conn.execute(CREATE_INDEX_SCORE_SQL)
        conn.commit()
    finally:
        conn.close()

    return path


def insert_jobs(jobs: list[dict], db_path: Optional[Path] = None) -> int:
    """Insert a batch of job listings. Duplicates (by job_url) are silently skipped.

    Args:
        jobs: List of dicts with keys matching the jobs table columns.
              Only 'site', 'job_url', and 'scraped_at' are required.
        db_path: Optional path override.

    Returns:
        Number of NEW rows inserted (duplicates excluded).
    """
    if not jobs:
        return 0

    path = db_path or DEFAULT_DB_PATH
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection(path)
    inserted = 0
    try:
        for job in jobs:
            try:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs
                        (site, job_url, title, company, location,
                         date_posted, description, scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.get("site", ""),
                        job.get("job_url", ""),
                        job.get("title"),
                        job.get("company"),
                        job.get("location"),
                        job.get("date_posted"),
                        job.get("description"),
                        now,
                    ),
                )
                if cursor.rowcount > 0:
                    inserted += 1
            except sqlite3.Error as exc:
                print(f"  DB WARNING: skipping row — {exc}")

        conn.commit()
    finally:
        conn.close()

    return inserted


def get_stats(db_path: Optional[Path] = None) -> dict:
    """Return summary stats from the database."""
    path = db_path or DEFAULT_DB_PATH
    conn = get_connection(path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_site = {
            row["site"]: row["count"]
            for row in conn.execute(
                "SELECT site, COUNT(*) as count FROM jobs GROUP BY site"
            ).fetchall()
        }
        scored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE match_score IS NOT NULL"
        ).fetchone()[0]
        return {"total": total, "by_site": by_site, "scored": scored}
    finally:
        conn.close()
