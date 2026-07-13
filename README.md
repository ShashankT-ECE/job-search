# Job Search Automation Pipeline

Automates job scraping, ranking, resume tailoring, and PDF generation.

## Current Status

Baseline resume renders from bullet bank (`resume/build_baseline.py`).

## Setup

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

`python resume/build_baseline.py` to generate baseline resume PDF.

## Note

This is a work in progress. Future stages will add JobSpy scraping, Gemini AI tailoring, and a Streamlit dashboard.
