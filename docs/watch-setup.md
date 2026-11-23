# Logging and asking from the Apple Watch

watchOS has no browser, so the web app can't run on the watch. Two
Shortcuts do the job instead: **Log to daylog** (say what you did; it goes
through the same pipeline as the Log tab) and **Ask daylog** (one question
to the assistant, in the same conversation as the Chat tab). Both run on
the watch, from Siri ("Hey Siri, log to daylog"), a watch-face complication,
the Smart Stack or the Action button on an Ultra, and work on the iPhone
too.

## 1. Get a key

In the app: **You → Watch & Shortcuts → Add a device**. Name it ("Apple
Watch"), and copy the key it shows (`dl_…`). It is shown once; the server
keeps only a hash. The card also shows the two URLs:

- `https://<your status site>/watch/log`
- `https://<your status site>/watch/ask`

Lost the watch, or the key leaked? Revoke it on the same card; its
Shortcuts stop working at once.

## 2. "Log to daylog"

In the Shortcuts app on the iPhone, new shortcut:

1. **Dictate Text** (Language: English; on the watch you tap Done when
   you've finished — watch dictation stops after roughly 30–60 seconds, so
   one thought per note).
2. **Get Contents of URL**
   - URL: the Log URL
   - Method: **POST**
   - Headers: `Authorization` = `Bearer dl_…` (your key)
   - Request Body: **JSON**, one field `text` (Text) = *Dictated Text*
3. **Show Result** — *Contents of URL* (or **Speak Text** with it).

Name it **Log to daylog**, and in its settings turn on **Show on Apple
Watch**.

The watch hears "Got it. Logging now" straight away. When the note is in
(about half a minute later), one notification says what was logged, and
how many things wait to be confirmed in the app: a hard date that moved, a
new place, a sport to check. The watch can't confirm, so those wait for a
tap in the app, as always.

## 3. "Ask daylog"

The same three steps with the **Ask** URL, and **Speak Text** (or Show
Result) on *Contents of URL* at the end.

A quick answer (within ~20 seconds) is spoken or shown right away. A longer
one — anything that searches the web — answers "Thinking", and the reply
arrives as one notification; the full text is in the Chat tab. Turn on the
app's notifications (You → Session) on the iPhone so they reach the watch.

## 4. The brief when you wake up

The brief normally comes at `BRIEF_HOUR` (7:00). With the same key, a
Shortcuts automation can make it the moment you're up, after your phone
has sent last night's sleep:

1. Shortcuts → **Automation** → **+** → **Sleep** → **Waking Up** (fires
   when your sleep schedule ends or you dismiss the wake alarm; **Alarm →
   Is Stopped** works too if you don't use a sleep schedule). **Run
   Immediately**.
2. A **Health Auto Export** action that exports now (search "Health Auto
   Export" in the action list; pick the one that runs your *Today* metrics
   automation or exports today's data to your URL). This puts last night's
   sleep in before the brief reads it.
3. **Wait** 20 seconds.
4. **Get Contents of URL** — `https://<your status site>/watch/brief`,
   Method **POST**, Header `Authorization` = `Bearer dl_…`, no body.

About a minute later the usual **☀️ Brief** notification arrives. It's made
once a day: a second run says "Today's brief is already in the app", and
the 7:00 brief skips a day you already woke up to. Set `BRIEF_HOUR` later
(say 11) on Railway to keep 7:00 from beating you to it on a slow morning;
it's still the fallback when the automation doesn't run.

## 5. Starting it fast

- **Siri:** "Hey Siri, log to daylog" / "Hey Siri, ask daylog".
- **Complication:** edit the watch face → a slot → Shortcuts → pick one.
- **Action button (Ultra):** Settings → Action Button → Shortcut.
- **Smart Stack:** add the Shortcuts widget.

## Limits

- A device can send 30 requests per 10 minutes; after 10 bad keys in 15
  minutes the inlet refuses everyone for a while.
- Text only: Shortcuts can't record audio on the watch, so watch notes use
  Apple's dictation rather than daylog's Whisper with your vocabulary.
  Extraction still reads them against your places and words.
- The key lives in plain text inside the Shortcut (synced via iCloud):
  don't share the shortcut. Revoke and re-add if you do.
