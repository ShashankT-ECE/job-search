#!/usr/bin/env python3
"""
build_baseline.py — Transform master_cv.yaml (bullet bank format) into
a valid RenderCV 2.3 YAML and render it to PDF.

Usage:
    python build_baseline.py
    python build_baseline.py --input resume/master_cv.yaml
    python build_baseline.py --dry-run  # generate YAML only, don't render

The script:
  1. Reads resume/master_cv.yaml
  2. Resolves highlight IDs → full text from each entry's highlight_bank
  3. Maps to RenderCV's entry types (ExperienceEntry, EducationEntry, NormalEntry, etc.)
  4. Writes resume/output/baseline_cv.yaml
  5. Calls `rendercv render` to produce a PDF
"""

import argparse
import copy
import os
import subprocess
import sys
from pathlib import Path

# Use ruamel.yaml for round-trip preservation (installed with rendercv)
from ruamel.yaml import YAML

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path("/home/shashankt/job-search")
RESUME_DIR = PROJECT_ROOT / "resume"
DEFAULT_INPUT = RESUME_DIR / "master_cv.yaml"
OUTPUT_DIR = RESUME_DIR / "output"
OUTPUT_YAML = OUTPUT_DIR / "baseline_cv.yaml"

# Default summary ID to use if nothing else is specified
DEFAULT_SUMMARY_ID = "sum_digital_verification"

# Map user section keys to RenderCV standard section names
# (RenderCV auto-detects entry types from the fields, so the section
#  key name is just a display title.)
SECTION_TITLE_MAP = {
    "experience": "Experience",
    "education": "Education",
    "projects": "Projects",
    "skills": "Skills",
    "publications": "Publications",
    "awards": "Awards",
    "certifications": "Certifications",
}


# ──────────────────────────────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────────────────────────────

def resolve_highlights(entry: dict) -> list[str] | None:
    """Given an entry dict with `highlight_bank` and `default_highlights`,
    resolve the default highlight IDs to their full text.

    Returns None if there are no default_highlights.
    """
    highlight_bank = entry.get("highlight_bank", {})
    default_ids = entry.get("default_highlights", [])

    if not default_ids:
        return None

    resolved = []
    for hid in default_ids:
        text = highlight_bank.get(hid)
        if text is not None:
            resolved.append(text.strip())
        else:
            print(f"  WARNING: highlight ID '{hid}' not found in highlight_bank, skipping")

    return resolved if resolved else None


def pick_summary(summary_bank: dict, preferred_id: str = DEFAULT_SUMMARY_ID) -> str | None:
    """Pick a summary from the summary_bank. Falls back to the first entry."""
    if not summary_bank:
        return None
    if preferred_id in summary_bank:
        return summary_bank[preferred_id].strip()
    # Fall back to first entry
    first_key = next(iter(summary_bank))
    print(f"  WARNING: '{preferred_id}' not found in summary_bank, using '{first_key}' instead")
    return summary_bank[first_key].strip()


def build_cv_sections(data: dict) -> dict:
    """Build the RenderCV `sections` dict from the user's bullet-bank data.

    Returns a dict suitable for the `cv.sections` field.
    """
    sections = {}

    # --- Summary / Welcome section ---
    summary_text = pick_summary(data.get("summary_bank", {}))
    if summary_text:
        # Use a TextEntry section (bare list of strings)
        sections["Summary"] = [summary_text]

    # --- Experience ---
    experience_entries = []
    for entry in data.get("experience", []):
        highlights = resolve_highlights(entry)
        if highlights is None:
            continue  # skip entries with no default highlights

        exp = {
            "company": entry.get("company", ""),
            "position": entry.get("position", ""),
            "start_date": entry.get("start_date"),
            "end_date": entry.get("end_date"),
            "location": entry.get("location"),
            "summary": entry.get("summary"),
            "highlights": highlights,
        }
        # Remove None values so RenderCV sees clean entries
        exp = {k: v for k, v in exp.items() if v is not None}
        experience_entries.append(exp)

    if experience_entries:
        sections["Experience"] = experience_entries

    # --- Education ---
    education_entries = []
    for entry in data.get("education", []):
        highlights = resolve_highlights(entry)
        edu = {
            "institution": entry.get("institution", ""),
            "area": entry.get("area", ""),
            "degree": entry.get("degree"),
            "grade": entry.get("grade"),
            "start_date": entry.get("start_date"),
            "end_date": entry.get("end_date"),
            "location": entry.get("location"),
            "summary": entry.get("summary"),
            "highlights": highlights,
        }
        edu = {k: v for k, v in edu.items() if v is not None}
        # institution and area are required
        if edu.get("institution") and edu.get("area"):
            education_entries.append(edu)

    if education_entries:
        sections["Education"] = education_entries

    # --- Projects ---
    project_entries = []
    for entry in data.get("projects", []):
        highlights = resolve_highlights(entry)
        proj = {
            "name": entry.get("name", ""),
            "start_date": entry.get("start_date"),
            "end_date": entry.get("end_date"),
            "location": entry.get("location"),
            "summary": entry.get("summary"),
            "highlights": highlights,
        }
        proj = {k: v for k, v in proj.items() if v is not None}
        if proj.get("name"):
            project_entries.append(proj)

    if project_entries:
        sections["Projects"] = project_entries

    # --- Skills (OneLineEntry) ---
    skills_entries = []
    for entry in data.get("skills", []):
        sk = {
            "label": entry.get("label", ""),
            "details": entry.get("details", ""),
        }
        if sk["label"] and sk["details"]:
            skills_entries.append(sk)

    if skills_entries:
        sections["Skills"] = skills_entries

    # --- Publications ---
    publication_entries = []
    for entry in data.get("publications", []):
        pub = {
            "title": entry.get("title", ""),
            "authors": entry.get("authors", []),
            "doi": entry.get("doi"),
            "url": entry.get("url"),
            "journal": entry.get("journal"),
            "date": entry.get("date"),
        }
        pub = {k: v for k, v in pub.items() if v is not None or k in ("title", "authors")}
        if pub.get("title") and pub.get("authors"):
            publication_entries.append(pub)

    if publication_entries:
        sections["Publications"] = publication_entries

    # --- Custom TextEntry sections (awards, certifications, etc.) ---
    for section_key in ("awards", "certifications"):
        entries = data.get(section_key, [])
        if entries:
            title = SECTION_TITLE_MAP.get(section_key, section_key.title())
            # If entries are dicts with 'bullet' key, treat as BulletEntry
            if entries and isinstance(entries[0], dict) and "bullet" in entries[0]:
                sections[title] = [{"bullet": e.get("bullet", "")} for e in entries]
            else:
                sections[title] = entries  # TextEntry: list of strings

    return sections


def build_design_section() -> dict:
    """Return a reasonable design section for the classic theme."""
    return {
        "theme": "classic",
        "page": {
            "size": "us-letter",
            "top_margin": "2cm",
            "bottom_margin": "2cm",
            "left_margin": "2cm",
            "right_margin": "2cm",
            "show_page_numbering": True,
            "show_last_updated_date": True,
        },
        "text": {
            "font_family": "Source Sans 3",
            "font_size": "10pt",
        },
    }


def build_rendercv_yaml(data: dict, today_str: str = "2026-07-13") -> dict:
    """Build the complete RenderCV YAML structure from the bullet-bank data."""
    personal = data.get("personal_info", {})

    # Build social_networks
    social_networks = []
    for sn in personal.get("social_networks", []):
        social_networks.append({
            "network": sn.get("network", ""),
            "username": sn.get("username", ""),
        })

    # Build CV section — skip empty/None phone (phonenumbers lib is strict)
    raw_phone = personal.get("phone")
    phone = raw_phone if raw_phone and str(raw_phone).strip() else None

    cv = {
        "name": personal.get("name", ""),
        "location": personal.get("location"),
        "email": personal.get("email"),
        "phone": phone,
        "website": personal.get("website"),
        "social_networks": social_networks if social_networks else None,
        "sections": build_cv_sections(data),
        "sort_entries": "reverse-chronological",
    }
    # Remove None/empty top-level cv fields
    cv = {k: v for k, v in cv.items() if v is not None and v != ""}

    rendercv_yaml = {
        "cv": cv,
        "design": build_design_section(),
        "locale": {
            "language": "en",
        },
        "rendercv_settings": {
            "date": today_str,
            "bold_keywords": [],
            "sort_entries": "reverse-chronological",
        },
    }

    return rendercv_yaml


def write_yaml(data: dict, path: Path) -> None:
    """Write a dict to a YAML file using ruamel.yaml."""
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 120  # wide lines
    yaml.default_flow_style = False

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f)
    print(f"  Wrote: {path}")


def render_pdf(yaml_path: Path) -> bool:
    """Call rendercv render on the generated YAML. Returns True on success."""
    print(f"\n  Rendering with RenderCV...")

    # Use explicit output paths so we control where files land
    output_folder = OUTPUT_DIR / "rendercv_output"
    pdf_dest = OUTPUT_DIR / "baseline_cv.pdf"

    result = subprocess.run(
        [
            "rendercv", "render", str(yaml_path),
            "-o", str(output_folder.name),
            "-pdf", str(pdf_dest),
        ],
        cwd=str(OUTPUT_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        print(f"\n  RENDER FAILED with exit code {result.returncode}")
        print(f"  STDOUT:\n{result.stdout}")
        print(f"  STDERR:\n{result.stderr}")
        return False

    print(result.stdout)

    if pdf_dest.exists():
        print(f"  PDF generated: {pdf_dest}")
        return True

    # Fallback: check default output folder
    default_output = OUTPUT_DIR / "rendercv_output"
    if default_output.exists():
        pdfs = list(default_output.glob("*.pdf"))
        if pdfs:
            print(f"  PDF generated: {pdfs[0]}")
            return True

    print(f"  WARNING: Render completed but no PDF found. Contents of {OUTPUT_DIR}:")
    for f in OUTPUT_DIR.iterdir():
        print(f"    -> {f}")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Transform master_cv.yaml into RenderCV YAML and PDF"
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Path to master_cv.yaml (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Generate YAML only, skip PDF rendering",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=OUTPUT_YAML,
        help=f"Output YAML path (default: {OUTPUT_YAML})",
    )
    args = parser.parse_args()

    # ── 1. Read input ──────────────────────────────────────────────
    input_path = args.input
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        print("Create one by copying the example:")
        print(f"  cp {RESUME_DIR / 'master_cv.yaml.example'} {input_path}")
        sys.exit(1)

    print(f"Reading: {input_path}")
    yaml = YAML()
    with open(input_path) as f:
        data = yaml.load(f)

    if data is None:
        print("ERROR: Input file is empty or invalid YAML.")
        sys.exit(1)

    # ── 2. Build RenderCV YAML ─────────────────────────────────────
    from datetime import date
    today_str = date.today().isoformat()

    print("Building RenderCV YAML...")
    output_data = build_rendercv_yaml(data, today_str)

    # ── 3. Write output ────────────────────────────────────────────
    output_path = args.output
    write_yaml(output_data, output_path)

    # ── 4. Render PDF (unless dry run) ─────────────────────────────
    if args.dry_run:
        print("\nDry run complete. YAML written. Skipping PDF render.")
        return

    success = render_pdf(output_path)
    if not success:
        print("\nTrying to re-validate by reading output YAML back...")
        # Read back and check for obvious issues
        with open(output_path) as f:
            re_read = yaml.load(f)
        print(f"  Output YAML re-read OK. Top-level keys: {list(re_read.keys())}")
        sys.exit(1)

    print("\nDone! Baseline CV generated successfully.")
    print(f"  YAML: {output_path}")
    pdf_path = OUTPUT_DIR / "baseline_cv.pdf"
    if pdf_path.exists():
        print(f"  PDF:  {pdf_path}")
    else:
        # Check default output folder
        for p in (OUTPUT_DIR / "rendercv_output").glob("*.pdf"):
            print(f"  PDF:  {p}")


if __name__ == "__main__":
    main()
