"""Phase 1 — resolve player names to stable FargoRate IDs (build plan §6).

Names collide (there can be several "Nathan Carroll"s), so resolution is a
deliberate confirm-the-match step done once per player. After that the pull
keys on `player_id` forever and never re-searches by name.

Commands:
  python src/resolve.py search "Nathan Carroll"      list candidate matches
  python src/resolve.py search "Nathan" --write      ...and save them to docs/resolve/
  python src/resolve.py add 1310533                  add a confirmed id to roster.json
  python src/resolve.py add-name "Nathan Carroll"    search + interactively pick (TTY)

roster.json is keyed on the integer `player_id` and stores the full API record
plus an `added_date`. Re-running only appends new players; existing entries are
never disturbed (idempotent).

Resolution needs internet to reach the API. From a machine with open internet
just run it directly. From the web/Actions-only setup, use the `fargo-resolve`
workflow (.github/workflows/resolve.yml), which runs this same script on a
runner and commits the results back for review.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import fargo_api
from fargo_api import FargoApiError, PlayerRecord

ROOT = Path(__file__).resolve().parent.parent
ROSTER_PATH = ROOT / "roster.json"
RESOLVE_DIR = ROOT / "docs" / "resolve"


def load_roster() -> dict:
    if ROSTER_PATH.exists():
        return json.loads(ROSTER_PATH.read_text())
    return {"players": {}}


def save_roster(roster: dict) -> None:
    ROSTER_PATH.write_text(json.dumps(roster, indent=2, ensure_ascii=False) + "\n")


def _format_candidate(rec: PlayerRecord) -> str:
    return (
        f"  id={rec.player_id}  {rec.name}  "
        f"rating={rec.rating} ({rec.rating_quality})  robustness={rec.robustness}  "
        f"loc={rec.location or '?'}  membership={rec.membership_id or '?'}"
    )


def cmd_search(name: str, write: bool, session) -> list[PlayerRecord]:
    results = fargo_api.search(name, session=session)
    if not results:
        print(f"No matches for {name!r}.")
    else:
        print(f"{len(results)} match(es) for {name!r}:")
        for rec in results:
            print(_format_candidate(rec))
    if write:
        RESOLVE_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "query"
        out = RESOLVE_DIR / f"candidates-{slug}.json"
        out.write_text(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False) + "\n")
        print(f"Wrote {len(results)} candidate(s) -> {out.relative_to(ROOT)}")
    return results


def add_player(rec: PlayerRecord) -> bool:
    """Append a confirmed player to roster.json. Returns True if newly added."""
    roster = load_roster()
    players = roster.setdefault("players", {})
    key = str(rec.player_id)
    if key in players:
        print(f"Player {rec.player_id} ({rec.name}) already in roster — leaving as-is.")
        return False
    players[key] = {
        "player_id": rec.player_id,
        "membership_id": rec.membership_id,
        "name": rec.name,
        "added_date": dt.date.today().isoformat(),
        "record": rec.raw,
    }
    save_roster(roster)
    print(f"Added {rec.player_id} ({rec.name}) to roster.")
    return True


def cmd_add_id(player_id: str, session) -> int:
    try:
        rec = fargo_api.get_player(player_id, session=session)
    except FargoApiError as exc:
        print(f"Could not fetch player {player_id}: {exc}", file=sys.stderr)
        return 1
    add_player(rec)
    return 0


def cmd_add_name(name: str, session) -> int:
    results = cmd_search(name, write=False, session=session)
    if not results:
        return 1
    if len(results) == 1:
        only = results[0]
        if sys.stdin.isatty():
            ans = input(f"Add the single match {only.player_id} ({only.name})? [y/N] ").strip().lower()
            if ans != "y":
                print("Aborted.")
                return 1
        add_player(only)
        return 0
    if not sys.stdin.isatty():
        print("\nMultiple matches and no TTY to choose. Re-run with:  "
              "python src/resolve.py add <id>", file=sys.stderr)
        return 2
    choice = input("Enter the id to add (or blank to abort): ").strip()
    if not choice:
        print("Aborted.")
        return 1
    match = next((r for r in results if str(r.player_id) == choice), None)
    if match is None:
        print(f"{choice} is not one of the listed candidates.", file=sys.stderr)
        return 1
    add_player(match)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Resolve FargoRate player names to ids.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="list candidate matches for a name")
    s.add_argument("name")
    s.add_argument("--write", action="store_true", help="save candidates under docs/resolve/")

    a = sub.add_parser("add", help="add a confirmed player_id to roster.json")
    a.add_argument("player_id")

    n = sub.add_parser("add-name", help="search a name and confirm the match interactively")
    n.add_argument("name")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    session = fargo_api.new_session()
    if args.command == "search":
        cmd_search(args.name, write=args.write, session=session)
        return 0
    if args.command == "add":
        return cmd_add_id(args.player_id, session=session)
    if args.command == "add-name":
        return cmd_add_name(args.name, session=session)
    return 2


if __name__ == "__main__":
    sys.exit(main())
