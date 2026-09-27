# daylog → journal + personal assistant: plan

Status: draft for discussion, 2026-09-27. Supersedes the SPEC's phase gate for
the parts listed here once agreed (update CLAUDE.md "Current phase" then).

## The two halves

1. **Ledger** — turns voice entries into quantified, place-linked facts. Every
   activity knows *where* it happened (a place id), *what kind* it was, and
   *how sure* we are. Nothing downstream should ever have to guess geography
   from free text again.
2. **Assistant** — reads the ledger plus goals and does work on it: researches
   places, keeps a wishlist of future destinations with real dossiers, and
   plans *when* to go where given goals, events, seasons, deadlines, and how
   hard each place is to reach from where the user is now.

The ledger is the foundation; the assistant is only as good as its grounding.
The Sept 16 "Uluwatu" error and the Sept 27 "Airport Rights is in Lombok"
error are the same bug: free-text places with no resolved identity.

## What went wrong (so the design prevents it)

| Failure | Root cause | Design answer |
|---|---|---|
| Airport Rights placed in Lombok | itinerary entries had no region; stale `current:` flag on Lombok | places have ids + parent region; "current" always derived from location trail (fixed 2026-09-27) |
| Kuta Lombok logged as Kuta Bali; Gerupuk "Bali" | extraction resolved ambiguous names by fame, not route | resolver matches against known places + recent trail, asks when ambiguous |
| Liveaboard, Labuan Bajo missing from trail | `location_change` only fires on an explicit "I moved to X" | trip mode: a multi-day trip is a location entry with stops; stops inferred by research, marked `confidence: inferred` |
| No knowledge of Lakey spots on arrival | nothing researches a new region | on arrival in a new region, research agent drafts places + spots for confirmation |
| Surf spots filed as itinerary stops | extraction had only "itinerary" as a bucket for place mentions | place mentions resolve to `places`; itinerary only holds *intentions* |

## Data model

One source of truth per fact; everything else derived.

### `places/<region-id>.yaml` (replaces `places.yaml`)

A tree: country → region/island → town → spot. One file per region keeps
files small and diffs readable.

```yaml
- id: sumbawa
  kind: region
  name: Sumbawa
  country: ID
  description: >        # researched, short
    ...
  sources: [{url: ..., fetched: 2026-09-27}]
  facts:                # typed, kind-specific
    cost_tier: low
    seasons: {surf: [05, 10]}
  my_notes:             # from journal, dated, linked
    - {date: 2026-09-24, text: "Scooter 80k/day, lessons 400-500k", ref: journal/2026-09-24}

- id: lakey-peak
  parent: lakey        # town → sumbawa
  kind: surf_spot
  name: Lakey Peak
  aliases: ["Lakey Point", "Lake Key Point"]   # transcription grounding
  lat: -8.807
  lon: 118.378
  confidence: approximate   # verified | approximate | inferred
  facts: {break: A-frame reef, tide: mid, swell: "S/SW 3-8ft", level: intermediate/advanced}
  description: ...
  sources: [...]
  my_notes: [...]
```

Kinds: `region, town, surf_spot, wind_spot, dive_site, hike, viewpoint,
beach, food, stay, event_venue, transit`. Food/stays included so "that ramen
place in Kuta" resolves too; the brief can filter by kind.

**No `visited` field.** Visits are derived: every journal activity carries a
`place` id, and location.yaml carries base stays. `vault.trail()` computes
"where have I been, when, doing what" from those. A stored flag would drift
the way `current: true` did.

### Journal frontmatter

`activities[].place: <id>` (and `place_confidence` when inferred). The
top-level `location` stays for human reading but becomes a place id too.

### `location.yaml`

Unchanged shape plus `place: <id>`, `mode: stay | trip | transit`, and for
trips a `stops: [{date, place, confidence}]` list. This is the base-camp
trail; activity-level detail lives in journals.

### `wishlist.yaml` (replaces `itinerary.yaml`)

Intentions only — places the user wants to go, never places they are.

```yaml
- place: mentawai
  why: [surf]                  # goal ids
  status: candidate | planned | booked | done | dropped
  window: {earliest: ..., latest: ..., hard: false}
  constraints: ["before internship 2026-10-19?"]
  dossier: research/mentawai.md
```

### `research/<place-id>.md`

Dossier per wishlist place, regenerated on a schedule: seasons and swell
windows, events in the next ~90 days, how to get there *from the current
location* (route, hours, cost), rough daily cost, visa notes, and dated
sources. Markdown because it's read by both the user and the planner model.

## Components

### Resolver (in extraction)
Given a transcript and the known places (names + aliases), return a place id
for every place mention, or `unresolved: "<text>"` with a guess of the
region. Ambiguous names are resolved against the last few trail entries and
the route described, never by fame.

### Research agent (new, `research.py`)
Claude + `web_search`, prompt in `prompts/research.md`. Jobs:
- **Resolve**: unresolved mention → existing id, or a drafted new place with
  description, coords, facts, sources.
- **Arrive**: first entry in a new region → draft the region's key spots for
  the user's activities (surf spots with tide/break/level, dive sites, hikes).
- **Trip reconstruction**: a multi-day trip ("Komodo liveaboard from Lombok")
  → typical route, matched day-by-day to what the journal describes →
  `stops` with `confidence: inferred`.
- **Dossier**: wishlist place → `research/<id>.md`.

Research writes go through a **confirm card** in Telegram (Add / Edit /
Skip), because inferred geography is exactly the class of error we're
fixing. Notes on already-known places are written without asking.

### Planner (new, `planner.py`)
Weekly and on `/plan`: current location + goals + hard deadlines + wishlist
dossiers + events → a proposed sequence with date windows and the reasoning
("Mentawai season closes in Oct; from Lakey it's ~2 days via Bali→Padang;
fits before the Oct 19 start only if you leave by ..."). User confirms → it
updates `wishlist.yaml` statuses and windows. Never books or contacts anyone.

### Brief
Consumes the planner's current proposal, events near the current place
*and* near planned/candidate places, and the derived trail. Region tags come
from ids, never inference.

### Commands
`/trail [range]` — where I've been. `/place <name>` — what we know + my notes.
`/want <place>` — add to wishlist, triggers a dossier. `/plan` — run planner.

## Build order

Each step ships on its own and is useful alone.

1. **Places v2 + trail.** Schema, migration of `places.yaml`/`itinerary.yaml`,
   `vault` read/write for places and wishlist, `vault.trail()`, `/trail`.
   Place ids + aliases in extraction (resolver, no research yet).
2. **Research agent + confirm cards.** Resolve / Arrive / Trip jobs. Then a
   one-off **backfill** over all existing journals (38 entries), reviewed in
   batches, to link every past activity to a place.
3. **Wishlist + dossiers.** `/want`, scheduled dossier refresh.
4. **Planner + brief integration.** `/plan`, weekly run, brief uses it.
5. Later: weekly review against intentions, map image of the trail.

## Decisions (answered 2026-09-27)

1. **Confirm-before-write: yes.** The planner proposes soft itineraries and
   asks for preferences; nothing inferred lands without a confirm.
2. **Spend: yes, and more of it** — the user wants 5-10x today's research
   depth (a real multi-minute, many-source research run every morning).
   Model and runtime choices are open, see "Open questions".
3. **Granularity: everything, including food** — plus structured meal and
   how-I-felt logging and Beli-style personal rankings of places.
4. **Gate: agreed**; CLAUDE.md now points here.

## Open questions (in discussion)

- Telegram becomes a chat with the assistant by default, with journaling
  moved to explicit menu buttons (today / yesterday / pick a date). Do
  voice notes stay journal-by-default?
- Where the deep research runs: API calls from the bot (per-token cost)
  vs. a scheduled Claude Code routine on the user's plan that writes into
  the vault.
- Rankings model: ordered list per category built by pairwise comparisons
  (Beli-style), score derived from position.
