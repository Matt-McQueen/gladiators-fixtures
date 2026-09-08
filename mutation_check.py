"""
Checks that the test suite actually catches the bugs it claims to.

Breaks `generate_ics.py` in specific ways, in a throwaway copy, and
confirms the intended test fails each time. A test that passes against
broken code is worse than no test: it produces false confidence. Every
mutation below corresponds to a real defect -- most of them bugs this
project actually shipped at some point.

Run it after changing any of the logic these mutations touch:

    python mutation_check.py

It exits non-zero if a mutation survives (the test isn't pulling its
weight) or no longer applies (the source moved, so the pairing needs
rechecking by hand). Deliberately not wired into CI: the mutations match
exact source strings, so innocuous reformatting would fail the build
rather than telling you anything useful.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
TARGET = "generate_ics.py"
SUITE = "test_generate_ics.py"

# (label, find, replace, the test expected to fail)
MUTATIONS = [
    (
        "tombstone carry-forward removed",
        'pending = {rec["uid"]: rec for rec in previous_state["tombstones"]}',
        "pending = {}",
        "test_tombstone_survives_runs_with_no_further_change",
    ),
    (
        "live-UID collision guard removed",
        'if old_dt < now_local or rec["uid"] in written_uids:',
        "if old_dt < now_local:",
        "test_tombstone_never_contradicts_a_live_event",
    ),
    (
        "repeat-opponent ambiguity guard removed",
        "if len(prev_list) != 1:\n            continue  # repeat opponent last run",
        "if False:\n            continue  # repeat opponent last run",
        "test_repeat_opponent_left_alone_when_one_drops_off",
    ),
    (
        "team guard reduced to a bare count check",
        '            and fixture_datetime(rec["date"], rec["time"]) > now_local\n',
        "",
        "test_allows_empty_team_when_only_past_fixtures_known",
    ),
    (
        "TTL handed a timedelta instead of an ISO string",
        'cal.add("x-published-ttl", vDuration(REFRESH_INTERVAL).to_ical().decode())',
        'cal.add("x-published-ttl", REFRESH_INTERVAL)',
        "test_published_ttl_is_an_iso8601_duration",
    ),
    (
        "ticket URL duplicated back into the description",
        "f\"Tip-off {fx['time']}.\"",
        "f\"Tip-off {fx['time']}. Tickets: {TICKETS_URL}\"",
        "test_ticket_url_not_duplicated_into_the_description",
    ),
    (
        "TBC end-of-day exemption removed",
        'cutoff = dt.replace(hour=23, minute=59) if fx["time_tbc"] else dt',
        "cutoff = dt",
        "test_survives_the_afternoon_of_its_own_match_day",
    ),
    (
        "TBC end-of-day cutoff applied to every fixture",
        'cutoff = dt.replace(hour=23, minute=59) if fx["time_tbc"] else dt',
        'cutoff = dt.replace(hour=23, minute=59)',
        "test_exemption_does_not_leak_to_fixtures_with_a_known_time",
    ),
]


def failing_tests(output: str) -> set:
    """Test method names reported as FAIL or ERROR by unittest."""
    names = set()
    for line in output.splitlines():
        if line.startswith(("FAIL: ", "ERROR: ")):
            names.add(line.split(" ")[1].strip("()").split(".")[-1])
    return names


def total_tests(output: str) -> int:
    for line in output.splitlines():
        if line.startswith("Ran ") and " test" in line:
            return int(line.split(" ")[1])
    return 0


def run_mutation(find: str, replace: str) -> tuple:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shutil.copy(SOURCE_DIR / TARGET, tmp)
        shutil.copy(SOURCE_DIR / SUITE, tmp)

        source = (tmp / TARGET).read_text(encoding="utf-8")
        if find not in source:
            return None, set(), 0
        (tmp / TARGET).write_text(source.replace(find, replace, 1), encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "-v"],
            cwd=tmp, capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        return output, failing_tests(output), total_tests(output)


def main() -> int:
    width = max(len(label) for label, *_ in MUTATIONS)
    problems = []

    print(f"Mutating {TARGET}, expecting {SUITE} to notice.\n")
    for label, find, replace, expected in MUTATIONS:
        output, failed, ran = run_mutation(find, replace)

        if output is None:
            verdict = "NOT APPLIED"
            problems.append(f"{label}: source string no longer present -- recheck this pairing")
        elif expected not in failed:
            verdict = "SURVIVED"
            problems.append(f"{label}: {expected} did not fail -- that test isn't pulling its weight")
        elif ran and len(failed) == ran:
            # A syntax error fails everything, which would look like a catch.
            verdict = "TOO BROAD"
            problems.append(f"{label}: broke the whole suite ({ran} tests) -- likely invalid, not a targeted catch")
        else:
            verdict = "caught"

        print(f"  {label.ljust(width)}  {verdict.ljust(12)} {expected}")
        collateral = sorted(failed - {expected}) if output else []
        if collateral:
            print(f"  {''.ljust(width)}  also failed: {', '.join(collateral)}")

    print()
    if problems:
        print("PROBLEMS:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"All {len(MUTATIONS)} mutations caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
