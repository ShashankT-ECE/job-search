"""
jobsearch.scraper — Run JobSpy against config.yaml and persist to SQLite.

Usage:
    python -m jobsearch.scraper          # uses default config.yaml
    python -m jobsearch.scraper --config my_config.yaml
"""

import argparse
import random
import sys
import time
from pathlib import Path

import yaml

from jobsearch.db import init_db, insert_jobs, get_stats

try:
    from jobspy import scrape_jobs
except ImportError:
    print("ERROR: python-jobspy is not installed. Run: pip install python-jobspy")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"
DEFAULT_DB = PROJECT_ROOT / "outputs" / "jobs.db"

# Map site name strings to JobSpy site identifiers
SITE_MAP = {
    "linkedin": "linkedin",
    "indeed": "indeed",
    "glassdoor": "glassdoor",
    "zip_recruiter": "zip_recruiter",
    "bayt": "bayt",
}


# ──────────────────────────────────────────────────────────────────────
# Main scraping logic
# ──────────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    """Load and validate the YAML config file."""
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    if cfg is None or "search" not in cfg:
        print("ERROR: config.yaml must have a top-level 'search' key.")
        sys.exit(1)

    return cfg


def run_scrape(config_path: Path = None, db_path: Path = None) -> int:
    """Execute the full scrape pipeline.

    Returns the number of new jobs inserted.
    """
    config_path = Path(config_path or DEFAULT_CONFIG)
    db_path = Path(db_path or DEFAULT_DB)

    cfg = load_config(config_path)
    search = cfg["search"]

    terms = search.get("terms", [])
    locations = search.get("locations", [])
    results_wanted = search.get("results_wanted", 15)
    hours_old = search.get("hours_old", 72)
    sites = search.get("sites", ["linkedin", "indeed", "glassdoor"])
    min_delay = search.get("min_delay", 3)
    max_delay = search.get("max_delay", 7)

    # Ensure the database is ready
    db_file = init_db(db_path)
    print(f"Database: {db_file}")

    total_queries = len(terms) * len(locations)
    total_inserted = 0
    query_num = 0

    print(f"\n{'='*60}")
    print(f"JobSpy Scraper — {total_queries} queries ({len(terms)} terms × {len(locations)} locations)")
    print(f"Sites: {', '.join(sites)}")
    print(f"Max results per query: {results_wanted}  |  Freshness: ≤{hours_old}h old")
    print(f"{'='*60}\n")

    for term in terms:
        for location in locations:
            query_num += 1
            delay = random.uniform(min_delay, max_delay)

            print(f"[{query_num}/{total_queries}] term={term!r}  location={location!r}")
            print(f"  Sleeping {delay:.1f}s before query...")

            if query_num > 1:  # No delay before the very first query
                time.sleep(delay)

            try:
                df = scrape_jobs(
                    site_name=sites,
                    search_term=term,
                    location=location,
                    results_wanted=results_wanted,
                    hours_old=hours_old,
                    country_indeed="india",
                    linkedin_fetch_description=True,
                )
            except Exception as exc:
                print(f"  ERROR during scrape_jobs: {exc}")
                continue

            if df is None or df.empty:
                print(f"  → 0 results")
                continue

            # Convert DataFrame rows to dicts for the DB layer
            jobs_list = []
            for _, row in df.iterrows():
                jobs_list.append({
                    "site": str(row.get("site", "")),
                    "job_url": str(row.get("job_url", "")),
                    "title": str(row.get("title", "")) if row.get("title") is not None else None,
                    "company": str(row.get("company", "")) if row.get("company") is not None else None,
                    "location": str(row.get("location", "")) if row.get("location") is not None else None,
                    "date_posted": str(row.get("date_posted", "")) if row.get("date_posted") is not None else None,
                    "description": str(row.get("description", "")) if row.get("description") is not None else None,
                })

            new_count = insert_jobs(jobs_list, db_path=db_path)
            total_inserted += new_count
            duplicates = len(jobs_list) - new_count
            print(f"  → {len(jobs_list)} returned, {new_count} new, {duplicates} duplicates")

    return total_inserted


def main():
    parser = argparse.ArgumentParser(
        description="Scrape jobs via JobSpy and store in SQLite"
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to config YAML (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to SQLite database (default: {DEFAULT_DB})",
    )
    args = parser.parse_args()

    # ── Run ──────────────────────────────────────────────────────────
    before_stats = get_stats(args.db)
    new_jobs = run_scrape(config_path=args.config, db_path=args.db)
    after_stats = get_stats(args.db)

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"SCRAPE COMPLETE")
    print(f"{'='*60}")
    print(f"  New jobs added this run : {new_jobs}")
    print(f"  Total jobs in database  : {after_stats['total']}")
    if after_stats["by_site"]:
        print(f"  Breakdown by site:")
        for site, count in sorted(after_stats["by_site"].items()):
            print(f"    {site:20s} : {count}")
    print(f"  Database path           : {args.db.resolve()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
