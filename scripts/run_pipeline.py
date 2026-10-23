"""
B2B LinkedIn orchestration runner:
  founder track (default): Ingest/Normalize -> RAW -> LMO -> Content Matrix -> writers
  company_page track:      Ingest/Normalize -> RAW -> Company LMO (+ handoff)
                           -> Company Matrix (2 Infographic + 2 Carousel)
                           -> company writers + gpt-image-2
  - gpt-6-luna (default) or Gemini 3.5 Flash rollback for text/JSON
  - OpenAI gpt-image-2: infographic + carousel slide PNG render
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ingest_normalize import (  # noqa: E402
    call_gemini_vision_caption,
    normalize_input,
    write_ingest_artifacts,
)

PROMPTS = ROOT / "prompts"
OUTPUTS = ROOT / "outputs"

DEFAULT_IMAGE_SIZE = "1024x1536"
MAX_CAROUSEL_SLIDES = 14
CAROUSEL_NEGATIVE_PROMPT = (
    "Generic template look, repeated layout, extra claims, missing text, color substitutions, "
    "font substitutions, AI filler, decorative clutter, unreadable spacing, fake dashboards, "
    "glowing robots, random decorative graphics, people, faces, headshots, human figures, "
    "silhouettes, avatars, hard-sell product brochure, pricing table, book-a-demo CTA, "
    "usernames, watermarks, slide counter boxes"
)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Tolerate trailing junk / extra braces after the first complete object.
        try:
            obj, _end = json.JSONDecoder().raw_decode(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            obj, _end = json.JSONDecoder().raw_decode(match.group(0))
            if isinstance(obj, dict):
                return obj
            raise


def call_gemini(
    system_prompt: str,
    user_content: str,
    model: str,
    api_key: str,
    *,
    expect_json: bool = True,
) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    closing = (
        "Return valid JSON only. No markdown fences. No preamble."
        if expect_json
        else "Output ONLY the final LinkedIn post text. No JSON. No preamble. No labels."
    )
    generation_config: dict = {
        "temperature": 0.3,
        "maxOutputTokens": 65536,
    }
    if expect_json:
        generation_config["responseMimeType"] = "application/json"

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            f"{system_prompt.strip()}\n\n"
                            f"===== USER PAYLOAD =====\n"
                            f"{user_content.strip()}\n"
                            f"===== END USER PAYLOAD =====\n\n"
                            f"{closing}"
                        )
                    }
                ],
            }
        ],
        "generationConfig": generation_config,
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
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini HTTP {e.code}: {detail}") from e

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"No candidates in response: {json.dumps(body)[:1000]}")
    parts = candidates[0].get("content", {}).get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("text")]
    if not texts:
        raise RuntimeError(f"Empty text in response: {json.dumps(body)[:1000]}")
    return "\n".join(texts)


def call_openai_chat(
    system_prompt: str,
    user_content: str,
    model: str,
    api_key: str,
    *,
    expect_json: bool = True,
) -> str:
    closing = (
        "Return valid JSON only. No markdown fences. No preamble."
        if expect_json
        else "Output ONLY the final LinkedIn post text. No JSON. No preamble. No labels."
    )
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    url = f"{base}/chat/completions"
    payload: dict = {
        "model": model,
        "max_completion_tokens": 16384,
        "messages": [
            {"role": "system", "content": system_prompt.strip()},
            {
                "role": "user",
                "content": f"{user_content.strip()}\n\n{closing}",
            },
        ],
    }
    if expect_json:
        payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI Chat HTTP {e.code}: {detail}") from e

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"No choices in response: {json.dumps(body)[:1000]}")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not str(content).strip():
        raise RuntimeError(f"Empty text in response: {json.dumps(body)[:1000]}")
    return str(content)


def call_text_llm(
    system_prompt: str,
    user_content: str,
    *,
    provider: str,
    model: str,
    api_key: str,
    expect_json: bool = True,
) -> str:
    if provider == "openai":
        return call_openai_chat(
            system_prompt, user_content, model, api_key, expect_json=expect_json
        )
    if provider == "gemini":
        return call_gemini(
            system_prompt, user_content, model, api_key, expect_json=expect_json
        )
    raise RuntimeError(f"Unknown TEXT_PROVIDER={provider} (use openai|gemini)")


def format_brand_dna_lock(handoff: dict) -> str:
    """Build a mandatory Brand DNA lock block from LMO handoff for all image gens in a run."""
    dna = handoff.get("brand_dna") if isinstance(handoff, dict) else None
    if not isinstance(dna, dict):
        dna = {}

    primary = str(dna.get("primary_color") or "").strip()
    secondary = str(dna.get("secondary_color") or "").strip()
    accent = str(dna.get("accent_color") or "").strip()
    heading = str(dna.get("heading_font") or "").strip()
    body = str(dna.get("body_font") or "").strip()
    background = str(dna.get("background_style") or "").strip()

    if not any([primary, secondary, accent, heading, body, background]):
        return (
            "BRAND DNA LOCK (mandatory for this entire run — do not invent a competing palette):\n"
            "No explicit brand palette was provided. Use one clean professional light-mode system only:\n"
            "primary_color: #0F172A\n"
            "secondary_color: #475569\n"
            "accent_color: #2563EB\n"
            "heading_font: Sora\n"
            "body_font: Inter\n"
            "background_style: Light mode clean slate\n"
            "Do not substitute other brand colors, neon accents, or alternate themes anywhere in this image."
        )

    lines = [
        "BRAND DNA LOCK (mandatory for this entire run — identical across infographic and all carousel slides):",
        f"primary_color: {primary or '#0F172A'}",
        f"secondary_color: {secondary or '#475569'}",
        f"accent_color: {accent or '#2563EB'}",
        f"heading_font: {heading or 'Sora'}",
        f"body_font: {body or 'Inter'}",
        f"background_style: {background or 'Light mode clean slate'}",
        "Use ONLY these exact colors and fonts. Do not invent, substitute, or introduce any other brand palette.",
        "Auto-generate only complementary readable text/border/background tones that match background_style.",
        "Every image in this run must share this same Brand DNA lock.",
    ]
    return "\n".join(lines)


def build_image_prompt(
    image_generation_prompt: str,
    negative_prompt: str,
    *,
    brand_dna_lock: str = "",
) -> str:
    prompt = (image_generation_prompt or "").strip()
    lock = (brand_dna_lock or "").strip()
    if lock:
        prompt = f"{lock}\n\n{prompt}"
    negative = (negative_prompt or "").strip()
    if negative:
        prompt = (
            f"{prompt}\n\n"
            f"Avoid the following (do not include any of these in the image): {negative}"
        )
    return prompt


def generate_openai_image(
    *,
    prompt: str,
    api_key: str,
    model: str,
    size: str = DEFAULT_IMAGE_SIZE,
) -> bytes:
    url = "https://api.openai.com/v1/images/generations"
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI Images HTTP {e.code}: {detail}") from e

    data = body.get("data") or []
    if not data:
        raise RuntimeError(f"OpenAI Images empty data: {json.dumps(body)[:1000]}")
    b64 = data[0].get("b64_json")
    if not b64:
        # Some responses may return url instead
        raise RuntimeError(
            "OpenAI Images response missing b64_json. "
            f"Keys={list(data[0].keys())}"
        )
    return base64.b64decode(b64)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


TAGLINE_STYLES = [
    "professional",
    "keyword_search",
    "benefit_first",
    "modern",
    "punchy",
]


def warn_company_lmo_constraints(company_json: dict) -> list[str]:
    """Light checks for company-page LMO (tagline 120, About 1000-1500, 5 options)."""
    warnings: list[str] = []
    tagline = company_json.get("tagline") or {}
    options = tagline.get("options") or []
    if not isinstance(options, list) or len(options) != 5:
        warnings.append(f"tagline.options count={len(options) if isinstance(options, list) else 'invalid'} (expected 5)")
    texts: list[str] = []
    if isinstance(options, list):
        for i, opt in enumerate(options):
            if not isinstance(opt, dict):
                warnings.append(f"tagline.options[{i}] is not an object")
                continue
            text = str(opt.get("text") or "")
            texts.append(text)
            n = len(text)
            if n > 120:
                warnings.append(f"tagline.options[{i}] {n} chars > 120")
            reported = opt.get("char_count")
            if reported != n:
                warnings.append(f"tagline.options[{i}] char_count={reported} actual={n}")
            style = opt.get("style")
            if i < len(TAGLINE_STYLES) and style != TAGLINE_STYLES[i]:
                warnings.append(
                    f"tagline.options[{i}] style={style!r} expected={TAGLINE_STYLES[i]!r}"
                )
    recommended = str(tagline.get("recommended") or "")
    if recommended and recommended not in texts:
        warnings.append("tagline.recommended is not one of the 5 option texts")
    if len(recommended) > 120:
        warnings.append(f"tagline.recommended {len(recommended)} chars > 120")
    about_obj = company_json.get("about_section") or {}
    about = str(about_obj.get("fixed") or "")
    about_n = len(about)
    if about_n < 1000 or about_n > 1500:
        warnings.append(f"about_section {about_n} chars (expected 1000-1500)")
    reported_about = about_obj.get("char_count")
    if reported_about != about_n:
        warnings.append(f"about_section.char_count={reported_about} actual={about_n}")
    return warnings


def normalize_format(value: object) -> str:
    return str(value or "").strip().lower()


def brief_growth_fields(post: dict) -> dict:
    """Pass-through Matrix growth-goal fields for storage + debugging."""
    return {
        "growth_angle": post.get("growth_angle", ""),
        "proof_type": post.get("proof_type", ""),
        "cta_style": post.get("cta_style", ""),
        "post_situation": post.get("post_situation", ""),
        "cta_type": post.get("cta_type", ""),
        "company_post_type": post.get("company_post_type", ""),
    }


def build_writer_payload(handoff: dict, post_brief: dict) -> str:
    return (
        "=== HANDOFF ===\n"
        f"{json.dumps(handoff, ensure_ascii=False, indent=2)}\n\n"
        "=== POST_BRIEF ===\n"
        f"{json.dumps(post_brief, ensure_ascii=False, indent=2)}\n"
    )


TEXT_POST_MIN_CHARS = 1000
TEXT_POST_MAX_CHARS = 1300


def text_post_in_range(text: str) -> bool:
    n = len(text.strip())
    return TEXT_POST_MIN_CHARS <= n <= TEXT_POST_MAX_CHARS


def load_text_post_bodies(posts_dir: Path) -> dict[int, str]:
    """Index -> post_text for text posts already on disk. Used so later posts can avoid copying them."""
    found: dict[int, str] = {}
    if not posts_dir.exists():
        return found
    for path in posts_dir.glob("post_*_text.json"):
        try:
            index = int(path.name.split("_")[1])
        except (IndexError, ValueError):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        text = str(data.get("post_text") or "").strip()
        if text:
            found[index] = text
    return found


def sibling_text_block(sibling_texts: dict[int, str], current_index: int) -> str:
    others = [sibling_texts[k] for k in sorted(sibling_texts) if k != current_index]
    if not others:
        body = "none"
    else:
        body = "\n\n".join(
            f"--- sibling {n} ---\n{text}" for n, text in enumerate(others, start=1)
        )
    return f"=== SIBLING_TEXT_POSTS ===\n{body}\n"


def text_length_revision_note(draft: str) -> str:
    n = len(draft.strip())
    if n < TEXT_POST_MIN_CHARS:
        move = (
            f"The draft is {n} characters. That is under {TEXT_POST_MIN_CHARS}. "
            "Expand the cause sentences and add a second clause on each step. "
            "Do not add an essay before the list."
        )
    else:
        move = (
            f"The draft is {n} characters. That is over {TEXT_POST_MAX_CHARS}. "
            "Shorten the cause sentences or the step clauses. "
            "Do not add an essay before the list."
        )
    return (
        "LENGTH REVISION (mandatory):\n"
        f"{move}\n"
        "Count the final paste with len(), including spaces and line breaks. "
        "Aim for 1050-1200 characters. Required range is 1000-1300.\n"
        "Keep the same facts. Do not invent clients, metrics, meetings, or case studies.\n"
        "Keep this post_situation's opening and both signposts. "
        "Do not reuse the first sentence or the signpost lines of any post in SIBLING_TEXT_POSTS. "
        "Do not paste sibling phrasing back in.\n"
        "Do not print writer labels such as Hook or CTA. "
        "Output ONLY the revised LinkedIn post.\n\n"
        "DRAFT:\n"
        f"{draft.strip()}\n"
    )


def post_outputs_complete(posts_dir: Path, index: int, fmt: str) -> bool:
    """True when a prior run already wrote the expected artifacts for this post."""
    if fmt in {"text post", "text"}:
        return (posts_dir / f"post_{index:02d}_text.json").exists()
    if fmt == "infographic":
        return (posts_dir / f"post_{index:02d}_infographic.json").exists() and (
            posts_dir / f"post_{index:02d}_infographic.png"
        ).exists()
    if fmt == "carousel":
        meta = posts_dir / f"post_{index:02d}_carousel.json"
        if not meta.exists():
            return False
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            slides = data.get("slides") or []
            if not slides:
                return False
            return all(
                (posts_dir / f"post_{index:02d}_carousel_slide_{n:02d}.png").exists()
                for n in range(1, len(slides) + 1)
            )
        except (json.JSONDecodeError, OSError, TypeError):
            return False
    return False


def run_post_writers(
    *,
    handoff: dict,
    matrix_json: dict,
    out_dir: Path,
    prompts: dict[str, str],
    text_provider: str,
    text_model: str,
    text_api_key: str,
    openai_api_key: str,
    openai_image_model: str,
    skip_complete: bool = False,
) -> dict:
    posts = matrix_json.get("weekly_content_matrix") or []
    if not isinstance(posts, list):
        posts = []

    posts_dir = out_dir / "04_posts"
    posts_dir.mkdir(parents=True, exist_ok=True)

    brand_dna_lock = format_brand_dna_lock(handoff)
    (out_dir / "00_brand_dna_lock.txt").write_text(brand_dna_lock + "\n", encoding="utf-8")

    text_posts_written = 0
    text_length_warnings: list[str] = []
    infographics_written = 0
    infographic_images_generated = 0
    infographic_image_errors: list[dict] = []
    carousels_written = 0
    carousel_slides_generated = 0
    carousel_image_errors: list[dict] = []
    skipped_formats: list[dict] = []

    sibling_texts = load_text_post_bodies(posts_dir)

    total = len(posts)
    for i, post in enumerate(posts, start=1):
        if not isinstance(post, dict):
            skipped_formats.append({"index": i, "format": "invalid", "reason": "not_object"})
            continue

        fmt = normalize_format(post.get("format"))
        label = f"{i}/{total}"
        if skip_complete and post_outputs_complete(posts_dir, i, fmt):
            print(f"STAGE 4 SKIP {label} already complete format={post.get('format')}")
            continue
        payload = build_writer_payload(handoff, post)

        if fmt in {"text post", "text"}:
            payload += "\n" + sibling_text_block(sibling_texts, i)
            print(f"STAGE 4 TEXT {label} ...")
            raw = call_text_llm(
                prompts["text"],
                payload,
                provider=text_provider,
                model=text_model,
                api_key=text_api_key,
                expect_json=False,
            )
            post_text = raw.strip()
            if not text_post_in_range(post_text):
                print(
                    f"STAGE 4 TEXT {label} length={len(post_text)} "
                    f"outside {TEXT_POST_MIN_CHARS}-{TEXT_POST_MAX_CHARS}, retry once"
                )
                revised = call_text_llm(
                    prompts["text"],
                    payload + "\n" + text_length_revision_note(post_text),
                    provider=text_provider,
                    model=text_model,
                    api_key=text_api_key,
                    expect_json=False,
                )
                post_text = revised.strip()
                raw = revised
            if not text_post_in_range(post_text):
                warning = (
                    f"post_{i:02d}_text length={len(post_text)} "
                    f"(expected {TEXT_POST_MIN_CHARS}-{TEXT_POST_MAX_CHARS})"
                )
                text_length_warnings.append(warning)
                print(f"  warn: {warning}")
            (posts_dir / f"post_{i:02d}_text_raw.txt").write_text(raw, encoding="utf-8")
            wrapped = {
                "day": post.get("day", ""),
                "format": "Text Post",
                "working_title": post.get("working_title", ""),
                "pillar": post.get("pillar", ""),
                "content_role": post.get("content_role", ""),
                "product_grounded": bool(post.get("product_grounded", False)),
                **brief_growth_fields(post),
                "post_text": post_text,
                "post_text_chars": len(post_text),
            }
            save_json(posts_dir / f"post_{i:02d}_text.json", wrapped)
            sibling_texts[i] = post_text
            text_posts_written += 1
            print(f"STAGE 4 TEXT {label} done chars={len(post_text)}")
            continue

        if fmt == "infographic":
            print(f"STAGE 4 INFOGRAPHIC (caption+prompt) {label} ...")
            raw = call_text_llm(
                prompts["infographic"],
                payload,
                provider=text_provider,
                model=text_model,
                api_key=text_api_key,
                expect_json=True,
            )
            (posts_dir / f"post_{i:02d}_infographic_raw.txt").write_text(raw, encoding="utf-8")
            info_json = extract_json(raw)
            post_text = str(info_json.get("post_text") or "").strip()
            image_generation_prompt = info_json.get("image_generation_prompt", "")
            negative_prompt = info_json.get("negative_prompt", "")
            png_name = f"post_{i:02d}_infographic.png"
            png_path = posts_dir / png_name

            if not post_text:
                print(f"STAGE 4 INFOGRAPHIC {label} warn: post_text missing from Agent 5 JSON")

            stored = {
                "day": post.get("day", ""),
                "format": "Infographic",
                "working_title": post.get("working_title", ""),
                "pillar": post.get("pillar", ""),
                "content_role": post.get("content_role", ""),
                "product_grounded": bool(post.get("product_grounded", False)),
                **brief_growth_fields(post),
                "post_text": post_text,
                "image_generation_prompt": image_generation_prompt,
                "negative_prompt": negative_prompt,
                "image_model": openai_image_model,
                "image_size": DEFAULT_IMAGE_SIZE,
                "image_path": "",
                "image_error": "",
                "brand_dna_lock": brand_dna_lock,
            }
            infographics_written += 1
            print(
                f"STAGE 4 INFOGRAPHIC {label} done "
                f"(caption={'yes' if post_text else 'NO'})"
            )

            if not openai_api_key:
                err = "OPENAI_API_KEY missing — caption + prompt saved, image not generated"
                stored["image_error"] = err
                infographic_image_errors.append(
                    {
                        "index": i,
                        "working_title": post.get("working_title", ""),
                        "error": err,
                    }
                )
                save_json(posts_dir / f"post_{i:02d}_infographic.json", stored)
                print(f"STAGE 4 INFOGRAPHIC IMAGE {label} skipped: {err}")
                continue

            print(f"STAGE 4 INFOGRAPHIC IMAGE {label} (OpenAI {openai_image_model}) ...")
            try:
                full_prompt = build_image_prompt(
                    image_generation_prompt,
                    negative_prompt,
                    brand_dna_lock=brand_dna_lock,
                )
                png_bytes = generate_openai_image(
                    prompt=full_prompt,
                    api_key=openai_api_key,
                    model=openai_image_model,
                    size=DEFAULT_IMAGE_SIZE,
                )
                png_path.write_bytes(png_bytes)
                stored["image_path"] = str(png_path.relative_to(out_dir)).replace("\\", "/")
                infographic_images_generated += 1
                print(f"STAGE 4 INFOGRAPHIC IMAGE {label} done -> {stored['image_path']}")
            except Exception as e:  # noqa: BLE001 - capture API failures per post
                stored["image_error"] = str(e)
                infographic_image_errors.append(
                    {
                        "index": i,
                        "working_title": post.get("working_title", ""),
                        "error": str(e),
                    }
                )
                print(f"STAGE 4 INFOGRAPHIC IMAGE {label} FAIL: {e}")

            save_json(posts_dir / f"post_{i:02d}_infographic.json", stored)
            continue

        if fmt == "carousel":
            print(f"STAGE 4 CAROUSEL (caption+slides) {label} ...")
            raw = call_text_llm(
                prompts["carousel"],
                payload,
                provider=text_provider,
                model=text_model,
                api_key=text_api_key,
                expect_json=True,
            )
            (posts_dir / f"post_{i:02d}_carousel_raw.txt").write_text(raw, encoding="utf-8")
            carousel_json = extract_json(raw)
            post_text = str(carousel_json.get("post_text") or "").strip()
            slides = carousel_json.get("slides") or []
            if not isinstance(slides, list):
                slides = []

            truncate_note = ""
            if len(slides) > MAX_CAROUSEL_SLIDES:
                truncate_note = (
                    f"Truncated from {len(slides)} to {MAX_CAROUSEL_SLIDES} slides (cost guard)"
                )
                slides = slides[:MAX_CAROUSEL_SLIDES]
                print(f"STAGE 4 CAROUSEL {label} warn: {truncate_note}")

            # Normalize counts after possible truncation
            for idx, slide in enumerate(slides, start=1):
                if isinstance(slide, dict):
                    slide["slide_number"] = idx
            total_slides = len(slides)
            total_inner = sum(
                1
                for s in slides
                if isinstance(s, dict) and str(s.get("slide_role", "")).lower() == "content"
            )

            alignment_warnings: list[str] = []
            if not post_text:
                alignment_warnings.append("post_text missing from carousel JSON")
            # Soft check: cover numerical promise vs inner count
            cover_headline = ""
            for s in slides:
                if isinstance(s, dict) and str(s.get("slide_role", "")).lower() == "cover":
                    cover_headline = str(s.get("headline") or "")
                    break
            m = re.search(r"\b(\d+)\s*(?:step|tip|point|myth|fact|stat|principle|mistake|framework)s?\b", cover_headline, re.I)
            if m and total_inner and int(m.group(1)) != total_inner:
                alignment_warnings.append(
                    f"cover promises {m.group(1)} but total_inner_slides={total_inner}"
                )
            if alignment_warnings:
                for w in alignment_warnings:
                    print(f"STAGE 4 CAROUSEL {label} warn: {w}")

            stored_slides: list[dict] = []
            for slide in slides:
                if not isinstance(slide, dict):
                    continue
                stored_slides.append(
                    {
                        "slide_number": slide.get("slide_number", 0),
                        "slide_role": slide.get("slide_role", ""),
                        "headline": slide.get("headline", ""),
                        "body_text": slide.get("body_text", ""),
                        "counter": slide.get("counter", ""),
                        "image_gen_prompt": slide.get("image_gen_prompt", ""),
                        "image_path": "",
                        "image_error": "",
                    }
                )

            stored = {
                "day": post.get("day", ""),
                "format": "Carousel",
                "working_title": post.get("working_title", ""),
                "pillar": post.get("pillar", ""),
                "content_role": post.get("content_role", ""),
                "product_grounded": bool(post.get("product_grounded", False)),
                **brief_growth_fields(post),
                "post_text": post_text,
                "total_slides": total_slides or carousel_json.get("total_slides", 0),
                "total_inner_slides": total_inner
                if total_inner
                else carousel_json.get("total_inner_slides", 0),
                "carousel_strategy": carousel_json.get("carousel_strategy") or {},
                "visual_dna_summary": carousel_json.get("visual_dna_summary") or {},
                "hook_type": carousel_json.get("hook_type", ""),
                "hook_reason": carousel_json.get("hook_reason", ""),
                "cta_goal": carousel_json.get("cta_goal", ""),
                "cta_reason": carousel_json.get("cta_reason", ""),
                "truncate_note": truncate_note,
                "alignment_warnings": alignment_warnings,
                "image_model": openai_image_model,
                "image_size": DEFAULT_IMAGE_SIZE,
                "brand_dna_lock": brand_dna_lock,
                "slides": stored_slides,
            }
            carousels_written += 1
            print(
                f"STAGE 4 CAROUSEL {label} done "
                f"(caption={'yes' if post_text else 'NO'}, {len(stored_slides)} slides)"
            )

            if not openai_api_key:
                err = "OPENAI_API_KEY missing — caption + slides saved, images not generated"
                for s in stored_slides:
                    s["image_error"] = err
                carousel_image_errors.append(
                    {
                        "index": i,
                        "working_title": post.get("working_title", ""),
                        "error": err,
                    }
                )
                save_json(posts_dir / f"post_{i:02d}_carousel.json", stored)
                print(f"STAGE 4 CAROUSEL IMAGES {label} skipped: {err}")
                continue

            for s in stored_slides:
                sn = int(s.get("slide_number") or 0)
                prompt = (s.get("image_gen_prompt") or "").strip()
                if not prompt:
                    err = "missing image_gen_prompt"
                    s["image_error"] = err
                    carousel_image_errors.append(
                        {
                            "index": i,
                            "slide": sn,
                            "working_title": post.get("working_title", ""),
                            "error": err,
                        }
                    )
                    continue

                png_name = f"post_{i:02d}_carousel_slide_{sn:02d}.png"
                png_path = posts_dir / png_name
                print(
                    f"STAGE 4 CAROUSEL IMAGE {label} slide {sn}/{len(stored_slides)} "
                    f"(OpenAI {openai_image_model}) ..."
                )
                try:
                    full_prompt = build_image_prompt(
                        prompt,
                        CAROUSEL_NEGATIVE_PROMPT,
                        brand_dna_lock=brand_dna_lock,
                    )
                    png_bytes = generate_openai_image(
                        prompt=full_prompt,
                        api_key=openai_api_key,
                        model=openai_image_model,
                        size=DEFAULT_IMAGE_SIZE,
                    )
                    png_path.write_bytes(png_bytes)
                    s["image_path"] = str(png_path.relative_to(out_dir)).replace("\\", "/")
                    carousel_slides_generated += 1
                    print(f"STAGE 4 CAROUSEL IMAGE {label} slide {sn} done -> {s['image_path']}")
                except Exception as e:  # noqa: BLE001
                    s["image_error"] = str(e)
                    carousel_image_errors.append(
                        {
                            "index": i,
                            "slide": sn,
                            "working_title": post.get("working_title", ""),
                            "error": str(e),
                        }
                    )
                    print(f"STAGE 4 CAROUSEL IMAGE {label} slide {sn} FAIL: {e}")

            save_json(posts_dir / f"post_{i:02d}_carousel.json", stored)
            continue

        skipped_formats.append(
            {
                "index": i,
                "day": post.get("day", ""),
                "format": post.get("format", ""),
                "working_title": post.get("working_title", ""),
                "reason": "unsupported_format",
            }
        )
        print(f"STAGE 4 SKIP {label} format={post.get('format')}")

    return {
        "text_posts_written": text_posts_written,
        "text_length_warnings": text_length_warnings,
        "infographics_written": infographics_written,
        "infographic_images_generated": infographic_images_generated,
        "infographic_image_errors": infographic_image_errors,
        "carousels_written": carousels_written,
        "carousel_slides_generated": carousel_slides_generated,
        "carousel_image_errors": carousel_image_errors,
        "skipped_formats": skipped_formats,
    }


def main() -> int:
    load_env(ROOT / ".env")
    text_provider = os.environ.get("TEXT_PROVIDER", "openai").strip().lower() or "openai"
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip()
    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_image_model = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2").strip()
    if text_provider == "openai":
        text_model = os.environ.get("TEXT_MODEL", "gpt-6-luna").strip() or "gpt-6-luna"
        text_api_key = openai_api_key
        if not text_api_key:
            print("FAIL: OPENAI_API_KEY missing (required for TEXT_PROVIDER=openai)", file=sys.stderr)
            return 1
    elif text_provider == "gemini":
        text_model = os.environ.get("TEXT_MODEL", gemini_model).strip() or gemini_model
        text_api_key = gemini_api_key
        if not text_api_key:
            print("FAIL: GEMINI_API_KEY missing (required for TEXT_PROVIDER=gemini)", file=sys.stderr)
            return 1
    else:
        print(f"FAIL: invalid TEXT_PROVIDER={text_provider} (use openai|gemini)", file=sys.stderr)
        return 1

    args = [a for a in sys.argv[1:] if a]
    stop_after = "all"
    if "--stop-after" in args:
        i = args.index("--stop-after")
        if i + 1 >= len(args):
            print(
                "FAIL: --stop-after requires one of: raw, lmo, matrix, company_lmo, company_matrix, all",
                file=sys.stderr,
            )
            return 1
        stop_after = args[i + 1].strip().lower()
        del args[i : i + 2]
    if stop_after not in {"raw", "lmo", "matrix", "company_lmo", "company_matrix", "all"}:
        print(
            f"FAIL: invalid --stop-after={stop_after} "
            "(use raw|lmo|matrix|company_lmo|company_matrix|all)",
            file=sys.stderr,
        )
        return 1

    track = "founder"
    if "--track" in args:
        i = args.index("--track")
        if i + 1 >= len(args):
            print("FAIL: --track requires one of: founder, company_page", file=sys.stderr)
            return 1
        track = args[i + 1].strip().lower()
        del args[i : i + 2]
    if track not in {"founder", "company_page"}:
        print(f"FAIL: invalid --track={track} (use founder|company_page)", file=sys.stderr)
        return 1

    resume_dir: Path | None = None
    if "--resume" in args:
        i = args.index("--resume")
        if i + 1 >= len(args):
            print("FAIL: --resume requires an outputs/run_* directory", file=sys.stderr)
            return 1
        resume_dir = Path(args[i + 1])
        del args[i : i + 2]
        if not resume_dir.exists():
            print(f"FAIL: resume dir not found: {resume_dir}", file=sys.stderr)
            return 1

    if resume_dir is not None and track == "company_page":
        print("FAIL: --resume is for founder post writers; omit it for --track company_page", file=sys.stderr)
        return 1

    prompts = {
        "raw": (PROMPTS / "01_raw_input_structurer.md").read_text(encoding="utf-8"),
        "lmo": (PROMPTS / "02_lmo.md").read_text(encoding="utf-8"),
        "company_lmo": (PROMPTS / "02_company_lmo.md").read_text(encoding="utf-8"),
        "matrix": (PROMPTS / "03_content_matrix.md").read_text(encoding="utf-8"),
        "company_matrix": (PROMPTS / "03_company_content_matrix.md").read_text(encoding="utf-8"),
        "text": (PROMPTS / "04_text_post.md").read_text(encoding="utf-8"),
        "infographic": (PROMPTS / "05_infographic_post.md").read_text(encoding="utf-8"),
        "carousel": (PROMPTS / "06_carousel_post.md").read_text(encoding="utf-8"),
        "company_infographic": (PROMPTS / "05_company_infographic_post.md").read_text(encoding="utf-8"),
        "company_carousel": (PROMPTS / "06_company_carousel_post.md").read_text(encoding="utf-8"),
    }

    if resume_dir is not None:
        out_dir = resume_dir
        run_id = out_dir.name.replace("run_", "", 1) if out_dir.name.startswith("run_") else out_dir.name
        handoff_path = out_dir / "02_handoff.json"
        matrix_path = out_dir / "03_content_matrix.json"
        lmo_path = out_dir / "02_lmo.json"
        if not handoff_path.exists() or not matrix_path.exists():
            print("FAIL: --resume needs 02_handoff.json and 03_content_matrix.json", file=sys.stderr)
            return 1
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        matrix_json = json.loads(matrix_path.read_text(encoding="utf-8"))
        lmo_json = json.loads(lmo_path.read_text(encoding="utf-8")) if lmo_path.exists() else {}
        print(f"TEXT_PROVIDER={text_provider}")
        print(f"TEXT_MODEL={text_model}")
        print(f"OPENAI_IMAGE_MODEL={openai_image_model}")
        print(f"OPENAI_API_KEY_SET={bool(openai_api_key)}")
        print(f"RESUME={out_dir}")
        print("STAGE 4/5 POST WRITERS (resume, skip complete) ...")
        writer_stats = run_post_writers(
            handoff=handoff,
            matrix_json=matrix_json,
            out_dir=out_dir,
            prompts=prompts,
            text_provider=text_provider,
            text_model=text_model,
            text_api_key=text_api_key,
            openai_api_key=openai_api_key,
            openai_image_model=openai_image_model,
            skip_complete=True,
        )
        print("STAGE 4/5 POST WRITERS done")
        summary = {
            "run_id": run_id,
            "text_provider": text_provider,
            "text_model": text_model,
            "openai_image_model": openai_image_model,
            "raw_input": "resume",
            "selected_positioning_mode": lmo_json.get("selected_positioning_mode")
            or handoff.get("selected_positioning_mode"),
            "selected_growth_goal": lmo_json.get("selected_growth_goal")
            or handoff.get("selected_growth_goal"),
            "confirmed_niche": lmo_json.get("confirmed_niche") or handoff.get("confirmed_niche"),
            "headline": ((lmo_json.get("profile_optimization") or {}).get("headline") or {}).get("fixed")
            or handoff.get("optimized_headline"),
            "pillars": [p.get("topic") for p in (lmo_json.get("content_pillars") or handoff.get("content_pillars") or []) if isinstance(p, dict)],
            "matrix_posts": (matrix_json.get("week_summary") or {}).get("total_posts"),
            "text_posts_written": writer_stats["text_posts_written"],
            "text_length_warnings": writer_stats.get("text_length_warnings") or [],
            "infographics_written": writer_stats["infographics_written"],
            "infographic_images_generated": writer_stats["infographic_images_generated"],
            "infographic_image_errors": writer_stats["infographic_image_errors"],
            "carousels_written": writer_stats["carousels_written"],
            "carousel_slides_generated": writer_stats["carousel_slides_generated"],
            "carousel_image_errors": writer_stats["carousel_image_errors"],
            "skipped_formats": writer_stats["skipped_formats"],
            "stop_after": "all",
            "status": "completed_resume",
        }
        save_json(out_dir / "00_summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"COMPLETED (resume): {out_dir}")
        return 0

    input_path = Path(args[0]) if args else ROOT / "inputs" / "kumar_apurva_raw.txt"
    if not input_path.exists():
        print(f"FAIL: input not found: {input_path}", file=sys.stderr)
        return 1

    run_id = time.strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUTS / f"run_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"TEXT_PROVIDER={text_provider}")
    print(f"TEXT_MODEL={text_model}")
    print(f"OPENAI_IMAGE_MODEL={openai_image_model}")
    print(f"OPENAI_API_KEY_SET={bool(openai_api_key)}")
    print(f"TRACK={track}")
    print(f"OUT={out_dir}")

    # STAGE 0 — ingest + normalize (file passthrough or multi-upload pack)
    print("STAGE 0/5 INGEST ...")
    use_vision = os.environ.get("INGEST_VISION", "1").strip() not in {"0", "false", "False"}
    fetch_urls = os.environ.get("INGEST_FETCH_URLS", "1").strip() not in {"0", "false", "False"}

    vision_fn = None
    if use_vision and input_path.is_dir() and gemini_api_key:

        def vision_fn(path: Path, purpose: str) -> str:
            return call_gemini_vision_caption(
                path,
                purpose,
                model=gemini_model,
                api_key=gemini_api_key,
            )

    try:
        ingest = normalize_input(
            input_path,
            vision_caption_fn=vision_fn,
            fetch_linkedin_urls=fetch_urls and input_path.is_dir(),
        )
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: ingest/normalize error: {e}", file=sys.stderr)
        return 1

    normalized_path, ingest_manifest_path = write_ingest_artifacts(ingest, out_dir)
    raw_text = ingest.normalized_text
    print(
        f"STAGE 0/5 INGEST done source={ingest.source} files={len(ingest.files)} "
        f"warnings={len(ingest.warnings)} -> {normalized_path.name}"
    )
    if ingest.warnings:
        for w in ingest.warnings[:12]:
            print(f"  warn: {w}")
        if len(ingest.warnings) > 12:
            print(f"  warn: ... +{len(ingest.warnings) - 12} more (see {ingest_manifest_path.name})")

    print("STAGE 1/5 RAW ...")
    raw_resp = call_text_llm(
        prompts["raw"],
        raw_text,
        provider=text_provider,
        model=text_model,
        api_key=text_api_key,
    )
    (out_dir / "01_raw_raw_response.txt").write_text(raw_resp, encoding="utf-8")
    raw_json = extract_json(raw_resp)
    save_json(out_dir / "01_raw.json", raw_json)
    agent_1_input = raw_json.get("agent_1_input")
    if not isinstance(agent_1_input, dict):
        print("FAIL: agent_1_input missing from RAW output", file=sys.stderr)
        return 1
    save_json(out_dir / "01_agent_1_input.json", agent_1_input)
    print("STAGE 1/5 RAW done")

    if stop_after == "raw":
        summary = {
            "run_id": run_id,
            "text_provider": text_provider,
            "text_model": text_model,
            "openai_image_model": openai_image_model,
            "raw_input": str(input_path),
            "ingest_source": ingest.source,
            "ingest_files": len(ingest.files),
            "ingest_warnings": len(ingest.warnings),
            "normalized_raw": str(normalized_path.relative_to(out_dir)).replace("\\", "/"),
            "track": track,
            "selected_positioning_mode": agent_1_input.get("selected_positioning_mode"),
            "selected_growth_goal": agent_1_input.get("selected_growth_goal"),
            "stop_after": stop_after,
            "status": "completed_stop_after_raw",
        }
        save_json(out_dir / "00_summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"COMPLETED (stop-after raw): {out_dir}")
        return 0

    if track == "company_page":
        print("STAGE 2/4 COMPANY PAGE LMO ...")
        company_payload = json.dumps(agent_1_input, ensure_ascii=False, indent=2)
        company_resp = call_text_llm(
            prompts["company_lmo"],
            company_payload,
            provider=text_provider,
            model=text_model,
            api_key=text_api_key,
        )
        (out_dir / "02_company_lmo_raw_response.txt").write_text(company_resp, encoding="utf-8")
        company_json = extract_json(company_resp)
        save_json(out_dir / "02_company_lmo.json", company_json)
        constraint_warnings = warn_company_lmo_constraints(company_json)
        for w in constraint_warnings:
            print(f"  warn: {w}")
        company_handoff = company_json.get("handoff")
        if not isinstance(company_handoff, dict):
            print("FAIL: handoff missing from Company Page LMO output", file=sys.stderr)
            return 1
        save_json(out_dir / "02_company_handoff.json", company_handoff)
        print("STAGE 2/4 COMPANY PAGE LMO done")
        about_fixed = ((company_json.get("about_section") or {}).get("fixed") or "")

        if stop_after in {"company_lmo", "lmo"}:
            summary = {
                "run_id": run_id,
                "text_provider": text_provider,
                "text_model": text_model,
                "openai_image_model": openai_image_model,
                "track": track,
                "raw_input": str(input_path),
                "ingest_source": ingest.source,
                "ingest_files": len(ingest.files),
                "ingest_warnings": len(ingest.warnings),
                "normalized_raw": str(normalized_path.relative_to(out_dir)).replace("\\", "/"),
                "selected_positioning_mode": agent_1_input.get("selected_positioning_mode"),
                "selected_growth_goal": agent_1_input.get("selected_growth_goal")
                or company_handoff.get("selected_growth_goal"),
                "company_title": company_json.get("company_title"),
                "tagline_recommended": (company_json.get("tagline") or {}).get("recommended"),
                "about_char_count": len(about_fixed),
                "brand_dna": company_handoff.get("brand_dna"),
                "constraint_warnings": constraint_warnings,
                "stop_after": "company_lmo",
                "status": "completed_stop_after_company_lmo",
            }
            save_json(out_dir / "00_summary.json", summary)
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            print(f"COMPLETED (stop-after company_lmo): {out_dir}")
            return 0

        print("STAGE 3/4 COMPANY CONTENT MATRIX ...")
        matrix_payload = json.dumps(company_handoff, ensure_ascii=False, indent=2)
        matrix_resp = call_text_llm(
            prompts["company_matrix"],
            matrix_payload,
            provider=text_provider,
            model=text_model,
            api_key=text_api_key,
        )
        (out_dir / "03_company_matrix_raw_response.txt").write_text(matrix_resp, encoding="utf-8")
        company_matrix_json = extract_json(matrix_resp)
        save_json(out_dir / "03_company_content_matrix.json", company_matrix_json)
        print("STAGE 3/4 COMPANY CONTENT MATRIX done")

        if stop_after in {"company_matrix", "matrix"}:
            summary = {
                "run_id": run_id,
                "text_provider": text_provider,
                "text_model": text_model,
                "openai_image_model": openai_image_model,
                "track": track,
                "raw_input": str(input_path),
                "ingest_source": ingest.source,
                "ingest_files": len(ingest.files),
                "ingest_warnings": len(ingest.warnings),
                "normalized_raw": str(normalized_path.relative_to(out_dir)).replace("\\", "/"),
                "selected_growth_goal": company_handoff.get("selected_growth_goal"),
                "company_title": company_json.get("company_title"),
                "tagline_recommended": (company_json.get("tagline") or {}).get("recommended"),
                "about_char_count": len(about_fixed),
                "brand_dna": company_handoff.get("brand_dna"),
                "matrix_posts": (company_matrix_json.get("week_summary") or {}).get("total_posts"),
                "format_counts": (company_matrix_json.get("week_summary") or {}).get("format_counts"),
                "constraint_warnings": constraint_warnings,
                "stop_after": "company_matrix",
                "status": "completed_stop_after_company_matrix",
            }
            save_json(out_dir / "00_summary.json", summary)
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            print(f"COMPLETED (stop-after company_matrix): {out_dir}")
            return 0

        print("STAGE 4/4 COMPANY POST WRITERS ...")
        company_writer_prompts = {
            **prompts,
            "infographic": prompts["company_infographic"],
            "carousel": prompts["company_carousel"],
        }
        writer_stats = run_post_writers(
            handoff=company_handoff,
            matrix_json=company_matrix_json,
            out_dir=out_dir,
            prompts=company_writer_prompts,
            text_provider=text_provider,
            text_model=text_model,
            text_api_key=text_api_key,
            openai_api_key=openai_api_key,
            openai_image_model=openai_image_model,
            skip_complete=False,
        )
        print("STAGE 4/4 COMPANY POST WRITERS done")
        summary = {
            "run_id": run_id,
            "text_provider": text_provider,
            "text_model": text_model,
            "openai_image_model": openai_image_model,
            "track": track,
            "raw_input": str(input_path),
            "ingest_source": ingest.source,
            "ingest_files": len(ingest.files),
            "ingest_warnings": len(ingest.warnings),
            "normalized_raw": str(normalized_path.relative_to(out_dir)).replace("\\", "/"),
            "selected_growth_goal": company_handoff.get("selected_growth_goal"),
            "company_title": company_json.get("company_title"),
            "tagline_recommended": (company_json.get("tagline") or {}).get("recommended"),
            "about_char_count": len(about_fixed),
            "brand_dna": company_handoff.get("brand_dna"),
            "matrix_posts": (company_matrix_json.get("week_summary") or {}).get("total_posts"),
            "format_counts": (company_matrix_json.get("week_summary") or {}).get("format_counts"),
            "company_post_type_counts": (company_matrix_json.get("week_summary") or {}).get(
                "company_post_type_counts"
            ),
            "infographics_written": writer_stats["infographics_written"],
            "infographic_images_generated": writer_stats["infographic_images_generated"],
            "infographic_image_errors": writer_stats["infographic_image_errors"],
            "carousels_written": writer_stats["carousels_written"],
            "carousel_slides_generated": writer_stats["carousel_slides_generated"],
            "carousel_image_errors": writer_stats["carousel_image_errors"],
            "skipped_formats": writer_stats["skipped_formats"],
            "constraint_warnings": constraint_warnings,
            "stop_after": stop_after,
            "status": "completed_company_page",
        }
        save_json(out_dir / "00_summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"COMPLETED (company page): {out_dir}")
        return 0

    print("STAGE 2/5 LMO ...")
    lmo_payload = json.dumps(agent_1_input, ensure_ascii=False, indent=2)
    lmo_resp = call_text_llm(
        prompts["lmo"],
        lmo_payload,
        provider=text_provider,
        model=text_model,
        api_key=text_api_key,
    )
    (out_dir / "02_lmo_raw_response.txt").write_text(lmo_resp, encoding="utf-8")
    lmo_json = extract_json(lmo_resp)
    save_json(out_dir / "02_lmo.json", lmo_json)
    handoff = lmo_json.get("handoff")
    if not isinstance(handoff, dict):
        print("FAIL: handoff missing from LMO output", file=sys.stderr)
        return 1
    save_json(out_dir / "02_handoff.json", handoff)
    print("STAGE 2/5 LMO done")

    if stop_after == "lmo":
        summary = {
            "run_id": run_id,
            "text_provider": text_provider,
            "text_model": text_model,
            "openai_image_model": openai_image_model,
            "raw_input": str(input_path),
            "ingest_source": ingest.source,
            "ingest_files": len(ingest.files),
            "ingest_warnings": len(ingest.warnings),
            "normalized_raw": str(normalized_path.relative_to(out_dir)).replace("\\", "/"),
            "selected_positioning_mode": agent_1_input.get("selected_positioning_mode")
            or lmo_json.get("selected_positioning_mode"),
            "selected_growth_goal": agent_1_input.get("selected_growth_goal")
            or lmo_json.get("selected_growth_goal")
            or (lmo_json.get("handoff") or {}).get("selected_growth_goal"),
            "confirmed_niche": lmo_json.get("confirmed_niche"),
            "headline": ((lmo_json.get("profile_optimization") or {}).get("headline") or {}).get("fixed"),
            "pillars": [p.get("topic") for p in (lmo_json.get("content_pillars") or []) if isinstance(p, dict)],
            "stop_after": stop_after,
            "status": "completed_stop_after_lmo",
        }
        save_json(out_dir / "00_summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"COMPLETED (stop-after lmo): {out_dir}")
        return 0

    print("STAGE 3/5 CONTENT MATRIX ...")
    matrix_payload = json.dumps(handoff, ensure_ascii=False, indent=2)
    matrix_resp = call_text_llm(
        prompts["matrix"],
        matrix_payload,
        provider=text_provider,
        model=text_model,
        api_key=text_api_key,
    )
    (out_dir / "03_matrix_raw_response.txt").write_text(matrix_resp, encoding="utf-8")
    matrix_json = extract_json(matrix_resp)
    save_json(out_dir / "03_content_matrix.json", matrix_json)
    print("STAGE 3/5 CONTENT MATRIX done")

    if stop_after == "matrix":
        summary = {
            "run_id": run_id,
            "text_provider": text_provider,
            "text_model": text_model,
            "openai_image_model": openai_image_model,
            "raw_input": str(input_path),
            "ingest_source": ingest.source,
            "ingest_files": len(ingest.files),
            "ingest_warnings": len(ingest.warnings),
            "normalized_raw": str(normalized_path.relative_to(out_dir)).replace("\\", "/"),
            "selected_positioning_mode": agent_1_input.get("selected_positioning_mode")
            or lmo_json.get("selected_positioning_mode"),
            "selected_growth_goal": agent_1_input.get("selected_growth_goal")
            or lmo_json.get("selected_growth_goal")
            or (lmo_json.get("handoff") or {}).get("selected_growth_goal"),
            "confirmed_niche": lmo_json.get("confirmed_niche"),
            "headline": ((lmo_json.get("profile_optimization") or {}).get("headline") or {}).get("fixed"),
            "pillars": [p.get("topic") for p in (lmo_json.get("content_pillars") or []) if isinstance(p, dict)],
            "matrix_posts": (matrix_json.get("week_summary") or {}).get("total_posts"),
            "stop_after": stop_after,
            "status": "completed_stop_after_matrix",
        }
        save_json(out_dir / "00_summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"COMPLETED (stop-after matrix): {out_dir}")
        return 0

    print("STAGE 4/5 POST WRITERS ...")
    writer_stats = run_post_writers(
        handoff=handoff,
        matrix_json=matrix_json,
        out_dir=out_dir,
        prompts=prompts,
        text_provider=text_provider,
        text_model=text_model,
        text_api_key=text_api_key,
        openai_api_key=openai_api_key,
        openai_image_model=openai_image_model,
    )
    print("STAGE 4/5 POST WRITERS done")

    summary = {
        "run_id": run_id,
        "text_provider": text_provider,
        "text_model": text_model,
        "openai_image_model": openai_image_model,
        "raw_input": str(input_path),
        "ingest_source": ingest.source,
        "ingest_files": len(ingest.files),
        "ingest_warnings": len(ingest.warnings),
        "normalized_raw": str(normalized_path.relative_to(out_dir)).replace("\\", "/"),
        "selected_positioning_mode": agent_1_input.get("selected_positioning_mode")
        or lmo_json.get("selected_positioning_mode"),
        "selected_growth_goal": agent_1_input.get("selected_growth_goal")
        or lmo_json.get("selected_growth_goal")
        or (lmo_json.get("handoff") or {}).get("selected_growth_goal"),
        "confirmed_niche": lmo_json.get("confirmed_niche"),
        "headline": ((lmo_json.get("profile_optimization") or {}).get("headline") or {}).get("fixed"),
        "pillars": [p.get("topic") for p in (lmo_json.get("content_pillars") or []) if isinstance(p, dict)],
        "matrix_posts": (matrix_json.get("week_summary") or {}).get("total_posts"),
        "text_posts_written": writer_stats["text_posts_written"],
        "text_length_warnings": writer_stats.get("text_length_warnings") or [],
        "infographics_written": writer_stats["infographics_written"],
        "infographic_images_generated": writer_stats["infographic_images_generated"],
        "infographic_image_errors": writer_stats["infographic_image_errors"],
        "carousels_written": writer_stats["carousels_written"],
        "carousel_slides_generated": writer_stats["carousel_slides_generated"],
        "carousel_image_errors": writer_stats["carousel_image_errors"],
        "skipped_formats": writer_stats["skipped_formats"],
        "stop_after": stop_after,
        "status": "completed",
    }
    save_json(out_dir / "00_summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"COMPLETED: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
