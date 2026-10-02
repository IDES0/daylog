# daylog

A personal Telegram bot in two halves: a **journal** that turns voice notes
into quantified, place-linked facts, and an **assistant** that researches,
plans and reviews on top of them. No database, no web frontend — plain
markdown and YAML in a private git-backed vault, Telegram as the only UI.

The original design is [`docs/SPEC.md`](docs/SPEC.md); what's being built
now, and why, is [`docs/ASSISTANT_PLAN.md`](docs/ASSISTANT_PLAN.md); project
rules live in [`CLAUDE.md`](CLAUDE.md). This file
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
- The places tree: each region's open checklist items, and the current
  region's spots with their facts (break, tide, level). The current region
  comes from location.yaml, never a hand-set flag.
- The morning research routine's file for the day, the wishlist's research
  timing, and the latest plan.
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
  bot.py           Telegram entrypoint: journaling pipeline, commands, job
                   schedule. Feature handlers live in *_flow.py modules.
  tg.py            Shared Telegram plumbing: the one auth check
                   (TELEGRAM_ALLOWED_USER_ID), vault handle, tz, chunking.
  vault.py         The ONLY module that touches the filesystem or git.
                   Syncs with the remote at most once a minute.
  transcribe.py    Voice (OGG) -> text via faster-whisper, lazy-loaded.
  extract.py       Transcript -> structured facts (activities, meals, felt,
                   goal progress, itinerary changes, place links, ...) via
                   one forced tool call, grounded in the places tree.
  places.py        The places tree (country -> region -> town -> spot):
                   lookup, disambiguation by region, prompt outlines.
  trail.py         Where the user has been — derived from location.yaml +
                   journal place links, never stored.
  daily.py         The 4am day cutoff, end-of-day reconcile job, weekly
                   review job.
  reconcile.py     Rebuilds a multi-note day in one pass.
  rankings.py      Beli-style tiers + binary insertion; rank_flow.py is
                   the Telegram side.
  llm.py           Pricing, monthly spend ledger (usage.yaml), budget
                   caps, and streamed calls for long requests.
  research.py      Web-search agent: resolve place names, map a region,
                   reconstruct a trip, write research files.
                   research_flow.py: confirm cards, /explore, /trip, ...
  wishlist_flow.py itinerary.yaml as a researched wishlist (/want).
  chat.py          The assistant: vault read tools, web search, proposal
                   tools. chat_flow.py: routing, menu, history.
  planner.py       2-3 dated route options; plan_flow.py applies a pick.
  review.py        Weekly numbers (exact) + the weekly review.
  brief.py         Morning brief (uses routine research, plans, wishlist).
  goals.py, itinerary.py, dateparse.py, calendar_feed.py,
  calendar_server.py       as before.
  sources/surf.py  Surfline-style hourly ratings per spot (`surf:` profiles).
  sources/fly.py   Paragliding flyability per launch (`fly:` profiles + user rules).
  edits.py         Confirmed edits proposed by chat (places, journal, goals, ...).
  sources/{marine,wind}.py   daily fallback numbers for unprofiled regions.
  prompts/*.md     All LLM system prompts — never inlined in Python.
tests/             pytest, temp git repo fixtures — never the real vault.
```

## Vault layout

The vault is a **separate, private** git repo (`daylog-vault`), cloned
locally and referenced via `VAULT_PATH`. The bot commits and pushes to it
after every write. Nothing personal ever goes in this (public) repo — the
schema below uses invented example data.

```
daylog-vault/
  journal/YYYY-MM-DD.md   # frontmatter + raw transcript + summary
  places/<region>.yaml     # the places tree, one file per region
  location.yaml            # where the user was based: stays, trips, transit
  itinerary.yaml           # the wishlist: intentions + hard dates
  goals.yaml               # hard/soft goals, slip_history
  rankings.yaml            # ordered lists per category and tier
  research/<place>.md      # destination research files
  research/daily/<date>.md # the morning research routine's output
  plans/<date>.md          # planner output (+ the chosen option)
  reviews/<week>.md        # weekly reviews
  usage.yaml               # API spend per month and kind
  profile.yaml             # durable preferences (surf comfort, fly rules)
  profile.md               # the user's operating principles — read by chat, planner, review, brief
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
# places/example-island.yaml — a node's region is its parent chain
- id: example-island
  name: Example Island
  kind: region
  parent: somecountry
  checklist:
    - item: Dive the reef pass
      status: todo
- id: example-town
  name: Example Town
  kind: town
  parent: example-island
- id: left-point
  name: Left Point
  kind: surf_spot
  parent: example-town
  aliases: [the point]          # spoken names learned from the journal
  lat: 0.0
  lon: 0.0
  confidence: approximate       # verified | approximate | inferred
  description: Long left over reef.
  facts: {break: left, swell_tide: "SW 4-6ft, mid tide", level: intermediate}
  sources: [{url: "https://example.com", fetched: 2026-01-01}]
  my_notes: [{date: 2026-01-02, text: best at dawn}]
```

## Interacting with the bot

| Input | Effect |
|---|---|
| Voice note | Transcribed and logged to today (before `DAY_CUTOFF_HOUR`, yesterday) |
| 📝 Log | Pick a day (today included); the next voice note or typed message is logged to it |
| 🌊 Surf / 🪂 Fly / ☀️ Brief / 🗺 Plan | Menu buttons that run `/surf`, `/fly`, `/brief`, `/plan` |
| ⋯ More | A second keyboard: status, upcoming, trail, rankings, wishlist, review, undo, usage, export |
| `/export [days\|all]` | The journal as two CSV files (one row per day, one per meal) with rough nutrition estimates, energy/mood/focus and hours per activity |
| Any other typed text | Goes to the assistant (vault tools + web search); changes come back as confirm cards |
| `/trail [days\|all]`, `/place <name>` | Where you've been; what's known about a place |
| `/rank <place>`, `/rankings [category]` | Beli-style rankings |
| `/explore`, `/trip`, `/backfill [days]` | Research: spots around you; a trip's stops; unlinked place names |
| `/research <place>`, `/want <place>`, `/wishlist` | Research files and the wishlist |
| `/plan` | 2-3 dated route options to pick from |
| `/review`, `/reconcile [date]` | Weekly review now; rebuild a day from all its notes |
| `/usage` | API spend this month vs budget |
| `/surf`, `/fly` | Rated surf windows per spot / paragliding flyability per launch — no LLM cost |
| `/undo` | Revert one of the bot's recent changes |
| `/status`, `/upcoming`, `/brief`, `/backdate`, `/start` | As before |

Scheduled: morning brief (`BRIEF_HOUR`), reconcile (`DAY_CUTOFF_HOUR`),
weekly review + plan (Sunday `REVIEW_HOUR`), and the cloud research
routine at 05:30 (see `docs/research-routine-prompt.md`).

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
| `DAY_CUTOFF_HOUR` | Local hour the journal day ends and the reconcile runs, default `4` |
| `REVIEW_HOUR` | Local hour of the Sunday weekly review, default `20` |
| `WEEKLY_PLAN` | `0` to skip the plan that follows the weekly review |
| `MONTHLY_BUDGET_USD` | Optional API work (research, planning, chat web search) pauses above this, default `40` |
| `RESEARCH_RUN_BUDGET_USD` | Per research run cap, default `1.50` |
| `CALENDAR_FEED_SECRET` | Optional. Enables the calendar feed at `/calendar/<secret>.ics` and the read-only status page at `/status/<secret>` (location, next legs, goals, last 7 days); unset disables both |
| `STATUS_PAGE_SLUG` | Optional. Serves the status page at `/status/<slug>` (e.g. `alex`) instead of under the feed secret. Short and typeable, so anyone who guesses it can read the page |
| `HEALTH_INGEST_SECRET` | Optional. Enables `POST /health/<secret>`: a JSON object of the day's numbers from the phone (`{"date": "2026-10-02", "steps": 8423, "sleep_h": 7.2}`), merged into `health.yaml` and joined by date into `/export` and the status page; unset disables it |
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
  guards against a corrupted state from an overlapping redeploy. On every
  boot it also fast-forwards onto the remote before anything else — the
  bot itself only reconciles with the remote reactively (when its own
  push is rejected), so a fix pushed directly to the vault from elsewhere
  would otherwise sit invisible until the container happened to write
  something that conflicted with it.
- Git push to the vault goes over SSH on port 443 (`ssh.github.com`),
  since Railway blocks outbound port 22.
- The calendar feed (if `CALENDAR_FEED_SECRET` is set) needs a public
  domain generated for this service in the Railway dashboard — Railway
  doesn't expose one by default just because a port is listening.

## Current phase status

The journal (steps 1-3) and the assistant (steps 4-9) from
[`docs/ASSISTANT_PLAN.md`](docs/ASSISTANT_PLAN.md) are built. RSS feeds
and the jobs-repo diff from the original spec are not.
