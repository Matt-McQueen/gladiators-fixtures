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
UID).

Before relying on this long-term: check robots.txt / site terms, and
consider asking the club if they'd support an official feed instead.
"""

import hashlib
import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
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
OUTPUT_FILE = "gladiators-fixtures.ics"
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


def build_calendar(fixtures: list[dict]) -> tuple[Calendar, dict]:
    """`fixtures` items must also carry 'team_code' ('M'/'W') and 'team_label'."""
    cal = Calendar()
    cal.add("prodid", "-//Caledonia Gladiators Fixtures//caledoniagladiators.com//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Caledonia Gladiators Fixtures")
    cal.add("x-wr-timezone", "Europe/London")
    cal.add("x-published-ttl", "PT12H")

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TEAM_TZ)
    stats = {"written": 0, "home": 0, "away": 0, "skipped_past": 0}

    for fx in fixtures:
        dt = datetime.strptime(f"{fx['date']} {fx['time']}", "%d/%m/%Y %I:%M %p")
        dt = dt.replace(tzinfo=TEAM_TZ)

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
        cal.add_component(event)

        stats["written"] += 1
        stats["home" if fx["is_home"] else "away"] += 1

    return cal, stats


def main():
    all_fixtures = []
    per_team_counts = {}

    for team in TEAMS:
        text = fetch_text(team["url"])
        fixtures = parse_fixtures(text)
        for fx in fixtures:
            fx["team_code"] = team["code"]
            fx["team_label"] = team["label"]
        all_fixtures.extend(fixtures)
        per_team_counts[team["label"]] = len(fixtures)
        print(f"Parsed {len(fixtures)} {team['label']} fixture(s) from {team['url']}")

    if not all_fixtures:
        raise SystemExit("No upcoming fixtures parsed -- the page structure may have changed.")

    cal, stats = build_calendar(all_fixtures)
    with open(OUTPUT_FILE, "wb") as f:
        f.write(cal.to_ical())

    if stats["skipped_past"]:
        print(
            f"NOTE: {stats['skipped_past']} fixture(s) had a tip-off time already in the past "
            f"and were excluded (the page's own \"already played\" marker should catch these "
            f"already -- if this number is consistently large, the marker detection may need "
            f"a look, though the output file itself is still correct either way)."
        )
    print(
        f"Wrote {stats['written']} fixtures to {OUTPUT_FILE} "
        f"({stats['home']} home, {stats['away']} away)"
    )


if __name__ == "__main__":
    main()
