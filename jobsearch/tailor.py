"""
jobsearch.tailor — Select best-fit resume bullets via DeepSeek and render tailored PDFs.

Usage:
    python -m jobsearch.tailor
    python -m jobsearch.tailor --limit 3
    python -m jobsearch.tailor --min-score 70
    python -m jobsearch.tailor --dry-run

Environment:
    DEEPSEEK_API_KEY in .env file
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from jobsearch.db import get_connection

# ──────────────────────────────────────────────────────────────────────
# Paths & constants
# ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESUME_DIR = PROJECT_ROOT / "resume"
OUTPUT_DIR = RESUME_DIR / "output"
DEFAULT_DB = PROJECT_ROOT / "outputs" / "jobs.db"
DEFAULT_RESUME = RESUME_DIR / "master_cv.yaml"

MODEL_NAME = "deepseek-chat"
RATE_LIMIT_DELAY = 1.0
DEFAULT_MIN_SCORE = 80

# RenderCV design — same as baseline builder
DESIGN_SECTION = {
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


# ──────────────────────────────────────────────────────────────────────
# Pydantic schemas for DeepSeek structured output
# ──────────────────────────────────────────────────────────────────────

class BulletSelection(BaseModel):
    entry_id: str
    selected_bullet_ids: list[str]


class TailorSelection(BaseModel):
    selected_summary_id: str
    experience_selections: list[BulletSelection]
    project_selections: list[BulletSelection]



# ──────────────────────────────────────────────────────────────────────
# Master CV loading & menu construction
# ──────────────────────────────────────────────────────────────────────

def load_master_cv(path: Path) -> dict:
    """Load master_cv.yaml and return the parsed data."""
    if not path.exists():
        print(f"ERROR: Master CV not found: {path}")
        sys.exit(1)
    with open(path) as f:
        data = yaml.safe_load(f)
    if data is None:
        print(f"ERROR: Empty master_cv.yaml")
        sys.exit(1)
    return data


def _iter_bank_items(bank) -> list[dict]:
    """Normalize a highlight_bank or summary_bank to a list of {id, text} dicts."""
    if isinstance(bank, list):
        return [{"id": item.get("id", ""), "text": item.get("text", "")} for item in bank if isinstance(item, dict)]
    elif isinstance(bank, dict):
        return [{"id": k, "text": v} for k, v in bank.items()]
    return []


def build_menu(data: dict) -> dict:
    """Extract all available choices from master_cv.yaml for the LLM.

    Returns a compact menu dict suitable for inclusion in a prompt.
    """
    menu = {}

    # Summaries
    summary_bank = data.get("summary_bank", [])
    menu["summaries"] = _iter_bank_items(summary_bank)

    # Experience entries
    menu["experiences"] = []
    for entry in data.get("experience", []):
        entry_id = entry.get("id", "")
        bullets = _iter_bank_items(entry.get("highlight_bank", []))
        menu["experiences"].append({
            "entry_id": entry_id,
            "company": entry.get("company", ""),
            "position": entry.get("position", ""),
            "available_bullets": bullets,
        })

    # Project entries
    menu["projects"] = []
    for entry in data.get("projects", []):
        entry_id = entry.get("id", "")
        bullets = _iter_bank_items(entry.get("highlight_bank", []))
        menu["projects"].append({
            "entry_id": entry_id,
            "name": entry.get("name", ""),
            "available_bullets": bullets,
        })

    return menu


def format_menu_for_prompt(menu: dict) -> str:
    """Render the menu as a readable text block for the DeepSeek prompt."""
    lines = []

    lines.append("=== AVAILABLE SUMMARIES ===")
    for s in menu["summaries"]:
        lines.append(f"  ID: {s['id']}")
        lines.append(f"  Text: {s['text'][:200]}...")
        lines.append("")

    lines.append("=== EXPERIENCE ENTRIES (with available bullet points) ===")
    for exp in menu["experiences"]:
        lines.append(f"  Entry ID: {exp['entry_id']}")
        lines.append(f"  Role: {exp['position']} @ {exp['company']}")
        for b in exp["available_bullets"]:
            lines.append(f"    Bullet ID: {b['id']}  |  {b['text'][:150]}")
        lines.append("")

    lines.append("=== PROJECT ENTRIES (with available bullet points) ===")
    for proj in menu["projects"]:
        lines.append(f"  Entry ID: {proj['entry_id']}")
        lines.append(f"  Name: {proj['name']}")
        for b in proj["available_bullets"]:
            lines.append(f"    Bullet ID: {b['id']}  |  {b['text'][:150]}")
        lines.append("")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# DeepSeek setup
# ──────────────────────────────────────────────────────────────────────

def init_deepseek():
    """Initialize the OpenAI client pointed at DeepSeek."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY not found.")
        print("  1. Create .env: cp .env.example .env")
        print("  2. Add key: DEEPSEEK_API_KEY=sk-...")
        print("  3. Get a key: https://platform.deepseek.com/api_keys")
        sys.exit(1)

    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )
    return client


# ──────────────────────────────────────────────────────────────────────
# Tailoring prompt
# ──────────────────────────────────────────────────────────────────────

TAILOR_PROMPT = textwrap.dedent("""\
    You are an expert ATS (Applicant Tracking System) optimization engine
    specializing in VLSI, ASIC, FPGA, and embedded systems roles.

    Below is a MENU of available resume content (summaries, experience entries
    with bullet points, and project entries with bullet points), followed by a
    JOB DESCRIPTION.

    YOUR TASK:
    1. Choose exactly ONE summary ID from the available summaries that best
       matches the job's requirements.
    2. For EVERY experience entry, select a subset of bullet IDs that maximize
       keyword match and relevance for this specific job. You may select 0-5
       bullets per entry. Only include bullets that directly map to the job's
       requirements, tools, or domain.
    3. For EVERY project entry, select a subset of bullet IDs that maximize
       relevance. Same rules: 0-5 bullets, only include what maps to the job.

    RULES:
    - Prefer quality over quantity — 2-3 highly relevant bullets are better
      than 5 loosely related ones.
    - If an entry is completely irrelevant to the job, select an empty list.
    - Reference IDs EXACTLY as they appear in the menu — do not invent IDs.
    - For education entries, always include all default bullets (education
      relevance is handled separately).

    {menu}

    === JOB DESCRIPTION ===
    Title: {title}
    Company: {company}
    Location: {location}

    {description}

    === OUTPUT FORMAT ===
    You MUST respond with ONLY a valid JSON object. No markdown, no code fences.
    The JSON must follow this exact schema:
    {
      "selected_summary_id": "<summary id from menu>",
      "experience_selections": [
        {"entry_id": "<experience entry id>", "selected_bullet_ids": ["<bullet id>", ...]}
      ],
      "project_selections": [
        {"entry_id": "<project entry id>", "selected_bullet_ids": ["<bullet id>", ...]}
      ]
    }
    Include EVERY experience and project entry from the menu, even if
    selected_bullet_ids is an empty list for irrelevant entries.
    """)


# ──────────────────────────────────────────────────────────────────────
# DeepSeek call with retry
# ──────────────────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=3, min=5, max=60),
    retry_error_callback=lambda retry_state: None,
    reraise=False,
)
def call_tailor(client, menu_text: str, job: dict) -> dict | None:
    """Call DeepSeek to select the best bullets. Returns parsed TailorSelection or None.

    Every dict key access uses .get().  Every list is type-checked before
    iteration.  Any failure prints the raw JSON and returns None so the
    caller skips the job gracefully.
    """
    prompt = TAILOR_PROMPT.format(
        menu=menu_text,
        title=job.get("title", "Unknown"),
        company=job.get("company", "Unknown"),
        location=job.get("location", "Unknown"),
        description=job.get("description") or "(No description provided)",
    )

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )

    content = response.choices[0].message.content

    # ── DEBUG: dump raw response immediately, before any parsing ──
    print("\n=== DEBUG: DEEPSEEK RAW RESPONSE ===")
    print(content)
    print("=====================================\n")

    # ── Step 1: parse JSON ──
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"  ⚠️  Invalid JSON: {exc}\n  Raw: {content[:800]}")
        return None

    if not isinstance(raw, dict):
        print(f"  ⚠️  Expected JSON object, got {type(raw).__name__}: {str(raw)[:500]}")
        return None

    print(f"  DEBUG: top-level keys in response: {list(raw.keys())}")

    # ── Step 2: try Pydantic validation first ──
    try:
        return TailorSelection.model_validate(raw)
    except Exception:
        pass  # fall through to manual construction

    # ── Step 3: manual construction — every access is defensive ──
    try:
        # --- summary ---
        summary_id = str(raw.get("selected_summary_id", ""))

        # --- experience selections ---
        exp_raw = raw.get("experience_selections")
        if not isinstance(exp_raw, list):
            print(f"  ⚠️  'experience_selections' is {type(exp_raw).__name__} (value: {str(exp_raw)[:200]}), using []")
            exp_raw = []
        exp_sel = []
        for i, s in enumerate(exp_raw):
            if not isinstance(s, dict):
                print(f"  ⚠️  experience_selections[{i}] is {type(s).__name__}, skipping")
                continue
            bullet_ids = s.get("selected_bullet_ids")
            if not isinstance(bullet_ids, list):
                print(f"  ⚠️  experience_selections[{i}].selected_bullet_ids is {type(bullet_ids).__name__}, using []")
                bullet_ids = []
            exp_sel.append(BulletSelection(
                entry_id=str(s.get("entry_id", "")),
                selected_bullet_ids=bullet_ids,
            ))

        # --- project selections ---
        proj_raw = raw.get("project_selections")
        if not isinstance(proj_raw, list):
            print(f"  ⚠️  'project_selections' is {type(proj_raw).__name__} (value: {str(proj_raw)[:200]}), using []")
            proj_raw = []
        proj_sel = []
        for i, s in enumerate(proj_raw):
            if not isinstance(s, dict):
                print(f"  ⚠️  project_selections[{i}] is {type(s).__name__}, skipping")
                continue
            bullet_ids = s.get("selected_bullet_ids")
            if not isinstance(bullet_ids, list):
                print(f"  ⚠️  project_selections[{i}].selected_bullet_ids is {type(bullet_ids).__name__}, using []")
                bullet_ids = []
            proj_sel.append(BulletSelection(
                entry_id=str(s.get("entry_id", "")),
                selected_bullet_ids=bullet_ids,
            ))

        return TailorSelection(
            selected_summary_id=summary_id,
            experience_selections=exp_sel,
            project_selections=proj_sel,
        )

    except KeyError as key_err:
        print(f"  ⚠️  KEYERROR — missing key: {key_err}")
        print(f"  Top-level keys: {list(raw.keys())}")
        for key in ["experience_selections", "project_selections"]:
            val = raw.get(key)
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                print(f"  Keys inside {key}[0]: {list(val[0].keys())}")
        return None
    except Exception as fallback_exc:
        print(f"  ⚠️  Fallback exception: {type(fallback_exc).__name__}: {fallback_exc}")
        return None


# ──────────────────────────────────────────────────────────────────────
# Assembly: selections → RenderCV YAML
# ──────────────────────────────────────────────────────────────────────

def _resolve_bullet_texts(bank, bullet_ids: list[str]) -> list[str]:
    """Given a highlight_bank (list or dict), return the text for each bullet ID."""
    bank_items = _iter_bank_items(bank)
    # Build lookup
    lookup = {item["id"]: item["text"] for item in bank_items}
    texts = []
    for bid in bullet_ids:
        text = lookup.get(bid)
        if text:
            texts.append(str(text).strip())
        else:
            print(f"    WARNING: bullet ID '{bid}' not found in bank")
    return texts


def _find_entry_by_id(entries: list, entry_id: str) -> dict | None:
    """Find an entry in a list by its 'id' field."""
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    return None


def extract_username_from_url(url: str) -> str:
    """Extract username from a social profile URL."""
    from urllib.parse import urlparse
    path = urlparse(str(url).rstrip("/")).path
    return path.strip("/").split("/")[-1]


def build_sections(data: dict, selection: TailorSelection) -> dict:
    """Build the RenderCV 'sections' dict using LLM-selected bullets."""
    sections = {}

    # --- Summary ---
    summary_bank = data.get("summary_bank", [])
    summary_items = _iter_bank_items(summary_bank)
    summary_lookup = {item["id"]: item["text"] for item in summary_items}
    summary_text = summary_lookup.get(selection.selected_summary_id)
    if not summary_text:
        # Fallback to first summary
        if summary_items:
            summary_text = summary_items[0]["text"]
            print(f"  WARNING: summary '{selection.selected_summary_id}' not found, using fallback")
    if summary_text:
        sections["Summary"] = [str(summary_text).strip()]

    # --- Experience ---
    experience_entries = []
    for sel in selection.experience_selections:
        entry = _find_entry_by_id(data.get("experience", []), sel.entry_id)
        if not entry:
            print(f"  WARNING: experience entry '{sel.entry_id}' not found in master_cv")
            continue
        highlights = _resolve_bullet_texts(entry.get("highlight_bank", []), sel.selected_bullet_ids)
        if not highlights:
            continue  # skip entries with no selected bullets
        exp = {
            "company": entry.get("company", ""),
            "position": entry.get("position", ""),
            "start_date": entry.get("start_date"),
            "end_date": entry.get("end_date"),
            "location": entry.get("location"),
            "highlights": highlights,
        }
        exp = {k: v for k, v in exp.items() if v is not None}
        experience_entries.append(exp)

    if experience_entries:
        sections["Experience"] = experience_entries

    # --- Education (always use default highlights) ---
    education_entries = []
    for entry in data.get("education", []):
        default_ids = entry.get("default_highlights", [])
        highlights = _resolve_bullet_texts(entry.get("highlight_bank", []), default_ids)
        edu = {
            "institution": entry.get("institution", ""),
            "area": entry.get("area", ""),
            "degree": entry.get("degree"),
            "start_date": entry.get("start_date"),
            "end_date": entry.get("end_date"),
            "location": entry.get("location"),
            "highlights": highlights if highlights else None,
        }
        edu = {k: v for k, v in edu.items() if v is not None}
        if edu.get("institution") and edu.get("area"):
            education_entries.append(edu)

    if education_entries:
        sections["Education"] = education_entries

    # --- Projects ---
    project_entries = []
    for sel in selection.project_selections:
        entry = _find_entry_by_id(data.get("projects", []), sel.entry_id)
        if not entry:
            print(f"  WARNING: project entry '{sel.entry_id}' not found in master_cv")
            continue
        highlights = _resolve_bullet_texts(entry.get("highlight_bank", []), sel.selected_bullet_ids)
        if not highlights:
            continue
        proj = {
            "name": entry.get("name", ""),
            "start_date": entry.get("start_date"),
            "end_date": entry.get("end_date"),
            "highlights": highlights,
        }
        proj = {k: v for k, v in proj.items() if v is not None}
        if proj.get("name"):
            project_entries.append(proj)

    if project_entries:
        sections["Projects"] = project_entries

    # --- Skills (always included as-is) ---
    skills_list = data.get("technical_skills", data.get("skills", []))
    skills_entries = []
    for sk in skills_list:
        if isinstance(sk, dict) and sk.get("label") and sk.get("details"):
            skills_entries.append({"label": sk.get("label", ""), "details": sk.get("details", "")})
    if skills_entries:
        sections["Skills"] = skills_entries

    # --- Honors & Awards (always included) ---
    achievements = data.get("achievements_and_recognition", data.get("achievements", []))
    achievement_entries = []
    for entry in achievements:
        text = entry.get("text") if isinstance(entry, dict) else entry
        if text:
            achievement_entries.append({"bullet": str(text).strip()})
    if achievement_entries:
        sections["Honors & Awards"] = achievement_entries

    return sections


def assemble_rendercv_yaml(data: dict, selection: TailorSelection) -> dict:
    """Build the complete RenderCV YAML data structure with tailored content."""
    personal = data.get("cv", data.get("personal_info", {}))

    # Social networks from links
    social_networks = []
    for link in personal.get("links", personal.get("social_networks", [])):
        if "url" in link:
            label = link.get("label", "")
            url = link.get("url", "")
            username = extract_username_from_url(url)
            social_networks.append({"network": label, "username": username})
        else:
            social_networks.append({"network": link.get("network", ""), "username": link.get("username", "")})

    raw_phone = personal.get("phone")
    phone = raw_phone if raw_phone and str(raw_phone).strip() else None

    cv = {
        "name": personal.get("name", ""),
        "location": personal.get("location"),
        "email": personal.get("email"),
        "phone": phone,
        "website": personal.get("website"),
        "social_networks": social_networks if social_networks else None,
        "sections": build_sections(data, selection),
        "sort_entries": "reverse-chronological",
    }
    cv = {k: v for k, v in cv.items() if v is not None and v != ""}

    return {
        "cv": cv,
        "design": DESIGN_SECTION,
        "locale": {"language": "en"},
        "rendercv_settings": {
            "date": date.today().isoformat(),
            "bold_keywords": [],
            "sort_entries": "reverse-chronological",
        },
    }


# ──────────────────────────────────────────────────────────────────────
# YAML writing & PDF rendering
# ──────────────────────────────────────────────────────────────────────

def write_yaml(data: dict, path: Path) -> None:
    """Write a dict to a YAML file (using ruamel.yaml for clean output)."""
    from ruamel.yaml import YAML
    yaml_writer = YAML()
    yaml_writer.indent(mapping=2, sequence=4, offset=2)
    yaml_writer.width = 120
    yaml_writer.default_flow_style = False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml_writer.dump(data, f)


def render_to_pdf(yaml_path: Path, pdf_path: Path) -> bool:
    """Render a RenderCV YAML to PDF using the rendercv CLI."""
    result = subprocess.run(
        [
            "rendercv", "render", str(yaml_path),
            "-o", "rendercv_output",
            "-pdf", str(pdf_path),
        ],
        cwd=str(OUTPUT_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"    RENDER FAILED:\n{result.stderr[:500]}")
        return False
    return pdf_path.exists()


# ──────────────────────────────────────────────────────────────────────
# Main orchestrator
# ──────────────────────────────────────────────────────────────────────

def _mock_selection() -> TailorSelection:
    """Return a hardcoded reasonable selection for testing the assembly pipeline."""
    return TailorSelection(
        selected_summary_id="sum_digital_verification",
        experience_selections=[
            BulletSelection(
                entry_id="tessolve_dv_intern",
                selected_bullet_ids=["dv_h1", "dv_h2", "dv_h3", "dv_h5", "dv_h6"],
            ),
            BulletSelection(
                entry_id="tessolve_pd_intern",
                selected_bullet_ids=["pd_h1", "pd_h2", "pd_h3"],
            ),
            BulletSelection(
                entry_id="nss_cbit",
                selected_bullet_ids=[],
            ),
        ],
        project_selections=[
            BulletSelection(
                entry_id="rtl2gds_agent",
                selected_bullet_ids=["r2g_h1", "r2g_h2", "r2g_h3"],
            ),
            BulletSelection(
                entry_id="pyuvm_framework",
                selected_bullet_ids=["puvm_h1"],
            ),
            BulletSelection(
                entry_id="fpga_image_accel",
                selected_bullet_ids=["fpga_h1", "fpga_h4"],
            ),
            BulletSelection(
                entry_id="cmos_logic_design",
                selected_bullet_ids=["cmos_h1"],
            ),
            BulletSelection(
                entry_id="sih_dewatering",
                selected_bullet_ids=["sih_h3"],
            ),
        ],
    )


def run_tailoring(
    db_path: Path = None,
    resume_path: Path = None,
    limit: int = None,
    min_score: int = DEFAULT_MIN_SCORE,
    dry_run: bool = False,
    mock: bool = False,
) -> dict:
    """Tailor resumes for high-scoring jobs.

    Returns stats dict with keys: total_candidates, tailored, errors.
    """
    db_path = Path(db_path or DEFAULT_DB)
    resume_path = Path(resume_path or DEFAULT_RESUME)

    # Load master CV once
    print("Loading master CV...")
    data = load_master_cv(resume_path)

    # Build the menu once (same for all jobs)
    print("Building menu of available content...")
    menu = build_menu(data)
    menu_text = format_menu_for_prompt(menu)
    print(f"  Summaries: {len(menu['summaries'])}")
    print(f"  Experiences: {len(menu['experiences'])}")
    print(f"  Projects: {len(menu['projects'])}")

    # Count candidates
    conn = get_connection(db_path)
    try:
        total_candidates = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE match_score >= ? AND tailored_resume_path IS NULL",
            (min_score,),
        ).fetchone()[0]
    finally:
        conn.close()

    if total_candidates == 0:
        print(f"\nNo jobs with match_score >= {min_score} and no existing tailored resume.")
        print(f"Tip: lower the threshold with --min-score, or run --rank first.")
        return {"total_candidates": 0, "tailored": 0, "errors": 0}

    print(f"\nJobs to tailor: {total_candidates} (score >= {min_score})")
    if limit and limit < total_candidates:
        print(f"  (limited to {limit} by --limit)")
        total_candidates = limit

    if dry_run:
        print("DRY RUN — skipping API calls and rendering. Setup OK.\n")
        return {"total_candidates": total_candidates, "tailored": 0, "errors": 0}

    if mock:
        print("MOCK MODE — using hardcoded selections (no DeepSeek API call).\n")
        client = None  # not used in mock mode
    else:
        # Init DeepSeek
        print("Initializing DeepSeek...")
        client = init_deepseek()
        print(f"  Model: {MODEL_NAME} (JSON mode)\n")

    # Ensure output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Process each job
    tailored = 0
    errors = 0

    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, title, company, location, description, match_score "
            "FROM jobs "
            "WHERE match_score >= ? AND tailored_resume_path IS NULL "
            "ORDER BY match_score DESC",
            (min_score,),
        )

        for idx, row in enumerate(cursor):
            if limit and idx >= limit:
                break

            job_id = row["id"]
            title = row["title"] or "Unknown"
            company = row["company"] or "Unknown"
            score = row["match_score"]

            print(
                f"[{idx + 1}/{total_candidates}] "
                f"Score={score} | {title[:60]} @ {company[:30]} ... ",
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
                # Select bullets (DeepSeek API or mock)
                if mock:
                    selection = _mock_selection()
                else:
                    selection = call_tailor(client, menu_text, job_dict)

                if selection is None:
                    print("FAILED (no selection returned)")
                    errors += 1
                    conn.commit()
                    continue

                # Assemble RenderCV YAML
                yaml_data = assemble_rendercv_yaml(data, selection)

                # Write YAML
                yaml_path = OUTPUT_DIR / f"tailored_{job_id}.yaml"
                write_yaml(yaml_data, yaml_path)

                # Render PDF
                pdf_path = OUTPUT_DIR / f"tailored_{job_id}.pdf"
                success = render_to_pdf(yaml_path, pdf_path)

                if success:
                    conn.execute(
                        "UPDATE jobs SET tailored_resume_path = ? WHERE id = ?",
                        (str(pdf_path.resolve()), job_id),
                    )
                    conn.commit()
                    tailored += 1
                    exp_count = len(selection.experience_selections)
                    proj_count = len(selection.project_selections)
                    exp_bullets = sum(len(s.selected_bullet_ids) for s in selection.experience_selections)
                    proj_bullets = sum(len(s.selected_bullet_ids) for s in selection.project_selections)
                    print(f"OK ({exp_count}exp/{proj_count}proj, {exp_bullets + proj_bullets} bullets)")
                else:
                    print("RENDER FAILED")
                    errors += 1
                    conn.commit()

            except Exception as exc:
                errors += 1
                exc_name = type(exc).__name__
                exc_msg = str(exc)[:120]
                print(f"ERROR: {exc_name}: {exc_msg}")
                conn.commit()

                # Early exit on quota exhaustion
                if "429" in exc_msg or "ResourceExhausted" in exc_name or "quota" in exc_msg.lower():
                    if errors >= 3:
                        print("\n  ⚠️  Repeated quota errors — stopping.")
                        print("  → Daily quota may be exhausted. Resume later.\n")
                        break

            # Rate limit (skip in mock mode)
            if not mock and idx < total_candidates - 1:
                time.sleep(RATE_LIMIT_DELAY)

    finally:
        conn.close()

    return {
        "total_candidates": total_candidates,
        "tailored": tailored,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Tailor resumes for high-scoring jobs using DeepSeek"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to SQLite database")
    parser.add_argument("--resume", type=Path, default=DEFAULT_RESUME, help="Path to master_cv.yaml")
    parser.add_argument("--limit", "-n", type=int, default=None, help="Only tailor N jobs")
    parser.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE, help="Minimum match_score threshold")
    parser.add_argument("--dry-run", action="store_true", help="Validate setup without API calls")
    parser.add_argument("--mock", action="store_true", help="Use hardcoded selections instead of DeepSeek (for testing assembly)")
    args = parser.parse_args()

    stats = run_tailoring(
        db_path=args.db,
        resume_path=args.resume,
        limit=args.limit,
        min_score=args.min_score,
        dry_run=args.dry_run,
        mock=args.mock,
    )

    print(f"\n{'='*60}")
    print("TAILORING COMPLETE")
    print(f"{'='*60}")
    print(f"  Candidates (score >= {args.min_score}): {stats['total_candidates']}")
    print(f"  Tailored successfully:  {stats['tailored']}")
    print(f"  Errors:                 {stats['errors']}")
    if stats["tailored"] > 0:
        print(f"  Output directory:       {OUTPUT_DIR.resolve()}")
        for pdf in sorted(OUTPUT_DIR.glob("tailored_*.pdf")):
            print(f"    {pdf.name}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
