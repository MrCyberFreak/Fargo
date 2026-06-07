"""Roster builder — import confirmed FargoRate ids from a DigitalPool export.

DigitalPool is built on FargoRate, so its enriched player export already carries
the FargoRate identity we need. The reliable id lives at
`properties.fargo_data.readableId` (the same value the FargoRate API calls
`readableId` and this codebase calls `player_id`).

Do NOT use the top-level `fargo_id` — it is the membership number with leading
zeros stripped (e.g. `fargo_data.membershipId` "0053174" -> `fargo_id` 53174),
not the join key. `fargo_readable_id` is null on many rows. Only
`fargo_data.readableId` is trustworthy across the file.

This importer needs NO network — it reads the ndjson and writes roster.json. The
scheduled pull (src/pull.py) validates each id on first fetch and skips any that
404 (partial-failure policy), so a stale id costs one logged skip, not a crash.

The source ndjson is NOT committed: it contains emails/phone numbers (PII) at the
top level. We extract only the non-PII Fargo fields. Point --path at the export
wherever it lives.

Usage:
  python src/import_digitalpool.py --path "X:/.../co-players-enriched.ndjson"
  python src/import_digitalpool.py --path ... --state CO        # location filter
  python src/import_digitalpool.py --path ... --dry-run         # report only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from resolve import load_roster, save_roster

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = Path(r"X:\Claude_Code\Projectes\Digital Pool\co-players-enriched.ndjson")


def normalize(record: dict, state_filter: str | None) -> dict | None:
    """Extract a slim roster candidate from one DigitalPool record.

    Returns None if the record has no usable FargoRate id or is filtered out by
    state. Keying is always on `fargo_data.readableId`.
    """
    fd = (record.get("properties") or {}).get("fargo_data") or {}
    rid = fd.get("readableId")
    if rid in (None, ""):
        return None
    state = fd.get("state")
    if state_filter and state != state_filter:
        return None
    try:
        player_id = int(str(rid).strip())
    except (TypeError, ValueError):
        return None

    name = (record.get("name")
            or f"{fd.get('firstName', '')} {fd.get('lastName', '')}".strip()
            or None)
    membership = fd.get("membershipId")
    return {
        "player_id": player_id,
        "membership_id": str(membership) if membership not in (None, "") else None,
        "name": name,
        "state": state,
        "source": "digitalpool",
        "digitalpool_id": record.get("id"),
    }


def read_candidates(path: Path, state_filter: str | None) -> tuple[dict[int, dict], dict]:
    """Parse the ndjson into {player_id: candidate}, deduped. Returns (cands, stats)."""
    stats = {"lines": 0, "no_id": 0, "filtered_state": 0, "kept_rows": 0, "bad_json": 0}
    candidates: dict[int, dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            stats["lines"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                stats["bad_json"] += 1
                continue
            cand = normalize(record, state_filter)
            if cand is None:
                # Distinguish "no id" from "filtered by state" for a useful report.
                fd = (record.get("properties") or {}).get("fargo_data") or {}
                if fd.get("readableId") in (None, ""):
                    stats["no_id"] += 1
                else:
                    stats["filtered_state"] += 1
                continue
            stats["kept_rows"] += 1
            # Dedup by player_id; first occurrence wins (stable, reproducible).
            candidates.setdefault(cand["player_id"], cand)
    stats["unique"] = len(candidates)
    return candidates, stats


def merge_into_roster(candidates: dict[int, dict], today: str) -> dict:
    """Append new player_ids to roster.json. Existing entries are never touched."""
    roster = load_roster()
    players = roster.setdefault("players", {})
    summary = {"added": 0, "already_present": 0}
    for player_id, cand in candidates.items():
        key = str(player_id)
        if key in players:
            summary["already_present"] += 1
            continue
        players[key] = {**cand, "added_date": today}
        summary["added"] += 1
    if summary["added"]:
        save_roster(roster)
    summary["roster_total"] = len(players)
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Import FargoRate ids from a DigitalPool export.")
    p.add_argument("--path", type=Path, default=DEFAULT_PATH,
                   help="path to the DigitalPool ndjson export")
    p.add_argument("--state", default="CO",
                   help="keep only this fargo_data.state (blank to keep all)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be added without writing roster.json")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.path.exists():
        print(f"Source file not found: {args.path}", file=sys.stderr)
        return 1
    state_filter = args.state or None

    candidates, stats = read_candidates(args.path, state_filter)
    print(f"scanned {stats['lines']} lines from {args.path.name}")
    print(f"  no FargoRate id      : {stats['no_id']}")
    if state_filter:
        print(f"  filtered out (!={state_filter}) : {stats['filtered_state']}")
    print(f"  kept rows            : {stats['kept_rows']}")
    print(f"  unique player_ids    : {stats['unique']}")
    if stats["bad_json"]:
        print(f"  unparseable lines    : {stats['bad_json']}")

    if args.dry_run:
        roster = load_roster().get("players", {})
        new = [c for pid, c in candidates.items() if str(pid) not in roster]
        print(f"\n[dry-run] would add {len(new)} new players "
              f"({len(candidates) - len(new)} already in roster).")
        return 0

    today = dt.date.today().isoformat()
    summary = merge_into_roster(candidates, today)
    print(f"\nadded={summary['added']} already_present={summary['already_present']} "
          f"roster_total={summary['roster_total']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
