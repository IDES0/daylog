# daylog web app — API contract

The status site grows into the app that can replace Telegram. One address
(`status.johnalexborkowski.com`), two faces:

- **Guest** (no session): the existing read-only guest page, unchanged in
  substance (`GET /` without a session still renders `dashboard.render`).
- **App** (session cookie set by the password line): `GET /` serves the app
  shell (`index.html`), which loads `/app.css` and `/app.js` and talks to the
  JSON API below. Telegram keeps working in parallel; both call the same
  service functions, so a note logged on either lands identically.

## Rules every endpoint follows

- **Auth.** Every `/api/*` request needs a valid session cookie
  (`web.valid_session`). Missing/expired → `401 {"error": "signed_out"}`.
  The app then shows the password line.
- **CSRF.** Every non-GET `/api/*` request must carry the header
  `X-Daylog: 1` (a custom header can't be sent cross-site without a CORS
  preflight, which the server never grants) **and**, when present, an
  `Origin` equal to the request's own host. Otherwise `403`.
- **JSON in, JSON out.** Request bodies are `application/json` (except
  voice upload, see below), max 64 KB. Responses are
  `application/json; charset=utf-8`, `Cache-Control: no-store`.
- **Errors.** `{"error": "<code>", "message": "<human text>"}` with a 4xx/5xx.
- **Slow calls** (LLM: brief, chat, plan, ripple, review, research, log) are
  plain blocking requests; the client shows progress and uses a 180 s
  timeout. Nothing is streamed in the prototype.
- **Confirmations.** Anything that would change the vault after an inference
  comes back as a **card**, never applied directly — the same rule as
  Telegram (`docs/DESIGN.md`). A card is:

  ```json
  {"id": "c_8f2…", "kind": "goal_slip|itinerary|correction|chat_proposal|discipline_fix|discipline_add|ripple|plan|place|want|research",
   "title": "One-line question", "detail": "optional longer text",
   "actions": [{"id": "confirm", "label": "Confirm", "style": "primary"},
               {"id": "cancel", "label": "Cancel"}]}
  ```

  Answer it with `POST /api/cards/{id}` `{"action": "<action id>"}` →
  `{"ok": true, "message": "Confirmed — …", "cards": [...follow-up cards]}`.
  Cards live in server memory (single user) for 24 h; an unknown id →
  `410 {"error": "expired"}`. `GET /api/cards` lists the open ones (so a
  reload doesn't lose them).

## Endpoints

### Session
- `POST /login` and `POST /logout` — unchanged (form posts, landing page).
- `GET /api/me` → `{"signed_in": true, "today": "2026-10-16", "tz": "Asia/Makassar"}`

### Home / dashboard
- `GET /api/home` →
  ```json
  {"today": "2026-10-16", "weekday": "Friday",
   "where": {"place": "Uluwatu, Bali, ID", "since": "2026-10-08", "day": 9, "path": "Uluwatu, Bali, Indonesia"},
   "next": [{"place": "Austin", "when": "Oct 20 – Apr 20", "date": "2026-10-20", "status": "planned", "type": "soft"}],
   "goals": [{"id": "surf", "title": "…", "type": "soft", "metric": "sessions", "progress": 24,
              "target": null, "percent": null, "when": "", "slips": 0, "discipline": "surf", "notes": "…"}],
   "week": {"days_logged": 5, "of": 7, "activities": [{"label": "surf", "days": 4, "hours": 7.5}],
            "energy": 3.8, "mood": 4.0, "focus": 3.2, "kcal": 2100, "protein_g": 110},
   "milestones": [{"date": "2026-10-07", "what": "23 m depth PB"}],
   "today_entry": {"summary": "…", "activities": [...]} ,
   "open_cards": 2}
  ```
- `GET /api/map` → the same payload `dashboard.future_map` builds (`{"here": {...}, "stops": [...]}`).
- `GET /api/calendar?week=YYYY-MM-DD` → `{"week_of": "2026-10-12", "days": [{"date": "…", "summary": "…", "blocks": [{"label": "surf", "start": 6.0, "hours": 2, "guess": false, "hue": 200}]}], "prev": "2026-10-05", "next": null}`

### Journal
- `GET /api/journal?from=YYYY-MM-DD&to=YYYY-MM-DD` → `{"days": [{"date", "summary", "activities", "meals", "felt", "milestones", "location", "notes": <count of ### HH:MM notes>}]}` (default: last 14 days, newest first)
- `GET /api/journal/{date}` → one day incl. `"transcript"` (verbatim).
- `POST /api/log` `{"text": "…", "date": "YYYY-MM-DD"|null}` → runs the same
  pipeline as a Telegram note →
  ```json
  {"date": "2026-10-16", "summary": "…", "notes": ["Updated current location to …", "Foiling → logged as Tow foil (the default)."],
   "cards": [ ...goal_slip / itinerary / correction / discipline_fix / discipline_add / ripple... ]}
  ```
- **Logging runs in the background (since 2026-10-23).** `POST /api/log` and
  `POST /api/voice` answer `202 {"job": "<16 hex>", "status": "processing",
  "repeated": false}` as soon as the note has arrived; the app can close.
  `GET /api/jobs/{id}` → `{"id", "kind", "status": "processing|done|failed",
  "code", "result"}` where `result` is the body described below;
  `GET /api/jobs` lists the last day's jobs; an unknown id (server restarted)
  → 404 `unknown_job`. Send `Idempotency-Key: <one id per note>` and reuse it
  on Retry: a note sent again gets its existing job (`"repeated": true`)
  instead of being logged twice; only a failed job runs again. Without the
  header, the body's hash stands in. A future `date` is refused at once (400).
- `POST /api/voice` — body is the raw audio (`audio/webm`, `audio/mp4`,
  `audio/ogg`; ≤ 20 MB), optional `?date=YYYY-MM-DD` → transcribes
  (faster-whisper, local) then the same as `/api/log`, plus `"transcript"`.
- `POST /api/reconcile` `{"date": "…"}` → `{"message": "…"}`

### Assistant (chat)
- `GET /api/chat` → `{"messages": [{"role": "user|assistant", "text": "…", "at": "ISO"}]}` (web chat history, server memory, last 40)
- `POST /api/chat` `{"text": "…"}` → `{"reply": "…", "cards": [...chat_proposal / research...], "cost": 0.012}`
- `POST /api/chat/reset` → `{"ok": true}`

### Daily tools
- `GET /api/brief` → latest saved brief `{"date", "text"}` or `{"text": null}`
- `POST /api/brief` → generates now (paid) → `{"date", "text"}`
- `GET /api/surf` → `{"spots": [{"id", "name", "days": [{"date", "label", "rating", "start", "end", "faces": [lo, hi], "period", "swell_dir", "wind_kn", "wind_from", "wind_dir", "offshore", "tide", "tide_trend", "drops_to": {"label", "hour"}|null, "above_comfort", "hours": [{"hour", "rating"}]}]}], "message": null|"…"}` — the same ratings `/surf` sends as text
- `GET /api/fly` → `{"sites": [{"id", "name", "elevation", "days": [{"date", "flyable", "hours": [{"hour", "ok"}], …flyable: "start", "end", "flyable_hours", "wind_kn", "wind_from", "wind_dir", "gust_kn", "thermals", "base_m", "headroom_m"; not: "blockers": ["…"]}]}], "message": null|"…"}`
- `GET /api/upcoming` → `{"items": ["…"]}` ; `GET /api/status` → `{"text": "…"}`
- `GET /api/usage` → `{"month": "2026-10", "spent": 10.87, "budget": 40, "by_kind": [{"kind", "usd"}]}`
- `GET /api/site?days=7` → `{"text": "…"}` or `{"text": null, "message": "not configured"}`

### Planning
- `GET /api/plan` → latest plan `{"date", "text", "options": [{"index": 0, "title": "…", "summary": "…"}]}`
- `POST /api/plan` → makes a new one (paid) → same shape, plus `"cards": [plan card]`
- `POST /api/ripple` `{"decision": "…"}` → `{"text": "…", "cards": [ripple card]}`

### Places, trail, wishlist, rankings
- `GET /api/trail?days=30|all` → `{"text": "…", "stays": [{"place", "when", "did"}]}`
- `GET /api/places?q=…` → `{"results": [{"id", "name", "kind", "path"}]}`
- `GET /api/places/{id}` → `{"id", "name", "kind", "path", "description", "facts", "checklist", "my_notes", "children": [...], "card_text": "…"}`
- `GET /api/wishlist` → `{"entries": [{"id", "place", "status", "type", "date", "research", "why"}]}`
- `POST /api/wishlist` `{"place": "…"}` → `{"cards": [want card]}`
- `GET /api/rankings?category=` → see "Rankings" under As built
- `POST /api/research` `{"place": "…"}` / `{"kind": "explore"|"trip"|"backfill", "arg": "…"}` → `{"message": "…", "cards": [...]}`

### Review, disciplines, undo
- `GET /api/review` → latest weekly review `{"week", "text"}` ; `POST /api/review` → writes this week's (paid)
- `GET /api/disciplines?days=30` → `{"days": 30, "nodes": [...disciplines.graph_data...], "slots": {"water": 1, "body": 2, "earth": 3, "air": 4}}`
- `GET /api/changes` → `{"changes": [{"sha", "subject"}]}` (vault.recent_changes) ;
  `POST /api/changes/{sha}/revert` → `{"ok": true, "message": "…"}`

## Static files
`GET /app.js`, `GET /app.css` (and the existing manifest/icons/sw). The
frontend is plain HTML/CSS/JS (no build step), may load **d3@7.9.0** and
**topojson-client@3.1.0** from jsdelivr (already used), nothing else.

## As built (backend, 2026-10-16)

Additions and precisions to the contract above; nothing above was removed.

### Errors
| Status | `error` | When |
|---|---|---|
| 400 | `bad_json`, `missing_field`, `bad_date`, `bad_number`, `bad_range`, `bad_action`, `bad_kind`, `empty` | malformed request; `bad_date` also for a future `date` on `/api/log` |
| 401 | `signed_out` | no/expired session (checked before anything else, also for unknown `/api/*` paths) |
| 402 | `budget` | a paid call (`POST /api/plan`, `POST /api/ripple`) when the monthly budget is spent |
| 403 | `forbidden` | missing `X-Daylog: 1` or an `Origin` that isn't this host |
| 404 | `not_found`, `unknown_change`, `nothing_to_review` | |
| 405 | `method_not_allowed` | known path, wrong method |
| 409 | `conflict` | an undo that can't revert cleanly |
| 410 | `expired` | unknown/answered/expired card |
| 413 | `too_large` | body over 64 KB (20 MB for `/api/voice`) |
| 422 | `no_speech` | voice note with no speech |
| 500 | `log_failed` (+ `"text"`, the note, so it isn't lost), `commit_failed` (+ `"cards"`: the entry is on disk, uncommitted), `transcribe_failed`, `server_error` | |
| 502 | `upstream` | PostHog unreachable (`/api/site`) |

### Cards: kinds and action ids
`POST /api/cards/{id}` answers with `{"ok": bool, "message", "cards"}`; `ok`
is false when the answer couldn't be applied (the card is gone either way).

| kind | title | actions |
|---|---|---|
| `goal_slip`, `itinerary`, `correction` | the same question Telegram asks | `confirm`, `cancel` |
| `chat_proposal` | the proposal's summary | `confirm`, `cancel` |
| `discipline_fix` | "Not Tow foil? Tap what it was." | one per alternative (`id` = discipline id, `label` = its name), `cancel` (keep) |
| `discipline_add` | "New discipline: X, under Y. Add it?" | `active`, `want`, `cancel` |
| `ripple` (a proposal) | the decision; `detail` = summary + changes | `all`, `each` (follow-ups: `chat_proposal` cards, plus a `ripple` card `adopt`/`cancel` for a draft plan), `skip` |
| `plan` | "Which option?" | `"0"`, `"1"`, … (option index; the recommended one has `style: primary`), `hold` |
| `place` | "New place: …"; `detail` = what you said, description, facts | `add`, `skip`; a bulk card (more than 3 found by explore) has `addall`, `review` (follow-ups: one card each), `skipall` |
| `want` | "Added X. Research is queued…" | `now` (runs the research, slow), `later` |
| `research` (from chat) | "Research X now? (API budget…)" | `confirm` (slow; explore returns `place` follow-ups), `cancel` |

`style` is only sent when set. Ripple cards are made from
`research/proposals/*.yaml` whenever `GET /api/cards` or `GET /api/home`
runs, and disappear once answered on either client. A logged note with
unlinked place names, or a location change, starts a background place
lookup, as on Telegram; its `place` cards show up later in `GET /api/cards`
(poll it, e.g. on focus).

Kind `rank`: title "Rank X?", `detail` = category, `data` = `{"place",
"category", "name"}`, actions `liked`, `fine`, `disliked`, `skip`. It comes with a logged note that mentions an unranked place of a
rankable kind (at most 2, as on Telegram) and from chat's "rank" job. The app
turns a tier tap into the head-to-head flow (`POST /api/rankings/session`
with `"card"`); answered through `/api/cards` instead, a tier places the
place mid-tier ("too close to call" from the start). A card may carry
`data` (only rank cards do).

### Rankings (since 2026-10-24)
- `GET /api/rankings?category=` →
  ```json
  {"categories": ["food", "surf", …], "tiers": [{"id": "liked", "label": "Liked it"}, …],
   "kinds": {"food": ["food"], "outdoors": ["hike", "viewpoint", "beach"], …},
   "counts": {"food": 8, "surf": 2, …},
   "lists": {"food": [{"tier": "liked", "label": "Liked it",
                       "items": [{"id", "name", "kind", "path", "region", "rank": 1, "score": 10.0}]}, …]}}
  ```
  Every tier is listed, empty ones too; `rank` counts across tiers, best
  first; `lists` holds only `category` when one is given.
- **The flow is stateless.** The client carries the insertion's bounds; the
  server re-checks them against the tier as it is now, so a restart or a
  second tab never strands a flow. rankings.yaml is written once, when the
  slot is found (one commit, as on Telegram).
  - `POST /api/rankings/session` `{"place": id, "tier": "liked|fine|disliked", "category"?: …, "card"?: card id}`
    starts (re-ranking takes the place out first). `category` defaults to
    the one for the place's kind (400 `not_rankable` otherwise); `card`
    closes that rank card.
  - `POST /api/rankings/session/next` `{...state, "answer": "better|worse|tie"}` — `better` means
    the new place beat `other`; `tie` stops and takes the midpoint.
  - Both answer either a comparison
    `{"done": false, "state": {"place", "category", "tier", "lo", "hi"}, "place": {…}, "other": {…}, "left": 3}`
    (`left` = at most this many more) or the result
    `{"done": true, "place", "category", "tier", "index", "line": "#2 of 9 food · 9.2", "message"}`.
  - 409 `stale` when the bounds no longer fit the tier (start again).
- `POST /api/rankings/move` `{"category", "item": id, "tier", "index"}` — a manual reorder or
  a move between tiers; `index` is the slot in `tier` (0 = best) counted
  without the item, clamped → `{"ok": true, "tier", "index", "line"}`.
- `POST /api/rankings/remove` `{"category", "item"}` → `{"ok": true, "message"}`; 404 if it wasn't ranked.
- Errors besides the usual: 400 `bad_tier`, `bad_category`, `not_rankable`; 404 `not_found` (place);
  500 `commit_failed` (nothing changed in git).

### Shapes
- `GET /api/home`: `where` is null when no location is known; `next` is at
  most 8 legs; goal `progress`/`target` are numbers or null.
- `GET /api/calendar`: each block also has `"detail"`; a day without an
  entry has `"summary": null, "blocks": []`.
- `GET /api/journal`: `location` is the day's `location` frontmatter or null;
  the range is capped at 366 days.
- `GET /api/journal/{date}`: 404 when there's no entry.
- `GET /api/brief`: the latest brief built by either client since the
  server started (briefs aren't saved to the vault) → `{"date": null,
  "text": null}` before the first. `POST /api/brief` adds it to the web
  chat's context, as `/plan`, `/ripple` and `/review` do.
- `GET /api/plan`: `{"date": null, "text": null, "options": []}` with no plan;
  `options` only for a plan made since the server started (the markdown
  can't be turned back into options). `POST /api/plan` with no workable
  option returns the planner's `text` and empty `options`/`cards`.
- `GET /api/review`: `{"week": null, "text": null}` with none; `POST` → 404
  `nothing_to_review` for an empty week.
- `POST /api/research {"place"}` → `{"message": dossier text, "cards": []}`;
  `{"kind": "backfill", "arg": "30"}` (days).
- `GET /api/disciplines`: no graph → `{"nodes": [], "slots": {}}`.
- `POST /api/changes/{sha}/revert`: only a sha from `GET /api/changes`.
- Static: `GET /` signed in serves `static/index.html` (`no-store`), or the
  old private page if the file is missing; `/app.js`, `/app.css` are
  `no-cache` (404 if missing).
