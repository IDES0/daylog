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
   - Date range: **Previous 7 Days** (each run re-sends full days, so a late
     sync fills in; re-sending changes nothing in the vault). Avoid
     hourly grouping with *Since Last Sync*: a partial day would overwrite
     the full one.
   - Sync cadence: every hour or two is plenty.
3. A second **REST API** automation for **Workouts**, same URL, JSON,
   version 2, *Previous 7 Days*. Turn **off** route data and workout
   metrics (heart-rate series): the calendar only needs start, end, kind
   and the totals, and the payload stays small.
4. Backfill: **Manual Export** with a start date as far back as you like,
   to the same URL, once for metrics and once for workouts. It is safe to
   repeat.
5. Check: the app shows the reply text, e.g. `saved 7 days of numbers, 7
   sleep, 3 workouts for 2026-11-16..2026-11-22`. Anything it couldn't
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
