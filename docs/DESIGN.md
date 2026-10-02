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
- **Telegram is the interface.** No interactive web frontend. The web
  surface is three things: the read-only calendar feed (secret path), a
  one-page read-only status view (a short path the user chose), and one
  inlet for the phone's daily health numbers (secret path).
- **The bot never contacts anyone on its own.** It may draft a message, and
  send it only after the user taps Confirm on the exact text and recipient.
  (Sending isn't built; today it only drafts.) The research routines stay
  read-only for email.
- **Every handler checks the Telegram user id first.** There is no other
  auth layer.
- **Hard dates never move silently.** A hard goal or itinerary date changes
  only after a Confirm in Telegram. Soft dates move freely and record every
  move in `slip_history`; a goal that has slipped four times is the signal.
- **Inferred facts are confirmed before they're written.** Research and
  chat propose; a confirm card applies. Notes on known places are written
  without asking.
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
- **`itinerary.yaml` holds intentions only**: places the user wants to go
  and hard dates, each `candidate | planned | done | dropped`. It doubles as
  the wishlist, with research status per entry.
- **`goals.yaml`**: hard goals (a deadline) and soft goals (a target window),
  optional metric/target/progress, and `slip_history`.
- **`journal/YYYY-MM-DD.md`**: frontmatter (activities, meals, felt, goal
  progress, ...), the verbatim transcript, a summary. A day ends at the 4am
  cutoff; a day told in several notes is rebuilt once at day end.
- **`health.yaml`**: the phone's daily numbers, kept beside the journal as
  reference, joined by date.
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

- **The bot (Railway)**: journaling, chat, brief, reconcile, weekly review,
  planner. Models and prices are in `llm.py`, with a monthly budget and a
  per-run research cap. Long calls stream.
- **Cloud routines (Claude Code, on the user's plan)**: a daily research
  run before the brief and a weekly deep dive. They write under `research/`
  and may enrich existing places; they never create places or touch the
  journal. Prompts: `docs/research-routine-prompt.md`,
  `docs/research-weekly-prompt.md`.
- **Sessions in Claude Code**: one-off work (bulk imports, backfills, deep
  research, analysis of the export tables) is done here rather than through
  the bot's paid API.

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

## Not built

From the original spec: RSS feeds and the new-grad jobs-repo diff in the
brief, static map images. Considered and declined: Strava (API needs a paid
subscription since 2026-06), Surfline (no official API; unofficial access
breaks its terms).

## Working rule for new features

Anything not described here or in the README needs an explicit conversation
with the user first. Build on a branch, keep ruff, mypy and the tests clean,
then merge.
