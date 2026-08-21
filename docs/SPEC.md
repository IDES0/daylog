# daylog — project specification

A personal Telegram bot for voice journaling, goal tracking, and a daily morning brief.
Plain markdown and YAML in a git repo. No database, no frontend.

---

## 1. What this is

A single-user system with two halves:

**Capture (evening).** Send a voice note to a Telegram bot. It transcribes, extracts
structured facts with an LLM, and appends a dated markdown file to a git-backed vault.

**Brief (morning).** A scheduled job pulls feeds, weather/marine forecast, and goal
status, summarises with an LLM, and sends one message. Replying to it asks for a
deeper dive on any item.

Everything else is a later phase.

---

## 2. Non-goals

These are deliberate exclusions. Do not add them without an explicit decision.

- **No database.** The vault is plain files. No SQLite, no Postgres, no ORM.
- **No web frontend.** Telegram is the interface. Obsidian reads the vault on mobile.
- **No scraping.** RSS feeds and public APIs only. If a source has no feed, skip it.
- **No automated outbound messaging to third parties.** The bot may *draft* an inquiry
  and send the draft to the user. It never contacts anyone else.
- **No continuous GPS tracking.** Location comes from Telegram's native location share
  or from what the user says in a voice note.
- **No scoring or grading of days.** Extract facts and surface goal slippage instead.

---

## 3. Repositories

Two separate repos.

| Repo | Visibility | Contents |
|---|---|---|
| `daylog` | public | All code. This spec. No personal data, ever. |
| `daylog-vault` | **private** | journal entries, goals, location, briefs |

The vault is cloned locally and its path passed via `VAULT_PATH`. The bot commits and
pushes to it after every write.

**Nothing personal goes in the public repo.** No sample journal entries with real
content, no goals file with real goals. Use obviously fake fixtures in tests.

---

## 4. Stack

Decisions are made. Do not substitute without asking.

- **Python 3.11+**, dependencies managed with `uv`
- **python-telegram-bot** v21+ (async) — bot handlers
- **faster-whisper** — local transcription, `base` or `small` model
- **anthropic** — official SDK
- **ruamel.yaml** — YAML read/write (preserves key order and comments; the user edits
  `goals.yaml` by hand)
- **feedparser** — RSS
- **httpx** — HTTP
- Git operations via `subprocess` calls to the `git` binary. No GitPython.

**Models:**
- Extraction and follow-ups: `claude-sonnet-5`
- Bulk feed summarisation: `claude-haiku-4-5-20251001`
- Verify current model strings at `docs.claude.com` before hardcoding.

Use the SDK's tool-use / structured output for extraction. Do not parse free-form
text with regex.

**Deployment:** Railway. One always-on service (the Telegram handler) plus one cron
job (the morning brief). Local development runs both with `uv run`.

---

## 5. Vault layout

```
daylog-vault/
  goals.yaml
  places.yaml
  location.yaml
  journal/
    2026-08-21.md
  briefs/
    2026-08-21.md
```

### 5.1 `journal/YYYY-MM-DD.md`

YAML frontmatter for structured fields, prose below.

```markdown
---
date: 2026-08-21
location: Canggu, Bali
activities:
  - type: surf
    hours: 2.0
    detail: Echo Beach, chest high, crowded
  - type: deep_work
    hours: 3.5
    detail: system design study
goal_progress:
  - goal_id: appli-2027
    delta: 4
skipped:
  - gym
mood: good
open_questions:
  - "worth paying for Surfline premium?"
---

## Transcript

[raw whisper output, unedited]

## Summary

[two or three sentences from the LLM]
```

The raw transcript is always preserved. Extraction is lossy and the model will
improve; the source of truth must not be.

### 5.2 `goals.yaml`

```yaml
- id: appli-2027
  title: 2027 new-grad applications
  type: hard              # hard | soft
  deadline: 2026-10-31
  metric: applications_sent
  target: 150
  progress: 0
  status: active          # active | done | dropped

- id: appi-solo
  title: APPI solo pilot rating
  type: soft
  target_window: [2026-10-01, 2026-12-31]
  status: active
  slip_history:
    - from: 2026-09-30
      to: 2026-12-31
      on: 2026-08-21
      reason: "prioritising applications"
```

**`slip_history` is the most important field in the project.** When the user pushes a
soft deadline, the system appends to it rather than overwriting the date. A goal that
has slipped four times is the signal worth surfacing; a single current date is not.

Hard deadlines never move silently. If extraction detects a request to move a `hard`
goal, the bot asks for confirmation in Telegram before writing.

### 5.3 `location.yaml`

Date ranges, not points.

```yaml
- place: Canggu, Bali, ID
  lat: -8.6478
  lon: 115.1385
  from: 2026-08-01
  to: null      # null means current
```

### 5.4 `places.yaml`

Hand-curated destination knowledge. Seeded manually, extended by voice.

```yaml
- name: Sopot, Bulgaria
  activities: [paragliding]
  season: [2026-04-01, 2026-09-30]
  cost_tier: low
  visa_us_passport: "90/180 Schengen-adjacent, check current"
  notes: "chairlift to 1400m launch, high flyable-day rate"
```

---

## 6. Components

```
daylog/
  CLAUDE.md
  pyproject.toml
  .env.example
  src/daylog/
    bot.py          # telegram handlers, entrypoint
    transcribe.py   # audio -> text
    extract.py      # text -> structured dict (LLM, tool use)
    vault.py        # all file read/write + git commit/push
    goals.py        # goal resolution, slip tracking, confirmation logic
    brief.py        # morning job: sources -> summary -> send
    sources/
      feeds.py      # RSS via feedparser
      marine.py     # Open-Meteo marine + weather (no API key)
      jobs.py       # diff new-grad GitHub repo JSON
  tests/
```

**`vault.py` is the only module that touches the filesystem or git.** Everything else
returns data. This keeps the pipeline testable without a real vault.

---

## 7. Environment

`.env.example`, committed. Real `.env` gitignored.

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_ID=      # numeric Telegram user id — REQUIRED
ANTHROPIC_API_KEY=
VAULT_PATH=../daylog-vault
BRIEF_HOUR=7
TZ=Asia/Makassar
```

**Security, non-optional:** Telegram bots are publicly discoverable by username. The
first thing every handler does is check `update.effective_user.id` against
`TELEGRAM_ALLOWED_USER_ID` and silently drop anything else. Write this before any
other handler logic. There is no auth layer beyond this check, so it has to be right.

---

## 8. Phases

Build in order. Do not start a phase before the previous one has been used for real.

### Phase 1 — the capture loop

The only phase that matters initially. Everything else is optional on top.

1. Telegram bot, allowlist check, responds to `/start`
2. Voice note handler: download OGG → faster-whisper → text
3. `extract.py`: one Claude call with structured output returning the journal schema
4. `vault.py`: write `journal/YYYY-MM-DD.md`, `git commit`, `git push`
5. Bot replies with the extracted summary for confirmation

**Done when:** a voice note sent from the phone results in a committed markdown file
in the private repo, and the user has done this for ten consecutive days without the
bot breaking.

If the user does not use it for ten days, stop the project. The problem is the habit,
not the code.

### Phase 2 — goals and morning brief

1. `goals.yaml` read/write, goal resolution by fuzzy title match in extraction
2. Slip tracking; confirmation prompt before moving any `hard` deadline
3. `sources/feeds.py` (RSS), `sources/marine.py` (Open-Meteo swell for current
   location from `location.yaml`), `sources/jobs.py` (diff repo JSON)
4. `brief.py`: gather → Haiku summary → send one Telegram message
5. Inline keyboard buttons for "dive deeper on X" → Sonnet follow-up in thread

**Done when:** a useful brief arrives every morning and goal progress updates from
voice without manual YAML editing.

### Phase 3 — later, only if 1 and 2 stuck

- Weekly rollup: concatenate the week's journal files into one Sonnet call, send a
  review comparing stated intentions against what happened
- `places.yaml` reasoning: "where should I go in November" answered from the curated
  file plus goals plus season windows
- Static map images via Mapbox Static Images API, sent as a Telegram photo
- Draft-only inquiry generation: bot writes a message for a hostel or school and sends
  it to the user to copy. It does not send it anywhere itself.

---

## 9. Conventions

- Type hints everywhere; `mypy` clean
- `ruff` for lint and format
- All LLM prompts live in `src/daylog/prompts/` as `.md` files, loaded at runtime —
  not inlined in Python string literals
- Log every LLM call's token usage to stdout
- Every write to the vault is followed by a commit with a message like
  `journal: 2026-08-21` or `goals: slip appi-solo`
- Handle the offline case: if `git push` fails, commit locally and retry on next write.
  Indonesian wifi is unreliable and the bot must not lose data because of it.

---

## 10. First session prompt for Claude Code

> Read CLAUDE.md and docs/SPEC.md. Implement Phase 1 only. Start with project
> scaffolding (`pyproject.toml` via `uv`, ruff, mypy config), then `vault.py` with
> tests against a temporary git repo fixture, then `transcribe.py`, then `extract.py`,
> then `bot.py`. Stop after Phase 1 and do not begin Phase 2.

Keep Claude Code scoped to one component per session. The failure mode on a project
like this is a single sprawling session that produces a lot of untested code.
