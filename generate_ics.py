"""
Generates a single subscribable .ics calendar feed covering both the
Caledonia Gladiators men's and women's fixtures, scraped from:

    https://caledoniagladiators.com/mens-fixtures/
    https://caledoniagladiators.com/womens-fixtures/

PARSER DESIGN NOTE: `fetch_text()` extracts real plain text via
BeautifulSoup's get_text() -- there is NO markdown decoration in it
(no "##" headings, no "**bold**", no "[text](url)" link brackets, and
images contribute nothing since get_text() doesn't read `alt`
attributes). The parser is anchored on the literal "VS" line, which is
the one element every single rendering of every fixture reliably has --
unlike the combined header line ("Caledonia Gladiators (W) – Essex
Rebels – 04/10/2026" style), which several women's home fixtures are
missing entirely (confirmed by direct inspection). The two team names
sit immediately before and after "VS" in the plain text; whichever one
isn't literally "Caledonia Gladiators (M)" / "(W)" is the opponent.
Date/time are found by searching a small window of lines just before
"VS"; venue and played-status are found in a window just after it.

Since there's no dependency on the wp-json REST API, the ICS UID is a
hash of team+opponent+date (stable across time-of-day changes for the
same fixture, but a postponement to a different date will produce a new
UID). To stop a postponement from leaving a stale duplicate sitting in
subscribers' calendars, this script also publishes a small state file
(STATE_FILE/STATE_URL) recording each run's fixtures and any CANCELLED
tombstones still owed; the next run fetches it and, where a
team+opponent pairing unambiguously moved date, emits an explicit
CANCELLED tombstone for the old UID -- and keeps re-emitting it every
run until that old date passes, since a feed that carried it for only
one publish cycle would be missed by any client polling less often.
See the build_calendar() docstring for the exact rule and its one
deliberate gap (repeat opponents in a season can't be disambiguated).

Before relying on this long-term: check robots.txt / site terms, and
consider asking the club if they'd support an official feed instead.
"""

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from icalendar import Alarm, Calendar, Event
from icalendar.prop import vDuration
from zoneinfo import ZoneInfo

TEAMS = [
    {
        "code": "M",
        "label": "Men's",
        "url": "https://caledoniagladiators.com/mens-fixtures/",
    },
    {
        "code": "W",
        "label": "Women's",
        "url": "https://caledoniagladiators.com/womens-fixtures/",
    },
]

HOME_ADDRESS = "Playsport Arena, Stewartfield Way, East Kilbride G74 4GT"
TEAM_TZ = ZoneInfo("Europe/London")
GAME_DURATION = timedelta(hours=2)
# How often subscribers are told to poll. MUST track the workflow cron in
# .github/workflows/update-fixtures.yml: a value longer than the publish
# cadence just makes every client lag behind for no reason.
REFRESH_INTERVAL = timedelta(hours=1)
OUTPUT_FILE = "gladiators-fixtures.ics"
STATE_FILE = "fixtures_state.json"
STATE_URL = "https://matt-mcqueen.github.io/gladiators-fixtures/fixtures_state.json"
TICKETS_URL = "https://app.fanbaseclub.com/Fan/Dashboard?clubId=210"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FixtureFeedBot/1.0; +https://example.com/bot)"}

DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*[ap]m)\b", re.IGNORECASE)
VS_RE = re.compile(r"^VS$", re.MULTILINE)
CALEDONIA_RE = re.compile(r"^Caledonia Gladiators \([MW]\)$")
VENUE_RE = re.compile(r"—\s*(Home Game|.+)$", re.MULTILINE)
PLAYED_RE = re.compile(r"^(Buy Tickets|Match Recap)$", re.MULTILINE)


def fetch_text(url: str) -> str:
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.get_text("\n", strip=True)


def fixture_datetime(date_str: str, time_str: str) -> datetime:
    """A fixture's tip-off as an aware datetime, from the scraped strings."""
    dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %I:%M %p")
    return dt.replace(tzinfo=TEAM_TZ)


def fetch_previous_state() -> dict:
    """
    Best-effort load of the state file this script wrote on its previous
    run (published to STATE_URL alongside the .ics), shaped as
    {"fixtures": [...], "tombstones": [...]}. Used to detect postponements
    and to keep their CANCELLED tombstones alive -- see build_calendar().

    Any failure (first-ever run before the file exists, a network hiccup, a
    corrupt file) is treated as "no previous state" rather than aborting
    the run: losing postponement detection for one cycle is a minor,
    self-correcting degradation, not worth failing the feed over.

    A bare list is accepted as the "fixtures" half, since that was the
    file's shape before tombstones were carried forward -- this is network
    input, so it gets normalised rather than trusted.
    """
    try:
        resp = requests.get(STATE_URL, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"NOTE: couldn't load previous state ({exc}) -- skipping postponement detection for this run.")
        return {"fixtures": [], "tombstones": []}

    if isinstance(payload, list):
        return {"fixtures": payload, "tombstones": []}
    return {
        "fixtures": payload.get("fixtures", []),
        "tombstones": payload.get("tombstones", []),
    }


def parse_fixtures(text: str) -> list[dict]:
    """
    Find each unique upcoming fixture. The site renders each fixture 2-3
    times, so we dedupe on (opponent, date), keeping the first
    occurrence.

    Anchored on the literal "VS" line, which is the one element every
    single rendering of every fixture reliably has -- unlike the
    combined header line (missing for several women's home fixtures) or
    any markdown-style decoration (this scrapes real plain text via
    BeautifulSoup, which has no "##"/"**"/brackets at all). The two team
    names sit immediately before and after "VS"; whichever one isn't
    literally "Caledonia Gladiators (M)"/"(W)" is the opponent.
    """
    lines = [ln for ln in text.split("\n") if ln.strip()]
    seen = set()
    fixtures = []

    for i, line in enumerate(lines):
        if line.strip() != "VS":
            continue

        before = lines[i - 1].strip() if i - 1 >= 0 else ""
        after = lines[i + 1].strip() if i + 1 < len(lines) else ""

        before_is_cal = bool(CALEDONIA_RE.match(before))
        after_is_cal = bool(CALEDONIA_RE.match(after))
        if before_is_cal == after_is_cal:
            continue  # neither/both matched Caledonia -- not a real fixture VS line
        opponent = after if before_is_cal else before

        # Date/time sit a few lines before "VS" (the date/time heading,
        # then optionally the combined header line, then the two team
        # names). Search a small bounded window rather than assuming an
        # exact offset, since the combined header is sometimes absent.
        lookback = "\n".join(lines[max(0, i - 6): i])
        date_matches = DATE_RE.findall(lookback)
        if not date_matches:
            continue
        date_str = date_matches[-1]  # nearest one to "VS"

        key = (opponent, date_str)
        if key in seen:
            continue
        seen.add(key)

        time_m = TIME_RE.search(lookback)
        time_str = time_m.group(1) if time_m else "12:00 pm"
        time_tbc = time_m is None

        # Venue / played-status sit a few lines after "VS".
        lookahead = "\n".join(lines[i + 1: i + 8])

        played_m = PLAYED_RE.search(lookahead)
        if played_m and played_m.group(1) == "Match Recap":
            continue  # already played

        venue_m = VENUE_RE.search(lookahead)
        venue = venue_m.group(1).strip() if venue_m else "TBC"
        is_home = venue == "Home Game"
        location = HOME_ADDRESS if is_home else venue

        fixtures.append(
            {
                "opponent": opponent,
                "date": date_str,
                "time": time_str,
                "location": location,
                "is_home": is_home,
                "time_tbc": time_tbc,
            }
        )

    return fixtures


def build_calendar(fixtures: list[dict], previous_state: dict) -> tuple[Calendar, dict, dict]:
    """
    `fixtures` items must also carry 'team_code' ('M'/'W') and 'team_label'.

    `previous_state` is last run's state (see STATE_FILE), shaped as
    {"fixtures": [...], "tombstones": [...]}.

    Postponement handling: if a (team, opponent) pairing had exactly one
    fixture last run and exactly one this run but the date changed, that's
    an unambiguous reschedule, so an explicit CANCELLED tombstone is
    emitted for the old UID/date. Subscribers' calendars then drop the
    stale entry, rather than this relying solely on the old UID quietly
    not being re-included -- compliant clients treat that as a delete, but
    not every ICS consumer does.

    Tombstones are carried forward in the state file and re-emitted every
    run until the old slot's date has passed. Emitting one only on the run
    that detected the change would put it in the feed for a single publish
    cycle, which a client polling less often than that can miss entirely
    -- leaving precisely the non-compliant clients a tombstone exists for
    stuck with a permanent wrong-date event.

    Deliberately NOT handled: a (team, opponent) pairing with more than
    one fixture in the group (e.g. a repeat opponent played home and away
    in the same season). The site gives no identifier beyond
    team+opponent+date, so which of several same-opponent fixtures moved
    can't be safely disambiguated -- attempting it risks cancelling the
    wrong instance. That case just falls back to the old passive-expiry
    behaviour.

    Returns (calendar, stats, current_state) where current_state is this
    run's state dict, meant to be persisted as STATE_FILE for the next
    run to compare against.
    """
    cal = Calendar()
    cal.add("prodid", "-//Caledonia Gladiators Fixtures//caledoniagladiators.com//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Caledonia Gladiators Fixtures")
    cal.add("x-wr-timezone", "Europe/London")
    # Both spellings of the same hint, from one constant so they can't
    # drift: REFRESH-INTERVAL is the RFC 7986 property (and needs a
    # timedelta), X-PUBLISHED-TTL is the older extension Outlook honours
    # (untyped, so it needs the ISO 8601 string -- handed a timedelta it
    # silently renders "6:00:00", which is not a valid duration).
    cal.add("refresh-interval", REFRESH_INTERVAL, parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", vDuration(REFRESH_INTERVAL).to_ical().decode())

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TEAM_TZ)
    stats = {
        "written": 0,
        "home": 0,
        "away": 0,
        "skipped_past": 0,
        "postponed_detected": 0,
        "tombstones_emitted": 0,
    }
    current_fixtures = []
    written_uids = set()

    for fx in fixtures:
        dt = fixture_datetime(fx["date"], fx["time"])

        # Belt-and-braces: the page's own "Match Recap" marker already
        # keeps played games out of `fixtures`, but this catches anything
        # that marker missed (or a fixture whose tip-off time has simply
        # passed since the page was last scraped).
        if dt < now_local:
            stats["skipped_past"] += 1
            continue

        team_tag = f"({fx['team_code']})"
        team_name = f"Caledonia Gladiators {team_tag}"

        # UID keyed on team+opponent+date (not time), so a tip-off-time
        # change updates the existing event instead of creating a
        # duplicate. A postponement to a different date will produce a
        # new UID. Team code is included so the men's and women's teams
        # playing the same opponent don't collide.
        uid_source = f"{fx['team_code'].lower()}-{fx['opponent'].lower()}-{fx['date']}"
        uid = hashlib.sha1(uid_source.encode()).hexdigest() + "@caledoniagladiators.com"

        home_away_tag = "Home" if fx["is_home"] else "Away"
        if fx["is_home"]:
            summary = f"{team_name} vs {fx['opponent']} (Home)"
        else:
            summary = f"{fx['opponent']} vs {team_name} (Away)"
        if fx["time_tbc"]:
            summary += " (time TBC)"

        description = (
            f"Super League Basketball -- {fx['team_label']} fixture ({home_away_tag.lower()}). "
            f"Tip-off {fx['time']}."
        )
        if fx["time_tbc"]:
            description += " Tip-off time not yet published -- shown as a placeholder."

        event = Event()
        event.add("uid", uid)
        event.add("dtstart", dt)
        event.add("dtend", dt + GAME_DURATION)
        event.add("dtstamp", now_utc)
        event.add("summary", summary)
        event.add("location", fx["location"])
        # Custom properties so team/home-away are machine-readable by
        # anything that parses the feed, not just visible in the text.
        event.add("x-fixture-team", fx["team_code"])
        event.add("x-fixture-home-away", home_away_tag.upper())
        event.add("description", description)
        if fx["is_home"]:
            event.add("url", TICKETS_URL)

        # Reminder the day before tip-off. Google Calendar ignores VALARMs
        # on subscribed feeds (it applies the subscriber's own default
        # notification instead), but Apple Calendar and Outlook honour
        # them.
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("trigger", timedelta(days=-1))
        alarm.add("description", f"Reminder: {summary}")
        event.add_component(alarm)

        cal.add_component(event)

        current_fixtures.append(
            {
                "team_code": fx["team_code"],
                "opponent": fx["opponent"],
                "date": fx["date"],
                "time": fx["time"],
                "is_home": fx["is_home"],
                "uid": uid,
            }
        )
        written_uids.add(uid)

        stats["written"] += 1
        stats["home" if fx["is_home"] else "away"] += 1

    prev_groups = defaultdict(list)
    for rec in previous_state["fixtures"]:
        prev_groups[(rec["team_code"], rec["opponent"])].append(rec)

    current_groups = defaultdict(list)
    for fx in fixtures:
        current_groups[(fx["team_code"], fx["opponent"])].append(fx)

    # Tombstones still owed from earlier runs, keyed by UID so a repeat
    # detection can't double-add one.
    pending = {rec["uid"]: rec for rec in previous_state["tombstones"]}

    for key, prev_list in prev_groups.items():
        if len(prev_list) != 1:
            continue  # repeat opponent last run -- can't safely disambiguate
        current_list = current_groups.get(key)
        if not current_list or len(current_list) != 1:
            continue  # repeat opponent now, or fixture gone entirely (played/cancelled)
        old, new = prev_list[0], current_list[0]
        if old["date"] != new["date"] and old["uid"] not in pending:
            pending[old["uid"]] = old
            stats["postponed_detected"] += 1

    still_pending = []
    for rec in pending.values():
        old_dt = fixture_datetime(rec["date"], rec["time"])
        # Retire a tombstone once its old slot has passed (the stale event
        # is then in the past and harmless), and never contradict a live
        # event on the same UID -- which happens if a fixture is moved
        # back to the date it originally held.
        if old_dt < now_local or rec["uid"] in written_uids:
            continue

        old_team_name = f"Caledonia Gladiators ({rec['team_code']})"
        if rec["is_home"]:
            old_summary = f"{old_team_name} vs {rec['opponent']} (Home)"
        else:
            old_summary = f"{rec['opponent']} vs {old_team_name} (Away)"

        cancel_event = Event()
        cancel_event.add("uid", rec["uid"])
        cancel_event.add("dtstart", old_dt)
        cancel_event.add("dtend", old_dt + GAME_DURATION)
        cancel_event.add("dtstamp", now_utc)
        cancel_event.add("sequence", 1)
        cancel_event.add("status", "CANCELLED")
        cancel_event.add("summary", f"CANCELLED (rescheduled) -- {old_summary}")
        cal.add_component(cancel_event)

        still_pending.append(rec)
        stats["tombstones_emitted"] += 1

    return cal, stats, {"fixtures": current_fixtures, "tombstones": still_pending}


def assert_team_coverage(per_team_counts: dict, previous_state: dict, now_local: datetime) -> None:
    """
    Raise SystemExit if a team's fixtures have silently vanished.

    A team parsing zero fixtures is legitimate once its season is over, but
    not while future-dated fixtures for that team are still on record from
    a previous run -- those should still be published, so their
    disappearance means acquisition broke rather than the schedule
    emptying. That distinction is the whole point of this check: don't
    reduce it to a bare count, or an ended season starts failing every run.

    main() calls this before writing any file, so a broken run leaves the
    last good feed published rather than replacing it with one that's
    missing a whole team behind a successful-looking exit.
    """
    for team in TEAMS:
        if per_team_counts[team["code"]]:
            continue
        expected = [
            rec
            for rec in previous_state["fixtures"]
            if rec["team_code"] == team["code"]
            and fixture_datetime(rec["date"], rec["time"]) > now_local
        ]
        if expected:
            expected.sort(key=lambda rec: fixture_datetime(rec["date"], rec["time"]))
            nxt = expected[0]
            raise SystemExit(
                f"Parsed 0 {team['label']} fixtures, but {len(expected)} future fixture(s) for that "
                f"team were on record last run (next: {nxt['opponent']} on {nxt['date']}). The page "
                f"structure has probably changed -- refusing to publish a partial feed."
            )


def main():
    previous_state = fetch_previous_state()

    all_fixtures = []
    per_team_counts = {}

    for team in TEAMS:
        text = fetch_text(team["url"])
        fixtures = parse_fixtures(text)
        for fx in fixtures:
            fx["team_code"] = team["code"]
            fx["team_label"] = team["label"]
        all_fixtures.extend(fixtures)
        per_team_counts[team["code"]] = len(fixtures)
        print(f"Parsed {len(fixtures)} {team['label']} fixture(s) from {team['url']}")

    assert_team_coverage(
        per_team_counts, previous_state, datetime.now(timezone.utc).astimezone(TEAM_TZ)
    )

    if not all_fixtures:
        raise SystemExit("No upcoming fixtures parsed -- the page structure may have changed.")

    cal, stats, current_state = build_calendar(all_fixtures, previous_state)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(cal.to_ical())
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(current_state, f, indent=2)

    if stats["skipped_past"]:
        print(
            f"NOTE: {stats['skipped_past']} fixture(s) had a tip-off time already in the past "
            f"and were excluded (the page's own \"already played\" marker should catch these "
            f"already -- if this number is consistently large, the marker detection may need "
            f"a look, though the output file itself is still correct either way)."
        )
    if stats["postponed_detected"]:
        print(
            f"NOTE: {stats['postponed_detected']} fixture(s) newly detected as rescheduled since "
            f"the last run."
        )
    if stats["tombstones_emitted"]:
        print(
            f"NOTE: {stats['tombstones_emitted']} CANCELLED tombstone(s) included in this feed, so "
            f"subscribers' calendars drop the stale entry. Each is re-published every run until its "
            f"old date passes."
        )
    print(
        f"Wrote {stats['written']} fixtures to {OUTPUT_FILE} "
        f"({stats['home']} home, {stats['away']} away)"
    )


if __name__ == "__main__":
    main()
