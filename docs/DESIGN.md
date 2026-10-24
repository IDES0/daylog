# daylog — design

Why daylog is built the way it is: the rules, the data model's reasoning and
the decisions made along the way. What exists and how to run it is in
[`README.md`](../README.md); the short working rules are in
[`CLAUDE.md`](../CLAUDE.md). This file replaces the original `SPEC.md`
(2026-08) and `ASSISTANT_PLAN.md` (2026-09); both are in git history.

## What it is

A single-user system in two halves.

1. **Ledger.** Voice notes become quantified, place-linked facts: what was
   done, where (a place id), meals with rough nutrition, how the user felt,
   goal progress. The raw transcript is always kept; everything else can be
   rebuilt from it.
2. **Assistant.** Reads the ledger plus goals and does work on it: researches
   places, keeps a wishlist with research files, plans when to go where,
   writes the morning brief and the weekly review.

The ledger is the foundation; the assistant is only as good as its grounding.

## Rules

- **No database.** The vault is plain markdown and YAML in a private git
  repo. `vault.py` is the only module that touches the filesystem or git.
- **The web app is the one interface.** The web surface: the calendar feed
  (secret path), the status site (one address on its own domain: a guest
  view, and the web app once one password line is answered), and one inlet
  for the phone's daily health numbers (secret path). The web app writes
  only behind the session and a CSRF header, through the service functions
  and confirm cards. The status site installs as a home-screen app. What the
  app does on its own (the brief, the review, a plan, decisions and places
  waiting) is left in the app and announced by one notification per event.
- **The app never contacts anyone but the user.** Notifications go only to
  the user's own devices. It may draft a message to someone else, and send
  it only after the user taps Confirm on the exact text and recipient.
  (Sending isn't built; today it only drafts.) The research routines stay
  read-only for email.
- **Every `/api/*` handler checks the session first.** The app and every
  `/api/*` call need the session cookie the password line sets, and every
  write also the `X-Daylog: 1` header with a same-host Origin; the health
  inlet and the calendar feed have their own secrets.
- **Hard dates never move silently.** A hard goal or itinerary date changes
  only after a Confirm card. Soft dates move freely and record every
  move in `slip_history`; a goal that has slipped four times is the signal.
- **Inferred facts are confirmed before they're written.** Research and
  chat propose; a confirm card applies. Notes on known places are written
  without asking, and so is the kind of a vague sport ("went foiling"),
  marked `guessed` with one tap to change it.
- **One source of truth per fact; everything else is derived.** The trail,
  visits, the "current" place, the export tables and the status page are
  computed on demand, never stored.
- **No scoring or grading of days.** Record facts; surface slippage.
- **Nothing personal in the code repo.** Tests use invented fixtures and a
  temp git repo, never the real vault.
- **Research reads the web properly** (search, fetch, read full pages). The
  original "RSS and public APIs only" rule was lifted on 2026-09-27.

## Data model, and why

- **Places are a tree** (`places/<region>.yaml`: country → region → town →
  spot) with ids, aliases, coordinates and a confidence. Journal activities
  and meals link to a place id. Free-text places caused the worst early
  errors (below).
- **`location.yaml` is the base-camp trail**: date-ranged stays, trips and
  transit. "Where am I" always comes from the open entry, never a flag.
- **`disciplines.yaml` is the sports graph**: each sport names what it is
  a kind of (`parents`, several allowed: wingfoil is Foiling and Air) and
  what must come first (`requires`: a goal done, or a sport logged). Roots
  are Water, Air, Earth, Body; snow sits under Earth → Mountain. Sessions
  roll up every ancestor once and are never stored; the roots are never
  summed. Each sport is `active` or `want`; not started, ready and locked
  are derived. Goals can name a `discipline`.
- **`itinerary.yaml` holds intentions only**: places the user wants to go
  and hard dates, each `candidate | planned | done | dropped`. It doubles as
  the wishlist, with research status per entry.
- **`goals.yaml`**: hard goals (a deadline) and soft goals (a target window),
  optional metric/target/progress, and `slip_history`.
- **`journal/YYYY-MM-DD.md`**: frontmatter (activities, meals, felt, goal
  progress, milestones, decisions, ...), the verbatim transcript, a summary. A day ends at the 4am
  cutoff; a day told in several notes is rebuilt once at day end.
  Activities may carry `at`, a rough start ("HH:MM") the extraction
  estimates for the week calendar; it's a ballpark, never a measurement.
- **`health.yaml`**: the phone's daily numbers, kept beside the journal as
  reference, joined by date.
- **`site.yaml`**: summaries of the personal site's PostHog analytics —
  one entry per day, written with the brief: counts by ref and the
  company-ref visits (ref, pages, what they tried, country). No IPs,
  person ids or recordings; those stay in PostHog.
- **`profile.md` / `profile.yaml`**: the user's principles, risk rules and
  preferences. Read by chat, planner, brief and review.
- **`research/`, `plans/`, `reviews/`**: markdown written by the assistant
  and the cloud research routines.

### What went wrong early, and the design answer

| Failure | Root cause | Answer |
|---|---|---|
| A Bali surf spot placed in Lombok | itinerary entries had no region; a stale `current:` flag | place ids with a parent region; "current" derived from the trail |
| Kuta (Lombok) logged as Kuta (Bali) | ambiguous names resolved by fame | resolver matches known places and the recent trail; asks when unsure |
| A liveaboard and a town missing from the trail | location only changed on "I moved to X" | trips are location entries with stops, inferred by research and marked so |
| No knowledge of a region on arrival | nothing researched new regions | arrival triggers a research draft, confirmed before writing |
| Surf spots filed as itinerary stops | "itinerary" was the only bucket for a place | mentions resolve to places; the itinerary holds intentions only |

## Where the work runs

- **The app (Railway)**: one process (`python -m daylog.app`) runs the web
  server (journaling, chat, research, planner on request) and the scheduler
  (brief, reconcile, weekly review + plan). Models and prices are in
  `llm.py`, with a monthly budget and a per-run research cap. Long calls
  stream.
- **Cloud routines (Claude Code, on the user's plan)**: a daily research
  run before the brief and a weekly deep dive. They write under `research/`
  and may enrich existing places; they never create places or touch the
  journal. Changes to the itinerary, goals or plan they only propose, as
  `research/proposals/*.yaml`; the app shows each as one card (the brief's
  notification says how many wait) and the user's tap applies it. Their prompts live in the routine
  config; `docs/research-routine-prompt.md` and
  `docs/research-weekly-prompt.md` are the copies to edit and paste.
- **PostHog (read-only)**: the brief ends with a Site block for the last
  24 hours, and `GET /api/site` gives the deeper breakdown on demand.
  Read-only personal key; no polling.
- **Sessions in Claude Code**: one-off work (bulk imports, backfills, deep
  research, analysis of the export tables) is done here rather than through
  the app's paid API.

## Decisions

| Date | Decision |
|---|---|
| 2026-08-21 | Phase 1 (capture loop) built; goal tracking added early as a deliberate exception |
| 2026-09-27 | Moved to the ledger + assistant plan; places tree, trail, rankings, research agent, wishlist, planner, chat, weekly review built |
| 2026-09-27 | Research may read full web pages |
| 2026-09-27 | `itinerary.yaml` kept (not renamed to a wishlist file) to avoid churning the calendar feed and hard-date flow |
| 2026-09-27 | Activity types have a fixed vocabulary, after a review split "diving" and "scuba diving" |
| 2026-09-28 | Research follows an explicit focus list in `profile.yaml`; two routines (daily, weekly) |
| 2026-10-02 | Meals carry rough nutrition estimates; mood and focus scored beside energy |
| 2026-10-02 | Read-only status page and a health inlet allowed on the web server |
| 2026-10-02 | "Never messages third parties" narrowed to "never without a Confirm on the exact text and recipient", so confirmed email can be added later |
| 2026-10-02 | The CSV export is a session tool (`python -m daylog.export`), not a bot command |
| 2026-10-10 | Activities are summarised by days done, with hours only where said (hours alone undercounted); journal gains `milestones` (certs, PBs, firsts), listed on the status page; `freedive` and `paraglide` join the activity vocabulary |
| 2026-10-10 | Read-only PostHog analytics for the personal site: a daily Site block in the brief (company refs included) and `/site` on demand; no 15-minute polling, since a few visits a day don't need it; summaries only in `site.yaml` |
| 2026-10-15 | A stated decision (job, flight home, injury) is traced through the itinerary, goals and plan: what it fixes, what it blocks or moves, ways to keep pursuing each goal, what it opens up. The morning research routine does it (on the user's plan, not the API) and writes a proposal file plus an optional revised plan; the bot shows it after the brief as one card. `/ripple` does it on the spot with the API. Prompted by accepting NASCO, which made seven itinerary entries and two goals stale |
| 2026-10-15 | The journal outranks the inbox and the routines' own files: a thing the user says is settled stays settled. The brief is a fixed, skimmable layout under ~200 words; forecasts come from the bot, not the routine |
| 2026-10-15 | The status page becomes a small status site on its own domain (`status.johnalexborkowski.com`, not linked from the personal site): one address (no slug, no `/status/...` or `/me` paths): a nicer guest view with a map of where you're headed (the travel page's style, future instead of past: only legs still ahead, not the wishlist and not the leg you're on), one password line whose wrong answer just shows the guest view again and whose right one shows the private view at the same address, and a PWA manifest so it installs as an app. `STATUS_PAGE_SLUG` and the status page under the feed secret are gone. Still read-only. Direction, not yet decided: the web app may take over from Telegram over time; each step that writes from the web gets its own entry here |
| 2026-10-15 | Events are researched every morning (today and the next 7 days, here, at the next base, and anything worth travelling for) and weekly (the next ~4 weeks); the brief carries the best five. Collecting experiences is one of the user's goals |
| 2026-10-15 | The routines work out their focus each day from the journal (what the user asked to have looked into, what keeps coming up, decisions and open questions), not a fixed list; `profile.yaml` `focus` becomes optional pins. The fixed list had gone three weeks stale |
| 2026-10-15 | Sports become a discipline graph (`disciplines.yaml`), replacing the fixed sport vocabulary: a DAG under Water, Air, Earth (Mountain → Snow) and Body, with two kinds of link kept apart — "kind of" (sessions roll up) and "requires" (unlocks, nothing rolls up), so a speedwing session is never paragliding time. The user sets only `active` or `want`; not started, ready and locked are derived. Goals take a `discipline`; progress stays as extracted, not recomputed. A vague sport is narrowed in code, not by the model: the kind logged most at that spot, then in that region, then by place (Austin → wakefoil), then a default (tow foil), written with `guessed` and a one-tap fix — an exception to confirming inferred facts, like location changes, because it's low stakes and corrected faster than confirmed. A sport the graph lacks joins only after an Add tap. Cliff jumping is Air only and deep water solo is climbing only: the water just catches you |
| 2026-10-15 | The private view gets a week calendar of the journal: activities as blocks at rough times, a tap opens the day's summary (not the transcript). Extraction now estimates a rough start (`at`) per activity from what's said and when the note was recorded; untimed ones follow the one before. Direction, not built: measured times from a smartwatch or GPS (workouts with real start/end, e.g. posted through the health inlet) would replace estimates where they exist |
| 2026-10-15 | The private view draws the whole discipline graph: a radial tree (sports on the outer ring, groupings inside, dashed curves to second parents), sized by all-time sessions, state shown by fill and stroke, a tap for each node's numbers and a list view beside it. Private only for now, since it carries goals and session counts; the guest view could show it later. Read-only, like the rest |
| 2026-10-16 | The web app may now write, behind the session and a CSRF header (`X-Daylog: 1`, same-host Origin, no CORS), through the same service functions and confirm cards as Telegram: the journaling pipeline moved out of `bot.py` into `journal_service.py`, read helpers into `services.py`, and the other flows expose the pure functions both clients call. Telegram stays an equal client. Web confirm cards live in server memory for 24 h; the contract is `docs/web-api.md` |
| 2026-10-17 | Speech-to-text gets a primer of the words the user says (Whisper's `initial_prompt`): `vocabulary` in profile.yaml first, then the current region's places, then the active sports, capped at ~700 characters. Before transcription, not after, because transcripts are kept verbatim — it's the only point a name can be heard right. Extraction also gets the user's list and writes those spellings in the facts. Prompted by NASCO → "Lasco", foiling → "forling", heelside → "hillside". Skate joins the disciplines (skateboarding and surfskating, under Earth and Body) |
| 2026-10-20 | Nothing spends money on its own over a passing mention. After a note, unlinked place names are only matched against known places (free); a web lookup for a new place runs only when asked (/backfill, the assistant), at most 3 searches, once per mention (`place_lookup` stamps it), never for a description ("…next to Savaya"). Prompted by ~$4 of automatic lookups on 2026-10-18 for one 1am shisha bar, run twice at once by two notes about the same day. The brief gets room to write after thinking (32k, effort medium) after two days of briefs lost to max_tokens; the server's TZ follows where the user lives |
| 2026-10-24 | The morning brief also arrives as a lock-screen notification through the installed web app (Web Push, iOS 16.4+): the app subscribes on a tap, the subscription lives in the vault (`push.yaml`), the server signs pushes with its VAPID keys, and only the scheduled brief notifies (a manual /brief doesn't). It's the user's own channel, like Telegram, so "never contacts anyone" holds. Web notes also keep the transcript when logging fails, with Try again |
| 2026-10-24 | Telegram is retired; the web app is the only interface. The user wants one interface, and a Railway env-var breach on 2026-10-21 left the bot token possibly exposed: with no bot, the token is harmless (revoke it with @BotFather anyway). `python -m daylog.app` runs the web server and an APScheduler clock (local `TZ`) for the brief, reconcile and the weekly review + plan. Every message the bot used to send becomes something in the app plus one notification per event: the brief (saved to `briefs/YYYY-MM-DD.md`, so it survives a restart), "N decisions waiting", the weekly review (it says when the budget skipped the plan), the weekly plan (its option card in the card store), new places from a note, a corrected goal total, a failed job. Commands without a screen (/explore, /trip, /backfill) stay on `POST /api/research` and the assistant. The `*_flow.py` modules keep their names and only their pure functions; `bot.py`, `tg.py`, `discipline_flow.py`, `dateparse.py` and python-telegram-bot are gone |
| 2026-10-24 | Web notes survive a deploy: the upload and a small job record are spooled before the 202 in `<vault>/.spool/` (on the volume, in `.git/info/exclude`, never committed; `SPOOL_PATH` overrides), a note a restart cut off is logged on start under its old job id and key, finished jobs keep answering for a day, and the audio is deleted once its job ends. A note that keeps taking the server down is failed after 3 starts |

## Not built

From the original spec: RSS feeds and the new-grad jobs-repo diff in the
brief, static map images. Considered and declined: Strava (API needs a paid
subscription since 2026-06), Surfline (no official API; unofficial access
breaks its terms).

### To do (wanted, not started — roughly in order)

- **Sleep tracking.** Bed and wake times from notes ("hit the sack around
  9"), plus sleep hours from the phone's health inlet when the Shortcut
  sends them; show nights on the week calendar and averages in the numbers.
- **Quantified analytics.** Trends over weeks and months: sessions and hours
  per discipline, sleep, energy/mood/focus, food, spend; correlations worth
  a glance (sleep vs. energy, sessions vs. mood). A tab in the web app.
- **Periods.** Spans without daily entries ("10 days in Lombok, ~3 surfs"):
  coarse counts with sources, shown as bands, never as made-up days; backfill
  from email (flights, bookings, receipts) and photo locations, each as a
  confirm card. Then migrate the "day approximate" stub entries into them.
- **Timezone follows location.** Store each stay's timezone (Open-Meteo
  returns it) and run the brief and the day cutoff by local time, instead of
  a fixed TZ env var changed by hand.
- **Cheaper chat.** A cheaper model for simple questions and/or caching the
  repeated context; a chat question costs ~$0.08 today.
- **Cheaper brief and plan.** Lower effort and fewer searches for the brief;
  the weekly plan on demand or fortnightly.

## Working rule for new features

Anything not described here or in the README needs an explicit conversation
with the user first. Build on a branch, keep ruff, mypy and the tests clean,
then merge.
