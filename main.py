"""
job-search pipeline orchestrator.

Future stages:
  1. JobSpy scrape → raw job listings
  2. Gemini rank → scored & filtered jobs
  3. Gemini tailor → per-job tailored resume bullets
  4. RenderCV render → PDF resumes via resume/build_baseline.py
  5. Streamlit dashboard → UI for browsing matches
"""

def main():
    print("Job search pipeline — not yet implemented.")


if __name__ == "__main__":
    main()
