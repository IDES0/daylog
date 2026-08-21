# daylog

Personal Telegram bot: voice journaling, goal tracking, morning brief.
Full spec in docs/SPEC.md — read it before implementing anything.

## Non-negotiables
- No database. Vault is plain markdown + YAML files.
- No web frontend. Telegram is the interface.
- No scraping. RSS and public APIs only.
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
Phase 1 (capture loop) is built and deployed. Goal tracking (goals.yaml
read/write, slip tracking, hard-deadline confirmation) was added on
2026-08-21 as a deliberate, informed exception to the "use Phase 1 for 10
days first" gate below — not a redefinition of it. The gate still applies
to everything else in Phase 2 (morning brief, feeds, marine/weather) and to
Phase 3: don't start those without the same explicit conversation.
