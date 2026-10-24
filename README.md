# daylog

A personal app in two halves: a **journal** that turns voice notes into
quantified, place-linked facts, and an **assistant** that researches, plans
and reviews on top of them. No database — plain markdown and YAML in a
private git-backed vault, with an installable web app (PWA) as the one
interface and Web Push notifications for what it does on its own.

This file documents what's built and how to run it. Why it's built this way,
the rules and the decision log are in [`docs/DESIGN.md`](docs/DESIGN.md);
the short working rules are in [`CLAUDE.md`](CLAUDE.md).

## What it does

### Journaling (voice or text)
Record a voice note or type one in the app's Log tab. Voice is transcribed
locally (faster-whisper, no audio ever leaves the machine except as
already-transcribed text to Anthropic), then a single Claude call extracts
structured facts — activities, mood, goal progress, goal slips, itinerary
changes — while the raw transcript is always kept verbatim alongside it.
The result is appended to `journal/YYYY-MM-DD.md` in the vault and
committed.

- **Multiple entries on the same day append**, they don't overwrite —
  each gets its own `### HH:MM` subsection.
- **Backdating**: pick the day in the Log tab before recording or typing
  (`date` on `POST /api/log` / `/api/voice`); a day that hasn't happened
  yet is refused.
- **A mid-message aside about a different day** ("...oh yeah, yesterday I
  also went surfing, forgot to mention it") — `other_day_notes` in
  extraction resolves the relative date and logs just that fact under the
  *other* day, while everything else in the message still logs under
  today as normal. This is for a forgotten fact dropped into an otherwise
  today-focused message, not for backdating the whole message (that's
  still the job of the Log tab's day picker, above) — extraction
  is told explicitly not to use this for a message that's entirely about
  one other day. The other day's entry gets a labeled pointer back to
  today's entry rather than a second copy of the full transcript, which
  stays complete and verbatim under today's date, where the recording
  actually belongs.
- **Corrections**: say a correction out loud ("actually I only surfed 1
  hour, not 2") and extraction is given the day's entry so far to resolve
  it against, by index. Only `activities`/`skipped`/`open_questions` can be
  corrected this way — the raw transcript is never edited or removed, only
  the structured facts, and only after a Confirm/Cancel card (a bad
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
- **Hard** goals (a real deadline) never move silently — the app asks for
  a Confirm/Cancel card before writing a moved date.

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
- **State**: you set `active` or `want`. The app derives *not started*
  (active, nothing logged), *ready* (wanted, prerequisites met) and
  *locked* (wanted, still needs something).
- **Totals**: sessions and hours roll up every ancestor once, computed on
  demand. A kitefoil session counts toward Water and Air; the roots are
  never added together.
- **Where it shows**: the You tab (`GET /api/disciplines`) shows
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

### Status and upcoming
`GET /api/status` is an on-demand plain-text read of current goals and itinerary
state — doesn't touch the LLM, doesn't get misfiled as a journal entry.
`GET /api/upcoming` is the same underlying data reshaped as a timeline: every
active goal/itinerary item that has a resolvable date, soonest first,
with anything past its date and still active marked `(overdue)` instead
of hidden.

### Morning brief (scheduled daily, and on demand)
One Claude call with the `web_search` tool (for anything time-sensitive —
local events, closures, festival dates) combined with everything already
in the vault:
- Goals and itinerary, weighed for judgment (a slip worth mentioning, a
  destination decision), not a rote recap — the status read covers the
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
from Home (`POST /api/brief`). Every brief is saved to `briefs/YYYY-MM-DD.md`,
so `GET /api/brief` still has the latest one after a restart. The scheduled
one also arrives as a notification (see **Notifications** below).

### Web surface (optional)
A small HTTP server (`calendar_server.py`) serves these, each off
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
    out. Once the web app's shell (`webapp/static/index.html`) ships, a
    signed-in `/` serves the web app instead (see **Web app** below); the
    private view is the fallback when the shell is missing.
  - **Installable**: a manifest, icons and a service worker (it caches
    nothing; it only shows an offline note), so Safari's Share → Add to Home
    Screen or Chrome's Install opens it as an app. Sign in inside the
    installed app, since iOS keeps its cookies apart from Safari's. The
    worker leaves form posts alone, and the password line answers with a
    small page that sets the cookie and moves on to `/` rather than a
    redirect: an installed iPhone app dropped the cookie from the redirect,
    so the right password just showed the guest view again.
- **Health inlet** at `POST /health/<HEALTH_INGEST_SECRET>`: one small JSON
  object a day from an iOS Shortcut (`{"date": "2026-10-02", "steps": 8423}`),
  merged into `health.yaml`. Numbers may carry units ("1.9 mi"); the reply
  names what was saved and what was skipped.
- **Web app** (with the status site): a JSON API under `/api/` and the app
  shell at `/` (signed in), `/app.js`, `/app.css`. See below.

### Web app
The status site, signed in, is the app: the only interface (Telegram was
retired on 2026-10-24, see DESIGN.md). It calls the service functions
(`journal_service.py`, `services.py`, the `*_flow.py` modules). The
contract is [`docs/web-api.md`](docs/web-api.md).

- Every `/api/*` request needs the session cookie (401 `signed_out`);
  every non-GET also the header `X-Daylog: 1` and, when sent, an `Origin`
  equal to the host (403). No CORS headers. Bodies: 64 KB JSON, 20 MB
  voice; never logged.
- Anything inferred comes back as a **card** (server memory, 24 h) and is
  applied only when answered: `GET /api/cards`, `POST /api/cards/{id}`.
- Endpoints: `me`, `home`, `map`, `calendar`, `journal`, `journal/{date}`,
  `log`, `voice`, `reconcile`, `chat` (+ `chat/reset`), `brief`, `surf`,
  `fly`, `upcoming`, `status`, `usage`, `site`, `plan`, `ripple`, `trail`,
  `places`, `places/{id}`, `wishlist`, `rankings`, `research`, `review`,
  `disciplines`, `changes` (+ `changes/{sha}/revert`), `cards`.
- The frontend is plain HTML/CSS/JS in `src/daylog/webapp/static/`, shipped
  as package data. The head-to-head ranking flow has no web version yet.

### Notifications
What the app does on its own arrives as a Web Push notification on the
devices that turned it on (You → Session; needs the VAPID keys below), one
per event, never more, and only ever to the user. A tap opens the app where
the result is:

| When | Notification | Where it lands in the app |
|---|---|---|
| `BRIEF_HOUR` | ☀️ Brief · the opening lines | Home: Morning brief (`GET /api/brief`) |
| After the brief, if any wait | "N decisions waiting" | Home: one card per proposal |
| Day cutoff, only if a goal total changed | "Goal totals corrected" | You: Goals |
| Sunday `REVIEW_HOUR` | "Weekly review is ready" (says so when the budget skipped the plan) | You: Weekly review |
| After the review (`WEEKLY_PLAN`) | "A plan for the next weeks" | Trips: Plan, with its option card |
| A note named places the lookup turned into proposals | "Places to confirm" | Home: Add/Skip cards |
| A scheduled job failed | "Brief failed", "Planning failed", ... | Server logs |

## Architecture

```
src/daylog/
  app.py           Entrypoint (`python -m daylog.app`): the web server and
                   the scheduler in one process.
  scheduler.py     The scheduled jobs (brief, reconcile, weekly review +
                   plan) on an APScheduler clock in local time, and the
                   notification each one sends.
  push.py          Web Push to the user's own devices.
  runtime.py       The vault handle and the timezone, shared by all.
  journal_service.py  The journaling pipeline (extract -> location -> goals
                   -> itinerary -> corrections -> journal -> disciplines),
                   client-free; returns what waits for a confirm.
  services.py      Read-side helpers: status, upcoming, trail, place card,
                   conditions, the brief (saved under briefs/).
  webapp/          The web app: api.py (routes), cards.py (confirm cards in
                   memory), chat_store.py, jobs.py (notes logged in the
                   background), static/ (the app shell).
  vault.py         The ONLY module that touches the filesystem or git.
                   Syncs with the remote at most once a minute.
  transcribe.py    Voice (OGG) -> text via faster-whisper, lazy-loaded and
                   dropped after 5 idle minutes to keep RAM low.
  vocab.py         The primer that biases transcription toward the user's
                   words: profile.yaml `vocabulary`, nearby places, sports.
  extract.py       Transcript -> structured facts (activities, meals, felt,
                   goal progress, itinerary changes, place links, ...) via
                   one forced tool call, grounded in the places tree.
  places.py        The places tree (country -> region -> town -> spot):
                   lookup, disambiguation by region, prompt outlines.
  trail.py         Where the user has been — derived from location.yaml +
                   journal place links, never stored.
  daily.py         The 4am day cutoff, end-of-day reconcile, the weekly
                   review.
  reconcile.py     Rebuilds a multi-note day in one pass.
  rankings.py      Beli-style tiers + binary insertion; rank_flow.py: which
                   places in an entry can be ranked, the list as text.
  llm.py           Pricing, monthly spend ledger (usage.yaml), budget
                   caps, and streamed calls for long requests.
  research.py      Web-search agent: resolve place names, map a region,
                   reconstruct a trip, write research files.
                   research_flow.py: linking, proposals, explore, trip, ...
  wishlist_flow.py itinerary.yaml as a researched wishlist.
  chat.py          The assistant: vault read tools, web search, proposal
                   tools. chat_flow.py: applies a confirmed proposal.
  planner.py       2-3 dated route options; plan_flow.py applies a pick.
  ripple.py        A decision traced through itinerary, goals and plan.
  proposals.py     research/proposals/*.yaml: changes waiting for a tap;
                   ripple_flow.py reads, applies and marks them.
  review.py        Weekly numbers (exact) + the weekly review.
  brief.py         Morning brief (uses routine research, plans, wishlist).
  goals.py         Goal resolution, slip tracking, confirmation logic.
  disciplines.py   The sports graph: roll-ups, prerequisites, states, the
                   vague-sport resolver, the fix-it and Add answers.
  itinerary.py     The same hard/soft/slip pattern for travel.
  calendar_feed.py The .ics feed; calendar_server.py serves it, the status
                   page and the health inlet.
  dashboard.py     The read-only status page (one static HTML page).
  health.py        Validates and merges the phone's daily numbers.
  analytics.py     Read-only PostHog queries for the personal site;
                   site_flow.py: the brief's Site block.
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
locally and referenced via `VAULT_PATH`. The app commits and pushes to it
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
  profile.yaml             # durable preferences (surf comfort, fly rules, vocabulary)
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
  discipline: paraglide   # optional: the goal shows under it in the disciplines view
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

## Using the app

| Tab | What's there |
|---|---|
| Home | Today, the morning brief (and "make one now"), surf and fly conditions (rated, no LLM cost), the last 7 days, the week, goals, what's next, milestones, and the cards waiting on you |
| Log | Record or type a note for today (before `DAY_CUTOFF_HOUR`, yesterday) or a picked day; recent days, each with a reconcile button |
| Assistant | The chat (vault tools + web search); changes it proposes come back as confirm cards, research it offers runs only on a tap |
| Trips | The map of next legs, the plan (2-3 dated route options to pick from), "What changed?" (trace a decision through itinerary, goals and plan now, instead of waiting for the morning research run), places, the wishlist (with "research now"), where you've been |
| You | Disciplines, the weekly review (and "write it now"), goals, reports (API spend, site analytics), rankings, milestones, recent changes (undo one), the session and notifications |

Research without a button yet (explore the area, reconstruct the last trip,
look up unlinked place names) is `POST /api/research` with `kind`
`explore` | `trip` | `backfill`, or ask the assistant.

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
cp .env.example .env   # fill in ANTHROPIC_API_KEY, VAULT_PATH, STATUS_PASSWORD
uv run python -m daylog.app   # web server + scheduler; open http://localhost:8080
```

Requires a local clone of the private vault repo at the path set by
`VAULT_PATH` (default `../daylog-vault`), with git remote push access.

### Environment variables

| Var | Purpose |
|---|---|
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
| `STATUS_PASSWORD` | Serves the status site at `/` with its password line, behind which is the web app — the only interface. Use a long passphrase |
| `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` | Web Push keys (base64url): the notifications (see **Notifications**) on devices that turned them on in the app (You → Session); without them the app still works, it just never notifies. `VAPID_SUBJECT` (`mailto:…`) is optional |
| `HEALTH_INGEST_SECRET` | Optional. Enables `POST /health/<secret>`: a JSON object of the day's numbers from the phone (`{"date": "2026-10-02", "steps": 8423, "sleep_h": 7.2}`), merged into `health.yaml` and joined by date into the export and the status page; unset disables it |
| `POSTHOG_PERSONAL_API_KEY` | Optional. A read-only PostHog personal key (scopes `query:read` and friends). With `POSTHOG_PROJECT_ID`, enables a Site block at the end of the morning brief (last 24 h, including visitors who came through a company `?ref=` tag, i.e. not linkedin/github/test) and the Reports card's site breakdown (`GET /api/site`). Nothing polls. Never written to the vault or logs |
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
  app itself only reconciles with the remote reactively (when its own
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
