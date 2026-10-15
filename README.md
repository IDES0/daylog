# daylog

A personal Telegram bot in two halves: a **journal** that turns voice notes
into quantified, place-linked facts, and an **assistant** that researches,
plans and reviews on top of them. No database — plain markdown and YAML in a
private git-backed vault, with Telegram as the interface.

This file documents what's built and how to run it. Why it's built this way,
the rules and the decision log are in [`docs/DESIGN.md`](docs/DESIGN.md);
the short working rules are in [`CLAUDE.md`](CLAUDE.md).

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

### Disciplines
`disciplines.yaml` holds your sports as a graph rather than a flat list.
Each sport names what it is a kind of (`parents`, possibly more than one:
wingfoiling is Foiling and Air) and what has to come first (`requires`: a
goal done, or a sport logged at least once). The roots are Water, Air,
Earth (with Mountain → Snow under it) and Body.
- **Logging**: extraction is given the graph and logs a sport under its id.
  Old labels still count through each node's `aliases`.
- **Vague sports**: "went foiling" is narrowed in code to the kind you log
  most at that spot, then in that region, then by place (`resolve.by_place`,
  e.g. Austin → wakefoil), then a default (tow foil). The reply says which
  and why, with buttons to change it; a reconcile keeps what you said or
  tapped.
- **New sports**: one the graph doesn't know gets an Add card (pursuing,
  want, or no); nothing joins the graph without the tap.
- **State**: you set `active` or `want`. The bot derives *not started*
  (active, nothing logged), *ready* (wanted, prerequisites met) and
  *locked* (wanted, still needs something).
- **Totals**: sessions and hours roll up every ancestor once, computed on
  demand. A kitefoil session counts toward Water and Air; the roots are
  never added together.
- **Where it shows**: `/disciplines` (or 🌀 Disciplines under ⋯ More) shows
  the tree. The brief and the planner get a compact summary, and the
  weekly review gets per-discipline numbers. A goal with a `discipline`
  shows under it.

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

### Web surface (optional)
A small HTTP server (`calendar_server.py`) serves three things, each off
until its variable is set, and 404s everything else:

- **Calendar feed** at `/calendar/<CALENDAR_FEED_SECRET>.ics`. Google and
  Apple Calendar subscribe to it by URL. Built fresh on every request: goal
  and itinerary dates as all-day events, and one event per journal day with
  the day's summary. Read-only: editing an event in the calendar app changes
  nothing in the vault. Refresh is the calendar app's own polling interval.
  It carries journal summaries, so treat the URL like a password.
- **Status site** at `/`, meant for its own domain
  (`status.johnalexborkowski.com`; nothing on the personal site links to
  it). One address, two views:
  - **Guest view**: the current place; a map of where you're headed next,
    styled like the site's travel map (the route from where you are through
    the legs still ahead, in date order); the same legs as a list; active
    goals, milestones, the last 7 days and where you've been. A leg is
    ahead if its window hasn't begun or its deadline hasn't passed: the leg
    you're on is "now", and undated candidates (the wishlist) don't show.
    A leg is placed by its `place_id`, or by an unambiguous name (exactly
    one place with that name, or one country named in it); a country with
    no coordinates is pinned at its middle.
  - **Private view**, when `STATUS_PASSWORD` is set: the guest view ends with
    one password line. The right password sets a signed 30-day cookie and
    the same address then shows the private view; anything else just
    shows the guest view again. The private view adds goal slips, phone
    numbers by day, and a **week calendar** (Monday first, ‹ › for other
    weeks via `/?week=YYYY-MM-DD`): each activity is a block at its rough
    start (`at`, estimated at extraction from what was said and when the
    note was recorded; `~`), or straight after the one before when it has
    none (`≈`, dashed), sized by its hours (1 h when unsaid). Tapping a day
    opens its full summary and the rough times. It also draws the
    **discipline graph** (d3, radial): Water, Air, Earth and Body around the
    centre, every sport on the outer ring under its first parent, a dashed
    curve to any other parent; size is all-time sessions, fill and stroke
    the state (pursuing, not started, ready, locked). Tapping a sport shows
    its numbers, goals and what it still needs; tapping a root zooms into
    it; "As a list" has the same facts as text. After 10 misses in 15 minutes every login
    fails until the window passes. Changing the password signs everyone
    out. Still read-only: Telegram changes things.
  - **Installable**: a manifest, icons and a service worker (it caches
    nothing; it only shows an offline note), so Safari's Share → Add to Home
    Screen or Chrome's Install opens it as an app. Sign in inside the
    installed app, since iOS keeps its cookies apart from Safari's.
- **Health inlet** at `POST /health/<HEALTH_INGEST_SECRET>`: one small JSON
  object a day from an iOS Shortcut (`{"date": "2026-10-02", "steps": 8423}`),
  merged into `health.yaml`. Numbers may carry units ("1.9 mi"); the reply
  names what was saved and what was skipped.

## Architecture

```
src/daylog/
  bot.py           Telegram entrypoint: journaling pipeline, commands, job
                   schedule. Feature handlers live in *_flow.py modules.
  tg.py            Shared Telegram plumbing: the one auth check
                   (TELEGRAM_ALLOWED_USER_ID), vault handle, tz, chunking.
  vault.py         The ONLY module that touches the filesystem or git.
                   Syncs with the remote at most once a minute.
  transcribe.py    Voice (OGG) -> text via faster-whisper, lazy-loaded and
                   dropped after 5 idle minutes to keep RAM low.
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
  ripple.py        A decision traced through itinerary, goals and plan (/ripple).
  proposals.py     research/proposals/*.yaml: changes waiting for a tap;
                   ripple_flow.py shows them after the brief and applies them.
  review.py        Weekly numbers (exact) + the weekly review.
  brief.py         Morning brief (uses routine research, plans, wishlist).
  goals.py         Goal resolution, slip tracking, confirmation logic.
  disciplines.py   The sports graph: roll-ups, prerequisites, states, the
                   vague-sport resolver. discipline_flow.py: fix-it and Add
                   cards, /disciplines.
  itinerary.py     The same hard/soft/slip pattern for travel.
  dateparse.py     Date phrases ("yesterday", "3 days ago") for backdating.
  calendar_feed.py The .ics feed; calendar_server.py serves it, the status
                   page and the health inlet.
  dashboard.py     The read-only status page (one static HTML page).
  health.py        Validates and merges the phone's daily numbers.
  analytics.py     Read-only PostHog queries for the personal site;
                   site_flow.py: the brief's Site block and /site.
  export.py        The journal as CSV tables for analysis; run from a shell
                   (`python -m daylog.export days|meals [N|all]`).
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
  disciplines.yaml         # the sports graph: parents, requires, status, aliases
  rankings.yaml            # ordered lists per category and tier
  research/<place>.md      # destination research files
  research/daily/<date>.md # the morning research routine's output
  plans/<date>.md          # planner output (+ the chosen option)
  reviews/<week>.md        # weekly reviews
  usage.yaml               # API spend per month and kind
  health.yaml              # the phone's daily numbers (steps, distance, ...) by date
  research/proposals/      # proposed itinerary/goal/plan changes, answered by a tap
  site.yaml                # site analytics: one summary per day, written with the brief
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
  discipline: paraglide   # optional: the goal shows under it in /disciplines
  status: active
  slip_history:
    - from: 2026-09-30
      to: 2026-12-31
      on: 2026-08-21
      reason: "prioritising applications"
```

```yaml
# disciplines.yaml — a node counts toward every ancestor
water: {name: Water}
air: {name: Air}
foil:
  name: Foiling
  parents: [water]
  status: active                # active | want
  aliases: [foiling]
  resolve: {by_place: {some-town: wake_foil}, default: tow_foil}
wing_foil: {name: Wingfoil, parents: [foil, air], status: active}
speedwing:
  name: Speedwinging
  parents: [air]
  status: want
  requires: [goal: paragliding] # met when that goal is done
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
| ⋯ More | A second keyboard: status, upcoming, trail, rankings, wishlist, review, undo, usage |
| Any other typed text | Goes to the assistant (vault tools + web search); changes come back as confirm cards |
| `/trail [days\|all]`, `/place <name>` | Where you've been; what's known about a place |
| `/rank <place>`, `/rankings [category]` | Beli-style rankings |
| `/explore`, `/trip`, `/backfill [days]` | Research: spots around you; a trip's stops; unlinked place names |
| `/research <place>`, `/want <place>`, `/wishlist` | Research files and the wishlist |
| `/plan` | 2-3 dated route options to pick from |
| `/review`, `/reconcile [date]` | Weekly review now; rebuild a day from all its notes |
| `/usage` | API spend this month vs budget |
| `/surf`, `/fly` | Rated surf windows per spot / paragliding flyability per launch — no LLM cost |
| `/site [days]` | The personal site's analytics from PostHog (default 7 days): visitors by day, ref, page, source, country and device; chess/spacetime use; company-ref visitors with recording links — no LLM cost |
| `/ripple <decision>` | Trace a decision (a job, a flight home, an injury) through the itinerary, goals and plan now, with the bot's model, instead of waiting for the morning research run, which does the same for any `decisions` in the journal. Either way the result is a proposal: one card after the brief to apply every change and the revised plan, go one by one, or skip |
| `/undo` | Revert one of the bot's recent changes |
| `/status`, `/upcoming` | Goals and itinerary now; the same as a dated timeline |
| `/disciplines [days]` | The sports graph: sessions and hours per discipline (default 30 days), what's not started, ready or locked, and goals under each — no LLM cost |
| `/brief`, `/backdate`, `/start` | The brief now; pick a day to log to; how to use the bot |

Scheduled: morning brief (`BRIEF_HOUR`), reconcile (`DAY_CUTOFF_HOUR`),
weekly review + plan (Sunday `REVIEW_HOUR`), and the cloud research
routines: daily at 05:30 and weekly on Sunday (see
`docs/research-routine-prompt.md` and `docs/research-weekly-prompt.md`).

For analysis, the journal exports as CSV from a shell or a Claude Code
session: `uv run python -m daylog.export days > days.csv` (one row per day:
place, rough nutrition, energy/mood/focus, hours per activity, phone
numbers) and `... meals` (one row per meal).

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
| `ANTHROPIC_API_KEY` | For extraction, chat, research, the brief and reviews |
| `VAULT_PATH` | Path to the local clone of `daylog-vault` |
| `BRIEF_HOUR` | Local hour (0-23) the scheduled brief sends, default `7` |
| `TZ` | IANA timezone, used for the brief schedule and date resolution |
| `DAY_CUTOFF_HOUR` | Local hour the journal day ends and the reconcile runs, default `4` |
| `REVIEW_HOUR` | Local hour of the Sunday weekly review, default `20` |
| `WEEKLY_PLAN` | `0` to skip the plan that follows the weekly review |
| `MONTHLY_BUDGET_USD` | Optional API work (research, planning, chat web search) pauses above this, default `40` |
| `RESEARCH_RUN_BUDGET_USD` | Per research run cap, default `1.50` |
| `CALENDAR_FEED_SECRET` | Optional. Enables the calendar feed at `/calendar/<secret>.ics` |
| `STATUS_SITE` | Optional. `1` serves the status site's guest view at `/` without a password line; `STATUS_PASSWORD` turns the site on by itself |
| `STATUS_PASSWORD` | Optional. Serves the status site at `/` with its password line and private view. Use a long passphrase |
| `HEALTH_INGEST_SECRET` | Optional. Enables `POST /health/<secret>`: a JSON object of the day's numbers from the phone (`{"date": "2026-10-02", "steps": 8423, "sleep_h": 7.2}`), merged into `health.yaml` and joined by date into the export and the status page; unset disables it |
| `POSTHOG_PERSONAL_API_KEY` | Optional. A read-only PostHog personal key (scopes `query:read` and friends). With `POSTHOG_PROJECT_ID`, enables a Site block at the end of the morning brief (last 24 h, including visitors who came through a company `?ref=` tag, i.e. not linkedin/github/test) and `/site [days]` for a deeper breakdown. Nothing polls. Never written to the vault or logs |
| `POSTHOG_PROJECT_ID` | The PostHog project the site reports to |
| `PORT` | For the web server. Railway injects this itself; default `8080` for local testing |

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
- The web server (feed, status site, health inlet) needs a public
  domain generated for this service in the Railway dashboard — Railway
  doesn't expose one by default just because a port is listening. For the
  status site's own domain, add it as a custom domain on the service
  (Settings → Networking) and point a CNAME for `status` at the target
  Railway gives; Railway issues the certificate.

## Status

The journal and the assistant are built and deployed. The decision log and
what was deliberately not built are in [`docs/DESIGN.md`](docs/DESIGN.md).
