"""
job-search pipeline orchestrator.

Stages:
  1. JobSpy scrape → raw job listings           (--scrape)
  2. Gemini rank → scored & filtered jobs        (--rank)
  3. Gemini tailor → per-job tailored resume     (future)
  4. RenderCV render → PDF resumes               (--build-resume)
  5. Streamlit dashboard → UI for browsing       (future)
"""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Job Search Automation Pipeline",
    )
    parser.add_argument(
        "--scrape",
        action="store_true",
        help="Run the JobSpy scraper to collect fresh job listings",
    )
    parser.add_argument(
        "--rank",
        action="store_true",
        help="Run Gemini ranking on scraped jobs (score 0-100)",
    )
    parser.add_argument(
        "--build-resume",
        action="store_true",
        help="Generate baseline resume PDF from master_cv.yaml",
    )
    # Future flags (not yet implemented, shown for discoverability)
    parser.add_argument(
        "--tailor",
        action="store_true",
        help="(Future) Run Gemini resume tailoring for top-ranked jobs",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="(Future) Launch the Streamlit dashboard",
    )

    args, unknown = parser.parse_known_args()

    # Default: if no flags given, print help
    if not any([args.scrape, args.rank, args.build_resume, args.tailor, args.dashboard]):
        parser.print_help()
        return

    # ── Scrape ───────────────────────────────────────────────────────
    if args.scrape:
        from jobsearch.scraper import run_scrape as scraper_run
        from jobsearch.db import init_db, get_stats

        db_path = Path(__file__).resolve().parent / "outputs" / "jobs.db"
        config_path = Path(__file__).resolve().parent / "config.yaml"

        init_db(db_path)  # ensure tables exist before get_stats
        before = get_stats(db_path)
        new_jobs = scraper_run(config_path=config_path, db_path=db_path)
        after = get_stats(db_path)

        print(f"\n{'='*60}")
        print(f"SCRAPE COMPLETE")
        print(f"{'='*60}")
        print(f"  New jobs added this run : {new_jobs}")
        print(f"  Total jobs in database  : {after['total']}")
        if after["by_site"]:
            print(f"  Breakdown by site:")
            for site, count in sorted(after["by_site"].items()):
                print(f"    {site:20s} : {count}")
        print(f"  Database path           : {db_path.resolve()}")
        print(f"{'='*60}")

    # ── Rank ─────────────────────────────────────────────────────────
    if args.rank:
        from jobsearch.ranker import main as ranker_main

        # Pass remaining argv to ranker's argparse (supports --limit, --dry-run)
        # Save original argv, remove '--rank' so ranker doesn't choke on it
        original_argv = sys.argv[:]
        sys.argv = [a for a in sys.argv if a != "--rank"]
        try:
            ranker_main()
        finally:
            sys.argv = original_argv

    # ── Build Resume ─────────────────────────────────────────────────
    if args.build_resume:
        import subprocess

        build_script = Path(__file__).resolve().parent / "resume" / "build_baseline.py"
        print(f"Running: {build_script}")
        result = subprocess.run(
            [sys.executable, str(build_script)],
            cwd=str(Path(__file__).resolve().parent),
        )
        sys.exit(result.returncode)

    # ── Future stubs ─────────────────────────────────────────────────
    if args.tailor:
        print("Tailoring via Gemini — not yet implemented.")
        sys.exit(1)

    if args.dashboard:
        print("Streamlit dashboard — not yet implemented.")
        sys.exit(1)


if __name__ == "__main__":
    main()
