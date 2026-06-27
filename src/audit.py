"""Read-only identity audits + cross-source merge-candidate emitter.

None of these MUTATE roster.json / people.json -- each emits a review file under
docs/resolve/. See docs/cross-league-identity.md section 5.

  collisions       -- inverted index source_member_key -> {fargo_ids}; any key on >1
                      Fargo id means one source player was linked to two humans. HARD
                      invariant: it must be empty (the CLI returns nonzero if not).
  merge-candidates -- a league name attached (via cross-links) to >1 distinct Fargo id
                      is *evidence* (not proof) of one human with two Fargo accounts.
                      Emits docs/resolve/people_merge_candidates.json for human review.
                      NEVER writes people_merges.json (that stays manual + curated).
  name-divergence  -- a cross-link whose name normalizes incompatibly with the Fargo
                      entry's name (surname differs, or first name not nickname/prefix
                      compatible) -- a mis-attachment smell.
  merge-sanity     -- re-run the union-find; flag any merged person whose ESTABLISHED
                      constituent ids are rated far apart or sit in different states.

The per-source member key matches people.py / crosswalk exactly: APA member_id,
NAPA napa_player_id, BCA synthetic `<division>:<norm name>`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import people
from namematch import first_compatible, first_name, norm, surname

ROOT = Path(__file__).resolve().parent.parent
ROSTER_PATH = ROOT / "roster.json"
MERGES_PATH = ROOT / "people_merges.json"
RESOLVE_DIR = ROOT / "docs" / "resolve"
COLLISIONS_PATH = RESOLVE_DIR / "audit_collisions.json"
NAME_DIVERGENCE_PATH = RESOLVE_DIR / "audit_name_divergence.json"
MERGE_SANITY_PATH = RESOLVE_DIR / "audit_merge_sanity.json"
MERGE_CANDIDATES_PATH = RESOLVE_DIR / "people_merge_candidates.json"

RATING_SPREAD_FLAG = 100   # established ids in one person rated this far apart -> flag


def _load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _roster_players() -> dict:
    return _load(ROSTER_PATH, {"players": {}}).get("players", {})


def _write(path: Path, payload: dict) -> None:
    RESOLVE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def source_member_key(source: str, m: dict) -> str | None:
    """The stable per-source identity key (matches people.py / crosswalk)."""
    if source == "apa":
        return None if m.get("member_id") is None else str(m["member_id"])
    if source == "napa":
        return None if m.get("napa_player_id") is None else str(m["napa_player_id"])
    if source == "bca":
        return f"{m.get('bca_division_id')}:{norm(m.get('name'))}"
    return None


def _crosslinks(entry: dict):
    """Yield (source, member_key, membership) for every cross-link on an entry."""
    for source in ("apa", "napa", "bca"):
        for m in entry.get(source, []) or []:
            key = source_member_key(source, m)
            if key is not None:
                yield source, key, m


def _fid_to_person(players: dict, merges: list) -> dict[int, int]:
    """fargo_player_id -> person_id (union-find root), so merged accounts of one human
    collapse to one person. Mirrors people.py exactly."""
    out: dict[int, int] = {}
    for root, ids in people._group_player_ids(players, merges).items():
        for i in ids:
            out[i] = root
    return out


def collisions(today: str) -> dict:
    """One source member linked to >1 distinct PERSON (the hard invariant; must be []).

    A member on two Fargo ids that are MERGED (people_merges.json) is the same human
    with two accounts -- people.py dedups it -- so that is NOT a collision; it is
    reported separately as `redundant_same_person` (informational, the link could be
    pruned to one account but is harmless)."""
    players = _roster_players()
    merges = _load(MERGES_PATH, [])
    fid_person = _fid_to_person(players, merges)

    index: dict[tuple[str, str], set[int]] = {}
    for pid, entry in players.items():
        fid = int(entry.get("player_id", pid))
        for source, key, _m in _crosslinks(entry):
            index.setdefault((source, key), set()).add(fid)

    bad, redundant = [], []
    for (s, k), fids in sorted(index.items()):
        persons = {fid_person.get(f, f) for f in fids}
        if len(persons) > 1:
            bad.append({"source": s, "member_key": k, "fargo_ids": sorted(fids),
                        "person_ids": sorted(persons)})
        elif len(fids) > 1:
            redundant.append({"source": s, "member_key": k, "fargo_ids": sorted(fids),
                              "person_id": next(iter(persons))})
    payload = {"generated_at": today, "collision_count": len(bad), "collisions": bad,
               "redundant_same_person_count": len(redundant),
               "redundant_same_person": redundant}
    _write(COLLISIONS_PATH, payload)
    return payload


def merge_candidates(today: str) -> dict:
    """Cross-source: a league name attached to >1 distinct Fargo id -> review candidate."""
    players = _roster_players()
    by_norm: dict[str, dict[int, set[str]]] = {}
    display: dict[str, str] = {}
    for pid, entry in players.items():
        fid = int(entry.get("player_id", pid))
        for source, _key, m in _crosslinks(entry):
            nm = m.get("name")
            if not nm:
                continue
            k = norm(nm)
            by_norm.setdefault(k, {}).setdefault(fid, set()).add(source)
            display.setdefault(k, nm)

    cands = []
    for k, fids in sorted(by_norm.items()):
        if len(fids) > 1:
            cands.append({"norm": k, "name": display[k],
                          "fargo_ids": [{"fargo_player_id": fid, "sources": sorted(srcs)}
                                        for fid, srcs in sorted(fids.items())]})
    payload = {"generated_at": today, "candidate_count": len(cands),
               "note": "evidence only -- confirm by hand into people_merges.json; never auto-merged",
               "candidates": cands}
    _write(MERGE_CANDIDATES_PATH, payload)
    return payload


def name_divergence(today: str) -> dict:
    """Cross-links whose name is incompatible with the Fargo entry name (mis-attach smell)."""
    players = _roster_players()
    flags = []
    for pid, entry in players.items():
        fid = int(entry.get("player_id", pid))
        ename = entry.get("name") or ""
        es, ef = surname(ename), first_name(ename)
        for source, _key, m in _crosslinks(entry):
            nm = m.get("name")
            if not nm:
                continue
            if surname(nm) != es or not first_compatible(first_name(nm), ef):
                flags.append({"fargo_player_id": fid, "fargo_name": ename,
                              "source": source, "link_name": nm})
    payload = {"generated_at": today, "flag_count": len(flags), "flags": flags}
    _write(NAME_DIVERGENCE_PATH, payload)
    return payload


def merge_sanity(today: str) -> dict:
    """Re-run the union-find; flag merged people whose established ids are far apart in
    rating or sit in different states."""
    players = _roster_players()
    merges = _load(MERGES_PATH, [])
    groups = people._group_player_ids(players, merges)
    latest = people.latest_ratings()

    flags = []
    for root, ids in groups.items():
        if len(ids) < 2:
            continue
        established = []
        states = set()
        for i in ids:
            e = players.get(str(i), {})
            st = (e.get("state") or "").strip()
            if st:
                states.add(st)
            r = latest.get(str(i))
            if r:
                try:
                    rating, rob = int(r[0]), int(r[1])
                except (TypeError, ValueError):
                    continue
                if rob >= 200:
                    established.append({"fargo_player_id": i, "rating": rating})
        flag: dict = {}
        if len(established) >= 2:
            spread = max(e["rating"] for e in established) - min(e["rating"] for e in established)
            if spread >= RATING_SPREAD_FLAG:
                flag["rating_spread"] = spread
                flag["established"] = sorted(established, key=lambda e: e["rating"])
        if len(states) > 1:
            flag["states"] = sorted(states)
        if flag:
            flag.update({"person_id": root, "fargo_player_ids": sorted(ids)})
            flags.append(flag)
    payload = {"generated_at": today, "flag_count": len(flags), "flags": flags}
    _write(MERGE_SANITY_PATH, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only cross-league identity audits.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("collisions", help="inverted-index collision check (hard invariant)")
    sub.add_parser("merge-candidates", help="emit cross-source people_merge_candidates.json")
    sub.add_parser("name-divergence", help="flag incompatible cross-link names")
    sub.add_parser("merge-sanity", help="flag suspicious merged people")
    sub.add_parser("all", help="run every audit")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today().isoformat()

    if args.command in ("collisions", "all"):
        out = collisions(today)
        print(f"collisions: {out['collision_count']} -> {COLLISIONS_PATH.relative_to(ROOT)}")
        if args.command == "collisions":
            return 1 if out["collision_count"] else 0
    if args.command in ("merge-candidates", "all"):
        out = merge_candidates(today)
        print(f"merge-candidates: {out['candidate_count']} -> {MERGE_CANDIDATES_PATH.relative_to(ROOT)}")
    if args.command in ("name-divergence", "all"):
        out = name_divergence(today)
        print(f"name-divergence: {out['flag_count']} -> {NAME_DIVERGENCE_PATH.relative_to(ROOT)}")
    if args.command in ("merge-sanity", "all"):
        out = merge_sanity(today)
        print(f"merge-sanity: {out['flag_count']} -> {MERGE_SANITY_PATH.relative_to(ROOT)}")
    if args.command == "all":
        # the collision invariant is the only hard failure
        return 1 if collisions(today)["collision_count"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
