from pathlib import Path

root = Path(__file__).resolve().parents[1]
src = (root / "inputs" / "kumar_apurva_raw.txt").read_text(encoding="utf-8")
pack = root / "inputs" / "packs" / "apurva_demo"

for d in [
    "onboarding",
    "linkedin_profile",
    "company_page",
    "case_studies",
    "company_materials",
    "company_images",
    "linkedin_posts",
    "social_posts",
    "social_posts/instagram",
]:
    (pack / d).mkdir(parents=True, exist_ok=True)

parts: dict[str, str] = {}
current = "preamble"
buf: list[str] = []
for line in src.splitlines(True):
    if line.startswith("=== ") and line.strip().endswith("==="):
        parts[current] = "".join(buf)
        current = line.strip().strip("= ").strip()
        buf = []
    else:
        buf.append(line)
parts[current] = "".join(buf)

(pack / "onboarding" / "onboarding.txt").write_text(
    parts.get("ONBOARDING / POSITIONING", "").strip() + "\n", encoding="utf-8"
)
(pack / "linkedin_profile" / "profile.txt").write_text(
    parts.get("LINKEDIN PROFILE", "").strip() + "\n", encoding="utf-8"
)

company = ""
for k, v in parts.items():
    if k.startswith("COMPANY PAGE"):
        company += f"=== {k} ===\n{v.strip()}\n\n"
(pack / "company_page" / "company_pages.txt").write_text(company.strip() + "\n", encoding="utf-8")

(pack / "case_studies" / "outspark_traction.txt").write_text(
    """Client / product context: OutSpark (CoolBoots portfolio) AI career growth platform
Problem: Professionals struggle to cut through job-market clutter with generic resumes and unprepared interviews.
Approach: Built AI-assisted resume creation, LinkedIn optimization, interview preparation, and career intelligence into a consumer career OS.
Outcome: 200k+ paid users within the first year of launch.
Timeframe: First 12 months after launch
""",
    encoding="utf-8",
)

(pack / "company_materials" / "coolboots_overview.txt").write_text(
    """CoolBoots Media — Company overview (brochure excerpt)
CoolBoots builds AI-driven platforms for professionals, enterprises, and publishers.
Portfolio: OutSpark, TimesJobs (25M+ job seekers, 20k+ daily resumes), TechGig (5.6M+ developers), Qureka (5000+ publishers).
ICP: job seekers/professionals, HR/talent acquisition leaders, publishers.
Market: India + global publisher partners.
""",
    encoding="utf-8",
)

(pack / "linkedin_post_urls.json").write_text(
    """[
  "https://www.linkedin.com/posts/example-post-1",
  "https://www.linkedin.com/posts/example-post-2"
]
""",
    encoding="utf-8",
)

(pack / "linkedin_posts" / "sample_past_post.txt").write_text(
    """Most talent platforms still treat a career as a static PDF.

When profiles only update when someone is job-hunting, the database decays every month.

The operators who win build activity loops — micro-updates, market triggers, and utility that stays valuable even when users are not actively looking.
""",
    encoding="utf-8",
)

(pack / "manifest.json").write_text(
    """{
  "selected_positioning_mode": "founder_plus_product",
  "selected_growth_goal": "lead_generation",
  "declared_niche": "AI-driven career platforms, talent discovery, and digital media ecosystems for professionals and enterprises",
  "target_audience": "Professionals/job seekers, HR/talent acquisition leaders, publishers, and digital-product peers in workforce tech",
  "business_goal_on_linkedin": "Build founder authority while showcasing CoolBoots portfolio platforms",
  "voice_preference": "executive, operator, product-and-growth hybrid",
  "things_to_avoid": "hype without numbers, vague corporate fluff",
  "profile_aspiration": "Known as the founder building AI career OS and talent ecosystems at scale in India"
}
""",
    encoding="utf-8",
)

(pack / "README.md").write_text(
    """# Apurva demo ingest pack

```bash
python scripts/run_pipeline.py inputs/packs/apurva_demo
```
""",
    encoding="utf-8",
)

print(f"pack ready: {pack}")
