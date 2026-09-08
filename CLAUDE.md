# Caledonia Gladiators Fixtures Feed

## What this project does
Scrapes the Caledonia Gladiators' men's and women's basketball fixtures
pages and generates a single subscribable `.ics` calendar feed. A
GitHub Actions workflow re-runs the script on a schedule and publishes
the result to GitHub Pages, so calendar apps subscribed to that URL
pick up fixture changes (time, venue, postponements) automatically.

- `generate_ics.py` -- the whole pipeline: fetch, parse, build calendar
- `test_generate_ics.py` -- stdlib `unittest`, no network, no extra
  dependency. Run with `python -m unittest -v`
- `requirements.txt` -- requests, beautifulsoup4, icalendar
- `.github/workflows/update-fixtures.yml` -- runs on a cron schedule,
  publishes `gladiators-fixtures.ics` **and** `fixtures_state.json` to
  the `gh-pages` branch (both generated -- `.gitignore`d on `main`)

## Critical gotcha: what `fetch_text()` actually returns
`fetch_text()` uses `requests` + `BeautifulSoup(...).get_text()`. This
returns **plain text with zero markdown decoration** -- no `##`
headings, no `**bold**`, no `[text](url)` link brackets, and images
contribute *nothing* (get_text() doesn't read `alt` attributes).

This bit us hard during development: every early version of the parser
was validated against hand-typed test files that mimicked *markdown*
formatting (because that's what a human sees when browsing the page
through a markdown-converting tool). Those regexes worked great against
markdown and matched literally nothing against the real plain text the
script actually receives, silently returning 0 fixtures. **If you ever
touch the parser, test it against realistic plain text (no `#`, `*`,
`[]`, `()`), not markdown.**

## Parser design (`parse_fixtures`)
Anchored on the literal `"VS"` line, which is the one element every
single rendering of every fixture reliably has. The combined header
line ("Caledonia Gladiators (W) – Essex Rebels – 04/10/2026") is
**missing entirely** for several real women's home fixtures -- don't
rely on it existing.

The two team names sit immediately before and after `"VS"` in the
plain-text line list; whichever one isn't literally
`"Caledonia Gladiators (M)"` / `"(W)"` is the opponent. Date/time are
found by searching a small window of lines just *before* `"VS"`;
venue and played-status are found in a window just *after* it.

Each fixture is rendered 2-3 times on the page (desktop + responsive
variants) -- dedupe on `(opponent, date)`, keep the first occurrence.

Known imperfection: the "Match Recap" (already-played) text detection
in `parse_fixtures` doesn't catch every past fixture reliably (root
cause not fully nailed down). This is **not currently a real bug** --
`build_calendar()` has a second, independent safety net that drops any
fixture whose actual kickoff/tip-off datetime is in the past, so the
output file is correct either way. `main()` reports both counts
separately so this is visible rather than silently masked. If you want
to actually fix the root cause, that's an open thread, not a blocker.

## Business rules to preserve
- **Home vs away wording**: home games read
  `"Caledonia Gladiators (X) vs Opponent (Home)"`; away games are
  **reversed**: `"Opponent vs Caledonia Gladiators (X) (Away)"`.
- **No ticket link on away games.** The Fanbase ticket link
  (`https://app.fanbaseclub.com/Fan/Dashboard?clubId=210`) only sells
  tickets for Gladiators' own venue, so home fixtures only get it as
  the event's `URL` property (a clickable link in most calendar apps).
  It's deliberately **not** duplicated into the `description` text.
- **"Tip-off" not "kickoff"** -- these are basketball games. Wording in
  descriptions and comments should say tip-off.
- **Day-before reminder**: every fixture (home and away) gets a
  `VALARM` (`ACTION:DISPLAY`, `TRIGGER:-P1D`). Apple Calendar and
  Outlook honour VALARMs from a subscribed feed; Google Calendar
  ignores them and applies the subscriber's own default notification
  instead -- that's a Google limitation, not a bug here.
- **Refresh interval must track the cron.** `REFRESH_INTERVAL` in
  `generate_ics.py` is the single source for both `REFRESH-INTERVAL`
  (RFC 7986) and `X-PUBLISHED-TTL` (the older extension Outlook
  honours), and must match the workflow cron. It was `PT12H` against a
  6-hourly publish once, which just made every subscriber lag for no
  reason. Two traps if you touch this: `X-PUBLISHED-TTL`
  is untyped, so handed a `timedelta` it silently renders `6:00:00`
  (not a valid duration) and needs the ISO 8601 string; while
  `REFRESH-INTERVAL` is DURATION-typed and rejects a string, needing
  the `timedelta` plus `VALUE=DURATION`.
- **UID** = `sha1(team_code + opponent + date)`, i.e. stable across a
  same-day time change, but a postponement to a different date
  produces a new UID.
- **Team code included in UID** so the men's and women's teams playing
  the same opponent on the same date (unlikely but possible) don't
  collide.
- **Postponement handling**: a stale event for the old date isn't just
  left to silently disappear from the feed. `generate_ics.py` publishes
  `fixtures_state.json` (STATE_FILE/STATE_URL) alongside the `.ics`
  after every run, shaped
  `{"fixtures": [...], "tombstones": [...]}` -- each record carrying
  team/opponent/date/time/is_home/uid. The *next* run fetches that file
  and, when a (team, opponent) pairing had exactly one fixture last run
  and exactly one this run but the date changed, treats it as an
  unambiguous reschedule and emits an explicit `STATUS:CANCELLED`
  tombstone VEVENT for the old UID/date -- see the `build_calendar()`
  docstring.
- **Tombstones must be carried forward, not emitted once.** A
  tombstone is kept in `state["tombstones"]` and **re-emitted every
  run** until its old date passes. This was a real bug once: emitting
  it only on the run that detected the change left it in the feed for a
  single publish cycle, so any client polling less often than that
  (Google Calendar and Outlook.com refresh subscriptions roughly
  daily) could miss it entirely -- stranding exactly the non-compliant
  clients a tombstone exists for with a permanent wrong-date event. Two
  guards go with this: a tombstone is retired once its slot is in the
  past, and is suppressed if its UID is also a live event this run
  (which happens when a fixture is moved *back* to a date it
  previously held).
- **Deliberately not handled:** a (team, opponent) pairing with more
  than one fixture on either side (a repeat opponent played twice in a
  season, which does happen -- e.g. Gladiators vs Liverpool). The site
  gives no identifier beyond team+opponent+date, so which of several
  same-opponent fixtures moved can't be safely disambiguated; that case
  silently falls back to passive expiry (the stale UID just isn't
  re-included in the next feed, which compliant subscription clients
  treat as a delete on their next refresh anyway).
  `fetch_previous_state()` treats any failure to load the state file
  (first-ever run, network hiccup, corrupt file) as "no previous state"
  rather than aborting the run, and normalises a bare list into the
  `fixtures` half since that was the file's older shape.
- **Refuse to publish a partial feed.** `main()` aborts *before writing
  any file* if a team parses 0 fixtures while future-dated fixtures for
  that team were on record in the previous state. Zero fixtures for a
  team is legitimate once its season ends, but not while games that
  should still be on the page are known -- that means the parse broke,
  and publishing anyway would silently drop a whole team's games from
  every subscriber's calendar behind a green CI tick. Failing instead
  leaves the last good feed live. Don't "simplify" this into a bare
  count check: the previous-state comparison is what separates a
  broken parse from an empty schedule.

## Deployment (already done, for reference)
- Repo pushed via GitHub Desktop, public, on GitHub's free tier.
  Actions on public repos are free with no minute cap, so schedule
  frequency isn't a cost concern.
- Workflow: `.github/workflows/update-fixtures.yml`, cron
  `0 * * * *` (hourly), triggers via `workflow_dispatch` too.
- "Workflow permissions" under Settings -> Actions -> General needed
  to be "Read and write" for the `gh-pages` push step to succeed.
- Live feed URL pattern:
  `https://<username>.github.io/<repo>/gladiators-fixtures.ics`

## Testing scope (deliberate)
`test_generate_ics.py` covers only what outlives the current scraper:
`build_calendar()` takes a plain list of fixture dicts and knows nothing
about where they came from, so those tests still hold if fixtures later
arrive from a direct feed instead of scraped HTML. There are
intentionally **no** tests for `fetch_text()`, `parse_fixtures()` or the
regexes -- they get deleted along with the scraper, and a broken parse
already fails safe in production via `assert_team_coverage()`. Don't
"improve coverage" by adding them back without that changing.

The suite is mutation-checked: removing the tombstone carry-forward, the
live-UID collision guard, the repeat-opponent ambiguity guard, the team
guard's date comparison, the TTL's ISO-string conversion, or
re-duplicating the ticket URL into the description each make a specific
test fail. If you change this logic, re-check that the relevant test
still fails when you break it deliberately -- a passing test against
broken code is worse than no test.

Tests run in their own workflow (`tests.yml`) on push, **not** in
`update-fixtures.yml`. A failing test must not stop the hourly publish,
or the live feed goes stale instead of merely flagging a problem.

## If something looks wrong in the output
The most reliable debugging method used throughout this project: fetch
the real live page, save the *actual* extracted plain text to a local
file, and run `parse_fixtures()` against that directly, comparing
output line-by-line against what's visibly on the real site. Don't
trust synthetic/hand-typed test data alone -- multiple bugs here only
showed up against genuine live content.
