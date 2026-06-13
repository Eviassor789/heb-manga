"""
Step 4 — Context-Aware Translation (Gemini API)

For each batch of TRANSLATION_PAGES_PER_REQUEST consecutive pages (default 5):
  1. Load each page's detection JSON — source_text is now filled by Step 2 (OCR)
  2. Collect every dialogue / narration region that has source_text
  3. Send all of those pages' regions as a single batched Gemini request
     (minimises RPM usage and gives Gemini more cross-page context)
  4. Gemini returns translated Hebrew text + any new glossary entries it noticed
  5. Write hebrew_text back into each page's detection JSON
  6. Merge glossary updates into <job_dir>/glossary.json for the next batch

Glossary system
───────────────
glossary.json starts empty and grows as Gemini identifies proper nouns
(character names, place names, titles). It is prepended to every subsequent
batch's user message so translations stay consistent across the whole file.

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
from typing import TypedDict

from core.job_manager import EmitFn
from core.rate_limiter import call_with_backoff

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response schema — used for Gemini constrained-decoding (response_schema=)
# ---------------------------------------------------------------------------

class _Translation(TypedDict):
    id: int
    hebrew_text: str


class _GeminiResponseRequired(TypedDict):
    translations: list[_Translation]


class _GeminiResponse(_GeminiResponseRequired, total=False):
    glossary_updates: dict[str, str]

# ---------------------------------------------------------------------------
# Gemini config
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "gemini-2.5-flash"
_TEMPERATURE   = 0.1   # very low = deterministic, minimises hallucination / omission

# How many consecutive pages' regions to send to Gemini in a single request.
# Cuts total API calls roughly N-fold (helps a lot on the free tier's 15 RPM
# limit) and gives Gemini more cross-page context for consistent character
# voice / gender / glossary use. Lower this if a very text-dense chapter ever
# produces truncated/invalid JSON.
_PAGES_PER_REQUEST = max(1, int(os.getenv("TRANSLATION_PAGES_PER_REQUEST", "5")))

# Cap on Gemini's response size. Larger batches → larger JSON responses;
# Gemini 2.5 Flash supports up to 65536 output tokens, so set the cap high
# enough that a full batch can never be truncated mid-JSON.
_MAX_OUTPUT_TOKENS = int(os.getenv("TRANSLATION_MAX_OUTPUT_TOKENS", "65536"))

# How many batches to translate in parallel.
# Free tier  → keep at 1 (15 RPM shared across all batches)
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

4. Use the supplied glossary to keep all names and terms consistent.
   If a term is already in the glossary, use its exact Hebrew value — do NOT
   re-derive it.

5. PROPER NOUN HANDLING — three distinct categories, each treated differently:

   A. PEOPLE'S NAMES → always transliterate phonetically, never translate.
      Do NOT substitute a biblical or traditional Hebrew equivalent.
        "Judas"    → "ג'ודס"    ✗ NOT "יהודה"
        "Jonathan" → "ג'ונתן"   ✗ NOT "יונתן"
        "John"     → "ג'ון"     ✗ NOT "יוחנן"
        "Jesus"    → "ג'יזס"   ✗ NOT "ישוע"
        "Mary"     → "מרי"      ✗ NOT "מרים"
        "Peter"    → "פיטר"     ✗ NOT "פטרוס"
        "Simon"    → "סיימון"   ✗ NOT "שמעון"

   B. NAMED ABILITIES / TECHNIQUES / POWERS / INVENTED COINED TERMS
      → transliterate phonetically (these are fictional words with no real meaning).
        "Nen"        → "נן"
        "Haki"       → "האקי"
        "Bungie Gum" → "באנג'י גאם"
        "Rasengan"   → "ראסנגאן"
        "Bankai"     → "בנקאי"

   C. DESCRIPTIVE PLACE NAMES / LOCATION TITLES / ORGANISATIONS / EXPRESSIONS
      whose words carry a clear English meaning → TRANSLATE semantically into Hebrew.
      Do NOT merely transliterate them.
        "The Golden Land"          → "הארץ המוזהבת"    ✗ NOT "הגולדן לנד"
        "Dark Forest"              → "היער האפל"        ✗ NOT "הדארק פורסט"
        "Kingdom of the Sun"       → "ממלכת השמש"
        "Flame Pillar"             → "עמוד הלהבה"
        "Hunter Association"       → "אגודת הצייד"
        "The Dark Continent"       → "היבשת האפלה"
      Exception: if the place/org is already in the glossary as a transliteration,
      keep the glossary value for consistency.

   Add every name or term you encounter to glossary_updates.

6. Exclamations and short outbursts (e.g. "STOP!", "No!") must feel punchy in
   Hebrew — short, sharp, colloquial.

7. The regions you receive are in reading order from one or more consecutive
   pages of the same manga chapter. Use every region as context for the
   scene, emotion, and who is speaking when you translate each individual
   bubble — earlier regions may establish a character's identity, gender, or
   tone that carries forward into later ones.

8. GENDERED HEBREW — critical for correctness.
   Hebrew grammar is fully gendered. Use the correct gender for every pronoun,
   verb conjugation, and adjective.  Infer each character's gender from context
   within this batch (pronouns, names, how others address them) and apply it
   consistently throughout your translations — including across pages within
   this batch.

9. Do not include any explanation, commentary, or markdown in your response.

Output requirements (strictly enforced)
────────────────────────────────────────
• You MUST return exactly one entry in "translations" for EVERY id in the
  input array — no id may be skipped or omitted from the output.
• "hebrew_text" MUST be a non-empty string — never null, never "".
  If the source is illegible or ambiguous, transliterate it phonetically
  rather than returning an empty value.
• Every proper noun / term you encounter must appear in "glossary_updates".
• When quoting a word or phrase INSIDE Hebrew text, use the Hebrew
  geresh-pair ״ (U+05F4) — NOT ASCII double-quotes — because a bare "
  inside a JSON string value breaks the parser.
  ✓  הוא אמר ״עצור״     ✗  הוא אמר "עצור"

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
# Per-job config  (written by main.py at job creation time)
# ---------------------------------------------------------------------------

def _load_job_config(job_dir: Path) -> dict:
    """Read job_config.json for the user-supplied Gemini API key (if any)."""
    p = job_dir / "job_config.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


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

    Pages are grouped into batches of TRANSLATION_PAGES_PER_REQUEST (default 5)
    consecutive pages, and each batch is sent to Gemini as a single request —
    this cuts the total number of API calls roughly N-fold (helps a lot on the
    free tier's 15 RPM limit) and gives Gemini more cross-page context for
    consistent character voice / gender / glossary use.

    The glossary is shared across concurrent batches using an asyncio.Lock.
    """
    await emit({"stage": "translate", "status": "running"})

    # Read per-job API key (set by the user in the browser, saved by main.py)
    job_config    = _load_job_config(job_dir)
    user_api_key: str | None = job_config.get("gemini_api_key") or None

    detection_dir   = job_dir / "detection"
    glossary        = _load_glossary(job_dir)
    glossary_lock   = asyncio.Lock()
    completed       = 0
    completed_lock  = asyncio.Lock()
    total           = len(pages)
    sem             = asyncio.Semaphore(_CONCURRENCY)

    # Token accounting (accumulated across all batches + retries)
    tokens_lock  = asyncio.Lock()
    tok_input    = 0
    tok_output   = 0
    tok_think    = 0

    async def _bump(n: int = 1) -> None:
        nonlocal completed
        async with completed_lock:
            completed += n
            await emit({"stage": "translate", "status": "running",
                        "page": completed, "total": total})

    # ── Pre-scan: load each page's detection JSON and collect the regions
    #    that actually need translation. Pages with no detection file or no
    #    translatable regions are marked done immediately and never enter a
    #    Gemini call. ─────────────────────────────────────────────────────────
    PageJob = tuple[Path, dict, list[dict]]   # (json_path, page_data, translatable_regions)
    page_jobs: list[PageJob] = []

    for page_path in pages:
        json_path = detection_dir / f"{page_path.stem}.json"

        if not json_path.exists():
            await _bump()
            continue

        page_data    = json.loads(json_path.read_text(encoding="utf-8"))
        translatable = _get_translatable(page_data["regions"])

        if not translatable:
            await _bump()
            continue

        page_jobs.append((json_path, page_data, translatable))

    # ── Group remaining pages into batches of _PAGES_PER_REQUEST ──────────────
    batches: list[list[PageJob]] = [
        page_jobs[i : i + _PAGES_PER_REQUEST]
        for i in range(0, len(page_jobs), _PAGES_PER_REQUEST)
    ]

    async def _process_batch(batch: list[PageJob]) -> None:
        nonlocal glossary, tok_input, tok_output, tok_think

        async with sem:                         # respect concurrency limit
            # Build one combined payload across every page in this batch, with
            # globally-unique ids (per-page region ids can collide). Each
            # region dict is a reference into its page_data["regions"] list,
            # so writing hebrew_text back into it mutates page_data in place.
            payload: list[dict] = []
            id_map:  dict[int, dict] = {}
            gid = 0
            for _json_path, _page_data, translatable in batch:
                for region in translatable:
                    payload.append({
                        "id":          gid,
                        "source_text": region["source_text"],
                        "type":        region.get("type", "dialogue"),
                    })
                    id_map[gid] = region
                    gid += 1

            # Snapshot glossary before the (potentially slow) API call
            async with glossary_lock:
                glossary_snapshot = dict(glossary)

            # Retry the entire batch call on transient errors (network, API
            # hiccup, bad response body). We wait a few seconds between
            # attempts so a brief service blip has time to recover.
            translations: list[dict] = []
            glossary_updates: dict[str, str] = {}
            batch_tokens: dict[str, int] = {"input": 0, "output": 0, "think": 0}
            batch_succeeded = False
            page_names = ", ".join(jp.stem for jp, _, _ in batch)

            for attempt in range(1, _MAX_BATCH_RETRIES + 2):  # +2 → 1 initial + N retries
                try:
                    translations, glossary_updates, batch_tokens = \
                        await _translate_batch(payload, glossary_snapshot,
                                               user_api_key=user_api_key)
                    batch_succeeded = True
                    break
                except Exception as exc:
                    if attempt <= _MAX_BATCH_RETRIES:
                        wait = min(10.0 * (2 ** (attempt - 1)), 120.0)
                        log.warning(
                            "[translator] Batch [%s] failed (attempt %d/%d): %s — retry in %.0f s",
                            page_names, attempt, _MAX_BATCH_RETRIES + 1, exc, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        log.error(
                            "[translator] Batch [%s] failed after %d attempts: %s — skipping.",
                            page_names, _MAX_BATCH_RETRIES + 1, exc,
                        )

            if not batch_succeeded:
                await _bump(len(batch))
                return

            # Accumulate token counts
            async with tokens_lock:
                tok_input  += batch_tokens.get("input",  0)
                tok_output += batch_tokens.get("output", 0)
                tok_think  += batch_tokens.get("think",  0)

            # Write hebrew_text back into the region objects
            for t in translations:
                region = id_map.get(t["id"])
                if region is not None:
                    region["hebrew_text"] = t["hebrew_text"]

            for json_path, page_data, _translatable in batch:
                json_path.write_text(
                    json.dumps(page_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            # Merge glossary updates under the lock so concurrent batches don't race
            if glossary_updates:
                async with glossary_lock:
                    glossary.update(glossary_updates)
                    _save_glossary(job_dir, glossary)

            log.info(
                "[translator] pages [%s] — translated %d region(s), %d new glossary term(s).",
                page_names, len(translations), len(glossary_updates),
            )
            await _bump(len(batch))

    await asyncio.gather(*(_process_batch(b) for b in batches))

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
# Region selection
# ---------------------------------------------------------------------------

def _get_translatable(regions: list[dict]) -> list[dict]:
    """Return regions that have OCR text and are not sound effects."""
    return [
        r for r in regions
        if r.get("source_text")           # OCR produced text
        and r.get("type") != "sfx"        # skip sound effects (MVP)
    ]


_MAX_RETRY_ATTEMPTS  = 5   # extra per-region retry attempts (blank/null hebrew_text)
_MAX_BATCH_RETRIES   = 5   # retries when the ENTIRE batch call fails (network/API error)
                           # waits: 10 s, 20 s, 40 s, 80 s, 120 s (exponential, capped)


# ---------------------------------------------------------------------------
# Batch Gemini call (may span multiple pages)
# ---------------------------------------------------------------------------

async def _translate_batch(
    payload:      list[dict],
    glossary:     dict[str, str],
    user_api_key: str | None = None,
) -> tuple[list[dict], dict[str, str], dict[str, int]]:
    """
    Send a batch of {id, source_text, type} regions — possibly spanning
    multiple consecutive pages — to Gemini in a single request.
    Returns (translations, glossary_updates, token_counts).

    *payload* ids must already be unique within the batch (the caller assigns
    a fresh global id per region so per-page region ids that collide across
    pages don't get conflated).

    The user message contains:
    • the current glossary (consistent name translations)
    • a JSON array of {id, source_text, type} objects to translate

    After the first response, any region whose hebrew_text is missing or empty
    is retried up to _MAX_RETRY_ATTEMPTS times so blank bubbles are minimised.
    """
    from google.genai import types  # noqa: PLC0415
    from google import genai       # noqa: PLC0415

    if user_api_key:
        client = genai.Client(api_key=user_api_key)
    else:
        client = await _get_client()
    model  = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip()

    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        temperature=_TEMPERATURE,
        response_mime_type="application/json",
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        # response_schema: TypedDict-based constrained decoding emits
        # additionalProperties:false which is only valid on Vertex AI /
        # Enterprise, not on the Developer API.  The combination of
        # response_mime_type + the system-prompt no-ASCII-quote rule +
        # _repair_unescaped_quotes() handles the blank-bubble problem
        # without needing tokenizer-level schema enforcement.
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
            f"Glossary (use these translations exactly):\n"
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
    translations, glossary_updates, total_tokens = \
        await _call_gemini(payload, glossary)

    # ── Retry loop for missing / blank translations ───────────────────────────
    id_to_source    = {r["id"]: r for r in payload}
    accumulated     = {t["id"]: t for t in translations}
    merged_glossary = {**glossary, **glossary_updates}

    for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
        # Find IDs that are still missing or have empty text
        missing_ids = [
            rid for rid in id_to_source
            if rid not in accumulated or not (accumulated[rid].get("hebrew_text") or "").strip()
        ]
        if not missing_ids:
            break

        log.warning(
            "[translator] %d region(s) missing after attempt %d — retrying: %s",
            len(missing_ids), attempt, missing_ids,
        )

        retry_batch = [id_to_source[rid] for rid in missing_ids]
        retry_trans, retry_gloss, retry_tokens = \
            await _call_gemini(retry_batch, merged_glossary)

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

# Matches a JSON key→string-value line, e.g.:  "hebrew_text": "some text",
# Group 1 = everything up-to-and-including the opening quote of the value
# Group 2 = the value content (may contain unescaped inner quotes)
# Group 3 = the closing quote plus optional trailing comma / whitespace
_STRING_VALUE_RE = re.compile(r'^(\s*"[^"\\]+"\s*:\s*")(.+)("(?:,\s*)?)$')


def _fix_inner_quotes(s: str) -> str:
    """
    Scan a JSON string *value* (already stripped of its surrounding quotes)
    and replace every unescaped ASCII double-quote with the Hebrew
    geresh-pair ״ (U+05F4), which is visually identical but JSON-safe.

    Escaped sequences (\\", \\\\, etc.) are left untouched.
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            # Escaped sequence — copy both chars verbatim
            out.append(ch)
            i += 1
            out.append(s[i])
        elif ch == '"':
            out.append("״")   # U+05F4 HEBREW PUNCTUATION GERSHAYIM
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _repair_unescaped_quotes(text: str) -> str | None:
    """
    Attempt to salvage a Gemini JSON response that contains unescaped ASCII
    double-quotes inside Hebrew string values (e.g. "הוא אמר "שלום" לה").

    Strategy — line-by-line scan:
      • Find lines that look like  "key": "value[,]
      • If the extracted value portion contains any bare `"`, replace them
        with ״ (U+05F4) — a safe Hebrew punctuation character.
      • Leave every other line untouched.

    Returns the repaired text if at least one substitution was made,
    or None when nothing could be fixed (so the caller can decide whether
    to log an error).
    """
    lines   = text.split("\n")
    repaired: list[str] = []
    changed = False

    for line in lines:
        m = _STRING_VALUE_RE.match(line)
        if m:
            prefix, value, suffix = m.group(1), m.group(2), m.group(3)
            if '"' in value:                         # has unescaped inner quotes
                fixed = _fix_inner_quotes(value)
                if fixed != value:
                    line = prefix + fixed + suffix
                    changed = True
        repaired.append(line)

    return "\n".join(repaired) if changed else None


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
        # First try: repair unescaped inner quotes (e.g. "הוא אמר "שלום"")
        repaired = _repair_unescaped_quotes(text)
        if repaired:
            try:
                data = json.loads(repaired)
                log.info("[translator] JSON repaired — replaced unescaped inner quotes with ״")
            except json.JSONDecodeError:
                log.error(
                    "[translator] Could not parse Gemini JSON response even after repair.\n"
                    "  First 500 chars: %s",
                    raw[:500],
                )
                return [], {}
        else:
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
