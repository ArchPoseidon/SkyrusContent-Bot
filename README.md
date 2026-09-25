# SkyrusContent Bot

Telegram bot that turns Meera Pillai's (Skinstinct) raw voice notes and text into LinkedIn post drafts — nothing auto-posts.

## Pipeline

1. **Receive** — text or voice note from the Telegram channel
2. **Transcribe** — voice notes transcribed via Gemini (OGG/Opus inline)
3. **Triage** — scored 0–10 for publishability; below threshold (default 5) is rejected with feedback
4. **News hook** — Gemini Google Search grounding fetches a recent relevant headline (domain allowlist enforced in code)
5. **Draft** — LinkedIn post in Meera's exact voice profile (500–650 words, no bullets/bold/emoji)
6. **Return** — draft sent back to Telegram; Meera reviews and publishes herself

`/regenerate` re-runs steps 4–5 on the last passed note without re-triaging.

## Setup

### 1. Supabase

Run `setup_supabase.sql` in the Supabase SQL editor, then `setup_supabase_grants.sql`.

### 2. Environment

Copy `.env.example` to `.env.bot` and fill in your credentials:

```
TELEGRAM_BOT_TOKEN=
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.8-flash
ALLOWED_CHAT_IDS=        # e.g. -1004480223443
SUPABASE_URL=
SUPABASE_KEY=            # use the service role JWT key
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python3 skinstinct_voice_bot.py > /tmp/bot.log 2>&1 &
```

## Stack

- `python-telegram-bot` v22+ (async)
- `google-genai` v1.47+ (Gemini SDK — NOT the deprecated `google-generativeai`)
- `supabase` v2.x
- Supabase tables: `sessions`, `triage_results`, `news_hooks`, `drafts`
