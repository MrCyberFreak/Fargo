"""Roster source — cross-reference an APA player master list to FargoRate.

Unlike DigitalPool (built on FargoRate, so every row already carries the
`readableId` join key), an APA export has **no FargoRate id and no state** — only
APA's own ids and the player's name. So the identity bridge is name-based:

  1. `crossref` (no network) — read the APA master json, clean names, and split
     the roster into three buckets by *name*:
       - matched     : APA name == exactly one rostered player's name. Attach the
                       APA membership(s) to that roster entry as a cross-link.
       - ambiguous   : APA name matches a rostered name held by >1 player_id.
                       Cannot pick safely → reported, not written.
       - new         : APA name not in the roster → queued for resolution.
     Writes the new-name queue to docs/resolve/apa_to_resolve.json and a summary
     to docs/resolve/apa_crossref_report.json.
  2. `resolve` (network, runs on a GitHub Actions runner) — for each queued name,
     search FargoRate and keep only `State == CO` candidates (the file has no
     state, so CO is how we disambiguate). Classify into resolved (exactly one CO
     match) / ambiguous (>1) / unfound (0) and write the review file
     docs/resolve/apa_resolution.json. **Nothing is added to roster.json here** —
     resolved matches are staged for human review (see CLAUDE.md decision).

Name collisions are real (the roster already has 40), which is exactly why the
project keys on player_id, never name. Every name-based link records
`match_method: "name"` so it stays auditable.

A single human can hold several APA memberships ("multiple skill levels"); all of
them are kept on the one resolved player rather than collapsed to one.

The raw APA file lives under basket/ and is git-ignored (names + member numbers +
session history). Only the curated extraction is committed.

Usage:
  python src/import_apa.py crossref                      # default basket/ path
  python src/import_apa.py crossref --path <file> --dry-run
  python src/import_apa.py resolve                       # network; runner only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from resolve import load_roster, save_roster

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "basket" / "players_master.json"
RESOLVE_DIR = ROOT / "docs" / "resolve"
TO_RESOLVE_PATH = RESOLVE_DIR / "apa_to_resolve.json"
REPORT_PATH = RESOLVE_DIR / "apa_crossref_report.json"
RESOLUTION_PATH = RESOLVE_DIR / "apa_resolution.json"

# League-fee annotations get prepended to some names, e.g. "Owes $150 Anna Byrd",
# "OWES$130 Jordan Freeman", "Owes $180Aaron Knobloch". Strip them off the front.
_FEE_PREFIX = re.compile(r"(?i)^\s*owes?\s*\$?\s*\d+\s*")


def clean_name(raw: str | None) -> str:
    """Strip fee prefixes and collapse whitespace; preserve the real name."""
    if not raw:
        return ""
    s = _FEE_PREFIX.sub("", raw)
    return re.sub(r"\s+", " ", s).strip()


def norm(name: str | None) -> str:
    """Normalize for name matching: lowercase, drop punctuation, collapse spaces."""
    if not name:
        return ""
    s = clean_name(name).lower()
    s = s.replace("'", "")            # O'Neill -> oneill (drop, don't split)
    s = re.sub(r"[.\-]", " ", s)      # hyphens/periods -> space
    return re.sub(r"\s+", " ", s).strip()


def membership_of(rec: dict) -> dict:
    """Slim, non-redundant APA membership descriptor stored as a cross-link."""
    return {
        "member_id": rec.get("memberId"),
        "member_number": rec.get("memberNumber"),
        "name": clean_name(rec.get("displayName")),
        "first_session": rec.get("firstSession"),
        "last_session": rec.get("lastSession"),
        "sessions_count": len(rec.get("sessions") or []),
    }


def load_apa(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def group_by_name(apa: dict) -> dict[str, list[dict]]:
    """normalized name -> [APA records]. One human can hold several memberships."""
    groups: dict[str, list[dict]] = {}
    for rec in apa.get("players", {}).values():
        key = norm(rec.get("displayName"))
        if not key:
            continue
        groups.setdefault(key, []).append(rec)
    return groups


def roster_name_index(roster: dict) -> dict[str, list[str]]:
    """normalized name -> [player_id, ...] from the existing roster."""
    index: dict[str, list[str]] = {}
    for pid, rec in roster.get("players", {}).items():
        index.setdefault(norm(rec.get("name")), []).append(pid)
    return index


def crossref(path: Path, dry_run: bool, today: str) -> dict:
    """Bucket APA names against the roster; write cross-links + the resolve queue."""
    apa = load_apa(path)
    roster = load_roster()
    by_name = group_by_name(apa)
    rindex = roster_name_index(roster)

    matched: list[dict] = []     # one roster id; cross-link attached
    ambiguous: list[dict] = []   # roster name held by >1 id; left for review
    to_resolve: list[dict] = []  # not in roster; queued for FargoRate search
    links_written = 0

    for name_key in sorted(by_name):
        recs = by_name[name_key]
        memberships = [membership_of(r) for r in recs]
        pids = rindex.get(name_key, [])

        if len(pids) == 1:
            pid = pids[0]
            matched.append({"player_id": int(pid), "name": recs[0].get("displayName"),
                            "memberships": memberships})
            if not dry_run:
                links_written += _attach_crosslink(roster, pid, memberships, today)
        elif len(pids) > 1:
            ambiguous.append({"name": clean_name(recs[0].get("displayName")),
                              "roster_player_ids": [int(p) for p in pids],
                              "memberships": memberships})
        else:
            to_resolve.append({"search_name": clean_name(recs[0].get("displayName")),
                               "norm": name_key, "memberships": memberships})

    report = {
        "generated_at": today,
        "source_file": path.name,
        "apa_players": len(apa.get("players", {})),
        "unique_apa_names": len(by_name),
        "matched_single": len(matched),
        "matched_crosslinks_written": links_written,
        "ambiguous_existing": len(ambiguous),
        "new_to_resolve": len(to_resolve),
        "ambiguous_detail": ambiguous,
    }

    if not dry_run:
        if links_written:
            save_roster(roster)
        RESOLVE_DIR.mkdir(parents=True, exist_ok=True)
        TO_RESOLVE_PATH.write_text(
            json.dumps(to_resolve, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def _attach_crosslink(roster: dict, pid: str, memberships: list[dict], today: str) -> int:
    """Add an `apa` cross-link list to one roster entry. Strictly additive:
    existing fields are never touched; re-running only adds new member_ids.
    Returns the number of memberships newly linked."""
    entry = roster["players"][pid]
    existing = entry.setdefault("apa", [])
    have = {m.get("member_id") for m in existing}
    added = 0
    for m in memberships:
        if m["member_id"] in have:
            continue
        existing.append({**m, "source": "apa", "match_method": "name", "added_date": today})
        have.add(m["member_id"])
        added += 1
    if not existing:                      # nothing to keep — drop the empty key
        entry.pop("apa", None)
    return added


def resolve(today: str) -> dict:
    """Search FargoRate for each queued name; classify by CO matches. Network."""
    import fargo_api  # imported lazily — crossref must run without network

    if not TO_RESOLVE_PATH.exists():
        print(f"No resolve queue at {TO_RESOLVE_PATH}; run crossref first.", file=sys.stderr)
        raise SystemExit(1)
    queue = json.loads(TO_RESOLVE_PATH.read_text(encoding="utf-8"))
    session = fargo_api.new_session()

    resolved: list[dict] = []
    ambiguous: list[dict] = []
    unfound: list[dict] = []
    errors = 0

    for i, item in enumerate(queue, 1):
        name = item["search_name"]
        try:
            results = fargo_api.search(name, session=session)
        except Exception as exc:  # one bad name must not sink the batch
            errors += 1
            print(f"  [{i}/{len(queue)}] ERROR {name!r}: {exc}", file=sys.stderr)
            unfound.append({**item, "error": str(exc)})
            continue
        co = [r for r in results if (r.location or "").upper() == "CO"]
        if len(co) == 1:
            r = co[0]
            resolved.append({"search_name": name, "player_id": r.player_id,
                             "fargo_name": r.name, "membership_id": r.membership_id,
                             "rating": r.rating, "robustness": r.robustness,
                             "rating_quality": r.rating_quality, "location": r.location,
                             "memberships": item["memberships"]})
        elif len(co) > 1:
            ambiguous.append({"search_name": name,
                              "candidates": [{"player_id": r.player_id, "name": r.name,
                                              "rating": r.rating, "robustness": r.robustness,
                                              "location": r.location} for r in co],
                              "memberships": item["memberships"]})
        else:
            unfound.append({"search_name": name,
                            "other_state_matches": len(results),
                            "memberships": item["memberships"]})
        if i % 100 == 0:
            print(f"  ...{i}/{len(queue)} searched "
                  f"(resolved={len(resolved)} ambiguous={len(ambiguous)} unfound={len(unfound)})")

    out = {
        "generated_at": today,
        "queue_size": len(queue),
        "resolved": resolved,
        "ambiguous": ambiguous,
        "unfound": unfound,
        "errors": errors,
    }
    RESOLVE_DIR.mkdir(parents=True, exist_ok=True)
    RESOLUTION_PATH.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cross-reference an APA master list to FargoRate.")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("crossref", help="bucket APA names vs roster; write resolve queue (no network)")
    c.add_argument("--path", type=Path, default=DEFAULT_PATH, help="path to the APA master json")
    c.add_argument("--dry-run", action="store_true", help="report only; write nothing")

    sub.add_parser("resolve", help="search FargoRate for queued names (network; runner)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today().isoformat()

    if args.command == "crossref":
        if not args.path.exists():
            print(f"APA source not found: {args.path}", file=sys.stderr)
            return 1
        rep = crossref(args.path, args.dry_run, today)
        tag = "[dry-run] " if args.dry_run else ""
        print(f"{tag}APA players={rep['apa_players']} unique_names={rep['unique_apa_names']}")
        print(f"  matched (1 roster id)  : {rep['matched_single']} "
              f"(crosslinks written: {rep['matched_crosslinks_written']})")
        print(f"  ambiguous vs roster    : {rep['ambiguous_existing']}")
        print(f"  new -> to resolve      : {rep['new_to_resolve']}")
        if not args.dry_run:
            print(f"\nwrote {TO_RESOLVE_PATH.relative_to(ROOT)} and {REPORT_PATH.relative_to(ROOT)}")
        return 0

    if args.command == "resolve":
        out = resolve(today)
        print(f"\nresolved={len(out['resolved'])} ambiguous={len(out['ambiguous'])} "
              f"unfound={len(out['unfound'])} errors={out['errors']}")
        print(f"wrote {RESOLUTION_PATH.relative_to(ROOT)}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
