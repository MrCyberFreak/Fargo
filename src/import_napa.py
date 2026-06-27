"""Roster source -- cross-reference the NAPA (Northern Colorado) player master to FargoRate.

Like APA, NAPA runs its OWN rating system (CueSpeed Rating) and its committed export
carries NO FargoRate id and NO usable state -- only NAPA's 8-digit `player_id`, the
player name, and an advisory CSR that is NOT on the Fargo scale (CSR->Fargo residual
is +/-70 pts, PoolPredict M4), so CSR NEVER gates a match. The identity bridge is
therefore name-based and CO-imputed, mirroring import_apa exactly:

  1. `crossref` (no network) -- parse the committed NAPA roster grids out of _ref/NAPA
     (src/napa_grid.py), build a league-wide player master (dedup on napa_player_id
     across all divisions), and bucket each name vs the roster:
       matched   : APA-style one rostered id -> attach a `napa[]` cross-link.
       ambiguous : rostered name held by >1 player_id -> reported, never auto-linked.
       new       : not in roster -> queued for FargoRate search.
     A name held by >1 DISTINCT napa id (a NAPA-internal collision) is **quarantined**
     -- never resolved name-only, because one link would fuse two NAPA humans onto a
     single Fargo id on a roster that is never pruned.
  2. `resolve` (network; runner) -- search FargoRate per queued name, CO-preferred
     (pick_match), nickname-variant fallback, transient-retry. Buckets -> resolved
     (exact single, auto-add) / variant_candidates / ambiguous / unfound.
  3. `add` / `reclassify` / `recover` / `manual` -- mirror import_apa; ONLY the
     `resolved` (exact-name single) bucket is auto-added. Precision over recall: a
     wrong name-only link tracks the wrong human forever, so quarantine beats a guess.

Source data is committed RAW HTML under _ref/NAPA (cloned by scripts/sync_ref); the
regenerable napa.db is NEVER rebuilt (no upstream-code execution). The shared
name-matching core lives in `namematch`. See docs/cross-league-identity.md.

Usage:
  python src/import_napa.py crossref                 # default _ref/NAPA; no network
  python src/import_napa.py crossref --ref <dir> --dry-run
  python src/import_napa.py resolve                  # network; runner only
  python src/import_napa.py add                       # apply resolved -> roster
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import sys
import time  # noqa: F401 -- backoff-sleep handle tests patch (sleep lives in namematch)
from pathlib import Path

from namematch import (  # noqa: F401 -- re-exported so import_napa.<fn> resolves in tests
    fargo_quality,
    first_compatible,
    first_name,
    is_co,
    norm,
    pick_match,
    recover_query,
    surname,
    variant_queries,
    _search_with_retry,
)
import ledger
from napa_grid import parse_grid_file
from resolve import load_roster, save_roster

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REF = ROOT / "_ref" / "NAPA"
RESOLVE_DIR = ROOT / "docs" / "resolve"
TO_RESOLVE_PATH = RESOLVE_DIR / "napa_to_resolve.json"
REPORT_PATH = RESOLVE_DIR / "napa_crossref_report.json"
RESOLUTION_PATH = RESOLVE_DIR / "napa_resolution.json"
MANUAL_PATH = RESOLVE_DIR / "napa_manual.json"
RECOVERY_PATH = RESOLVE_DIR / "napa_recovery.json"
COLLISIONS_PATH = RESOLVE_DIR / "napa_name_collisions.json"
UNLINK_PATH = RESOLVE_DIR / "napa_unlink.json"

# NAPA of Northern Colorado: every division is CO, so the source state is imputed CO.
# Advisory only -- resolution gates on FargoRate's OWN `location` (is_co), never this.
IMPUTED_STATE = "CO"


def _confidence(method: str) -> str:
    return "medium" if method == "variant" else "high"


def latest_grids(ref: Path) -> dict[str, Path]:
    """division_id -> newest committed roster_grid.html under _ref/NAPA/data/raw."""
    latest: dict[str, tuple[str, Path]] = {}
    pattern = str(ref / "data" / "raw" / "*" / "*" / "roster_grid.html")
    for f in glob.glob(pattern):
        p = Path(f)
        did, date = p.parents[1].name, p.parents[0].name
        if not did.isdigit():                     # skip _states/_recon/etc.
            continue
        if did not in latest or date > latest[did][0]:
            latest[did] = (date, p)
    return {did: p for did, (date, p) in latest.items()}


def build_master(ref: Path) -> dict[int, dict]:
    """napa_player_id -> slim descriptor, aggregated across every division (latest grid
    each). CSR merges non-null values; the player's primary division is the one whose
    grid date is newest. One human can play several NAPA divisions -> one master entry."""
    grids = latest_grids(ref)
    agg: dict[int, dict] = {}
    for did in sorted(grids):
        path = grids[did]
        date = path.parents[0].name
        for pl in parse_grid_file(path):
            m = agg.setdefault(pl.napa_player_id, {"csr": {}, "divisions": [], "_seen": {}})
            m["_seen"][did] = (date, pl.name, pl.division_name)
            if did not in m["divisions"]:
                m["divisions"].append(did)
            for k, v in (pl.csr or {}).items():
                if v is not None:
                    m["csr"][k] = v

    out: dict[int, dict] = {}
    for pid, m in agg.items():
        # primary = most recent grid date; its captured name/division win
        did, (date, name, divname) = max(m["_seen"].items(), key=lambda kv: kv[1][0])
        out[pid] = {
            "napa_player_id": pid,
            "name": name,
            "division_id": did,
            "division_name": divname,
            "divisions": sorted(m["divisions"]),
            "csr": m["csr"],
            "imputed_state": IMPUTED_STATE,
        }
    return out


def group_by_name(master: dict[int, dict]) -> dict[str, list[dict]]:
    """normalized name -> [master entries]. Length > 1 == NAPA-internal collision."""
    groups: dict[str, list[dict]] = {}
    for e in master.values():
        groups.setdefault(norm(e["name"]), []).append(e)
    return groups


def roster_name_index(roster: dict) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for pid, rec in roster.get("players", {}).items():
        index.setdefault(norm(rec.get("name")), []).append(pid)
    return index


def _attach_crosslink(roster: dict, pid: str, memberships: list[dict], today: str,
                      method: str = "name", suppress: set | None = None) -> int:
    """Add a `napa` cross-link list to one roster entry. Strictly additive (dedup on
    napa_player_id); existing fields are never touched. A `(napa_player_id, pid)` pair
    in `suppress` (the napa_unlink ledger) is never (re)added. Returns memberships
    newly linked."""
    suppress = suppress or set()
    entry = roster["players"][pid]
    existing = entry.setdefault("napa", [])
    have = {m.get("napa_player_id") for m in existing}
    added = 0
    for m in memberships:
        if (str(m["napa_player_id"]), int(pid)) in suppress:   # corrected wrong link -> skip
            continue
        if m["napa_player_id"] in have:
            continue
        existing.append({**m, "source": "napa", "match_method": method,
                         "confidence": _confidence(method), "added_date": today})
        have.add(m["napa_player_id"])
        added += 1
    if not existing:
        entry.pop("napa", None)
    return added


def crossref(ref: Path, dry_run: bool, today: str) -> dict:
    """Bucket NAPA names against the roster; write cross-links + the resolve queue.
    NAPA-internal name collisions are quarantined (never auto-resolved)."""
    master = build_master(ref)
    roster = load_roster()
    by_name = group_by_name(master)
    rindex = roster_name_index(roster)
    suppress = ledger.load_suppress(UNLINK_PATH)

    matched: list[dict] = []
    ambiguous: list[dict] = []
    to_resolve: list[dict] = []
    collisions: list[dict] = []
    links_written = 0

    for name_key in sorted(by_name):
        entries = by_name[name_key]
        if len(entries) > 1:                       # >1 distinct napa human, same name
            collisions.append({"norm": name_key, "name": entries[0]["name"],
                               "napa_player_ids": sorted(e["napa_player_id"] for e in entries)})
            continue
        memberships = [entries[0]]
        pids = rindex.get(name_key, [])
        if len(pids) == 1:
            pid = pids[0]
            matched.append({"player_id": int(pid), "name": entries[0]["name"],
                            "memberships": memberships})
            if not dry_run:
                links_written += _attach_crosslink(roster, pid, memberships, today, suppress=suppress)
        elif len(pids) > 1:
            ambiguous.append({"name": entries[0]["name"],
                              "roster_player_ids": [int(p) for p in pids],
                              "memberships": memberships})
        else:
            to_resolve.append({"search_name": entries[0]["name"], "norm": name_key,
                               "memberships": memberships})

    report = {
        "generated_at": today,
        "source": str(ref.relative_to(ROOT)) if ref.is_relative_to(ROOT) else str(ref),
        "napa_players": len(master),
        "unique_napa_names": len(by_name),
        "matched_single": len(matched),
        "matched_crosslinks_written": links_written,
        "ambiguous_existing": len(ambiguous),
        "name_collisions_quarantined": len(collisions),
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
        COLLISIONS_PATH.write_text(
            json.dumps(collisions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def _resolved_row(item: dict, rec, method: str) -> dict:
    return {"search_name": item["search_name"], "matched_via": method,
            "player_id": rec.player_id, "fargo_name": rec.name,
            "membership_id": rec.membership_id, "rating": rec.rating,
            "robustness": rec.robustness, "rating_quality": rec.rating_quality,
            "location": rec.location, "memberships": item["memberships"]}


def resolve(today: str) -> dict:
    """Search FargoRate for each queued NAPA name (CO-preferred, nickname fallback,
    transient-retry). Network; runs on a runner. Mirrors import_apa.resolve."""
    import fargo_api  # lazy -- crossref/add must run without network

    if not TO_RESOLVE_PATH.exists():
        print(f"No resolve queue at {TO_RESOLVE_PATH}; run crossref first.", file=sys.stderr)
        raise SystemExit(1)
    queue = json.loads(TO_RESOLVE_PATH.read_text(encoding="utf-8"))
    session = fargo_api.new_session()

    resolved: list[dict] = []
    variant_candidates: list[dict] = []
    ambiguous: list[dict] = []
    unfound: list[dict] = []
    errors = 0

    for i, item in enumerate(queue, 1):
        name = item["search_name"]
        try:
            base = _search_with_retry(fargo_api, name, session)
        except Exception as exc:
            errors += 1
            print(f"  [{i}/{len(queue)}] ERROR {name!r}: {exc}", file=sys.stderr)
            unfound.append({**item, "error": str(exc)})
            continue

        status, hit = pick_match(base)
        method = "name"
        tried: list[str] = []

        if status == "none":
            want = surname(name)
            vcands = []
            for vq in variant_queries(name):
                tried.append(vq)
                try:
                    for r in _search_with_retry(fargo_api, vq, session):
                        if surname(r.name) == want:
                            vcands.append(r)
                except Exception:
                    continue
            if vcands:
                status, hit = pick_match(vcands)
                method = "variant"

        if status == "resolved" and method == "name":
            resolved.append(_resolved_row(item, hit, method))
        elif status == "resolved":
            row = _resolved_row(item, hit, method)
            row["variants_tried"] = tried
            variant_candidates.append(row)
        elif status == "ambiguous":
            ambiguous.append({"search_name": name, "matched_via": method,
                              "candidates": [{"player_id": r.player_id, "name": r.name,
                                              "rating": r.rating, "robustness": r.robustness,
                                              "location": r.location} for r in hit],
                              "memberships": item["memberships"]})
        else:
            unfound.append({"search_name": name, "memberships": item["memberships"]})

        if i % 100 == 0:
            print(f"  ...{i}/{len(queue)} searched (resolved={len(resolved)} "
                  f"variant={len(variant_candidates)} ambiguous={len(ambiguous)} "
                  f"unfound={len(unfound)})")

    out = {
        "generated_at": today,
        "queue_size": len(queue),
        "resolved": resolved,
        "variant_candidates": variant_candidates,
        "ambiguous": ambiguous,
        "unfound": unfound,
        "errors": errors,
    }
    RESOLVE_DIR.mkdir(parents=True, exist_ok=True)
    RESOLUTION_PATH.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def add(today: str, include_variants: bool = False) -> dict:
    """Append matches to roster.json (no network). Idempotent: new ids create a slim
    entry; existing ids just gain the NAPA cross-link. Only `resolved` by default."""
    if not RESOLUTION_PATH.exists():
        print(f"No resolution file at {RESOLUTION_PATH}; run resolve first.", file=sys.stderr)
        raise SystemExit(1)
    res = json.loads(RESOLUTION_PATH.read_text(encoding="utf-8"))
    roster = load_roster()
    players = roster.setdefault("players", {})
    suppress = ledger.load_suppress(UNLINK_PATH)
    summary = {"created": 0, "crosslinked_existing": 0, "already_present": 0}

    rows = list(res.get("resolved", []))
    if include_variants:
        rows += res.get("variant_candidates", [])

    for x in rows:
        key = str(x["player_id"])
        method = x.get("matched_via", "name")
        memberships = x.get("memberships", [])
        if key in players:
            n = _attach_crosslink(roster, key, memberships, today, method, suppress=suppress)
            summary["crosslinked_existing" if n else "already_present"] += 1
        else:
            players[key] = {
                "player_id": x["player_id"],
                "membership_id": x.get("membership_id"),
                "name": x.get("fargo_name"),
                "state": x.get("location"),
                "source": "napa",
                "added_date": today,
                "napa": [{**m, "source": "napa", "match_method": method,
                          "confidence": _confidence(method), "added_date": today}
                         for m in memberships],
            }
            summary["created"] += 1

    save_roster(roster)
    summary["roster_total"] = len(players)
    return summary


def reclassify(today: str) -> dict:
    """Re-bucket stored `ambiguous` candidates through the current `pick_match` (no
    network); promote any that now resolve into `resolved`. Mirrors import_apa."""
    from types import SimpleNamespace

    if not RESOLUTION_PATH.exists():
        print(f"No resolution file at {RESOLUTION_PATH}; run resolve first.", file=sys.stderr)
        raise SystemExit(1)
    res = json.loads(RESOLUTION_PATH.read_text(encoding="utf-8"))

    still: list[dict] = []
    promoted = 0
    for entry in res.get("ambiguous", []):
        recs = [SimpleNamespace(player_id=c["player_id"], location=c.get("location"),
                                name=c.get("name"), rating=c.get("rating"),
                                robustness=c.get("robustness")) for c in entry["candidates"]]
        status, hit = pick_match(recs)
        if status == "resolved":
            res["resolved"].append({
                "search_name": entry["search_name"],
                "matched_via": entry.get("matched_via", "name"),
                "player_id": hit.player_id, "fargo_name": hit.name,
                "membership_id": None, "rating": hit.rating, "robustness": hit.robustness,
                "rating_quality": fargo_quality(hit.robustness), "location": hit.location,
                "memberships": entry["memberships"]})
            promoted += 1
        else:
            still.append(entry)

    res["ambiguous"] = still
    RESOLUTION_PATH.write_text(
        json.dumps(res, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"promoted": promoted, "resolved_total": len(res["resolved"]),
            "ambiguous_remaining": len(still)}


def manual(today: str) -> dict:
    """Add hand-picked resolutions from docs/resolve/napa_manual.json (no network).
    Each pick is `{search_name, player_id, [fargo_name, membership_id, location]}`;
    the NAPA membership is looked up from the resolve queue by name. Mirrors import_apa."""
    if not MANUAL_PATH.exists():
        print(f"No manual picks at {MANUAL_PATH}.", file=sys.stderr)
        raise SystemExit(1)
    picks = json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
    queue = json.loads(TO_RESOLVE_PATH.read_text(encoding="utf-8")) if TO_RESOLVE_PATH.exists() else []
    mem_by_norm = {norm(q["search_name"]): q["memberships"] for q in queue}

    roster = load_roster()
    players = roster.setdefault("players", {})
    summary = {"created": 0, "crosslinked_existing": 0, "already_present": 0, "no_membership": 0}

    for p in picks:
        key = str(p["player_id"])
        memberships = p.get("memberships") or mem_by_norm.get(norm(p["search_name"]), [])
        if not memberships:
            summary["no_membership"] += 1
        if key in players:
            n = _attach_crosslink(roster, key, memberships, today, "manual")
            summary["crosslinked_existing" if n else "already_present"] += 1
        else:
            players[key] = {
                "player_id": p["player_id"],
                "membership_id": p.get("membership_id"),
                "name": p.get("fargo_name") or p.get("search_name"),
                "state": p.get("location"),
                "source": "napa",
                "added_date": today,
                "napa": [{**m, "source": "napa", "match_method": "manual",
                          "confidence": "high", "added_date": today}
                         for m in memberships],
            }
            summary["created"] += 1

    save_roster(roster)
    summary["roster_total"] = len(players)
    return summary


def recover(today: str) -> dict:
    """Second-chance pass over `unfound` names (network; runner): prefix-surname search,
    keep surname+first-compatible candidates. Stages everything, adds nothing. Mirrors APA."""
    import fargo_api

    if not RESOLUTION_PATH.exists():
        print(f"No resolution file at {RESOLUTION_PATH}; run resolve first.", file=sys.stderr)
        raise SystemExit(1)
    res = json.loads(RESOLUTION_PATH.read_text(encoding="utf-8"))
    unfound = [x for x in res.get("unfound", []) if "error" not in x]
    session = fargo_api.new_session()

    cache: dict[str, list | None] = {}
    recovered: list[dict] = []
    still = errors = 0

    for i, x in enumerate(unfound, 1):
        q = recover_query(x["search_name"])
        if not q:
            still += 1
            continue
        if q not in cache:
            try:
                cache[q] = _search_with_retry(fargo_api, q, session)
            except Exception:
                cache[q] = None
        results = cache[q]
        if results is None:
            errors += 1
            still += 1
            continue

        want_sur, want_first = surname(x["search_name"]), first_name(x["search_name"])
        compat: dict = {}
        for r in results:
            if surname(r.name) == want_sur and first_compatible(want_first, first_name(r.name)):
                compat.setdefault(r.player_id, r)
        if compat:
            recovered.append({
                "search_name": x["search_name"], "query": q,
                "candidates": [{"player_id": r.player_id, "name": r.name, "location": r.location,
                                "rating": r.rating, "robustness": r.robustness,
                                "membership_id": r.membership_id} for r in compat.values()],
                "memberships": x["memberships"]})
        else:
            still += 1
        if i % 100 == 0:
            print(f"  ...{i}/{len(unfound)} scanned (recovered={len(recovered)} queries={len(cache)})")

    out = {"generated_at": today, "unfound_scanned": len(unfound),
           "recovered": recovered, "still_unfound": still,
           "unique_queries": len(cache), "errors": errors}
    RESOLVE_DIR.mkdir(parents=True, exist_ok=True)
    RECOVERY_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def unlink(today: str) -> dict:
    """Apply docs/resolve/napa_unlink.json (no network): strip any existing napa
    cross-link whose (napa_player_id, player_id) pair was recorded as wrong."""
    suppress = ledger.load_suppress(UNLINK_PATH)
    roster = load_roster()
    removed = ledger.strip_suppressed(roster, "napa", lambda m: m.get("napa_player_id"), suppress)
    if removed:
        save_roster(roster)
    return {"suppressed_pairs": len(suppress), "removed": removed}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cross-reference the NAPA player master to FargoRate.")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("crossref", help="bucket NAPA names vs roster; write resolve queue (no network)")
    c.add_argument("--ref", type=Path, default=DEFAULT_REF, help="path to the _ref/NAPA clone")
    c.add_argument("--dry-run", action="store_true", help="report only; write nothing")

    sub.add_parser("resolve", help="search FargoRate for queued names (network; runner)")
    ad = sub.add_parser("add", help="append resolved matches to roster.json (no network)")
    ad.add_argument("--variants", action="store_true", help="also add reviewed variant_candidates")
    sub.add_parser("reclassify", help="re-bucket ambiguous via current pick_match (no network)")
    sub.add_parser("manual", help="add hand-picked resolutions from napa_manual.json (no network)")
    sub.add_parser("recover", help="second-chance fuzzy search over unfound names (network; runner)")
    sub.add_parser("unlink", help="strip wrong napa links per napa_unlink.json (no network)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today().isoformat()

    if args.command == "crossref":
        if not args.ref.exists():
            print(f"NAPA ref not found: {args.ref}  (run scripts/sync_ref NAPA)", file=sys.stderr)
            return 1
        rep = crossref(args.ref, args.dry_run, today)
        tag = "[dry-run] " if args.dry_run else ""
        print(f"{tag}NAPA players={rep['napa_players']} unique_names={rep['unique_napa_names']}")
        print(f"  matched (1 roster id)      : {rep['matched_single']} "
              f"(crosslinks written: {rep['matched_crosslinks_written']})")
        print(f"  ambiguous vs roster        : {rep['ambiguous_existing']}")
        print(f"  name collisions quarantined: {rep['name_collisions_quarantined']}")
        print(f"  new -> to resolve          : {rep['new_to_resolve']}")
        if not args.dry_run:
            print(f"\nwrote {TO_RESOLVE_PATH.relative_to(ROOT)}, {REPORT_PATH.relative_to(ROOT)}, "
                  f"{COLLISIONS_PATH.relative_to(ROOT)}")
        return 0

    if args.command == "resolve":
        out = resolve(today)
        print(f"\nresolved={len(out['resolved'])} (auto-add) "
              f"variant_candidates={len(out['variant_candidates'])} (review) "
              f"ambiguous={len(out['ambiguous'])} (review) "
              f"unfound={len(out['unfound'])} errors={out['errors']}")
        print(f"wrote {RESOLUTION_PATH.relative_to(ROOT)}")
        return 0

    if args.command == "add":
        s = add(today, include_variants=args.variants)
        tag = " (incl. variants)" if args.variants else ""
        print(f"created={s['created']} crosslinked_existing={s['crosslinked_existing']} "
              f"already_present={s['already_present']} roster_total={s['roster_total']}{tag}")
        return 0

    if args.command == "reclassify":
        s = reclassify(today)
        print(f"promoted {s['promoted']} ambiguous -> resolved "
              f"(resolved_total={s['resolved_total']} ambiguous_remaining={s['ambiguous_remaining']})")
        return 0

    if args.command == "manual":
        s = manual(today)
        print(f"created={s['created']} crosslinked_existing={s['crosslinked_existing']} "
              f"already_present={s['already_present']} no_membership={s['no_membership']} "
              f"roster_total={s['roster_total']}")
        return 0

    if args.command == "recover":
        s = recover(today)
        print(f"\nscanned {s['unfound_scanned']} unfound in {s['unique_queries']} queries -> "
              f"recovered={len(s['recovered'])} still_unfound={s['still_unfound']} errors={s['errors']}")
        print(f"wrote {RECOVERY_PATH.relative_to(ROOT)}")
        return 0

    if args.command == "unlink":
        s = unlink(today)
        print(f"unlink: suppressed_pairs={s['suppressed_pairs']} removed={s['removed']}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
