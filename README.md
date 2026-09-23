# daylog

A personal Telegram bot for voice/text journaling, goal and travel
tracking, and a daily morning brief. No database, no web frontend — plain
markdown and YAML in a private git-backed vault, Telegram as the only UI.

Full design intent lives in [`docs/SPEC.md`](docs/SPEC.md); project rules
and current phase status live in [`CLAUDE.md`](CLAUDE.md). This file
documents what's actually built and how to run it.

## What it does

### Journaling (voice or text)
Send a voice note or a text message to the bot. Voice is transcribed
locally (faster-whisper, no audio ever leaves the machine except as
already-transcribed text to Anthropic), then a single Claude call extracts
structured facts — activities, mood, goal progress, goal slips, itinerary
changes — while the raw transcript is always kept verbatim alongside it.
The result is appended to `journal/YYYY-MM-DD.md` in the vault and
committed.

- **Multiple entries on the same day append**, they don't overwrite —
  each gets its own `### HH:MM` subsection.
- **Backdating**: send a plain text message that's *only* a date phrase
  ("yesterday", "3 days ago", "August 20", an ISO date) immediately before
  a voice note, and that note logs under that date instead of today. A
  message that merely mentions a date in passing is treated as journal
  content, not a date override. `/backdate` offers the same thing as an
  inline button list (today, yesterday, then the last several days by
  weekday name) instead of typing the phrase.
- **A mid-message aside about a different day** ("...oh yeah, yesterday I
  also went surfing, forgot to mention it") — `other_day_notes` in
  extraction resolves the relative date and logs just that fact under the
  *other* day, while everything else in the message still logs under
  today as normal. This is for a forgotten fact dropped into an otherwise
  today-focused message, not for backdating the whole message (that's
  still the job of a date sent first, or `/backdate`, above) — extraction
  is told explicitly not to use this for a message that's entirely about
  one other day. The other day's entry gets a labeled pointer back to
  today's entry rather than a second copy of the full transcript, which
  stays complete and verbatim under today's date, where the recording
  actually belongs.
- **Corrections**: say a correction out loud ("actually I only surfed 1
  hour, not 2") and extraction is given the day's entry so far to resolve
  it against, by index. Only `activities`/`skipped`/`open_questions` can be
  corrected this way — the raw transcript is never edited or removed, only
  the structured facts, and only after an inline Confirm/Cancel (a bad
  overwrite is worse than a harmless duplicate). If the entry changed
  between the correction being proposed and confirmed, the removal is
  refused rather than risk deleting the wrong thing. Corrections to a goal
  or itinerary date go through the existing slip/confirmation flow instead
  — see Goal tracking / Itinerary below. Mood and the per-day `location`
  field already self-correct: a restated value simply overwrites the old
  one.
- **Location tracking**: `location.yaml` (a separate, date-ranged history —
  see Vault layout below) drives what the morning brief thinks your
  current base is, and it only ever changes when extraction detects an
  explicit statement that you've moved ("I'm in Ubud now"), never from a
  place just mentioned in passing. It's given your current recorded
  location on every extraction call so it can tell an actual move apart
  from a place already correctly recorded. This applies immediately, no
  confirmation — low stakes, and easy to correct by just saying where you
  actually are. Approximate coordinates are recorded only when the model
  is confident in them; without them the location still updates, it just
  won't have a marine/wind forecast until coordinates are known.

### Goal tracking
`goals.yaml` holds hard-deadline and soft-target goals. Voice mentions of
progress or slipped dates are extracted automatically:
- **Soft** goals (a target window, no hard deadline) auto-apply and record
  every slip in `slip_history` — a goal that's slipped four times is the
  signal worth surfacing, not just its current date.
- **Hard** goals (a real deadline) never move silently — the bot asks for
  inline Confirm/Cancel in Telegram before writing a moved date.

### Itinerary / "flexible calendar"
`itinerary.yaml` mirrors the same hard/soft/slip pattern for travel:
candidate destinations, soft target windows, and hard dates/deadlines
(e.g. a visa expiry) that also require confirmation before moving. A
`candidate` entry is a loose, undecided plan — it shows up on the
calendar feed marked `TENTATIVE` rather than looking identical to a
firm, `CONFIRMED` one.

### `/status` and `/upcoming`
`/status` is an on-demand plain-text read of current goals and itinerary
state — doesn't touch the LLM, doesn't get misfiled as a journal entry.
`/upcoming` is the same underlying data reshaped as a timeline: every
active goal/itinerary item that has a resolvable date, soonest first,
with anything past its date and still active marked `(overdue)` instead
of hidden.

### Morning brief (`/brief`, and scheduled daily)
One Claude call with the `web_search` tool (for anything time-sensitive —
local events, closures, festival dates) combined with everything already
in the vault:
- Goals and itinerary, weighed for judgment (a slip worth mentioning, a
  destination decision), not a rote recap — `/status` already covers the
  plain numbers.
- Curated `places.yaml` knowledge: per-destination checklists of real
  must-do items (status: planned/todo/done, not just category tags), and
  for surf/wind-sport spots, named breaks with skill level, ideal swell
  direction, and ideal wind direction.
- Live swell and wind forecasts (Open-Meteo, free, no key) for the current
  location **and** any curated nearby spots, so the brief can say "swell's
  better at X than where you are" instead of only reporting one spot.
- `profile.yaml`: durable personal preferences (skill level, preferred
  wave direction) the brief weighs recommendations against.
- An explicit date+weekday reference table handed to the model — it's
  told to read weekdays off the table rather than compute them, which
  previously produced wrong answers ("the 25th is a Friday" when it
  wasn't).

Scheduled once daily at `BRIEF_HOUR` (local `TZ`), and available on demand
via `/brief`.

### Calendar feed (optional, read-only)
A small HTTP server (`calendar_server.py`) serves an iCalendar (`.ics`)
feed at `/calendar/<CALENDAR_FEED_SECRET>.ics` — Google Calendar and Apple
Calendar can both subscribe to it directly by URL, no OAuth or per-provider
integration needed. It's built fresh from vault data on every request:
- Goal/itinerary deadlines and target windows, as all-day events.
- One all-day event per journal day, titled from that day's distinct
  activity types (plus location, if recorded), with the full daily summary
  as the event description.

It's **one-way and read-only by construction** — editing or deleting an
event in Google/Apple Calendar has no effect on the vault; this is a view
onto daylog's data, not another way to write to it. Refresh timing is
whatever the calendar app's own subscription-polling interval is (commonly
several hours), not instant. The endpoint is opt-in: with
`CALENDAR_FEED_SECRET` unset the server doesn't start at all, and any path
other than the exact configured secret 404s — this is the only HTTP
surface the bot has, so the secret is the only thing standing between your
goals/itinerary/journal history and anyone who finds the URL. Generate a
real one (e.g. `openssl rand -hex 16`), and treat the full feed URL like a
password.

## Architecture

```
src/daylog/
  bot.py          Telegram handlers + entrypoint. Every handler checks the
                   sender against TELEGRAM_ALLOWED_USER_ID first — the
                   only auth layer, so it runs before anything else.
  vault.py         The ONLY module that touches the filesystem or git.
                   Everything else works with plain data in memory.
  transcribe.py    Voice (OGG) -> text via faster-whisper, lazy-loaded.
  extract.py       Transcript -> structured facts via one forced
                   tool-use Claude call (goal_progress, goal_slips,
                   itinerary_changes, corrections, location_change,
                   other_day_notes). Validates the returned shape before
                   trusting it (a schema-obeying tool call can still put
                   the wrong Python type in a field) and is grounded with
                   places.yaml's curated spot names, so a garbled
                   transcription of an obscure real place has something
                   concrete to match against instead of defaulting to a
                   famous but wrong one.
  dateparse.py     Deterministic (non-LLM) parsing for the small date-
                   override vocabulary — a control-flow signal, not
                   fact extraction, so it's exact rather than inferred.
  goals.py         Goal resolution against the live list, slip tracking,
                   hard-deadline confirmation logic. Pure data in/out.
  itinerary.py     Same pattern as goals.py, for travel/destinations.
  brief.py         Morning brief: gathers vault context, one Claude call
                   with web_search, returns plain text (not parsed).
  calendar_feed.py Pure logic: goals/itinerary/journal data -> .ics bytes.
  calendar_server.py  Minimal stdlib HTTP server exposing calendar_feed.py
                   at an unguessable path. The bot's only HTTP surface.
  prompts/*.md     All LLM system prompts — never inlined in Python.
  sources/
    marine.py      Swell/wave forecast, Open-Meteo marine API.
    wind.py        Wind speed/gusts/direction, Open-Meteo forecast API.
tests/             pytest, temp git repo fixtures — never the real vault.
```

## Vault layout

The vault is a **separate, private** git repo (`daylog-vault`), cloned
locally and referenced via `VAULT_PATH`. The bot commits and pushes to it
after every write. Nothing personal ever goes in this (public) repo — the
schema below uses invented example data.

```
daylog-vault/
  journal/YYYY-MM-DD.md   # frontmatter + raw transcript + LLM summary
  goals.yaml               # hard/soft goals, slip_history
  itinerary.yaml           # candidate destinations, hard/soft dates
  places.yaml               # hand-curated destination knowledge
  location.yaml             # date-ranged location history (from: / to:)
  profile.yaml               # durable personal preferences
  briefs/                    # sent morning briefs, for reference
```

```yaml
# goals.yaml
- id: appli-2027
  title: 2027 new-grad applications
  type: hard              # hard | soft — hard deadlines need confirmation to move
  deadline: 2026-10-31
  metric: applications_sent
  target: 150
  progress: 0
  status: active

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

```yaml
# places.yaml
- name: Example Bay, Somewhere
  activities: [surf, diving]
  cost_tier: mid
  notes: free-text summary of the destination
  checklist:                       # actionable must-dos, not category tags
    - item: Dive the reef pass
      status: todo
      notes: best on an incoming tide
  surf_spots:                      # named breaks, for swell comparison + skill matching
    - name: Left Point
      lat: 0.0
      lon: 0.0
      break_type: left
      ideal_swell_direction: SW, 4-6ft
      level: intermediate
  wind_spots:                      # named wind-sport spots, for foiling/kiting
    - name: The Bay (wind foiling)
      lat: 0.0
      lon: 0.0
      notes: check direction before booking, blows out on onshore days
```

## Interacting with the bot

| Input | Effect |
|---|---|
| Voice note | Transcribed, extracted, appended to today's journal entry |
| Text message | Same pipeline as voice, text in instead of transcribed audio |
| Text that's *only* a date phrase, then a voice note | The voice note logs under that date instead of today |
| `/backdate` | Same as above, via an inline button list instead of typing the phrase |
| `/status` | Plain read of current goals + itinerary — no LLM |
| `/upcoming` | Dated goals/itinerary items, soonest first, overdue ones flagged — no LLM |
| `/brief` | Generate and send the morning brief on demand |
| `/start` | Registers the chat, confirms the bot is alive |
| Confirm/Cancel buttons | Appear when extraction detects a hard-deadline move or a correction; nothing hard ever moves, and nothing already-logged is removed, without this |

All commands also appear in Telegram's native `/` command menu.

## Running locally

```
uv sync
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, ANTHROPIC_API_KEY
uv run python -m daylog.bot
```

Requires a local clone of the private vault repo at the path set by
`VAULT_PATH` (default `../daylog-vault`), with git remote push access.

### Environment variables

| Var | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | Numeric Telegram user id — the only user the bot will respond to |
| `ANTHROPIC_API_KEY` | For extraction and the morning brief |
| `VAULT_PATH` | Path to the local clone of `daylog-vault` |
| `BRIEF_HOUR` | Local hour (0-23) the scheduled brief sends, default `7` |
| `TZ` | IANA timezone, used for the brief schedule and date resolution |
| `CALENDAR_FEED_SECRET` | Optional. Enables the calendar feed at `/calendar/<secret>.ics`; unset disables it entirely |
| `PORT` | Only relevant with `CALENDAR_FEED_SECRET` set. Railway injects this itself; default `8080` for local testing |

## Testing

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src/daylog tests
uv run pytest -q
```

Tests never touch the real vault — a temp git repo fixture (`tests/conftest.py`)
stands in for it.

## Deployment

Railway, Dockerfile-based build (`railway.json`):
- The whisper model is pre-downloaded at image build time so the
  container never needs Hugging Face access at runtime.
- A persistent volume holds the local vault clone across restarts.
- `docker-entrypoint.sh` checks the volume's git state is actually healthy
  (not just that `.git` exists) before deciding to reuse vs. re-clone —
  guards against a corrupted state from an overlapping redeploy.
- Git push to the vault goes over SSH on port 443 (`ssh.github.com`),
  since Railway blocks outbound port 22.
- The calendar feed (if `CALENDAR_FEED_SECRET` is set) needs a public
  domain generated for this service in the Railway dashboard — Railway
  doesn't expose one by default just because a port is listening.

## Current phase status

Phase 1 (capture loop) is built and has been in real daily use since
deployment. Goal tracking, itinerary tracking, `/status`, and the morning
brief (with swell/wind/curated-places research) were each added as
deliberate, explicit exceptions to the original "prove Phase 1 for 10 days
first" gate — see `CLAUDE.md` for the up-to-date record of what's an
intentional exception versus still-gated. RSS feeds and the jobs-repo diff
source from the original spec are not built; nothing currently reads
`sources/feeds.py` or `sources/jobs.py` because those files don't exist yet.
