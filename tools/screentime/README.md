# Mac screen time for daylog

A small script that reads [ActivityWatch](https://activitywatch.net) on this Mac
every 15 minutes and sends daylog which app (and, in the browser, which website)
was in front while you were at the keyboard. daylog files the sessions under
`health/days/YYYY-MM-DD.yaml` (`screen`), beside the watch and phone numbers.

## What is sent

Per session: the device (`mac`), the app name, for browser time the bare domain
(`youtube.com`), and the local start and end times with their UTC offset.
Never window titles, never full URLs, never anything typed. Idle and locked time
is left out (ActivityWatch's AFK watcher), as are private-window tabs' domains.
It goes only to your daylog inlet.

## Setup

1. **ActivityWatch 0.14 or newer**

   ```sh
   brew install --cask activitywatch
   ```

   Open ActivityWatch, allow the macOS Accessibility prompt (the window watcher
   needs it to see the front app), and turn on "start at login" from its menu-bar
   icon. Check http://localhost:5600 shows a timeline.

2. **The web watcher in your browser**, for per-site time. In Dia, install
   "ActivityWatch Web Watcher" from the Chrome Web Store (Dia is supported from
   ActivityWatch 0.14). Safari and Firefox have their own versions. Arc shuts
   down on 2026-12-22, so don't bother setting it up there. Without the
   extension browser time is still counted, just without a site.

3. **Install the forwarder**

   ```sh
   tools/screentime/install.sh
   ```

   It checks ActivityWatch is up, asks for the inlet URL
   (`https://<host>/health/<HEALTH_INGEST_SECRET>`; typing is hidden), saves it
   to `~/Library/Application Support/daylog-screentime/config.json` (mode 600),
   shows a dry run of the last hour, and loads the LaunchAgent
   `com.daylog.screentime` (runs at login and every 15 minutes). Run it in the
   Terminal app so it can ask; where nothing can be typed (a script, an
   assistant's shell), pass the URL instead:
   `DAYLOG_INLET_URL='https://…' tools/screentime/install.sh`. It uses macOS's
   own `/usr/bin/python3` when there is one.

The first run sends the last 26 hours. After that each run sends from two hours
before the last successful one; the server replaces a session it already has, so
the overlap is harmless. A failed send leaves the bookmark where it was, and the
next run catches up (up to 14 days back).

## Checking on it

```sh
tail -f ~/Library/Logs/daylog-screentime.log      # one line per run
python3 ~/Library/Application\ Support/daylog-screentime/daylog_screentime.py --dry-run --since 3
launchctl print gui/$UID/com.daylog.screentime | head -20
```

`--dry-run` prints the JSON instead of sending it; `--since HOURS` overrides the
window. The log shows only the inlet's host, never the URL (it holds the secret).

Settings (env vars, or keys in `config.json`): `DAYLOG_INLET_URL`, `AW_URL`
(default `http://localhost:5600`), `LOOKBACK_HOURS` (first run, default 26),
`MIN_SECONDS` (shorter fragments are dropped, default 20).

## Uninstall

```sh
tools/screentime/install.sh uninstall
```

Unloads the agent and removes the plist, the script copy, the config and the
bookmark. ActivityWatch itself stays; remove it with
`brew uninstall --cask activitywatch` if you no longer want it.
