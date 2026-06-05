"""Phase 2 — the scheduled pull (build plan §7; rules in CLAUDE.md §Recording).

For each rostered player, look up the current rating/robustness by player_id and
apply the recording rules:

  * never seen before            -> append a `baseline` row
  * rating OR robustness differs -> append a `change` row
  * both identical               -> write nothing

Only `rating` and `robustness` trigger a row. `rating_quality` is recorded for
visibility (so the preliminary->established transition shows up in the log) but
is NOT an independent trigger.

Partial-failure policy: one player's fetch failure is logged and skipped; the
players that succeeded are still recorded and committed. The process exits
non-zero ONLY when *every* rostered player failed — a systemic API/network
problem worth a red run. A no-op run (nothing changed) writes no rows, so the
workflow's `git diff` guard produces no commit. Re-running the same day is
idempotent.

The core is `run_pull(...)`, which takes its roster, history path and fetch
function as arguments so it can be exercised against temp files with a fake
client in tests/ — no network required. `main()` wires in the real paths and
the live API client.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Callable

import fargo_api
from fargo_api import FargoApiError, PlayerRecord

ROOT = Path(__file__).resolve().parent.parent
ROSTER_PATH = ROOT / "roster.json"
HISTORY_PATH = ROOT / "data" / "history.csv"

FIELDNAMES = [
    "date_found", "player_id", "readable_id", "name",
    "rating", "robustness", "rating_quality", "entry_type",
]

FetchFn = Callable[..., PlayerRecord]


def load_roster(path: Path) -> dict:
    """Return the {player_id_str: entry} mapping, preserving insertion order."""
    data = json.loads(path.read_text())
    return data.get("players", {})


def load_last_entries(path: Path) -> dict[int, dict]:
    """Most-recent history row per player_id.

    history.csv is append-only and chronological, so the last row seen for a
    given player_id is that player's current standing.
    """
    last: dict[int, dict] = {}
    if not path.exists():
        return last
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                last[int(row["player_id"])] = row
            except (KeyError, ValueError):
                continue
    return last


def append_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def run_pull(
    roster: dict,
    history_path: Path,
    *,
    fetch: FetchFn | None = None,
    today: str | None = None,
    session=None,
) -> dict:
    """Apply the recording rules for every rostered player. Returns a summary."""
    fetch = fetch or fargo_api.get_player
    today = today or dt.date.today().isoformat()
    last_entries = load_last_entries(history_path)

    summary = {"checked": 0, "baselined": 0, "changed": 0, "unchanged": 0, "failed": 0}
    new_rows: list[dict] = []

    for key, entry in roster.items():
        pid = int(entry["player_id"])
        name_hint = entry.get("name", key)
        summary["checked"] += 1

        try:
            rec = fetch(pid, session=session)
        except FargoApiError as exc:
            summary["failed"] += 1
            print(f"FAIL   {pid} {name_hint}: {exc}")
            continue

        prev = last_entries.get(pid)
        if prev is not None:
            unchanged = (
                int(prev["rating"]) == rec.rating
                and int(prev["robustness"]) == rec.robustness
            )
            if unchanged:
                summary["unchanged"] += 1
                print(f"OK     {pid} {rec.name}: unchanged ({rec.rating}/{rec.robustness})")
                continue
            entry_type = "change"
        else:
            entry_type = "baseline"

        new_rows.append({
            "date_found": today,
            "player_id": rec.player_id,
            "readable_id": rec.membership_id or "",
            "name": rec.name,
            "rating": rec.rating,
            "robustness": rec.robustness,
            "rating_quality": rec.rating_quality,
            "entry_type": entry_type,
        })

        if entry_type == "baseline":
            summary["baselined"] += 1
            print(f"BASE   {pid} {rec.name}: {rec.rating}/{rec.robustness} ({rec.rating_quality})")
        else:
            summary["changed"] += 1
            print(f"CHANGE {pid} {rec.name}: "
                  f"{prev['rating']}/{prev['robustness']} -> {rec.rating}/{rec.robustness}")

    if new_rows:
        append_rows(history_path, new_rows)

    summary["new_rows"] = len(new_rows)
    return summary


def main() -> int:
    roster = load_roster(ROSTER_PATH)
    session = fargo_api.new_session()

    # Live runs pace themselves between players (tests inject a fake fetch and
    # never hit this path, so they stay instant).
    def paced_fetch(pid, session=None):
        rec = fargo_api.get_player(pid, session=session)
        time.sleep(fargo_api.REQUEST_DELAY)
        return rec

    summary = run_pull(roster, HISTORY_PATH, fetch=paced_fetch, session=session)

    print("\n--- run summary ---")
    print(
        f"checked={summary['checked']} baselined={summary['baselined']} "
        f"changed={summary['changed']} unchanged={summary['unchanged']} "
        f"failed={summary['failed']} new_rows={summary['new_rows']}"
    )

    if summary["checked"] > 0 and summary["failed"] == summary["checked"]:
        print("ERROR: every player failed — systemic problem; exiting non-zero.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
