#!/usr/bin/env python3
"""
Skinstinct LinkedIn Voice Bot v2
Telegram bot that turns Meera Pillai's voice/text notes into LinkedIn post drafts.

Pipeline:
  1. Receive text or voice note from Telegram
  2. Transcribe voice via Gemini (OGG/Opus inline audio)
  3. Triage for publishability (0–10 score, reject below threshold)
  4. Fetch a recent news hook via Gemini Google Search grounding, domain-allowlisted
  5. Generate a draft LinkedIn post in Meera's voice
  6. Return draft to Telegram — nothing is posted to LinkedIn automatically

Uses the google-genai SDK (google.genai), not the deprecated google-generativeai.
"""

import asyncio
import base64
import json
import logging
import os
import tempfile
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(".env.bot")  # bot-specific env; falls back to .env if not found
load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]

ALLOWED_CHAT_IDS: set[int] = {
    int(x.strip())
    for x in os.environ["ALLOWED_CHAT_IDS"].split(",")
    if x.strip()
}

PUBLISHABILITY_THRESHOLD: int = int(os.getenv("PUBLISHABILITY_THRESHOLD", "5"))

_DEFAULT_TRUSTED_DOMAINS = (
    "business-standard.com,economictimes.indiatimes.com,livemint.com,"
    "financialexpress.com,thehindubusinessline.com,moneycontrol.com,"
    "reuters.com,bloomberg.com,forbesindia.com,cnbctv18.com"
)
TRUSTED_DOMAINS: set[str] = {
    d.strip().lower()
    for d in os.getenv("TRUSTED_NEWS_DOMAINS", _DEFAULT_TRUSTED_DOMAINS).split(",")
    if d.strip()
}

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("skinstinct_bot")

# ── Clients ────────────────────────────────────────────────────────────────────

from google import genai  # noqa: E402
from google.genai import types as gtypes  # noqa: E402
from supabase import Client, create_client  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

gemini = genai.Client(api_key=GEMINI_API_KEY)
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Meera's voice profile ──────────────────────────────────────────────────────

VOICE_PROFILE = """
MEERA PILLAI / SKINSTINCT — VOICE PROFILE

STRUCTURAL SKELETON (five-beat arc, used in every piece):
1. Cold open with a specific number or scene — never a general statement.
   Examples: "23% of our product returns," "pH 3.5 to maximise AHA activity."
2. The gap — what the label/industry says vs. what is actually true.
3. Mechanism, broken into an enumerated set — almost always framed as "three things."
4. A hedge, explicitly stated as a hedge — "I want to be precise about what I'm not saying."
   She flags her own reasonableness mid-argument, every single time.
5. A close that hands the reader a question to ask someone else, not a reason to buy.
   "Ask for them. Not from us specifically." "If the answer is anything other than..."

SENTENCE RHYTHM
Long explanatory sentences with subordinate clauses, broken by one-line,
full-stop paragraphs for emphasis. Short lines land right after a technical
passage as a punctuation beat, not a new idea.
  "Niacinamide is not inert."
  "Nobody else in the room asked about it."

FORMATTING
No bullet points, no bold, no emoji, no exclamation marks — anywhere.
Everything is prose, including lists. Paragraphs: 2–5 sentences.
Numbers are always digits, never spelled out (23%, not twenty-three percent).

EVIDENCE STYLE
Every claim anchored to a specific figure — a percentage, a pH value, a timeframe,
a study — used mid-argument as proof, not as a headline stat.
Internal Skinstinct data is used freely. External studies are cited sparingly.

VOCABULARY REGISTER
Precise clinical/formulation-chemistry terms (INCI, CoA, transepidermal water loss,
bioavailable, occlusive-to-humectant ratio) always immediately translated into
plain consequence for the reader. Zero hype words: no "miracle," "revolutionary,"
"breakthrough," "game-changing."

SELF-POSITIONING
Brand mentions minimized and almost always hedged: "We don't currently sell a Vitamin C
product," "I'm not selling a sunscreen." Routinely discloses what Skinstinct hasn't
solved yet. This under-selling is load-bearing — removing it breaks the tone.

LENGTH & FORMAT
LinkedIn posts: 500–650 words. Start cold on the content, end cold on the final
sentence. No "Hi," opener, no "Meera" sign-off.
"""

SYSTEM_PROMPT = f"""You are a ghostwriter for Meera Pillai, founder of Skinstinct — a science-first D2C skincare brand in India.
You write LinkedIn posts in Meera's exact voice.

Her voice profile:
{VOICE_PROFILE}

Non-negotiable rules:
- Output ONLY the post text. No preamble, no "Here is the post:", no commentary.
- 500–650 words. Count carefully. Under 500 or over 650 is a failure.
- No bullet points, no bold, no emoji, no exclamation marks.
- Never invent statistics, studies, percentages, or specific claims not present in the
  input note or the verified news hook.
- Every number in the post must trace directly to the input note or the verified news hook.
- Follow the five-beat structural skeleton.
- Close with a question the reader can ask someone else, not a reason to buy from Skinstinct.
"""

# ── Supabase helpers ───────────────────────────────────────────────────────────
# DB failures are logged but never stop the main pipeline — the draft is the deliverable.


def db_save_session(chat_id: int, input_type: str, raw_input: str, transcribed_text: str) -> Optional[int]:
    try:
        r = supabase_client.table("sessions").insert({
            "chat_id": chat_id,
            "input_type": input_type,
            "raw_input": raw_input,
            "transcribed_text": transcribed_text,
        }).execute()
        return r.data[0]["id"] if r.data else None
    except Exception as exc:
        log.error("db_save_session failed: %s", exc)
        return None


def db_save_triage(session_id: int, score: int, feedback: str, passed: bool) -> None:
    try:
        supabase_client.table("triage_results").insert({
            "session_id": session_id,
            "score": score,
            "feedback": feedback,
            "passed": passed,
        }).execute()
    except Exception as exc:
        log.error("db_save_triage failed: %s", exc)


def db_save_hook(session_id: int, hook_text: str, source_name: str, source_url: str, trusted: bool) -> None:
    try:
        supabase_client.table("news_hooks").insert({
            "session_id": session_id,
            "hook_text": hook_text,
            "source_name": source_name,
            "source_url": source_url,
            "trusted": trusted,
        }).execute()
    except Exception as exc:
        log.error("db_save_hook failed: %s", exc)


def db_save_draft(session_id: int, draft_text: str, version: int) -> None:
    try:
        supabase_client.table("drafts").insert({
            "session_id": session_id,
            "draft_text": draft_text,
            "version": version,
        }).execute()
    except Exception as exc:
        log.error("db_save_draft failed: %s", exc)


def db_get_last_passed_session(chat_id: int) -> Optional[dict]:
    """Return the most recent session that passed triage, or None."""
    try:
        sessions = (
            supabase_client.table("sessions")
            .select("id, transcribed_text")
            .eq("chat_id", chat_id)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        for session in sessions.data:
            triage = (
                supabase_client.table("triage_results")
                .select("passed")
                .eq("session_id", session["id"])
                .eq("passed", True)
                .limit(1)
                .execute()
            )
            if triage.data:
                return session
        return None
    except Exception as exc:
        log.error("db_get_last_passed_session failed: %s", exc)
        return None


def db_next_draft_version(session_id: int) -> int:
    try:
        r = (
            supabase_client.table("drafts")
            .select("version")
            .eq("session_id", session_id)
            .order("version", desc=True)
            .limit(1)
            .execute()
        )
        return (r.data[0]["version"] + 1) if r.data else 1
    except Exception as exc:
        log.error("db_next_draft_version failed: %s", exc)
        return 1

# ── Gemini retry helper ────────────────────────────────────────────────────────

async def _gemini_generate(model: str, contents, config=None, retries: int = 4) -> object:
    """Call Gemini with exponential backoff on 503 (overload) errors."""
    import asyncio as _asyncio
    delay = 3
    for attempt in range(retries):
        try:
            if config is not None:
                return await gemini.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
            return await gemini.aio.models.generate_content(
                model=model, contents=contents
            )
        except Exception as exc:
            if "503" in str(exc) and attempt < retries - 1:
                log.warning("Gemini 503 overload, retrying in %ds (attempt %d/%d)…", delay, attempt + 1, retries)
                await _asyncio.sleep(delay)
                delay *= 2
            else:
                raise

# ── Pipeline step 1: transcribe voice ─────────────────────────────────────────


async def step_transcribe_voice(file_id: str, bot) -> str:
    """Download OGG/Opus voice note from Telegram and transcribe via Gemini."""
    tg_file = await bot.get_file(file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await tg_file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    log.info("Voice note downloaded: %d bytes", len(audio_bytes))

    # Telegram voice notes are OGG containers with Opus codec (mime: audio/ogg).
    # Gemini supports OGG inline; typical voice notes are well under the 20MB limit.
    response = await _gemini_generate(
        model=GEMINI_MODEL,
        contents=[
            gtypes.Part(
                inline_data=gtypes.Blob(mime_type="audio/ogg", data=audio_bytes)
            ),
            "Transcribe this audio verbatim. Return only the transcription text, nothing else.",
        ],
    )
    return response.text.strip()

# ── Pipeline step 2: triage ────────────────────────────────────────────────────


async def step_triage(text: str) -> dict:
    """Score publishability 0–10. Returns {score: int, feedback: str}."""
    prompt = f"""You are evaluating a raw note from Meera Pillai, founder of Skinstinct (a D2C skincare brand), to decide if it has enough substance to become a LinkedIn post.

Score 0–10:
- 8-10: Has a specific claim, data point, number, or vivid scene. A strong angle is clear.
- 5-7: Has some substance; the core idea exists but needs development.
- 0-4: Too vague, generic, or thin. No specific angle, claim, or data point.

Also write 1–3 sentences of honest feedback: what is present and what is thin or missing.

Return ONLY valid JSON in this exact format, with no other text before or after:
{{"score": <integer 0-10>, "feedback": "<string>"}}

The note to evaluate:
{text}"""

    response = await _gemini_generate(model=GEMINI_MODEL, contents=prompt)
    raw = response.text.strip()

    # Strip markdown code fences if Gemini wraps the JSON
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.lower().startswith("json"):
            raw = raw[4:]

    return json.loads(raw.strip())

# ── Pipeline step 3: news hook ─────────────────────────────────────────────────


def _bare_domain(uri: str) -> str:
    try:
        netloc = urlparse(uri).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


async def step_fetch_news_hook(topic_text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Use Gemini's built-in Google Search grounding to find a recent, relevant news hook.
    After the call, every returned source domain is checked against TRUSTED_DOMAINS.
    If no allowlisted domain is found, the hook is discarded entirely.

    Returns (hook_text, source_name, source_url) or (None, None, None).
    """
    prompt = f"""Search for recent news from the last 3 months relevant to this topic from a skincare / D2C beauty brand perspective:

Topic: {topic_text[:600]}

Focus areas: skincare formulation science, D2C beauty industry, Indian cosmetic regulation, beauty market trends, ingredient efficacy research.

Rules for sourcing:
- Only draw on recognized business and trade publications: Economic Times, Business Standard, Bloomberg, Reuters, Mint, Financial Express, Forbes India, CNBC TV18, The Hindu Business Line, Moneycontrol.
- Disregard blogs, forums, brand PR/press-release sites, and unverified sources even if they appear in search results.

If you find a relevant credible story, summarize the key finding in 2–3 sentences, plain text, no markdown.
If no relevant credible story is found, respond with exactly the word: NO_HOOK_FOUND"""

    try:
        response = await _gemini_generate(
            model=GEMINI_MODEL,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                tools=[gtypes.Tool(google_search=gtypes.GoogleSearch())],
            ),
        )
        hook_text = response.text.strip()

        if not hook_text or hook_text == "NO_HOOK_FOUND":
            log.info("No news hook found by Gemini.")
            return None, None, None

        # Enforce the domain allowlist by reading grounding metadata.
        # Prompt-level restriction is advisory; this is the real enforcement.
        trusted_name: Optional[str] = None
        trusted_url: Optional[str] = None

        try:
            for candidate in (response.candidates or []):
                gm = getattr(candidate, "grounding_metadata", None)
                if not gm:
                    continue
                for chunk in (getattr(gm, "grounding_chunks", None) or []):
                    web = getattr(chunk, "web", None)
                    if not web:
                        continue
                    uri = getattr(web, "uri", "") or ""
                    title = getattr(web, "title", "") or ""
                    domain = _bare_domain(uri)
                    if domain and any(
                        domain == td or domain.endswith("." + td)
                        for td in TRUSTED_DOMAINS
                    ):
                        trusted_name = title or domain
                        trusted_url = uri
                        log.info("Trusted news source found: %s", domain)
                        break
                if trusted_name:
                    break
        except Exception as meta_exc:
            log.warning("Could not parse grounding metadata — discarding hook: %s", meta_exc)
            return None, None, None

        if not trusted_name:
            log.info("News hook found but no allowlisted domain in grounding metadata — discarding.")
            return None, None, None

        return hook_text, trusted_name, trusted_url

    except Exception as exc:
        log.error("step_fetch_news_hook error: %s", exc)
        return None, None, None

# ── Pipeline step 4: draft ─────────────────────────────────────────────────────


async def step_generate_draft(
    note_text: str,
    hook_text: Optional[str],
    hook_source: Optional[str],
) -> str:
    """Generate a LinkedIn post draft in Meera's voice."""
    if hook_text and hook_source:
        hook_block = (
            f"\nVERIFIED NEWS HOOK\n"
            f"Source: {hook_source}\n"
            f"Summary: {hook_text}\n\n"
            "You may reference this hook if it strengthens the post. "
            "Any fact from this hook must be used accurately — do not add detail or embellish."
        )
    else:
        hook_block = (
            "\nNo verified recent industry news hook is available. "
            "Do not invent any external reference, study, or statistic."
        )

    prompt = f"""{SYSTEM_PROMPT}

MEERA'S RAW NOTE
(Stay faithful to the claim and any specific data points in it. Do not soften or inflate them.)
{note_text}
{hook_block}

Write the LinkedIn post now:"""

    response = await _gemini_generate(model=GEMINI_MODEL, contents=prompt)
    return response.text.strip()

# ── Telegram handlers ──────────────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main handler for text and voice messages. Runs the full pipeline."""
    chat_id = update.effective_chat.id
    if chat_id not in ALLOWED_CHAT_IDS:
        return

    # effective_message covers both regular messages and channel posts
    msg = update.effective_message
    if not msg:
        return

    # Step 1 — normalize input to plain text
    if msg.voice:
        await msg.reply_text("Got the voice note. Transcribing...")
        try:
            note_text = await step_transcribe_voice(msg.voice.file_id, context.bot)
            input_type = "voice"
            raw_input = f"[voice file_id:{msg.voice.file_id}]"
            log.info("Voice transcribed (%d chars)", len(note_text))
        except Exception as exc:
            log.error("Transcription failed: %s", exc)
            await msg.reply_text(
                "Transcription failed. Please try again, or send it as a text message."
            )
            return
    elif msg.text:
        note_text = msg.text.strip()
        input_type = "text"
        raw_input = note_text
    else:
        return

    if not note_text:
        await msg.reply_text("I couldn't get any text from that. Try again.")
        return

    # Persist session (failure is non-fatal)
    session_id = await asyncio.to_thread(
        db_save_session, chat_id, input_type, raw_input, note_text
    )

    # Step 2 — triage
    await msg.reply_text("Scoring the note...")
    try:
        triage = await step_triage(note_text)
        score = int(triage["score"])
        feedback = str(triage["feedback"])
    except Exception as exc:
        log.error("Triage failed: %s", exc)
        await msg.reply_text("Something went wrong scoring this note. Please try again.")
        return

    passed = score >= PUBLISHABILITY_THRESHOLD
    if session_id:
        await asyncio.to_thread(db_save_triage, session_id, score, feedback, passed)

    if not passed:
        await msg.reply_text(
            f"Score: {score}/10\n\n"
            f"{feedback}\n\n"
            "Send a new message when you have more to work with."
        )
        return

    # Step 3 — news hook
    await msg.reply_text(f"Score: {score}/10. Looking for a recent news hook...")
    hook_text, hook_source, hook_url = await step_fetch_news_hook(note_text)

    if session_id:
        await asyncio.to_thread(
            db_save_hook,
            session_id,
            hook_text or "",
            hook_source or "",
            hook_url or "",
            bool(hook_text),
        )

    # Step 4 — draft
    await msg.reply_text("Drafting the post...")
    try:
        draft = await step_generate_draft(note_text, hook_text, hook_source)
    except Exception as exc:
        log.error("Draft generation failed: %s", exc)
        await msg.reply_text("Draft generation failed. Please try again.")
        return

    if session_id:
        version = await asyncio.to_thread(db_next_draft_version, session_id)
        await asyncio.to_thread(db_save_draft, session_id, draft, version)

    # Step 5 — return draft to Meera; nothing is posted to LinkedIn automatically
    no_hook_note = (
        "\n\n(No verified recent industry story turned up for this topic, so the draft doesn't lean on one.)"
        if not hook_text
        else ""
    )
    await msg.reply_text(f"Here's your draft:{no_hook_note}\n\n{draft}")


async def handle_regenerate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-run steps 3–4 on the last note that passed triage. Does not re-triage."""
    chat_id = update.effective_chat.id
    if chat_id not in ALLOWED_CHAT_IDS:
        return

    msg = update.effective_message
    if not msg:
        return

    session = await asyncio.to_thread(db_get_last_passed_session, chat_id)
    if not session:
        await msg.reply_text(
            "No previous accepted note to regenerate from. Send a new note first."
        )
        return

    session_id = session["id"]
    note_text = session["transcribed_text"]

    await msg.reply_text("Fetching a fresh news hook and regenerating the draft...")

    hook_text, hook_source, hook_url = await step_fetch_news_hook(note_text)
    if hook_text and hook_source:
        await asyncio.to_thread(
            db_save_hook, session_id, hook_text, hook_source, hook_url or "", True
        )

    try:
        draft = await step_generate_draft(note_text, hook_text, hook_source)
    except Exception as exc:
        log.error("Regenerate draft failed: %s", exc)
        await msg.reply_text("Regeneration failed. Please try again.")
        return

    version = await asyncio.to_thread(db_next_draft_version, session_id)
    await asyncio.to_thread(db_save_draft, session_id, draft, version)

    no_hook_note = (
        "\n\n(No verified recent industry story turned up, so the draft doesn't lean on one.)"
        if not hook_text
        else ""
    )
    await msg.reply_text(f"Fresh draft (v{version}):{no_hook_note}\n\n{draft}")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("regenerate", handle_regenerate))
    # Handle regular messages AND channel posts (channel posts use effective_message, not message)
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.UpdateType.CHANNEL_POSTS,
        handle_message,
    ))
    app.add_handler(MessageHandler(filters.VOICE, handle_message))
    log.info(
        "Skinstinct bot starting. Allowed chat IDs: %s. Model: %s. Threshold: %d/10.",
        ALLOWED_CHAT_IDS,
        GEMINI_MODEL,
        PUBLISHABILITY_THRESHOLD,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
