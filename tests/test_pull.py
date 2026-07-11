"""Behavioral tests for the recording rules (build plan acceptance §11).

These exercise run_pull() against a temp history.csv with a fake fetch function,
so they need no network and never touch the real data file. They cover the
acceptance criteria whose demonstration would otherwise pollute the production
record (a change row, a partial failure):

  #3 baseline     -> first sighting writes one baseline row
  #4 no-op        -> identical values write nothing
  #5 change       -> a differing value writes exactly one change row
  #6 partial fail -> one player's failure is skipped; others still recorded
  (+) all-fail    -> main()'s exit-code policy
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fargo_api  # noqa: E402
from fargo_api import FargoApiError, PlayerRecord, quality_for  # noqa: E402
import pull  # noqa: E402


def make_rec(player_id, rating, robustness, name="Test Player"):
    return PlayerRecord(
        player_id=player_id,
        membership_id=f"M{player_id}",
        name=name,
        rating=rating,
        robustness=robustness,
        location="CO",
        rating_quality=quality_for(robustness),
        row_id=f"guid-{player_id}",
        raw={},
    )


def roster_of(*ids):
    return {str(i): {"player_id": i, "name": f"Player {i}"} for i in ids}


def read_rows(path):
    import csv
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def fetch_from(table):
    """Build a fake fetch that returns canned records / raises for missing ids."""
    def _fetch(pid, session=None):
        if pid not in table:
            raise FargoApiError(f"forced failure for {pid}")
        return table[pid]
    return _fetch


def test_baseline_first_sighting(tmp_path):
    hist = tmp_path / "history.csv"
    fetch = fetch_from({1310533: make_rec(1310533, 438, 63, "Nathan Carroll")})

    summary = pull.run_pull(roster_of(1310533), hist, fetch=fetch, today="2026-06-05")

    assert summary["baselined"] == 1 and summary["new_rows"] == 1
    rows = read_rows(hist)
    assert len(rows) == 1
    assert rows[0]["entry_type"] == "baseline"
    assert rows[0]["rating"] == "438" and rows[0]["robustness"] == "63"
    assert rows[0]["readable_id"] == "M1310533"
    assert rows[0]["rating_quality"] == "preliminary"  # 63 < 200


def test_no_change_writes_nothing(tmp_path):
    hist = tmp_path / "history.csv"
    fetch = fetch_from({1310533: make_rec(1310533, 438, 63)})

    pull.run_pull(roster_of(1310533), hist, fetch=fetch, today="2026-06-05")
    second = pull.run_pull(roster_of(1310533), hist, fetch=fetch, today="2026-06-06")

    assert second["unchanged"] == 1 and second["new_rows"] == 0
    assert len(read_rows(hist)) == 1  # still just the baseline


def test_change_appends_one_row(tmp_path):
    hist = tmp_path / "history.csv"
    pull.run_pull(roster_of(1310533), hist,
                  fetch=fetch_from({1310533: make_rec(1310533, 438, 63)}), today="2026-06-05")

    # rating moves 438 -> 441
    summary = pull.run_pull(roster_of(1310533), hist,
                            fetch=fetch_from({1310533: make_rec(1310533, 441, 70)}), today="2026-06-10")

    assert summary["changed"] == 1 and summary["new_rows"] == 1
    rows = read_rows(hist)
    assert len(rows) == 2
    assert rows[-1]["entry_type"] == "change"
    assert rows[-1]["rating"] == "441" and rows[-1]["robustness"] == "70"


def test_robustness_only_change_is_recorded(tmp_path):
    hist = tmp_path / "history.csv"
    pull.run_pull(roster_of(1310533), hist,
                  fetch=fetch_from({1310533: make_rec(1310533, 438, 63)}), today="2026-06-05")
    # rating same, robustness differs -> still a change
    summary = pull.run_pull(roster_of(1310533), hist,
                            fetch=fetch_from({1310533: make_rec(1310533, 438, 64)}), today="2026-06-06")
    assert summary["changed"] == 1
    assert read_rows(hist)[-1]["robustness"] == "64"


def test_partial_failure_records_others(tmp_path):
    hist = tmp_path / "history.csv"
    # player 1 succeeds, player 2 is absent from the table -> fetch raises
    fetch = fetch_from({1: make_rec(1, 500, 250, "Good Player")})

    summary = pull.run_pull(roster_of(1, 2), hist, fetch=fetch, today="2026-06-05")

    assert summary["failed"] == 1
    assert summary["baselined"] == 1
    rows = read_rows(hist)
    assert len(rows) == 1 and rows[0]["player_id"] == "1"
    assert rows[0]["rating_quality"] == "established"  # 250 >= 200


def test_parallel_fetch_preserves_order_and_rules(tmp_path):
    # workers>1 fetches concurrently; history.csv must still come out in roster
    # order (append-only diff stability) with the recording rules unchanged.
    # Slow the low ids so, without order preservation, they'd land last.
    import time

    ids = [5, 4, 3, 2, 1]
    table = {i: make_rec(i, 400 + i, 100 + i, f"Player {i}") for i in ids}

    def slow_fetch(pid, session=None):
        time.sleep(0.02 * pid)  # higher ids finish first
        return table[pid]

    hist = tmp_path / "history.csv"
    summary = pull.run_pull(
        roster_of(*ids), hist, fetch=slow_fetch, today="2026-06-05", workers=5
    )

    assert summary["baselined"] == 5 and summary["new_rows"] == 5
    rows = read_rows(hist)
    # roster order is [5,4,3,2,1] — preserved despite finish order being reversed
    assert [r["player_id"] for r in rows] == ["5", "4", "3", "2", "1"]
    assert [r["rating"] for r in rows] == ["405", "404", "403", "402", "401"]


def test_parallel_partial_failure(tmp_path):
    # A failing fetch among concurrent workers is skipped; the rest still record.
    hist = tmp_path / "history.csv"
    fetch = fetch_from({1: make_rec(1, 500, 250), 3: make_rec(3, 300, 40)})

    summary = pull.run_pull(
        roster_of(1, 2, 3), hist, fetch=fetch, today="2026-06-05", workers=4
    )

    assert summary["failed"] == 1 and summary["baselined"] == 2
    rows = read_rows(hist)
    assert [r["player_id"] for r in rows] == ["1", "3"]


def test_all_fail_exits_nonzero(tmp_path, monkeypatch):
    hist = tmp_path / "history.csv"
    monkeypatch.setattr(pull, "ROSTER_PATH", tmp_path / "roster.json")
    monkeypatch.setattr(pull, "HISTORY_PATH", hist)
    (tmp_path / "roster.json").write_text(
        '{"players": {"2": {"player_id": 2, "name": "Missing"}}}'
    )

    def always_fail(pid, session=None):
        raise FargoApiError("boom")

    monkeypatch.setattr(fargo_api, "get_player", always_fail)
    assert pull.main() == 1  # every player failed -> red run


def test_some_succeed_exits_zero(tmp_path, monkeypatch):
    hist = tmp_path / "history.csv"
    monkeypatch.setattr(pull, "ROSTER_PATH", tmp_path / "roster.json")
    monkeypatch.setattr(pull, "HISTORY_PATH", hist)
    (tmp_path / "roster.json").write_text(
        '{"players": {"1": {"player_id": 1, "name": "Ok"},'
        ' "2": {"player_id": 2, "name": "Missing"}}}'
    )

    def one_ok(pid, session=None):
        if pid == 1:
            return make_rec(1, 400, 100)
        raise FargoApiError("missing")

    monkeypatch.setattr(fargo_api, "get_player", one_ok)
    assert pull.main() == 0  # at least one succeeded -> green run
