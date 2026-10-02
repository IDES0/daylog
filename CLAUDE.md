# daylog

Personal Telegram bot: voice journaling, goal tracking, morning brief.
Full spec in docs/SPEC.md — read it before implementing anything.

## Non-negotiables
- No database. Vault is plain markdown + YAML files.
- No interactive web frontend. Telegram is the interface. The only web
  surface, at secret paths: the read-only calendar feed and one-page status
  view, and one inlet that accepts the phone's daily health numbers
  (changed 2026-10-02).
- Research reads the web properly: search, fetch and read full pages
  (the Stage 1 "RSS and public APIs only" rule was lifted 2026-09-27).
- The bot never messages third parties. It drafts; the user sends.
- vault.py is the ONLY module that touches the filesystem or git.
- Every Telegram handler checks user id against TELEGRAM_ALLOWED_USER_ID first.

## Stack
Python 3.11+, uv, python-telegram-bot v21+ (async), faster-whisper,
anthropic SDK, ruamel.yaml, feedparser, httpx. Git via subprocess.

## Conventions
- ruff for lint/format, mypy clean, type hints everywhere
- Prompts live in src/daylog/prompts/*.md, never inline in Python
- Tests use a temp git repo fixture, never the real vault

## Current phase
Phase 1 (capture loop), goal tracking, itinerary tracking and the morning
brief are built and deployed. On 2026-09-27 the user agreed to move to the
ledger + personal assistant plan in docs/ASSISTANT_PLAN.md — that plan,
not the old SPEC phase list, now defines what to build and in what order.
Build it step by step as the plan lays out; anything outside the plan
still needs an explicit conversation first.
