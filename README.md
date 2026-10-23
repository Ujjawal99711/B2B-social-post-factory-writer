# B2B Social Post Factory Writer

**A multi-agent AI orchestration pipeline that produces LinkedIn content for B2B founders and company pages.**

Turn messy founder inputs (onboarding, profile, case studies, brand DNA) into a planned week of LinkedIn posts — text, infographics, and carousels — with brand-locked image generation.

---

## Product thesis

B2B founders do not need another blank chat window. They need a **content operating system**: agents that structure reality, lock strategy, plan the week, then write and design posts that stay on-brand and on-contract.

This product is that OS — a **cluster of specialized AI agents**, each with one job, handoff contracts between stages, and hard quotas so quality does not drift.

| Who it serves | What they get |
|---------------|---------------|
| Founders & SME owners | Personal + product LinkedIn growth |
| Company pages | Title, About, Brand DNA + 4 visual posts/week |
| Operators / agencies | Repeatable pack → run → ship workflow |

---

## Agent cluster

Specialists, not one mega-prompt. Each agent owns a stage; the orchestrator (`scripts/run_pipeline.py`) runs the relay.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                     ORCHESTRATOR  ·  run_pipeline.py                      │
│              schedules agents · locks Brand DNA · writes artifacts        │
└─────────────────────────────────────────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│  INGEST AGENT    │      │  RAW STRUCTURER  │      │  STRATEGY (LMO)  │
│  Stage 00        │ ───► │  Stage 01        │ ───► │  Stage 02        │
│  Normalize packs │      │  Structure chaos │      │  Lock positioning │
│  + optional      │      │  into signed     │      │  growth goal,    │
│  vision captions │      │  JSON facts      │      │  Brand DNA, voice│
└──────────────────┘      └──────────────────┘      └────────┬─────────┘
                                                             │
                                                             ▼
                                                  ┌──────────────────┐
                                                  │  MATRIX PLANNER  │
                                                  │  Stage 03        │
                                                  │  Week of briefs  │
                                                  │  + hard quotas   │
                                                  └────────┬─────────┘
                             ┌─────────────────────────────┼─────────────────────────────┐
                             ▼                             ▼                             ▼
                  ┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
                  │  TEXT WRITER     │        │  INFOGRAPHIC     │        │  CAROUSEL        │
                  │  Stage 04        │        │  WRITER  ·  05   │        │  WRITER  ·  06   │
                  │  Paste-ready     │        │  Caption + image │        │  Caption + slide │
                  │  LinkedIn copy   │        │  prompt JSON     │        │  outline JSON    │
                  └──────────────────┘        └────────┬─────────┘        └────────┬─────────┘
                                                       │                           │
                                                       ▼                           ▼
                                              ┌──────────────────┐        ┌──────────────────┐
                                              │  IMAGE RENDERER  │        │  IMAGE RENDERER  │
                                              │  gpt-image-2     │        │  gpt-image-2     │
                                              │  Infographic PNG │        │  Slide PNGs × N  │
                                              └──────────────────┘        └──────────────────┘
```

### Agent roster

| Agent | Role (PM view) | Model | Primary output |
|-------|----------------|-------|----------------|
| **Ingest Agent** | Ops — unify uploads into one normalized brief | Local extractors (+ optional Gemini vision) | `00_normalized_raw.txt`, ingest manifest |
| **RAW Structurer** | Analyst — extract facts; invent nothing | `gpt-6-luna` (default) | `01_raw.json` |
| **LMO Strategist** | Strategy — positioning, growth goal, Brand DNA, voice, handoff | `gpt-6-luna` | `02_lmo.json`, `02_handoff.json` |
| **Company LMO** | Company track strategist — title, taglines, About, company Brand DNA | `gpt-6-luna` | `02_company_lmo.json`, company handoff |
| **Matrix Planner** | Editorial — week plan with format quotas & growth contracts | `gpt-6-luna` | `03_content_matrix.json` |
| **Company Matrix** | Company editorial — exactly 2 Infographic + 2 Carousel | `gpt-6-luna` | `03_company_content_matrix.json` |
| **Text Writer** | Copy — founder-voice LinkedIn text posts | `gpt-6-luna` | `04_posts/post_XX_text.json` |
| **Infographic Writer** | Creative — caption + locked image prompt | `gpt-6-luna` | Infographic JSON |
| **Carousel Writer** | Narrative design — caption + per-slide copy/prompts | `gpt-6-luna` | Carousel JSON |
| **Image Renderer** | Production — brand-locked PNGs | `gpt-image-2` | Infographic + carousel slide PNGs |

**Rollback:** set `TEXT_PROVIDER=gemini` to run text/JSON agents on Gemini instead of OpenAI.

---

## Two product tracks

Stages 00–01 are shared. Then the cluster branches.

### Founder track (default)

Weekly personal brand / lead-gen content for the founder.

| Quota (8-post week) | Formats |
|---------------------|---------|
| Hard format mix | **3 Text + 2 Carousel + 3 Infographic** |
| Scheduling | Weekdays only (no weekends) |

### Company page track (`--track company_page`)

Company presence assets + a tight visual week.

| Deliverable | Detail |
|-------------|--------|
| Strategy | Title, 5 taglines, About, company Brand DNA / handoff |
| Posts | **2 Infographic + 2 Carousel** (educational / case / BTS / news — no polls) |

```bash
python scripts/run_pipeline.py inputs/your_raw.txt --track company_page
python scripts/run_pipeline.py inputs/your_raw.txt --track company_page --stop-after company_lmo
```

---

## Strategy contracts (non-negotiable)

Agents do not freestyle growth. Two independent axes from onboarding:

```text
selected_positioning_mode: founder_plus_product | founder_only
selected_growth_goal:      lead_generation | personal_branding
```

| Axis | Controls |
|------|----------|
| **Positioning** | How much product/company appears in posts |
| **Growth goal** | Audience, focus, tone, CTA, proof, targeting |

| | Personal branding | Lead generation |
|--|-------------------|-----------------|
| Audience | Broad relevant pros | Narrow ICP / buyers |
| Focus | Ideas, POV, lessons, stories | Problems, solutions, use cases, outcomes |
| CTA | Soft or none | Situation→CTA map; soft DM OK; no comment bait |
| Proof | Experience from handoff only | Case / ROI from handoff only |

Writers **never invent** metrics, clients, or case scenarios. Matrix tags each brief with `growth_angle`, `proof_type`, `cta_style`, and (for lead gen) `post_situation` + `cta_type`.

---

## Brand DNA lock

| Input | Behavior |
|-------|----------|
| User provides colors (hex or names) | Kept / converted to `#RRGGBB` — never ignored |
| No Brand DNA | LMO generates niche-differentiated DNA |
| Every image call | Run-level `00_brand_dna_lock.txt` prepended to prompts |

---

## Quick start

### 1. Clone & install

```bash
git clone https://github.com/Ujjawal99711/B2B-social-post-factory-writer.git
cd B2B-social-post-factory-writer
pip install -r requirements.txt
```

### 2. Configure secrets (local only)

```bash
cp .env.example .env
# Edit .env — add OPENAI_API_KEY (required for default path)
```

> **Security:** `.env` is gitignored. Never commit API keys. Only `.env.example` (placeholders) lives in the repo.

```env
OPENAI_API_KEY=...
TEXT_PROVIDER=openai
TEXT_MODEL=gpt-6-luna
OPENAI_IMAGE_MODEL=gpt-image-2
# Optional Gemini rollback / vision:
# TEXT_PROVIDER=gemini
# GEMINI_API_KEY=...
```

### 3. Run

```bash
# Legacy single text file
python scripts/run_pipeline.py inputs/your_raw.txt

# Recommended: multi-file ingest pack
python scripts/run_pipeline.py inputs/packs/apurva_demo

# Company page track
python scripts/run_pipeline.py inputs/your_raw.txt --track company_page
```

If `OPENAI_API_KEY` is missing, caption + prompt JSON still saves; PNG generation is skipped and recorded in error lists.

---

## Ingest pack layout

```text
inputs/packs/<customer>/
  manifest.json                 # optional: positioning, growth goal, niche, brand_dna
  onboarding/                   # PDF, DOC/DOCX, TXT
  linkedin_profile/             # PDF, DOC/DOCX, TXT
  company_page/                 # PDF, DOC/DOCX, TXT
  case_studies/                 # multi files
  company_materials/            # decks / brochures
  company_images/               # JPEG/PNG
  linkedin_posts/               # pasted post text (preferred)
  linkedin_post_urls.json       # optional; fetch is best-effort
  social_posts/<platform>/      # screenshots
```

Rules: **5 MB max per file**. Convert legacy `.doc` / `.ppt` to `.docx` / `.pptx` / `.pdf` / `.txt`.

Preview normalize only:

```bash
python scripts/ingest_normalize.py inputs/packs/apurva_demo outputs/ingest_preview
```

---

## Outputs

Each run writes `outputs/run_YYYYMMDD_HHMMSS/`:

| Artifact | Meaning |
|----------|---------|
| `00_normalized_raw.txt` / `00_ingest_manifest.json` | Ingest |
| `00_brand_dna_lock.txt` | Locked Brand DNA for all image prompts |
| `01_raw.json` → `02_lmo.json` → `03_content_matrix.json` | Strategy chain |
| `04_posts/post_XX_text.json` | Text posts |
| `04_posts/post_XX_infographic.json` + `.png` | Infographic |
| `04_posts/post_XX_carousel.json` + `slide_YY.png` | Carousel |
| `00_summary.json` | Counters & run health |

Default image size: `1024x1536` (portrait). Carousel hard cap: **14 slides**.

---

## Repository map

| Path | Purpose |
|------|---------|
| [`prompts/`](prompts/) | Agent system prompts (one file per stage) |
| [`schemas/`](schemas/) | JSON contracts between agents |
| [`scripts/run_pipeline.py`](scripts/run_pipeline.py) | Orchestrator |
| [`scripts/ingest_normalize.py`](scripts/ingest_normalize.py) | Ingest Agent |
| [`inputs/`](inputs/) | Sample raw files & demo packs |
| [`.env.example`](.env.example) | Env template (no secrets) |

---

## Quality bar (acceptance)

- Positioning stays `founder_plus_product` or `founder_only`
- Growth goal stays `lead_generation` or `personal_branding`
- Growth Goal Contract honored across Matrix + all writers
- No invented ROI, clients, or case scenarios
- Founder matrix: **3 / 2 / 3** Text / Carousel / Infographic; company: **2 / 2** Infographic / Carousel
- Infographic & carousel JSON always include `post_text` + image fields
- User Brand DNA never overwritten by LMO
- Secrets never in git — only local `.env`

---

## Out of scope (current release)

- Visual Post writer
- Image *editing* endpoint (generations only)
- Guaranteed LinkedIn URL scraping (paste post text instead)
- Using uploaded company images as direct style references (captions only today)

---

## License & contribution

Private product codebase for [Ujjawal99711/B2B-social-post-factory-writer](https://github.com/Ujjawal99711/B2B-social-post-factory-writer). Open an issue or PR if you are collaborating on agent prompts, schemas, or the orchestrator.
