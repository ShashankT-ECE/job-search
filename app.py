"""
app.py — Streamlit dashboard for the job-search pipeline.

Launch: streamlit run app.py   or   python main.py --dashboard
"""

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Job Search Pipeline",
    page_icon="🔍",
    layout="wide",
)

# ──────────────────────────────────────────────────────────────────────
# Database connection
# ──────────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).resolve().parent / "outputs" / "jobs.db"
OUTPUT_DIR = Path(__file__).resolve().parent / "resume" / "output"


@st.cache_data(ttl=30)
def load_jobs() -> pd.DataFrame:
    """Load the jobs table into a DataFrame."""
    if not DB_PATH.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT id, site, job_url, title, company, location, date_posted, "
        "description, scraped_at, match_score, tailored_resume_path "
        "FROM jobs ORDER BY scraped_at DESC",
        conn,
    )
    conn.close()

    if df.empty:
        return df

    # Convert NULL scores for display
    df["score_display"] = df["match_score"].apply(
        lambda x: f"{int(x)}%" if pd.notna(x) else "Unscored"
    )
    # For filtering: NULL → 0
    df["score_filter"] = df["match_score"].fillna(0).astype(int)

    # Truncate description for list view
    df["description_preview"] = df["description"].apply(
        lambda x: (str(x)[:300] + "...") if pd.notna(x) and len(str(x)) > 300 else str(x)
    )

    return df


def pdf_exists(tailored_path) -> bool:
    """Check if a tailored PDF actually exists on disk."""
    if pd.isna(tailored_path) or not tailored_path:
        return False
    return Path(tailored_path).exists()


# ──────────────────────────────────────────────────────────────────────
# Load data
# ──────────────────────────────────────────────────────────────────────
df = load_jobs()

if df.empty:
    st.warning("No jobs in the database yet. Run `python main.py --scrape` first.")
    st.stop()

# ──────────────────────────────────────────────────────────────────────
# Sidebar filters
# ──────────────────────────────────────────────────────────────────────
st.sidebar.title("🔍 Filters")

# Show unscored toggle
show_unscored = st.sidebar.checkbox("Show unscored jobs", value=True)

# Min score slider
min_score = st.sidebar.slider(
    "Minimum match score",
    min_value=0,
    max_value=100,
    value=0,
    step=5,
    help="Jobs with score below this are hidden. Unscored jobs treated as 0.",
)

# Only show tailored
only_tailored = st.sidebar.checkbox("Only show jobs with tailored resumes", value=False)

# Location filter
locations = sorted(df["location"].dropna().unique().tolist())
selected_locations = st.sidebar.multiselect(
    "Location",
    options=locations,
    default=[],
    help="Show all locations if none selected.",
)

# Site filter
sites = sorted(df["site"].dropna().unique().tolist())
selected_sites = st.sidebar.multiselect(
    "Source site",
    options=sites,
    default=[],
    help="Show all sites if none selected.",
)

# ──────────────────────────────────────────────────────────────────────
# Apply filters
# ──────────────────────────────────────────────────────────────────────
filtered = df.copy()

# Score filter
if show_unscored:
    filtered = filtered[
        (filtered["score_filter"] >= min_score) | (filtered["match_score"].isna())
    ]
else:
    filtered = filtered[
        filtered["match_score"].notna() & (filtered["score_filter"] >= min_score)
    ]

# Location filter
if selected_locations:
    filtered = filtered[filtered["location"].isin(selected_locations)]

# Site filter
if selected_sites:
    filtered = filtered[filtered["site"].isin(selected_sites)]

# Tailored-only filter
if only_tailored:
    filtered = filtered[filtered["tailored_resume_path"].notna()]

# ──────────────────────────────────────────────────────────────────────
# Header metrics
# ──────────────────────────────────────────────────────────────────────
st.title("🔍 Job Search Pipeline")

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total jobs", len(df))
with col2:
    scored_count = df["match_score"].notna().sum()
    st.metric("Scored", scored_count)
with col3:
    tailored_count = df["tailored_resume_path"].notna().sum()
    st.metric("Tailored", tailored_count)
with col4:
    st.metric("Showing", len(filtered))

st.divider()

# ──────────────────────────────────────────────────────────────────────
# Job list
# ──────────────────────────────────────────────────────────────────────

if filtered.empty:
    st.info("No jobs match the current filters. Try adjusting the sidebar.")
    st.stop()

st.caption(f"Showing {len(filtered)} job(s)")

for _, row in filtered.iterrows():
    job_id = row["id"]
    title = str(row["title"]) if pd.notna(row["title"]) else "Untitled"
    company = str(row["company"]) if pd.notna(row["company"]) else "Unknown"
    location = str(row["location"]) if pd.notna(row["location"]) else "—"
    site = str(row["site"]) if pd.notna(row["site"]) else "—"
    job_url = str(row["job_url"]) if pd.notna(row["job_url"]) else "#"
    date_posted = str(row["date_posted"]) if pd.notna(row["date_posted"]) else "—"
    score_disp = row["score_display"]
    score_val = row["score_filter"]

    # Score badge color
    if score_disp == "Unscored":
        score_badge = "⚪ Unscored"
    elif score_val >= 80:
        score_badge = f"🟢 {score_disp}"
    elif score_val >= 50:
        score_badge = f"🟡 {score_disp}"
    else:
        score_badge = f"🔴 {score_disp}"

    with st.expander(
        f"{score_badge}  |  **{title[:80]}** @ {company[:40]}  |  {location[:30]}",
        expanded=False,
    ):
        # ── Job details ─────────────────────────────────────────
        col_a, col_b = st.columns([3, 1])

        with col_a:
            st.markdown(f"**{title}**")
            st.caption(f"🏢 {company}  |  📍 {location}  |  📅 {date_posted}  |  🔗 {site}")

        with col_b:
            # Apply link button
            if job_url and job_url != "#":
                st.markdown(
                    f'<a href="{job_url}" target="_blank" rel="noopener noreferrer">'
                    f'<button style="background:#FF4B4B;color:white;border:none;'
                    f'padding:8px 20px;border-radius:6px;font-size:15px;'
                    f'cursor:pointer;width:100%;font-weight:bold;">'
                    f'🔗 1-Click Apply</button></a>',
                    unsafe_allow_html=True,
                )

        # ── Description ─────────────────────────────────────────
        st.markdown("**📄 Description**")
        description = row["description"]
        if pd.notna(description) and str(description).strip():
            st.text_area(
                "Job description",
                value=str(description),
                height=250,
                label_visibility="collapsed",
                disabled=True,
            )
        else:
            st.caption("No description available.")

        # ── Tailored resume ─────────────────────────────────────
        st.markdown("**📎 Resume**")
        tailored_path = row.get("tailored_resume_path")

        if pd.notna(tailored_path) and tailored_path and pdf_exists(tailored_path):
            pdf_file = Path(tailored_path)
            try:
                with open(pdf_file, "rb") as f:
                    pdf_bytes = f.read()
                col_dl, col_info = st.columns([1, 3])
                with col_dl:
                    st.download_button(
                        label=f"📥 Download {pdf_file.name}",
                        data=pdf_bytes,
                        file_name=pdf_file.name,
                        mime="application/pdf",
                        type="primary",
                    )
                with col_info:
                    st.caption(f"Tailored resume ready ({round(len(pdf_bytes) / 1024)} KB)")
            except FileNotFoundError:
                st.caption("📝 Tailored resume pending — file not found on disk.")
        else:
            st.caption("📝 Tailored resume pending — run `python main.py --tailor` to generate.")

st.divider()
st.caption(f"Data from {DB_PATH.resolve()}")
