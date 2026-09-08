# Caledonia Gladiators Fixtures Feed

## What this project does
Scrapes the Caledonia Gladiators' men's and women's basketball fixtures
pages and generates a single subscribable `.ics` calendar feed. A
GitHub Actions workflow re-runs the script on a schedule and publishes
the result to GitHub Pages, so calendar apps subscribed to that URL
pick up fixture changes (time, venue, postponements) automatically.

- `generate_ics.py` -- the whole pipeline: fetch, parse, build calendar
- `requirements.txt` -- requests, beautifulsoup4, icalendar
- `.github/workflows/update-fixtures.yml` -- runs on a cron schedule,
  publishes `gladiators-fixtures.ics` to the `gh-pages` branch

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
  tickets for Gladiators' own venue, so it's only included in the
  `description` for home fixtures.
- **"Tip-off" not "kickoff"** -- these are basketball games. Wording in
  descriptions and comments should say tip-off.
- **UID** = `sha1(team_code + opponent + date)`, i.e. stable across a
  same-day time change, but a postponement to a different date
  produces a new UID (old event ages out via the past-date filter on
  the next run once its would-be time has passed).
- **Team code included in UID** so the men's and women's teams playing
  the same opponent on the same date (unlikely but possible) don't
  collide.

## Deployment (already done, for reference)
- Repo pushed via GitHub Desktop, public, on GitHub's free tier.
  Actions on public repos are free with no minute cap, so schedule
  frequency isn't a cost concern.
- Workflow: `.github/workflows/update-fixtures.yml`, cron
  `0 */6 * * *` (every 6 hours), triggers via `workflow_dispatch` too.
- "Workflow permissions" under Settings -> Actions -> General needed
  to be "Read and write" for the `gh-pages` push step to succeed.
- Live feed URL pattern:
  `https://<username>.github.io/<repo>/gladiators-fixtures.ics`

## If something looks wrong in the output
The most reliable debugging method used throughout this project: fetch
the real live page, save the *actual* extracted plain text to a local
file, and run `parse_fixtures()` against that directly, comparing
output line-by-line against what's visibly on the real site. Don't
trust synthetic/hand-typed test data alone -- multiple bugs here only
showed up against genuine live content.
