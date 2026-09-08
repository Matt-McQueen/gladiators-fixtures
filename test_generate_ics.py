"""
Tests for the parts of this pipeline that outlive the current scraper.

Deliberately scoped. `build_calendar()` takes a plain list of fixture
dicts and knows nothing about where they came from, so everything here
still holds if fixtures later arrive from a direct feed instead of
scraped HTML. There are no tests for `fetch_text()`, `parse_fixtures()`
or the regexes: those get deleted along with the scraper, and a broken
parse already fails safe in production via `assert_team_coverage()`.
Nothing here touches the network.

Run:  python -m unittest -v
"""

import unittest
from datetime import datetime, timedelta

from icalendar import vDuration

import generate_ics as gi


def days_ahead(n: int) -> str:
    """A date n days from now, in the site's DD/MM/YYYY form."""
    return (datetime.now(gi.TEAM_TZ) + timedelta(days=n)).strftime("%d/%m/%Y")


def fixture(
    opponent="Testville Rockets",
    *,
    date=None,
    time="7:30 pm",
    team_code="M",
    is_home=True,
    time_tbc=False,
):
    return {
        "team_code": team_code,
        "team_label": "Men's" if team_code == "M" else "Women's",
        "opponent": opponent,
        "date": date or days_ahead(30),
        "time": time,
        "location": gi.HOME_ADDRESS if is_home else "Some Away Arena",
        "is_home": is_home,
        "time_tbc": time_tbc,
    }


def state_record(team_code, date, opponent="Testville Rockets"):
    return {
        "team_code": team_code,
        "opponent": opponent,
        "date": date,
        "time": "7:30 pm",
        "is_home": True,
        "uid": f"{team_code}-{date}@test",
    }


def empty_state():
    return {"fixtures": [], "tombstones": []}


def build(fixtures, previous_state=None):
    return gi.build_calendar(fixtures, previous_state or empty_state())


def events(cal):
    return [c for c in cal.walk() if c.name == "VEVENT"]


def cancelled_uids(cal):
    return {str(e["uid"]) for e in events(cal) if str(e.get("status", "")) == "CANCELLED"}


def live_uids(cal):
    return {str(e["uid"]) for e in events(cal) if str(e.get("status", "")) != "CANCELLED"}


class TombstoneLifecycle(unittest.TestCase):
    """
    The one genuinely stateful, multi-run piece of logic here, and the only
    one that has actually shipped broken -- reasoning about it in your head
    demonstrably doesn't work.
    """

    def test_no_tombstone_without_previous_state(self):
        cal, stats, state = build([fixture()])
        self.assertEqual(stats["tombstones_emitted"], 0)
        self.assertEqual(cancelled_uids(cal), set())
        self.assertEqual(state["tombstones"], [])

    def test_postponement_tombstones_the_old_date(self):
        _, _, before = build([fixture(date=days_ahead(30))])
        old_uid = before["fixtures"][0]["uid"]

        cal, stats, after = build([fixture(date=days_ahead(44))], before)

        self.assertEqual(stats["postponed_detected"], 1)
        self.assertIn(old_uid, cancelled_uids(cal))
        self.assertNotIn(old_uid, live_uids(cal))
        # The rescheduled fixture itself is still published, uncancelled.
        self.assertIn(after["fixtures"][0]["uid"], live_uids(cal))

    def test_tombstone_survives_runs_with_no_further_change(self):
        """
        Regression: the tombstone was once emitted only on the run that
        detected the reschedule, so it sat in the feed for a single publish
        cycle and then vanished while the old date was often still weeks
        off. Any client polling less often than that missed it entirely.
        """
        new_date = days_ahead(44)
        _, _, before = build([fixture(date=days_ahead(30))])
        old_uid = before["fixtures"][0]["uid"]

        _, _, state = build([fixture(date=new_date)], before)

        for run in range(2, 6):
            cal, stats, state = build([fixture(date=new_date)], state)
            self.assertIn(
                old_uid, cancelled_uids(cal), f"tombstone vanished on run {run}"
            )
            self.assertEqual(stats["tombstones_emitted"], 1)
            self.assertEqual(stats["postponed_detected"], 0, "re-detected as new")
            self.assertEqual([r["uid"] for r in state["tombstones"]], [old_uid])

    def test_tombstone_retired_once_its_date_passes(self):
        stale = state_record("M", days_ahead(-3))
        previous = {"fixtures": [], "tombstones": [stale]}

        cal, stats, state = build([fixture()], previous)

        self.assertEqual(stats["tombstones_emitted"], 0)
        self.assertEqual(state["tombstones"], [])
        self.assertNotIn(stale["uid"], cancelled_uids(cal))

    def test_tombstone_never_contradicts_a_live_event(self):
        """A fixture moved back to a date it previously held."""
        date_a, date_b = days_ahead(30), days_ahead(44)
        _, _, s0 = build([fixture(date=date_a)])
        uid_a = s0["fixtures"][0]["uid"]
        _, _, s1 = build([fixture(date=date_b)], s0)
        uid_b = s1["fixtures"][0]["uid"]

        cal, _, _ = build([fixture(date=date_a)], s1)

        self.assertIn(uid_a, live_uids(cal))
        self.assertNotIn(uid_a, cancelled_uids(cal))
        # ...and the date it just vacated gets tombstoned instead.
        self.assertIn(uid_b, cancelled_uids(cal))

    def test_repeat_opponent_is_left_alone(self):
        """
        Two fixtures against the same opponent in a season can't be matched
        across runs (team+opponent+date is the only identity available), so
        a date change there must not cancel a guessed instance.
        """
        keep, moved_from, moved_to = days_ahead(30), days_ahead(60), days_ahead(75)
        _, _, before = build([fixture(date=keep), fixture(date=moved_from)])

        cal, stats, _ = build([fixture(date=keep), fixture(date=moved_to)], before)

        self.assertEqual(stats["postponed_detected"], 0)
        self.assertEqual(cancelled_uids(cal), set())

    def test_repeat_opponent_left_alone_when_one_drops_off(self):
        """
        The asymmetric case: two fixtures against an opponent last run, one
        this run because the other was played. Pairing the survivor against
        an arbitrary one of the two would tombstone a game that actually
        went ahead, as "CANCELLED (rescheduled)".
        """
        played, remaining = days_ahead(30), days_ahead(60)
        _, _, before = build([fixture(date=played), fixture(date=remaining)])

        cal, stats, _ = build([fixture(date=remaining)], before)

        self.assertEqual(stats["postponed_detected"], 0)
        self.assertEqual(cancelled_uids(cal), set())


class TeamCoverageGuard(unittest.TestCase):
    """
    Distinguishes "acquisition broke" from "this team's season ended".
    Both look like zero fixtures; only the previous state tells them apart.
    """

    def setUp(self):
        self.now = datetime.now(gi.TEAM_TZ)

    def test_aborts_when_team_empty_but_future_fixtures_known(self):
        previous = {"fixtures": [state_record("W", days_ahead(20))], "tombstones": []}

        with self.assertRaises(SystemExit) as caught:
            gi.assert_team_coverage({"M": 33, "W": 0}, previous, self.now)

        self.assertIn("Women's", str(caught.exception))

    def test_allows_empty_team_when_only_past_fixtures_known(self):
        previous = {"fixtures": [state_record("W", days_ahead(-20))], "tombstones": []}
        gi.assert_team_coverage({"M": 33, "W": 0}, previous, self.now)

    def test_allows_empty_team_on_first_run(self):
        gi.assert_team_coverage({"M": 33, "W": 0}, empty_state(), self.now)

    def test_another_teams_records_do_not_trigger_it(self):
        previous = {"fixtures": [state_record("M", days_ahead(20))], "tombstones": []}
        gi.assert_team_coverage({"M": 33, "W": 0}, previous, self.now)


class UidContract(unittest.TestCase):
    """
    The UID is a contract with every subscriber's calendar: too stable and
    a real change is missed, too volatile and everyone gets duplicates.
    """

    def uid_for(self, **kwargs):
        _, _, state = build([fixture(**kwargs)])
        return state["fixtures"][0]["uid"]

    def test_time_change_keeps_the_uid(self):
        date = days_ahead(30)
        self.assertEqual(
            self.uid_for(date=date, time="4:00 pm"),
            self.uid_for(date=date, time="7:30 pm"),
        )

    def test_date_change_changes_the_uid(self):
        self.assertNotEqual(
            self.uid_for(date=days_ahead(30)),
            self.uid_for(date=days_ahead(44)),
        )

    def test_team_code_disambiguates_same_opponent_and_date(self):
        date = days_ahead(30)
        self.assertNotEqual(
            self.uid_for(date=date, team_code="M"),
            self.uid_for(date=date, team_code="W"),
        )


class BusinessRules(unittest.TestCase):
    def only_event(self, fx):
        cal, _, _ = build([fx])
        found = events(cal)
        self.assertEqual(len(found), 1)
        return found[0]

    def test_home_wording(self):
        event = self.only_event(fixture("Liverpool", is_home=True))
        self.assertEqual(
            str(event["summary"]), "Caledonia Gladiators (M) vs Liverpool (Home)"
        )

    def test_away_wording_is_reversed(self):
        event = self.only_event(fixture("Liverpool", is_home=False))
        self.assertEqual(
            str(event["summary"]), "Liverpool vs Caledonia Gladiators (M) (Away)"
        )

    def test_ticket_url_on_home_fixtures_only(self):
        home = self.only_event(fixture(is_home=True))
        away = self.only_event(fixture(is_home=False))
        self.assertEqual(str(home["url"]), gi.TICKETS_URL)
        self.assertIsNone(away.get("url"))

    def test_ticket_url_not_duplicated_into_the_description(self):
        home = self.only_event(fixture(is_home=True))
        self.assertNotIn(gi.TICKETS_URL, str(home["description"]))

    def test_description_says_tip_off_not_kickoff(self):
        event = self.only_event(fixture())
        description = str(event["description"])
        self.assertIn("Tip-off", description)
        self.assertNotIn("kickoff", description.lower())

    def test_day_before_reminder(self):
        event = self.only_event(fixture())
        alarms = [c for c in event.walk() if c.name == "VALARM"]
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]["trigger"].dt, timedelta(days=-1))

    def test_fixture_whose_tip_off_has_passed_is_dropped(self):
        """
        The safety net the "already played" marker detection leans on, so
        an imperfect marker can't put a past game in the feed.
        """
        cal, stats, state = build([fixture(date=days_ahead(-1))])
        self.assertEqual(events(cal), [])
        self.assertEqual(stats["skipped_past"], 1)
        self.assertEqual(state["fixtures"], [])


class CalendarHeaders(unittest.TestCase):
    def setUp(self):
        self.cal, _, _ = build([fixture()])

    def test_published_ttl_is_an_iso8601_duration(self):
        """
        X-PUBLISHED-TTL is untyped, so handing it a timedelta silently
        emits "1:00:00" -- not a duration, and no error anywhere.
        """
        ttl = str(self.cal["x-published-ttl"])
        self.assertRegex(ttl, r"^P")
        self.assertEqual(ttl, vDuration(gi.REFRESH_INTERVAL).to_ical().decode())

    def test_refresh_interval_matches_the_ttl(self):
        refresh = self.cal["refresh-interval"]
        self.assertEqual(refresh.dt, gi.REFRESH_INTERVAL)
        self.assertEqual(refresh.params["VALUE"], "DURATION")


if __name__ == "__main__":
    unittest.main(verbosity=2)
