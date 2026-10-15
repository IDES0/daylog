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
- `GET /api/surf` → `{"text": "…"}` (same text `/surf` sends) ; `GET /api/fly` likewise
- `GET /api/upcoming` → `{"items": ["…"]}` ; `GET /api/status` → `{"text": "…"}`
- `GET /api/usage` → `{"text": "…"}`
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
- `GET /api/rankings?category=` → `{"categories": [...], "lists": {"<category>": [{"tier", "items": ["…"]}]}}`
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
