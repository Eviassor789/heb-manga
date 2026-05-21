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
Rate limiting is handled by core/rate_limiter.py (exponential backoff).

Required environment variable:
  GEMINI_API_KEY     — get one free at https://aistudio.google.com/
Optional:
  GEMINI_MODEL       — default: gemini-2.0-flash

SDK: google-genai (new SDK — replaces deprecated google-generativeai)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from core.job_manager import EmitFn
from core.rate_limiter import call_with_backoff

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini config
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "gemini-2.5-flash"
_TEMPERATURE   = 0.1   # very low = deterministic, minimises hallucination / omission

# How many pages to translate in parallel.
# Free tier  → keep at 1 (15 RPM shared across all pages)
# Paid tier  → set TRANSLATION_CONCURRENCY=5 (or higher) in .env for a big speedup
_CONCURRENCY = max(1, int(os.getenv("TRANSLATION_CONCURRENCY", "1")))

# ---------------------------------------------------------------------------
# Gemini 2.5 Flash pricing  (USD per 1 M tokens, as of 2025)
# https://ai.google.dev/pricing
#
# Gemini 2.5 Flash thinking tokens are billed at the OUTPUT rate ($0.30/1M),
# NOT at the Pro/thinking-mode premium ($3.50/1M).  Using $3.50 caused a
# ~40 % overestimate of Gemini cost.
# ---------------------------------------------------------------------------
_PRICE_INPUT_PER_M  = 0.075   # prompt tokens (text + image)
_PRICE_OUTPUT_PER_M = 0.300   # candidate tokens
_PRICE_THINK_PER_M  = 0.300   # thinking tokens — same rate as output for 2.5 Flash
_ILS_PER_USD        = 3.65    # approximate exchange rate shown in summary

_SYSTEM_INSTRUCTION = """\
You are an expert manga and comic-book translator fluent in both English and \
modern Israeli Hebrew.

Your job is to translate English comic dialogue into natural, colloquial \
Israeli Hebrew that sounds like something a real Israeli person would say — \
NOT formal, textbook, or biblical Hebrew.

Translation rules
─────────────────
1. COMPLETENESS IS MANDATORY. Translate every single word of the source text.
   Never shorten, summarise, condense, or omit any part of the dialogue.
   If the original has ten words your translation must convey all ten ideas —
   cutting words is a translation error.

2. Preserve the speaker's personality and tone. A tough soldier sounds tough
   in Hebrew, a scared child sounds scared, a villain sounds menacing.
   Match the register (casual slang vs. formal speech) of the original.

3. Do NOT add nikud (vowel marks / נקודות).

4. Use the supplied character glossary to keep all names consistent.

5. PHONETIC NAME TRANSLITERATION (critical rule, no exceptions).
   For any proper noun — character name, place name, title — that is NOT in
   the glossary, transliterate it by its ENGLISH PRONUNCIATION into Hebrew
   letters. Do NOT substitute a biblical or traditional Hebrew equivalent.

   Required examples (follow this pattern exactly):
     "Judas"   → "ג'ודס"    ✗ NOT "יהודה"
     "John"    → "ג'ון"     ✗ NOT "יוחנן"
     "Jesus"   → "ג'יזס"   ✗ NOT "ישוע"
     "Mary"    → "מרי"      ✗ NOT "מרים"
     "Peter"   → "פיטר"     ✗ NOT "פטרוס"
     "Simon"   → "סיימון"   ✗ NOT "שמעון"
     "Matthew" → "מת'יו"    ✗ NOT "מתתיהו"

   Add every name you encounter to glossary_updates regardless of whether it
   was already in the glossary.

6. Exclamations and short outbursts (e.g. "STOP!", "No!") must feel punchy in
   Hebrew — short, sharp, colloquial.

7. The regions you receive are all from the same manga page. Use every region
   as context for the scene, emotion, and who is speaking when you translate
   each individual bubble.

8. Do not include any explanation, commentary, or markdown in your response.

Output requirements (strictly enforced)
────────────────────────────────────────
• You MUST return exactly one entry in "translations" for EVERY id in the
  input array — no id may be skipped or omitted from the output.
• "hebrew_text" MUST be a non-empty string — never null, never "".
  If the source is illegible or ambiguous, transliterate it phonetically
  rather than returning an empty value.
• Every proper noun you encounter must appear in "glossary_updates".

Output format
─────────────
Return ONLY valid JSON in exactly this structure — no other text:

{
  "translations": [
    {"id": <integer>, "hebrew_text": "<translated string>"}
  ],
  "glossary_updates": {
    "<English name / term>": "<Hebrew equivalent>"
  }
}\
"""

# ---------------------------------------------------------------------------
# Lazy client singleton (created once, reused for all pages)
# ---------------------------------------------------------------------------

_client = None


async def _get_client():
    """Return cached genai.Client, creating it on first call."""
    global _client
    if _client is None:
        loop = asyncio.get_running_loop()
        _client = await loop.run_in_executor(None, _create_client)
    return _client


def _create_client():
    """Synchronous client creation — runs in thread executor."""
    from google import genai  # noqa: PLC0415

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set.\n"
            "  1. Get a free key at https://aistudio.google.com/\n"
            "  2. Add it to backend/.env:  GEMINI_API_KEY=your_key_here\n"
            "  3. Restart the server."
        )

    return genai.Client(api_key=api_key)


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

    Concurrency is controlled by TRANSLATION_CONCURRENCY env var (default 1).
    With a paid Gemini API key, setting it to 5 can cut translation time by ~5×.
    The glossary is shared across concurrent tasks using an asyncio.Lock.
    """
    await emit({"stage": "translate", "status": "running"})

    detection_dir   = job_dir / "detection"
    glossary        = _load_glossary(job_dir)
    glossary_lock   = asyncio.Lock()
    completed       = 0
    completed_lock  = asyncio.Lock()
    total           = len(pages)
    sem             = asyncio.Semaphore(_CONCURRENCY)

    # Token accounting (accumulated across all pages + retries)
    tokens_lock  = asyncio.Lock()
    tok_input    = 0
    tok_output   = 0
    tok_think    = 0

    async def _process_page(page_path: Path) -> None:
        nonlocal completed, glossary, tok_input, tok_output, tok_think

        async with sem:                         # respect concurrency limit
            json_path = detection_dir / f"{page_path.stem}.json"

            if not json_path.exists():
                async with completed_lock:
                    completed += 1
                    await emit({"stage": "translate", "status": "running",
                                "page": completed, "total": total})
                return

            page_data    = json.loads(json_path.read_text(encoding="utf-8"))
            translatable = _get_translatable(page_data["regions"])

            if not translatable:
                async with completed_lock:
                    completed += 1
                    await emit({"stage": "translate", "status": "running",
                                "page": completed, "total": total})
                return

            # Snapshot glossary before the (potentially slow) API call
            async with glossary_lock:
                glossary_snapshot = dict(glossary)

            try:
                translations, glossary_updates, page_tokens = await _translate_page(
                    translatable, glossary_snapshot
                )
            except Exception as exc:
                log.error("[translator] Page %s failed: %s", page_path.name, exc)
                async with completed_lock:
                    completed += 1
                    await emit({"stage": "translate", "status": "running",
                                "page": completed, "total": total})
                return

            # Accumulate token counts
            async with tokens_lock:
                tok_input  += page_tokens.get("input",  0)
                tok_output += page_tokens.get("output", 0)
                tok_think  += page_tokens.get("think",  0)

            # Write hebrew_text back into the region objects
            id_to_hebrew = {t["id"]: t["hebrew_text"] for t in translations}
            for region in page_data["regions"]:
                if region["id"] in id_to_hebrew:
                    region["hebrew_text"] = id_to_hebrew[region["id"]]

            json_path.write_text(
                json.dumps(page_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Merge glossary updates under the lock so concurrent tasks don't race
            if glossary_updates:
                async with glossary_lock:
                    glossary.update(glossary_updates)
                    _save_glossary(job_dir, glossary)

            log.info(
                "[translator] %s — translated %d region(s), %d new glossary term(s).",
                page_path.name, len(translations), len(glossary_updates),
            )
            async with completed_lock:
                completed += 1
                await emit({"stage": "translate", "status": "running",
                            "page": completed, "total": total})

    await asyncio.gather(*(_process_page(p) for p in pages))

    # ── Cost summary ──────────────────────────────────────────────────────────
    cost_usd = (
        tok_input  / 1_000_000 * _PRICE_INPUT_PER_M  +
        tok_output / 1_000_000 * _PRICE_OUTPUT_PER_M +
        tok_think  / 1_000_000 * _PRICE_THINK_PER_M
    )
    cost_ils = cost_usd * _ILS_PER_USD
    cost_info = {
        "usd":    round(cost_usd, 4),
        "ils":    round(cost_ils, 4),
        "tokens": {
            "input":  tok_input,
            "output": tok_output,
            "think":  tok_think,
            "total":  tok_input + tok_output + tok_think,
        },
    }
    log.info(
        "[translator] Cost summary — input=%d out=%d think=%d → $%.4f USD / ₪%.4f ILS",
        tok_input, tok_output, tok_think, cost_usd, cost_ils,
    )

    await emit({
        "stage":       "translate",
        "status":      "done",
        "total_pages": total,
        "cost":        cost_info,
    })
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


_MAX_RETRY_ATTEMPTS = 2  # extra attempts to fill missing / null translations


async def _translate_page(
    regions:  list[dict],
    glossary: dict[str, str],
) -> tuple[list[dict], dict[str, str], dict[str, int]]:
    """
    Send one page's regions to Gemini and return (translations, glossary_updates).

    The user message contains:
    • the current glossary (so Gemini uses consistent name translations)
    • a JSON array of {id, source_text, type} objects to translate

    After the first response, any region whose hebrew_text is missing or empty
    is retried up to _MAX_RETRY_ATTEMPTS times so blank bubbles are minimised.
    """
    from google.genai import types  # noqa: PLC0415

    client = await _get_client()
    model  = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip()

    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        temperature=_TEMPERATURE,
        response_mime_type="application/json",
    )

    def _extract_tokens(response) -> dict[str, int]:
        """Pull token counts out of usage_metadata — gracefully handles missing fields."""
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return {"input": 0, "output": 0, "think": 0}
        return {
            "input":  int(getattr(usage, "prompt_token_count",     0) or 0),
            "output": int(getattr(usage, "candidates_token_count", 0) or 0),
            "think":  int(getattr(usage, "thoughts_token_count",   0) or 0),
        }

    async def _call_gemini(
        batch: list[dict], gloss: dict
    ) -> tuple[list[dict], dict[str, str], dict[str, int]]:
        glossary_block = json.dumps(gloss, ensure_ascii=False, indent=2) if gloss else "{}"
        user_message = (
            f"Character glossary (use these translations exactly):\n"
            f"{glossary_block}\n\n"
            f"Translate these {len(batch)} comic region(s) to Hebrew:\n"
            f"{json.dumps(batch, ensure_ascii=False, indent=2)}"
        )
        response = await call_with_backoff(
            lambda: client.aio.models.generate_content(
                model=model,
                contents=user_message,
                config=config,
            )
        )
        translations, glossary_updates = _parse_response(
            response.text, expected_ids={r["id"] for r in batch}
        )
        return translations, glossary_updates, _extract_tokens(response)

    # ── Initial call ──────────────────────────────────────────────────────────
    payload = [
        {
            "id":          r["id"],
            "source_text": r["source_text"],
            "type":        r.get("type", "dialogue"),
        }
        for r in regions
    ]

    translations, glossary_updates, total_tokens = await _call_gemini(payload, glossary)

    # ── Retry loop for missing / blank translations ───────────────────────────
    id_to_source    = {r["id"]: r for r in payload}
    accumulated     = {t["id"]: t for t in translations}
    merged_glossary = {**glossary, **glossary_updates}

    for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
        # Find IDs that are still missing or have empty text
        missing_ids = [
            rid for rid in id_to_source
            if rid not in accumulated or not accumulated[rid]["hebrew_text"].strip()
        ]
        if not missing_ids:
            break

        log.warning(
            "[translator] %d region(s) missing after attempt %d — retrying: %s",
            len(missing_ids), attempt, missing_ids,
        )

        retry_batch = [id_to_source[rid] for rid in missing_ids]
        retry_trans, retry_gloss, retry_tokens = await _call_gemini(retry_batch, merged_glossary)

        for t in retry_trans:
            accumulated[t["id"]] = t
        glossary_updates.update(retry_gloss)
        merged_glossary.update(retry_gloss)
        # Accumulate retry token usage too
        for k in total_tokens:
            total_tokens[k] += retry_tokens.get(k, 0)

    return list(accumulated.values()), glossary_updates, total_tokens


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(
    raw: str,
    expected_ids: set[int] | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """
    Parse Gemini's JSON response into (translations, glossary_updates).

    Even with response_mime_type="application/json" the model occasionally
    wraps output in markdown fences — we strip them defensively.
    If parsing fails we return empty results so the page is skipped gracefully
    rather than crashing the whole job.

    Entries with null / empty hebrew_text are kept in the output so the caller
    can detect them and schedule a retry — they are NOT silently dropped.
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
    seen_ids: set[int] = set()

    for item in raw_translations:
        if not isinstance(item, dict):
            continue
        if "id" not in item:
            continue

        try:
            rid = int(item["id"])
        except (ValueError, TypeError):
            continue

        if rid in seen_ids:
            continue  # deduplicate
        seen_ids.add(rid)

        raw_text = item.get("hebrew_text")
        # Treat JSON null (→ Python None) and empty strings as blank
        hebrew = str(raw_text).strip() if raw_text is not None else ""

        translations.append({"id": rid, "hebrew_text": hebrew})

    # ── Validate glossary_updates ─────────────────────────────────────────
    raw_glossary = data.get("glossary_updates", {})
    glossary_updates: dict[str, str] = {}
    if isinstance(raw_glossary, dict):
        for k, v in raw_glossary.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                glossary_updates[k.strip()] = v.strip()

    if expected_ids:
        missing = expected_ids - seen_ids
        if missing:
            log.warning("[translator] Response missing IDs: %s", sorted(missing))

    return translations, glossary_updates


def _strip_markdown(text: str) -> str:
    """Remove ```json ... ``` fences that some model versions add."""
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$",          "", text.strip())
    return text.strip()
