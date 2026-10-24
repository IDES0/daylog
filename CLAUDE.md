# daylog

Personal app in two halves: a journal that turns voice notes into
quantified, place-linked facts, and an assistant that researches, plans and
reviews on top of them. The web app (an installable PWA) is the only
interface; Telegram was retired on 2026-10-24.

- What exists and how to run it: `README.md`
- Why it's built this way, the rules and the decision log: `docs/DESIGN.md`
  — read it before implementing anything.

## Non-negotiables
- No database. Vault is plain markdown + YAML files.
- The web app is the one interface. The web surface: the calendar feed
  (secret path), the status site (one address on its own domain: a guest
  view, and behind one password line the web app, installable as a PWA),
  and one inlet for the phone's daily health numbers (secret path). The web
  app writes only behind the session + `X-Daylog` CSRF header, through the
  service functions and confirm cards (docs/web-api.md).
- Every `/api/*` handler checks the session first.
- The app never contacts anyone but the user. Notifications (Web Push) go
  only to the user's own devices, one per event. It may draft a message to
  someone else, and send it only after the user taps Confirm on the exact
  text and recipient.
- vault.py is the ONLY module that touches the filesystem or git.
- Hard dates never move without a Confirm; inferred facts are confirmed
  before they're written.

## Stack
Python 3.11+, uv, APScheduler (the scheduled jobs), faster-whisper,
anthropic SDK, ruamel.yaml, httpx, pywebpush. Git via subprocess. Deployed
on Railway; entrypoint `python -m daylog.app`.

## Conventions
- ruff for lint/format, mypy clean, type hints everywhere
- Prompts live in syesrc/daylog/prompts/*.md, never inline in Python
- Tests use a temp git repo fixture, never the real vault
- Build on a branch; merge when ruff, mypy and pytest pass

## Current state
The journal and the assistant described in docs/DESIGN.md are built and
deployed. Anything not described there or in the README needs an explicit
conversation with the user first; record the decision in DESIGN.md's log.
