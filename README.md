# Job Search Automation Pipeline

End-to-end automation for VLSI/embedded job hunting: scrape listings,
AI-rank matches, generate tailored resumes, and browse via a web dashboard.

## Project Structure

```
job-search/
├── app.py                   # Streamlit dashboard (web UI)
├── main.py                  # CLI orchestrator (--scrape, --rank, --tailor, etc.)
├── config.yaml              # Search terms, locations, rate-limit settings
├── requirements.txt
├── .env.example             # GEMINI_API_KEY setup reference
├── jobsearch/
│   ├── scraper.py           # JobSpy → SQLite (iterative term×location loop)
│   ├── db.py                # SQLite schema, insert, stats helpers
│   ├── ranker.py            # Gemini scoring (structured JSON, 4.1s rate limit)
│   └── tailor.py            # Gemini bullet selection → RenderCV → PDF
├── resume/
│   ├── master_cv.yaml       # Your bullet bank (source data — list-of-dicts format)
│   ├── build_baseline.py    # Default resume: bullet bank → RenderCV YAML → PDF
│   ├── SCHEMA_NOTES.md      # RenderCV 2.3 schema reference
│   └── output/              # Generated YAML + PDF (gitignored)
└── outputs/                 # SQLite database (gitignored)
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file with your Gemini API key:
```bash
cp .env.example .env
# Edit .env → GEMINI_API_KEY=your_key_here
# Get a key at: https://aistudio.google.com/apikey
```

## Pipeline

| Step | Command | Description |
|---|---|---|
| **Scrape** | `python main.py --scrape` | JobSpy scrapes LinkedIn, Indeed, Glassdoor → `outputs/jobs.db` |
| **Rank** | `python main.py --rank` | Gemini scores every job 0–100 against your resume |
| **Tailor** | `python main.py --tailor` | Gemini selects best-fit bullets, generates tailored PDFs for score ≥ 80 |
| **Dashboard** | `python main.py --dashboard` | Streamlit UI → browse, 1-click apply, download PDFs |
| **Baseline** | `python main.py --build-resume` | One-off: render your default resume from the bullet bank |

### Optional flags

```bash
python main.py --scrape                          # full scrape
python main.py --rank --limit 10                 # test-rank 10 jobs
python main.py --rank --dry-run                  # validate setup only
python main.py --tailor --limit 3                # tailor top 3 matches
python main.py --tailor --mock                   # test assembly without API
python main.py --tailor --min-score 70           # lower threshold
python main.py --dashboard                       # launch web UI at localhost:8501
```

## How It Works

```
config.yaml          master_cv.yaml
     │                     │
     ▼                     ▼
JobSpy scraper        Gemini ranker (0–100)
     │                     │
     ▼                     ▼
outputs/jobs.db ◄──── match_score
     │
     ▼
Gemini tailor ──► selects best bullets from master_cv.yaml
     │
     ▼
resume/output/tailored_{id}.pdf
     │
     ▼
Streamlit dashboard (browse, apply, download)
```

## Tech Stack

| Component | Library |
|---|---|
| Job scraping | `python-jobspy` (LinkedIn, Indeed, Glassdoor) |
| AI scoring + tailoring | `google-genai` (gemini-2.0-flash) with structured JSON output |
| Resume rendering | `rendercv` 2.3 (Typst → PDF) |
| Database | SQLite (`outputs/jobs.db`) |
| Dashboard | Streamlit |
| Rate limiting | `tenacity` (exponential backoff) + 4.1s delay (15 RPM free tier) |

## API Quota Note

The Gemini free tier provides ~1,500 requests/day. If you hit a quota wall:
- Wait for the daily reset (midnight UTC / ~5:30 AM IST)
- Use `--limit N` to process fewer jobs per run
- Use `--dry-run` to validate setup without consuming quota
- Use `--mock` (tailor only) to test the assembly pipeline without API calls
