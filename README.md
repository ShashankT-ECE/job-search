# Job Search Automation Pipeline

Automates job scraping, ranking, resume tailoring, and PDF generation.

## Current Status

**Baseline resume renders from bullet bank** — `resume/build_baseline.py` converts
`resume/master_cv.yaml` (a bullet bank with highlight variants) into a valid
RenderCV 2.3 YAML and renders it to PDF.

## Project Structure

```
job-search/
├── jobsearch/              # Future: scrape, rank, tailor modules
├── resume/
│   ├── master_cv.yaml      # Your bullet bank (source data)
│   ├── build_baseline.py   # Converter: bullet bank → RenderCV YAML → PDF
│   ├── SCHEMA_NOTES.md     # RenderCV 2.3 schema reference
│   └── output/             # Generated YAML + PDF (gitignored)
├── config.yaml             # Future: search terms, weights, locations
├── main.py                 # Future: pipeline orchestrator stub
└── requirements.txt
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# Generate baseline resume PDF from your bullet bank
python resume/build_baseline.py

# Dry run — generate YAML only, skip PDF render
python resume/build_baseline.py --dry-run

# PDF output: resume/output/baseline_cv.pdf
```

## Pipeline Stages (Future)

1. **JobSpy scrape** — collect job listings from multiple boards
2. **Gemini rank** — score and filter jobs by match quality
3. **Gemini tailor** — generate per-job tailored resume bullets
4. **RenderCV render** — produce tailored PDFs via this converter
5. **Streamlit dashboard** — browse matches and generated resumes
