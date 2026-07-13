# RenderCV 2.3 Schema Notes

Generated from actual installed package exploration.

## Top-Level YAML Structure

```yaml
cv:
  name: ...
  location: ...
  email: ...
  phone: ...
  website: ...
  photo: ...          # path relative to YAML file
  social_networks:
    - network: LinkedIn  # one of: LinkedIn, GitHub, GitLab, IMDB, Instagram, ORCID, Mastodon, StackOverflow, ResearchGate, YouTube, Google Scholar, Telegram, Leetcode, X
      username: ...
  sections:
    section_title_here:  # arbitrary name, auto-detected entry type
      - ...entry...
  sort_entries: none  # "reverse-chronological" | "chronological" | "none"

design:
  theme: classic  # classic | sb2nov | engineeringresumes | engineeringclassic | moderncv
  page: {...}
  colors: {...}
  text: {...}
  links: {...}
  header: {...}
  section_titles: {...}
  entries: {...}
  highlights: {...}
  entry_types: {...}

locale:
  language: en
  ...

rendercv_settings:
  date: "2026-07-13"
  bold_keywords: []
  sort_entries: none
```

## Entry Types and Their Fields

### ExperienceEntry (characteristic: `company`, `position`)
- **company** (required, str)
- **position** (required, str)
- date (int | str | null)
- start_date (str pattern `\d{4}-\d{2}(-\d{2})?` | int | null)
- end_date ("present" | str `\d{4}-\d{2}(-\d{2})?` | int | null)
- location (str | null)
- summary (str | null)
- highlights (list[str] | null)

### EducationEntry (characteristic: `institution`, `area`, `degree`, `grade`)
- **institution** (required, str)
- **area** (required, str)
- degree (str | null)
- grade (str | null)
- date (int | str | null)
- start_date (str | int | null)
- end_date ("present" | str | int | null)
- location (str | null)
- summary (str | null)
- highlights (list[str] | null)

### NormalEntry (characteristic: `name`) — used for projects
- **name** (required, str)
- date (int | str | null)
- start_date (str | int | null)
- end_date ("present" | str | int | null)
- location (str | null)
- summary (str | null)
- highlights (list[str] | null)

### OneLineEntry (characteristic: `label`, `details`)
- **label** (required, str)
- **details** (required, str)

### BulletEntry (characteristic: `bullet`)
- **bullet** (required, str)

### PublicationEntry (characteristic: `title`, `authors`, `doi`, `url`, `journal`)
- **title** (required, str)
- **authors** (required, list[str])
- doi (str matching `\b10\..*` | null)
- url (HttpUrl | null)
- journal (str | null)
- date (int | str | null)

### TextEntry
- Just a plain list[str] in the section

## Date Formats
- `start_date`: "YYYY-MM-DD", "YYYY-MM", or integer year
- `end_date`: same as start_date, OR literal string "present"
- Can also use arbitrary text like "Fall 2023" via the `date` field

## Entry Type Auto-Detection
RenderCV looks at which keys are present in the dict to determine the type:
- Has `company` key → ExperienceEntry
- Has `institution` key → EducationEntry
- Has `name` key (but not company/institution) → NormalEntry
- Has `label` key → OneLineEntry
- Has `bullet` key → BulletEntry
- Plain string → TextEntry

## Design/Theme Options (classic theme)
All design fields are optional — defaults are used if not specified. Full defaults shown in generated Test_Person_CV.yaml.
