# Setting up the watch, phone and computer feeds

daylog takes measurements from your own devices through one inlet,
`POST https://<status-site>/health/<HEALTH_INGEST_SECRET>` (set
`HEALTH_INGEST_SECRET` on Railway; the path is the password, so treat the
full URL like one). Three sources feed it. Each works without the others.

| Source | What it sends | Cost |
|---|---|---|
| Health Auto Export (iPhone app) | Apple Watch sleep with stages, workouts at their real times, resting HR, HRV, steps, exercise, VO₂ max, daylight… | The app's paid tier (automations) |
| Mac forwarder (`tools/screentime`) | Mac screen time per app and per website, as timed sessions | Free (ActivityWatch) |
| iPhone Shortcuts | iPhone screen time per app, daily totals; exact times for a few chosen apps | Free (iOS 26 for totals) |

What happens to it: daily numbers go to `health.yaml`, timed records to
`health/days/YYYY-MM-DD.yaml` in the vault. The week calendar draws watch
workouts at their measured times (a told "surf" takes the watch's 06:40–08:25),
sleep under the day, and screen time as a strip at the edge of each day. The
Health tab shows trends against your own 28-day usual; the brief, the weekly
review and chat read the same numbers.

## 1. Apple Watch → Health Auto Export

1. Install **Health Auto Export – JSON+CSV** on the iPhone and give it read
   access to Health (all categories you want; Sleep, Heart, Activity and
   Workouts at least).
2. **Automations → New → REST API**, one for health metrics:
   - URL: the inlet URL above.
   - Data type: *Health Metrics*. Pick at least: Step Count, Active Energy,
     Resting Heart Rate, Heart Rate Variability, Heart Rate, Apple Exercise
     Time, Apple Stand Hour, Sleep Analysis, VO₂ Max, Respiratory Rate,
     Blood Oxygen Saturation, Time in Daylight, Walking + Running Distance.
   - Export format: **JSON**. Export version: **2**.
   - Aggregation / time grouping: **Day**. Leave *Summarize Data* on (sleep
     then arrives as one record per night with its stages; off works too,
     as stage segments).
   - Date range: **Today**, sync every hour. "Previous 7 Days" stops at
     yesterday, so on its own last night's sleep and today's numbers would
     arrive a day late and the morning brief would never see them. Avoid
     hourly grouping with *Since Last Sync*: a partial day would overwrite
     the full one.
   - Then **duplicate it** with Date range **Previous 7 Days**, once a day:
     the catch-up for anything a sync missed while the phone was locked.
     Re-sending changes nothing in the vault.
3. A **REST API** automation for **Workouts**, same URL, JSON, version 2,
   Date range **Since Last Sync** (each workout has an id, so a re-send
   just replaces it), every hour. Turn **off** route data and workout
   metrics (heart-rate series): the calendar only needs start, end, kind
   and the totals, and the payload stays small.
4. Backfill: **Manual Export** with a start date as far back as you like,
   to the same URL, once for metrics and once for workouts. It is safe to
   repeat.
5. Check: the app shows the reply text, e.g. `saved 7 days of numbers, 7
   nights of sleep, 3 workouts for 2026-11-16..2026-11-22`. Anything it couldn't
   read is listed after `ignored:`.

iOS only lets apps read Health while the phone is unlocked, so syncs
happen when you use the phone; the 7-day window catches up.

## 2. Mac screen time → ActivityWatch + forwarder

See [`tools/screentime/README.md`](../tools/screentime/README.md). In short:
install ActivityWatch 0.14+ (`brew install --cask activitywatch`), add its
web-watcher extension to your browser (Dia; Arc shuts down on 2026-12-22),
then run `tools/screentime/install.sh` and paste the inlet URL. A
LaunchAgent sends sessions every 15 minutes: app name, bare domain and
times only, never window titles or full URLs. Idle and locked time is left
out.

## 3. iPhone screen time → Shortcuts

**Daily totals (iOS 26+).** In Shortcuts → Automation → New → *Time of
Day*, 23:55, daily, *Run Immediately*:

1. *Get App & Website Data* (Screen Time) — today, this iPhone.
2. *Format Date* — Current Date, custom format `yyyy-MM-dd`.
3. *Get Contents of URL* — the inlet URL, Method POST, Request Body JSON:
   - `screen_totals` (Dictionary):
     - `date` → Formatted Date
     - `device` → `iphone`
     - `apps` → the output of step 1 (as text)

The inlet reads the apps as `App: 1h 23m` lines, `{app: duration}` or
`[{app, minutes}]`, so the action's text output works as is. Run it once by
hand first and look at the reply: if the apps come back under `ignored:`,
the format differs from these; send the reply text to a session and the
parser gets a line.

**Exact times for a few apps (optional).** For each app worth watching
(YouTube, Instagram, TikTok…), two automations — *App → [the app] → Is
Opened* and *Is Closed*, *Run Immediately*, notifications off — each with
one *Get Contents of URL* POST:

```json
{"screen_event": {"device": "iphone", "app": "Instagram", "event": "open",
                  "at": "<Current Date as yyyy-MM-dd HH:mm:ss Z>"}}
```

(`"event": "close"` in the second). The calendar pairs them into sessions;
an open with no close counts until the next event, at most 30 minutes. The
nightly totals still give the day's minutes; these only add the when.

## Privacy

Everything stays in your private vault. Nothing here is shown on the guest
view of the status site. The Mac forwarder never sends titles or URLs. The
inlet URL is the only key: if it leaks, change `HEALTH_INGEST_SECRET` and
update the three senders.

## 4. Where you were → Overland (iPhone)

daylog turns your phone's location into **visits** (*Waco Surf 15:40–19:10*):
a left-edge strip on the week calendar, named visits in your calendar feed,
and chat can answer "where was I Tuesday afternoon". Only the visits are
saved to the vault; raw GPS points stay on the server's disk until they
become a visit, and are never committed. It doesn't change your base in
`location.yaml`; that still follows what you say.

1. Install **Overland** (free, open source) from the App Store and allow
   location **Always**.
2. Settings → **Receiver Endpoint**: the same secret as the health inlet,
   with `/location/` instead of `/health/`:
   `https://<status-site>/location/<HEALTH_INGEST_SECRET>`
3. Start tracking. Sensible settings: desired accuracy ~100 m, points per
   batch ~50, automatic pausing on. Overland batches points and sends them
   when it can; daylog answers `{"result": "ok"}`, so the queue empties.
   A stricter battery mode (significant changes only) still works, with
   coarser visit times.

A stay counts as a visit after 10 minutes within ~150 m, and takes the
name of a known place within ~250 m (a spot, venue or restaurant with
coordinates; not a whole town). A stay nothing names takes the name of the
place your note puts you at around then ("dinner at Bola's"), shown with a
"?", and when that place has no location yet you get one card, **Save
where Bola's is?**; after that, visits there are named for real. The gaps
between stays show as trips ("on the move 1 h 05 to Waco Surf"). OwnTracks in HTTP mode works with the same
URL if you prefer it.
