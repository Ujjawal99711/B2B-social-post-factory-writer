"""
Stage 0 — Ingest + Normalize

Accepts either:
  - a legacy single .txt raw file (passthrough), or
  - an onboarding pack folder with typed uploads

Produces a single normalized text packet for Agent 0 (RAW structurer).
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

SLOT_EXTS: dict[str, set[str]] = {
    "onboarding": {".pdf", ".doc", ".docx", ".txt", ".md"},
    "case_studies": {".pdf", ".doc", ".docx", ".txt", ".md"},
    "company_materials": {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".md"},
    "company_images": {".jpg", ".jpeg", ".png"},
    "linkedin_profile": {".pdf", ".doc", ".docx", ".txt", ".md"},
    "company_page": {".pdf", ".doc", ".docx", ".txt", ".md"},
    "social_posts": {".jpg", ".jpeg", ".png"},
    "linkedin_posts": {".txt", ".md", ".json"},
}

SLOT_DIRS = (
    "onboarding",
    "linkedin_profile",
    "company_page",
    "case_studies",
    "company_materials",
    "company_images",
    "linkedin_posts",
    "social_posts",
)

VisionCaptionFn = Callable[[Path, str], str]


@dataclass
class FileResult:
    slot: str
    path: str
    filename: str
    status: str  # ok | skipped | error
    reason: str = ""
    chars: int = 0
    platform: str = ""
    caption: str = ""
    url: str = ""


@dataclass
class IngestResult:
    normalized_text: str
    source: str  # file | pack
    source_path: str
    files: list[FileResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_path": self.source_path,
            "file_count": len(self.files),
            "files": [asdict(f) for f in self.files],
            "warnings": self.warnings,
            "normalized_chars": len(self.normalized_text),
        }


def _safe_read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _validate_file(path: Path, allowed_exts: set[str]) -> str | None:
    if not path.is_file():
        return "not a file"
    ext = path.suffix.lower()
    if ext not in allowed_exts:
        return f"unsupported extension {ext}"
    size = path.stat().st_size
    if size <= 0:
        return "empty file"
    if size > MAX_FILE_BYTES:
        return f"exceeds 5 MB ({size} bytes)"
    return None


def extract_txt(path: Path) -> str:
    return _safe_read_text(path).strip()


def extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("pypdf is required for PDF extraction. pip install pypdf") from e
    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts).strip()


def extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as e:
        raise RuntimeError("python-docx is required for DOCX extraction. pip install python-docx") from e
    doc = Document(str(path))
    parts = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()


def extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError as e:
        raise RuntimeError("python-pptx is required for PPTX extraction. pip install python-pptx") from e
    prs = Presentation(str(path))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        slide_bits: list[str] = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text and shape.text.strip():
                slide_bits.append(shape.text.strip())
        if slide_bits:
            parts.append(f"[Slide {i}]\n" + "\n".join(slide_bits))
    return "\n\n".join(parts).strip()


def extract_doc_legacy(path: Path) -> str:
    """Best-effort for old .doc / .ppt binary formats — usually fails; caller records warning."""
    raise RuntimeError(
        f"Legacy binary format {path.suffix} is not supported. Convert to .docx/.pptx/.pdf/.txt"
    )


def extract_document(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".txt", ".md"}:
        return extract_txt(path)
    if ext == ".pdf":
        return extract_pdf(path)
    if ext == ".docx":
        return extract_docx(path)
    if ext == ".pptx":
        return extract_pptx(path)
    if ext in {".doc", ".ppt"}:
        return extract_doc_legacy(path)
    raise RuntimeError(f"No extractor for {ext}")


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(ext, "application/octet-stream")


def default_image_caption(path: Path, purpose: str) -> str:
    return (
        f"[Image file: {path.name}; purpose: {purpose}. "
        "No vision caption available — use filename/context only.]"
    )


def call_gemini_vision_caption(
    path: Path,
    purpose: str,
    *,
    model: str,
    api_key: str,
) -> str:
    """Optional multimodal caption via Gemini."""
    mime = guess_mime(path)
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    prompt = (
        f"You are extracting usable text/context from an onboarding upload for a B2B LinkedIn system.\n"
        f"Purpose: {purpose}\n"
        f"Filename: {path.name}\n\n"
        "Describe and extract:\n"
        "- Any readable text in the image (OCR)\n"
        "- What the image shows (product UI, team, office, logo, social post, etc.)\n"
        "- Any metrics, product names, CTAs, or brand cues\n"
        "Be factual. Do not invent. Keep under 250 words."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini vision HTTP {e.code}: {detail}") from e

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError("No vision candidates")
    parts = candidates[0].get("content", {}).get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("text")]
    if not texts:
        raise RuntimeError("Empty vision caption")
    return "\n".join(texts).strip()


def fetch_url_text(url: str, timeout: int = 20) -> str:
    """Best-effort URL fetch. LinkedIn usually blocks; caller should allow paste fallback."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; B2BIngest/1.0; +https://localhost) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    html = raw.decode("utf-8", errors="replace")
    # Strip scripts/styles then tags — crude but useful when fetch succeeds
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 40:
        raise RuntimeError("Fetched page had too little extractable text (likely blocked)")
    return text[:8000]


def _list_slot_files(pack_dir: Path, slot: str) -> list[Path]:
    d = pack_dir / slot
    if not d.is_dir():
        return []
    skip_names = {"readme.md", "readme.txt", ".gitkeep"}
    files: list[Path] = []
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.name.lower() in skip_names:
            continue
        files.append(p)
    return files


def _platform_from_path(path: Path, pack_dir: Path) -> str:
    """Infer social platform from parent folder under social_posts/."""
    try:
        rel = path.relative_to(pack_dir / "social_posts")
    except ValueError:
        return ""
    parts = rel.parts
    if len(parts) >= 2:
        return parts[0].lower()
    # filename hint: instagram__shot.png
    name = path.stem.lower()
    for platform in ("instagram", "facebook", "twitter", "x", "youtube", "tiktok", "threads"):
        if name.startswith(platform) or f"_{platform}_" in f"_{name}_":
            return platform
    return "unknown"


def _load_manifest_overrides(pack_dir: Path) -> dict[str, Any]:
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _load_linkedin_urls(pack_dir: Path) -> list[str]:
    urls: list[str] = []
    # linkedin_post_urls.json at pack root
    for name in ("linkedin_post_urls.json", "linkedin_urls.json"):
        p = pack_dir / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                urls.extend(str(u).strip() for u in data if str(u).strip())
            elif isinstance(data, dict):
                items = data.get("urls") or data.get("posts") or []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, str) and item.strip():
                            urls.append(item.strip())
                        elif isinstance(item, dict) and item.get("url"):
                            urls.append(str(item["url"]).strip())
    # plain text file, one URL per line
    for name in ("linkedin_post_urls.txt", "linkedin_urls.txt"):
        p = pack_dir / name
        if p.exists():
            for line in _safe_read_text(p).splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line.startswith("http"):
                    urls.append(line)
    # dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:5]  # form requires last 5


def _section(title: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"=== {title} ===\n{body}\n"


def normalize_pack(
    pack_dir: Path,
    *,
    vision_caption_fn: VisionCaptionFn | None = None,
    fetch_linkedin_urls: bool = True,
) -> IngestResult:
    pack_dir = pack_dir.resolve()
    if not pack_dir.is_dir():
        raise FileNotFoundError(f"Pack folder not found: {pack_dir}")

    overrides = _load_manifest_overrides(pack_dir)
    results: list[FileResult] = []
    warnings: list[str] = []
    sections: list[str] = []

    # Optional free-text notes at pack root
    for note_name in ("notes.txt", "onboarding_answers.txt", "raw_notes.txt"):
        note_path = pack_dir / note_name
        if note_path.is_file():
            err = _validate_file(note_path, {".txt", ".md"})
            if err:
                results.append(
                    FileResult("notes", str(note_path), note_path.name, "skipped", reason=err)
                )
                continue
            text = extract_txt(note_path)
            results.append(
                FileResult("notes", str(note_path), note_path.name, "ok", chars=len(text))
            )
            sections.append(_section("ONBOARDING / FREE-TEXT NOTES", text))

    # Positioning fields from manifest (explicit form choices)
    positioning_lines: list[str] = []
    if overrides.get("selected_positioning_mode"):
        positioning_lines.append(
            f"selected_positioning_mode: {overrides['selected_positioning_mode']}"
        )
    if overrides.get("selected_growth_goal"):
        positioning_lines.append(f"selected_growth_goal: {overrides['selected_growth_goal']}")
    for key in (
        "declared_niche",
        "target_audience",
        "business_goal_on_linkedin",
        "voice_preference",
        "things_to_avoid",
        "profile_aspiration",
    ):
        if overrides.get(key):
            positioning_lines.append(f"{key}: {overrides[key]}")
    if positioning_lines:
        sections.append(_section("ONBOARDING / POSITIONING", "\n".join(positioning_lines)))

    slot_titles = {
        "onboarding": "ONBOARDING FORM UPLOAD",
        "linkedin_profile": "LINKEDIN PROFILE",
        "company_page": "COMPANY PAGE",
        "case_studies": "CASE STUDIES",
        "company_materials": "COMPANY MATERIALS (DECKS / BROCHURES)",
        "company_images": "COMPANY IMAGES",
        "linkedin_posts": "PAST LINKEDIN POSTS (PASTED TEXT)",
        "social_posts": "SOCIAL MEDIA POSTS",
    }

    for slot in SLOT_DIRS:
        allowed = SLOT_EXTS[slot]
        files = _list_slot_files(pack_dir, slot)
        if not files:
            continue

        chunk_parts: list[str] = []
        for path in files:
            rel = str(path.relative_to(pack_dir)).replace("\\", "/")
            err = _validate_file(path, allowed)
            if err:
                results.append(FileResult(slot, rel, path.name, "skipped", reason=err))
                warnings.append(f"{rel}: {err}")
                continue

            try:
                if slot in {"company_images", "social_posts"}:
                    purpose = (
                        f"social media screenshot ({_platform_from_path(path, pack_dir)})"
                        if slot == "social_posts"
                        else "company image / brand asset"
                    )
                    if vision_caption_fn:
                        try:
                            caption = vision_caption_fn(path, purpose)
                        except Exception as e:  # noqa: BLE001 — record and continue
                            caption = default_image_caption(path, purpose)
                            warnings.append(f"{rel}: vision caption failed ({e})")
                    else:
                        caption = default_image_caption(path, purpose)
                        warnings.append(f"{rel}: vision disabled — filename-only caption")

                    platform = _platform_from_path(path, pack_dir) if slot == "social_posts" else ""
                    results.append(
                        FileResult(
                            slot,
                            rel,
                            path.name,
                            "ok",
                            chars=len(caption),
                            platform=platform,
                            caption=caption[:500],
                        )
                    )
                    header = f"[file: {rel}]"
                    if platform:
                        header = f"[platform: {platform}] {header}"
                    chunk_parts.append(f"{header}\n{caption}")
                else:
                    text = extract_document(path)
                    if not text:
                        results.append(
                            FileResult(slot, rel, path.name, "error", reason="no extractable text")
                        )
                        warnings.append(f"{rel}: no extractable text")
                        continue
                    results.append(
                        FileResult(slot, rel, path.name, "ok", chars=len(text))
                    )
                    chunk_parts.append(f"[file: {rel}]\n{text}")
            except Exception as e:  # noqa: BLE001
                results.append(FileResult(slot, rel, path.name, "error", reason=str(e)))
                warnings.append(f"{rel}: {e}")

        if chunk_parts:
            sections.append(_section(slot_titles[slot], "\n\n---\n\n".join(chunk_parts)))

    # LinkedIn post URLs (up to 5)
    urls = _load_linkedin_urls(pack_dir)
    if urls:
        url_parts: list[str] = []
        for i, url in enumerate(urls, start=1):
            body = f"URL: {url}\n"
            if fetch_linkedin_urls:
                try:
                    fetched = fetch_url_text(url)
                    body += f"FETCHED_TEXT:\n{fetched}"
                    status = "ok"
                    reason = ""
                    chars = len(fetched)
                except Exception as e:  # noqa: BLE001
                    body += (
                        "FETCH_FAILED: LinkedIn/pages often block automated fetch. "
                        f"Error: {e}. Prefer pasting post text into linkedin_posts/."
                    )
                    status = "error"
                    reason = str(e)
                    chars = 0
                    warnings.append(f"linkedin url {i}: fetch failed ({e})")
            else:
                body += "FETCH_SKIPPED: paste post text into linkedin_posts/ for voice analysis."
                status = "skipped"
                reason = "fetch disabled"
                chars = 0
            results.append(
                FileResult(
                    "linkedin_post_urls",
                    url,
                    f"post_url_{i}",
                    status,
                    reason=reason,
                    chars=chars,
                    url=url,
                )
            )
            url_parts.append(f"[LinkedIn post URL {i}]\n{body}")
        sections.append(_section("LINKEDIN POST URLS", "\n\n---\n\n".join(url_parts)))

    # Brand DNA from manifest
    brand = overrides.get("brand_dna")
    if isinstance(brand, dict) and any(brand.values()):
        lines = [f"{k}: {v}" for k, v in brand.items() if v]
        sections.append(_section("BRAND DNA", "\n".join(lines)))

    # Extra freeform from manifest
    if overrides.get("additional_context"):
        sections.append(_section("ADDITIONAL CONTEXT", str(overrides["additional_context"])))

    normalized = "\n".join(s for s in sections if s).strip()
    if not normalized:
        raise RuntimeError(
            f"Pack produced empty normalized input: {pack_dir}. "
            "Add files under onboarding/, linkedin_profile/, company_page/, etc."
        )

    return IngestResult(
        normalized_text=normalized + "\n",
        source="pack",
        source_path=str(pack_dir),
        files=results,
        warnings=warnings,
    )


def normalize_input(
    path: Path,
    *,
    vision_caption_fn: VisionCaptionFn | None = None,
    fetch_linkedin_urls: bool = True,
) -> IngestResult:
    """Normalize a single .txt file or an ingest pack folder."""
    path = path.resolve()
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        return IngestResult(
            normalized_text=text,
            source="file",
            source_path=str(path),
            files=[
                FileResult(
                    "legacy_raw_txt",
                    str(path),
                    path.name,
                    "ok",
                    chars=len(text),
                )
            ],
            warnings=[],
        )
    if path.is_dir():
        return normalize_pack(
            path,
            vision_caption_fn=vision_caption_fn,
            fetch_linkedin_urls=fetch_linkedin_urls,
        )
    raise FileNotFoundError(f"Input not found: {path}")


def write_ingest_artifacts(result: IngestResult, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = out_dir / "00_normalized_raw.txt"
    manifest_path = out_dir / "00_ingest_manifest.json"
    normalized_path.write_text(result.normalized_text, encoding="utf-8")
    manifest_path.write_text(
        json.dumps(result.to_manifest(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return normalized_path, manifest_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python scripts/ingest_normalize.py <file.txt|pack_dir> [out_dir]")
        raise SystemExit(2)
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("outputs") / "ingest_preview"
    result = normalize_input(src, fetch_linkedin_urls=False)
    n_path, m_path = write_ingest_artifacts(result, out)
    print(json.dumps(result.to_manifest(), indent=2))
    print(f"Normalized: {n_path}")
    print(f"Manifest: {m_path}")
