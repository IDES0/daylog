# Your job: the evening request sweep (every night, ~22:30 local)

Whatever they asked for in today's journal gets researched tonight, so
tomorrow's brief has the answer. Take 15-25 minutes; depth beats breadth.

1. Read today's journal (and yesterday's, in case it came in late) for
   requests and open threads: "research …", "look into …", "figure out …",
   "I need to find …", "should I …", plus the `decisions` and
   `open_questions` fields. Skip what the morning routine or a decision
   file already answered.
2. Take the 1-4 that matter most to them (their own requests first, then
   anything with a deadline, then what serves their goals and `profile.md`).
3. For each: the answer, the real options with dates, prices and links, a
   recommendation, and the next step. If it's a decision in progress,
   create or update `research/decisions/<slug>.md` (options, facts, what's
   still unknown, a dated log at the bottom).
4. Write `research/daily/<tomorrow>-requests.md`:
   ```
   # Requests — from <today>'s journal
   ## <their words, short>
   Answer, options, recommendation, next step. Links.
   ```
   At most ~400 words; link the decision files for depth.

## Also every night: what's booked (read-only Gmail)

The app has no mail access; you are how bookings reach the calendar and the
morning brief. After the requests, look through Gmail for confirmations and
changes of things with a time: activity and lesson bookings (surf sessions,
dives, classes), flights, trains and buses, tours, restaurant reservations,
appointments, event tickets, check-ins. Cover yesterday through the next 14
days; look back ~30 days of mail for ones made earlier.

Write one file per day that has any, `research/times/<date>.yaml` (the date
the booking happens, local to where it happens), a YAML list:

```yaml
- id: waco-surf-48213        # stable: the sender plus its booking reference
  kind: surf                 # the word their journal uses for it (surf, dive,
                             # paraglide, ...), else flight, tour, meal,
                             # appointment, event, stay
  title: Waco Surf session   # short, no personal details
  place: Waco Surf
  at: "16:00"                # local start, 24 h
  until: "17:00"             # local end, when the email says it
  status: booked             # booked | changed | cancelled
  source: "email: Waco Surf booking confirmation"
```

- Rewrite a day's file whole each night from what the mail says now: a
  changed time replaces the old one (same `id`), a cancelled booking stays
  with `status: cancelled` so the app drops it.
- Several sessions are several entries ("4pm, 6pm and 7pm" are three).
- A flight is one entry on its departure day (`at` the local departure,
  `until` the local arrival when it's the same day), `place` "AUS → SFO".
- Only what has a confirmed date and time. Never codes, booking numbers
  beyond what makes the `id` unique, addresses, prices or email addresses.
- Don't touch files for days before yesterday.
- Commit with the rest: `git add research && git commit -m "research: requests+times <date>"`.
