"""Person/profile layer — group FargoRate player_ids into one human.

`roster.json` is the flat **scrape list**: one entry per FargoRate `player_id`,
and the daily pull keys on `player_id` (unchanged — see CLAUDE.md). But a single
human can hold several FargoRate accounts (duplicate registrations) and several
league memberships (APA today, NAPA/others later). `people.json` is the **master
profile file**: one entry per *person*, owning all their player_ids + memberships
+ source notes. Every player_id stays in roster.json and is still scraped daily;
the profile just *aggregates* them.

`people.json` is GENERATED (idempotent, no network) from:
  - `roster.json`         — every player_id, its name, source, league cross-links
  - `people_merges.json`  — human-confirmed "these player_ids are one person"
New roster additions flow in automatically on the next `build`; a person is never
duplicated (player_ids group by merge, else stand alone).

Membership records are source-tagged (`{"source": "apa", ...}`) so additional
leagues plug in with no schema change.

Commands:
  python src/people.py build       regenerate people.json from roster + merges
  python src/people.py profiles    render docs/profiles.md (merged people; --all for everyone)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

from namematch import norm

ROOT = Path(__file__).resolve().parent.parent
ROSTER_PATH = ROOT / "roster.json"
MERGES_PATH = ROOT / "people_merges.json"
PEOPLE_PATH = ROOT / "people.json"
HISTORY_PATH = ROOT / "data" / "history.csv"
PROFILES_PATH = ROOT / "docs" / "profiles.md"
CROSSWALK_PATH = ROOT / "docs" / "crosswalk.json"

# Source-tagged league cross-link lists that hang on a roster entry.
_CROSSLINK_SOURCES = ("apa", "napa", "bca")


def _load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _sources_of(entry: dict) -> set[str]:
    """Where this roster entry's identity came from."""
    s: set[str] = set()
    if entry.get("source"):
        s.add(entry["source"])
    if entry.get("record") and not entry.get("source"):
        s.add("resolve")          # added via resolve.py (full API record, no source tag)
    for src in _CROSSLINK_SOURCES:
        if entry.get(src):
            s.add(src)
    return s or {"unknown"}


def _bca_member_id(league: str, parent_name: str) -> str:
    """BCA carries NO per-player id, so synthesize a stable key from league slug +
    the parent roster entry's name (norm'd).  One link can cover multiple leagues,
    so callers emit one membership row per league.

    Key format: ``<league>:<norm(parent_name)>`` — matches the crosswalk contract in
    docs/cross-league-identity.md section 6 (``<division_id>:<norm_name>``; the bca
    importer uses league slugs rather than numeric division ids)."""
    return f"{league}:{norm(parent_name or '')}"


def _memberships_of(entry: dict) -> list[dict]:
    """Source-tagged league memberships from a roster entry. Each source emits a
    stable `member_id` so build()'s `(source, member_id)` dedup is correct: APA uses
    its member_id, NAPA the napa_player_id, BCA a synthetic league+name key.

    One BCA link can span multiple leagues; we emit one membership record per league
    so each gets its own crosswalk key and none are silently collapsed."""
    out = []
    for m in entry.get("apa", []) or []:
        out.append({"source": m.get("source", "apa"), "member_id": m.get("member_id"),
                    "member_number": m.get("member_number"), "name": m.get("name"),
                    "match_method": m.get("match_method")})
    for m in entry.get("napa", []) or []:
        out.append({"source": "napa", "member_id": m.get("napa_player_id"),
                    "member_number": None, "name": m.get("name"),
                    "division_id": m.get("division_id"),
                    "match_method": m.get("match_method"), "confidence": m.get("confidence")})
    parent_name = entry.get("name") or ""
    for m in entry.get("bca", []) or []:
        leagues = m.get("leagues") or []
        if not leagues:
            # No league slugs: fall back to a single entry keyed by empty league
            out.append({"source": "bca", "member_id": _bca_member_id("", parent_name),
                        "member_number": None, "name": parent_name,
                        "division_id": None,
                        "match_method": m.get("match_method"), "confidence": m.get("confidence")})
        else:
            for league in leagues:
                out.append({"source": "bca", "member_id": _bca_member_id(league, parent_name),
                            "member_number": None, "name": parent_name,
                            "division_id": league,
                            "match_method": m.get("match_method"), "confidence": m.get("confidence")})
    return out


def _group_player_ids(roster: dict, merges: list[dict]) -> dict[int, list[int]]:
    """Union-find over player_ids; the smallest id in a group is its root."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)   # keep the smallest id as root

    for pid in roster:
        find(int(pid))
    for m in merges:
        ids = [int(i) for i in m["fargo_player_ids"] if str(i) in roster]
        for other in ids[1:]:
            union(ids[0], other)

    groups: dict[int, list[int]] = {}
    for pid in roster:
        groups.setdefault(find(int(pid)), []).append(int(pid))
    return groups


def build(today: str) -> dict:
    """Generate people.json from roster.json + people_merges.json."""
    roster = _load(ROSTER_PATH, {"players": {}}).get("players", {})
    merges = _load(MERGES_PATH, [])
    groups = _group_player_ids(roster, merges)
    note_by_root = {min(int(i) for i in m["fargo_player_ids"]): m.get("reason", "") for m in merges}

    people: dict[str, dict] = {}
    for root, ids in groups.items():
        ids = sorted(ids)
        entries = [roster[str(i)] for i in ids]
        memberships: list[dict] = []
        seen: set = set()
        sources: set[str] = set()
        dates: list[str] = []
        for e in entries:
            sources |= _sources_of(e)
            if e.get("added_date"):
                dates.append(e["added_date"])
            for mem in _memberships_of(e):
                key = (mem["source"], mem.get("member_id"))
                if key not in seen:
                    seen.add(key)
                    memberships.append(mem)
        people[str(root)] = {
            "person_id": root,
            "display_name": entries[0].get("name") or str(root),
            "fargo_player_ids": ids,
            "memberships": memberships,
            "sources": sorted(sources),
            "notes": note_by_root.get(root, ""),
            "added_date": min(dates) if dates else today,
        }

    out = {
        "generated_at": today,
        "person_count": len(people),
        "merged_count": sum(1 for p in people.values() if len(p["fargo_player_ids"]) > 1),
        "people": dict(sorted(people.items(), key=lambda kv: int(kv[0]))),
    }
    PEOPLE_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def latest_ratings() -> dict[str, tuple[str, str, str]]:
    """player_id -> (rating, robustness, date) from the last history.csv row for it
    (history is append-only in chronological order, so the last write wins)."""
    latest: dict[str, tuple[str, str, str]] = {}
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                latest[row["player_id"]] = (row["rating"], row["robustness"], row["date_found"])
    return latest


def _render_profile(p: dict, latest: dict) -> list[str]:
    lines = [f"## {p['display_name']}"]
    if len(p["fargo_player_ids"]) > 1:
        lines.append(f"_merged profile — {len(p['fargo_player_ids'])} FargoRate accounts_")
    for fid in p["fargo_player_ids"]:
        r = latest.get(str(fid))
        rating = f"rating {r[0]}, robustness {r[1]} (as of {r[2]})" if r else "pending first pull"
        lines.append(f"- FargoRate `{fid}` — {rating}")
    for m in p["memberships"]:
        # APA shows its public member number; NAPA/BCA have none, so show the id +
        # division (BCA's member_id is a synthetic division:name key).
        ident = m.get("member_number") or m.get("member_id")
        div = f" div {m['division_id']}" if m.get("division_id") else ""
        lines.append(f"- {m['source'].upper()} {ident}{div} ({m.get('match_method') or 'n/a'})")
    lines.append(f"- sources: {', '.join(p['sources'])}")
    if p.get("notes"):
        lines.append(f"- note: {p['notes']}")
    lines.append("")
    return lines


def profiles(today: str, all_people: bool) -> int:
    """Render profile cards to docs/profiles.md. By default only people with >1
    FargoRate account or >1 source (the cases the profile layer exists for);
    --all renders everyone."""
    people = _load(PEOPLE_PATH, {"people": {}}).get("people", {})
    latest = latest_ratings()
    chosen = [p for p in people.values()
              if all_people or len(p["fargo_player_ids"]) > 1 or len(p["sources"]) > 1]
    chosen.sort(key=lambda p: p["display_name"].lower())

    scope = "all" if all_people else "merged / multi-source"
    lines = [f"# Player profiles ({len(chosen)} of {len(people)} people — {scope})",
             "", f"_generated {today}; ratings from data/history.csv_", ""]
    for p in chosen:
        lines += _render_profile(p, latest)
    PROFILES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(chosen)


def _confidence(m: dict) -> str:
    """A cross-link's confidence: its own field if set (NAPA/BCA), else derived from
    match_method (APA has none -- variant is medium, everything else high)."""
    return m.get("confidence") or ("medium" if m.get("match_method") == "variant" else "high")


def _row_fids(row: dict) -> set[int]:
    """FargoRate ids a crosswalk row represents (singular row or ambiguous-plural row)."""
    if row.get("fargo_player_id") is not None:
        return {int(row["fargo_player_id"])}
    return {int(x) for x in row.get("fargo_player_ids", [])}


def crosswalk(today: str) -> dict:
    """Publish docs/crosswalk.json -- the slim, PII-free source-id -> FargoRate map
    PoolPredict consumes instead of joining by name+state (see
    docs/cross-league-identity.md section 6). Each source id maps to its
    fargo_player_id (rating grain), person_id (the human, for pooling accounts),
    confidence, and match_method."""
    roster = _load(ROSTER_PATH, {"players": {}}).get("players", {})
    people = _load(PEOPLE_PATH, {"people": {}}).get("people", {})

    fargo_to_person: dict[int, int] = {}
    for person in people.values():
        for fid in person["fargo_player_ids"]:
            fargo_to_person[int(fid)] = person["person_id"]

    out: dict[str, dict] = {"apa": {}, "napa": {}, "bca": {}}
    for pid_str, entry in roster.items():
        fid = int(entry.get("player_id", pid_str))
        person_id = fargo_to_person.get(fid)
        for m in entry.get("apa", []) or []:
            if m.get("member_id") is not None:
                out["apa"][str(m["member_id"])] = {
                    "fargo_player_id": fid, "person_id": person_id,
                    "confidence": _confidence(m), "match_method": m.get("match_method")}
        for m in entry.get("napa", []) or []:
            if m.get("napa_player_id") is not None:
                out["napa"][str(m["napa_player_id"])] = {
                    "fargo_player_id": fid, "person_id": person_id,
                    "confidence": _confidence(m), "match_method": m.get("match_method")}
        parent_name = entry.get("name") or ""
        for m in entry.get("bca", []) or []:
            for league in (m.get("leagues") or [""]):
                key = _bca_member_id(league, parent_name)
                prev = out["bca"].get(key)
                if prev is None:
                    out["bca"][key] = {"fargo_player_id": fid, "person_id": person_id,
                                       "confidence": _confidence(m), "match_method": m.get("match_method")}
                elif fid not in _row_fids(prev):
                    # Same id-less `league:norm(name)` key on a DIFFERENT FargoRate id: two
                    # distinct people the BCA key can't separate (id-less source). Flag the key
                    # ambiguous instead of silently overwriting one id, so PoolPredict must
                    # disambiguate rather than trust a wrong single mapping. These keys are
                    # human-reviewed in docs/resolve/collision_allowlist.json.
                    out["bca"][key] = {"ambiguous": True,
                                       "fargo_player_ids": sorted(_row_fids(prev) | {fid}),
                                       "confidence": "ambiguous", "match_method": "ambiguous"}

    payload = {"generated_at": today,
               "counts": {src: len(rows) for src, rows in out.items()}, **out}
    CROSSWALK_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build the person/profile layer (people.json).")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="regenerate people.json from roster + merges")
    pr = sub.add_parser("profiles", help="render docs/profiles.md")
    pr.add_argument("--all", action="store_true", help="render every person, not just merged/multi-source")
    sub.add_parser("crosswalk", help="publish docs/crosswalk.json (source-id -> fargo/person map)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today().isoformat()
    if args.command == "build":
        out = build(today)
        print(f"people={out['person_count']} (merged profiles: {out['merged_count']}) "
              f"-> {PEOPLE_PATH.relative_to(ROOT)}")
        return 0
    if args.command == "profiles":
        n = profiles(today, all_people=args.all)
        print(f"rendered {n} profile(s) -> {PROFILES_PATH.relative_to(ROOT)}")
        return 0
    if args.command == "crosswalk":
        out = crosswalk(today)
        c = out["counts"]
        print(f"crosswalk apa={c['apa']} napa={c['napa']} bca={c['bca']} "
              f"-> {CROSSWALK_PATH.relative_to(ROOT)}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
