"""
Step 4 — Context-Aware Translation (Gemini API)

For each page:
  1. Load the detection JSON — source_text is now filled by Step 2 (OCR)
  2. Collect every dialogue / narration region that has source_text
  3. Send the whole page as a single batched Gemini request (minimises RPM usage)
  4. Gemini returns translated Hebrew text + any new glossary entries it noticed
  5. Write hebrew_text back into the detection JSON
  6. Merge glossary updates into <job_dir>/glossary.json for the next page

Glossary system
───────────────
glossary.json starts empty and grows as Gemini identifies proper nouns
(character names, place names, titles). It is prepended to every subsequent
page's user message so translations stay consistent across the whole file.

Free-tier limits: 15 RPM · 1 M TPM · 1 500 RPD
Rate limiting is handled by utils/rate_limiter.py (exponential backoff).

Required environment variable:
  GEMINI_API_KEY     — get one free at https://aistudio.google.com/
Optional:
  GEMINI_MODEL       — default: gemini-2.0-flash
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from utils.job_manager import EmitFn
from utils.rate_limiter import call_with_backoff

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini config
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "gemini-2.0-flash"
_TEMPERATURE   = 0.3   # lower = more deterministic, consistent tone

_SYSTEM_INSTRUCTION = """\
You are an expert manga and comic-book translator fluent in both English and \
modern Israeli Hebrew.

Your job is to translate English comic dialogue into natural, colloquial \
Israeli Hebrew that sounds like something a real Israeli person would say — \
NOT formal, textbook, or biblical Hebrew.

Translation rules
─────────────────
1. Translate meaning and emotion, not words literally.
2. Preserve the speaker's personality: a tough soldier sounds tough in Hebrew,
   a scared child sounds scared in Hebrew.
3. Do NOT add nikud (vowel marks / נקודות).
4. Use the supplied character glossary to keep all names consistent.
5. For names NOT in the glossary, transliterate them phonetically into Hebrew
   letters and add them to glossary_updates.
6. Exclamations and short outbursts (e.g. "STOP!", "No!") should feel punchy
   in Hebrew — short, sharp, natural.
7. Do not include any explanation, commentary, or markdown in your response.

Output format
─────────────
Return ONLY valid JSON in exactly this structure:

{
  "translations": [
    {"id": <integer>, "hebrew_text": "<translated string>"}
  ],
  "glossary_updates": {
    "<English name / term>": "<Hebrew equivalent>"
  }
}

glossary_updates must contain every proper noun (character name, place, title,
nickname) you encountered, whether it was already in the glossary or new.\
"""

# ---------------------------------------------------------------------------
# Lazy model singleton (created once, reused for all pages)
# ---------------------------------------------------------------------------

_model = None


async def _get_model():
    """Return cached GenerativeModel, creating it on first call."""
    global _model
    if _model is None:
        loop = asyncio.get_running_loop()
        _model = await loop.run_in_executor(None, _create_model)
    return _model


def _create_model():
    """Synchronous model creation — runs in thread executor."""
    import google.generativeai as genai  # noqa: PLC0415

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set.\n"
            "  1. Get a free key at https://aistudio.google.com/\n"
            "  2. Add it to backend/.env:  GEMINI_API_KEY=your_key_here\n"
            "  3. Restart the server."
        )

    genai.configure(api_key=api_key)

    return genai.GenerativeModel(
        model_name=os.getenv("GEMINI_MODEL", _DEFAULT_MODEL),
        system_instruction=_SYSTEM_INSTRUCTION,
        generation_config=genai.GenerationConfig(
            temperature=_TEMPERATURE,
            # Forces the model to return raw JSON without markdown fences.
            # We still strip fences defensively in _parse_response().
            response_mime_type="application/json",
        ),
    )


# ---------------------------------------------------------------------------
# Glossary helpers
# ---------------------------------------------------------------------------

def _glossary_path(job_dir: Path) -> Path:
    return job_dir / "glossary.json"


def _load_glossary(job_dir: Path) -> dict[str, str]:
    path = _glossary_path(job_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_glossary(job_dir: Path, glossary: dict[str, str]) -> None:
    _glossary_path(job_dir).write_text(
        json.dumps(glossary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Public async entrypoint
# ---------------------------------------------------------------------------

async def translate(job_dir: Path, pages: list[Path], emit: EmitFn) -> list[Path]:
    """
    Translate OCR'd dialogue to Hebrew across all pages.

    Reads detection/NNN.json → fills hebrew_text → writes back.
    Maintains a running glossary.json for cross-page consistency.
    Returns the same pages list unchanged (pipeline chaining convention).
    """
    await emit({"stage": "translate", "status": "running"})

    detection_dir = job_dir / "detection"
    glossary      = _load_glossary(job_dir)
    total         = len(pages)

    for i, page_path in enumerate(pages, start=1):
        json_path = detection_dir / f"{page_path.stem}.json"

        if not json_path.exists():
            await emit({"stage": "translate", "status": "running",
                        "page": i, "total": total})
            continue

        page_data    = json.loads(json_path.read_text(encoding="utf-8"))
        translatable = _get_translatable(page_data["regions"])

        if not translatable:
            # No dialogue on this page — skip the API call entirely
            await emit({"stage": "translate", "status": "running",
                        "page": i, "total": total})
            continue

        # ── Call Gemini (with automatic 429 retry) ───────────────────────────
        try:
            translations, glossary_updates = await _translate_page(
                translatable, glossary
            )
        except Exception as exc:
            # Non-fatal: log, leave hebrew_text as null, continue to next page
            log.error("[translator] Page %s failed: %s", page_path.name, exc)
            await emit({"stage": "translate", "status": "running",
                        "page": i, "total": total})
            continue

        # ── Write hebrew_text back into region objects ────────────────────────
        id_to_hebrew = {t["id"]: t["hebrew_text"] for t in translations}
        for region in page_data["regions"]:
            if region["id"] in id_to_hebrew:
                region["hebrew_text"] = id_to_hebrew[region["id"]]

        json_path.write_text(
            json.dumps(page_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # ── Merge & persist glossary updates ─────────────────────────────────
        if glossary_updates:
            glossary.update(glossary_updates)
            _save_glossary(job_dir, glossary)

        log.info(
            "[translator] Page %d/%d — translated %d region(s), "
            "%d new glossary term(s).",
            i, total, len(translations), len(glossary_updates),
        )
        await emit({"stage": "translate", "status": "running",
                    "page": i, "total": total})

    await emit({"stage": "translate", "status": "done", "total_pages": total})
    return pages


# ---------------------------------------------------------------------------
# Per-page Gemini call
# ---------------------------------------------------------------------------

def _get_translatable(regions: list[dict]) -> list[dict]:
    """Return regions that have OCR text and are not sound effects."""
    return [
        r for r in regions
        if r.get("source_text")           # OCR produced text
        and r.get("type") != "sfx"        # skip sound effects (MVP)
    ]


async def _translate_page(
    regions:  list[dict],
    glossary: dict[str, str],
) -> tuple[list[dict], dict[str, str]]:
    """
    Send one page's regions to Gemini and return (translations, glossary_updates).

    The user message contains:
    • the current glossary (so Gemini uses consistent name translations)
    • a JSON array of {id, source_text, type} objects to translate
    """
    model = await _get_model()

    glossary_block = (
        json.dumps(glossary, ensure_ascii=False, indent=2)
        if glossary else "{}"
    )

    payload = [
        {
            "id":          r["id"],
            "source_text": r["source_text"],
            "type":        r.get("type", "dialogue"),
        }
        for r in regions
    ]

    user_message = (
        f"Character glossary (use these translations exactly):\n"
        f"{glossary_block}\n\n"
        f"Translate these {len(payload)} comic region(s) to Hebrew:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    response = await call_with_backoff(
        lambda: model.generate_content_async(user_message)
    )

    return _parse_response(response.text)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> tuple[list[dict], dict[str, str]]:
    """
    Parse Gemini's JSON response into (translations, glossary_updates).

    Even with response_mime_type="application/json" the model occasionally
    wraps output in markdown fences — we strip them defensively.
    If parsing fails we return empty results so the page is skipped gracefully
    rather than crashing the whole job.
    """
    text = _strip_markdown(raw).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.error(
            "[translator] Could not parse Gemini JSON response.\n"
            "  First 500 chars: %s",
            raw[:500],
        )
        return [], {}

    # ── Validate translations ─────────────────────────────────────────────
    raw_translations = data.get("translations", [])
    translations: list[dict] = []
    for item in raw_translations:
        if not isinstance(item, dict):
            continue
        if "id" not in item or "hebrew_text" not in item:
            continue
        translations.append({
            "id":          int(item["id"]),
            "hebrew_text": str(item["hebrew_text"]).strip(),
        })

    # ── Validate glossary_updates ─────────────────────────────────────────
    raw_glossary = data.get("glossary_updates", {})
    glossary_updates: dict[str, str] = {}
    if isinstance(raw_glossary, dict):
        for k, v in raw_glossary.items():
            if isinstance(k, str) and isinstance(v, str) and k and v:
                glossary_updates[k.strip()] = v.strip()

    return translations, glossary_updates


def _strip_markdown(text: str) -> str:
    """Remove ```json ... ``` fences that some model versions add."""
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$",          "", text.strip())
    return text.strip()
