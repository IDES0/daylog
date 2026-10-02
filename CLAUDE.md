# daylog

Personal Telegram bot in two halves: a journal that turns voice notes into
quantified, place-linked facts, and an assistant that researches, plans and
reviews on top of them.

- What exists and how to run it: `README.md`
- Why it's built this way, the rules and the decision log: `docs/DESIGN.md`
  — read it before implementing anything.

## Non-negotiables
- No database. Vault is plain markdown + YAML files.
- No interactive web frontend. Telegram is the interface. The only web
  surface: the read-only calendar feed (secret path), a one-page read-only
  status view (at a short path the user chose), and one inlet that accepts
  the phone's daily health numbers (secret path).
- The bot never contacts anyone on its own. It may draft a message, and
  send it only after the user taps Confirm on the exact text and recipient.
- vault.py is the ONLY module that touches the filesystem or git.
- Every Telegram handler checks user id against TELEGRAM_ALLOWED_USER_ID first.
- Hard dates never move without a Confirm; inferred facts are confirmed
  before they're written.

## Stack
Python 3.11+, uv, python-telegram-bot v21+ (async), faster-whisper,
anthropic SDK, ruamel.yaml, httpx. Git via subprocess. Deployed on Railway.

## Conventions
- ruff for lint/format, mypy clean, type hints everywhere
- Prompts live in syesrc/daylog/prompts/*.md, never inline in Python
- Tests use a temp git repo fixture, never the real vault
- Build on a branch; merge when ruff, mypy and pytest pass

## Current state
The journal and the assistant described in docs/DESIGN.md are built and
deployed. Anything not described there or in the README needs an explicit
conversation with the user first; record the decision in DESIGN.md's log.
