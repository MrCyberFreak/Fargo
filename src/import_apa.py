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
     Writes the new-name queue to docs/resolve/apa_to_resolve.json.
  2. `resolve` (network, runs on a GitHub Actions runner) — for each queued name,
     search FargoRate. A single unambiguous match is accepted regardless of state
     (CO is *preferred* when both a CO and out-of-state player share the name, so
     clean local matches are never lost). Names that return zero matches are
     retried with first-name nickname variations (Andy<->Andrew, Mike<->Michael,
     ...) guarded by a surname match. A name with >1 distinct match is
     **ambiguous** and flagged, never auto-picked — without a state the wrong
     same-name player would be tracked forever (the roster is never pruned).
     Transient API errors (HTTP 500) are retried. Writes the review file
     docs/resolve/apa_resolution.json with four buckets: `resolved` (exact-name
     single match), `variant_candidates` (single match found only via a nickname
     variant), `ambiguous` (>1 match), `unfound`.
  3. `add` (no network) — append every `resolved` (exact) match to roster.json
     (slim entry + APA cross-link), or attach the cross-link if the id already
     exists. variant_candidates / ambiguous / unfound are left for manual review.

Name collisions are real (the roster already has 40), which is exactly why the
project keys on player_id, never name. Every name-based link records its
`match_method` ("name" or "variant") so it stays auditable. Only exact-name
single matches are auto-added; variant + ambiguous matches are staged for review.

A single human can hold several APA memberships ("multiple skill levels"); all of
them are kept on the one resolved player rather than collapsed to one.

The raw APA file lives under basket/ and is git-ignored (names + member numbers +
session history). Only the curated extraction is committed.

Usage:
  python src/import_apa.py crossref                      # default basket/ path
  python src/import_apa.py crossref --path <file> --dry-run
  python src/import_apa.py resolve                       # network; runner only
  python src/import_apa.py add                           # apply resolved -> roster
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

from resolve import load_roster, save_roster

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "basket" / "players_master.json"
RESOLVE_DIR = ROOT / "docs" / "resolve"
TO_RESOLVE_PATH = RESOLVE_DIR / "apa_to_resolve.json"
REPORT_PATH = RESOLVE_DIR / "apa_crossref_report.json"
RESOLUTION_PATH = RESOLVE_DIR / "apa_resolution.json"
MANUAL_PATH = RESOLVE_DIR / "apa_manual.json"
RECOVERY_PATH = RESOLVE_DIR / "apa_recovery.json"

# League-fee annotations get prepended to some names, e.g. "Owes $150 Anna Byrd",
# "OWES$130 Jordan Freeman", "Owes $180Aaron Knobloch". Strip them off the front.
_FEE_PREFIX = re.compile(r"(?i)^\s*owes?\s*\$?\s*\d+\s*")

# Common English given-name <-> nickname groups. Each line is one equivalence
# class; every member maps to all the others. Used only to retry names that
# returned zero FargoRate matches (a surname match is still required), so a wider
# net here costs a few extra searches, not false roster adds.
_NICKNAME_GROUPS = [
    {"andrew", "andy", "drew"}, {"anthony", "tony"}, {"benjamin", "ben"},
    {"bradley", "brad"}, {"charles", "charlie", "chuck"},
    {"christopher", "chris"}, {"daniel", "dan", "danny"},
    {"david", "dave", "davey"}, {"donald", "don", "donnie"},
    {"douglas", "doug"}, {"edward", "ed", "eddie", "ted"},
    {"frederick", "fred"}, {"gregory", "greg"}, {"jacob", "jake"},
    {"james", "jim", "jimmy", "jamie"}, {"jeffrey", "jeff"},
    {"jonathan", "jon"}, {"john", "johnny", "jack"}, {"joseph", "joe", "joey"},
    {"joshua", "josh"}, {"kenneth", "ken", "kenny"}, {"lawrence", "larry"},
    {"matthew", "matt"}, {"michael", "mike", "mick"}, {"nathan", "nate"},
    {"nathaniel", "nate", "nathan"}, {"nicholas", "nick", "nik"},
    {"patrick", "pat"}, {"peter", "pete"}, {"philip", "phillip", "phil"},
    {"raymond", "ray"}, {"richard", "rick", "rich", "dick", "richie"},
    {"robert", "rob", "bob", "bobby"}, {"ronald", "ron", "ronnie"},
    {"samuel", "sam", "sammy"}, {"stephen", "steven", "steve"},
    {"thomas", "tom", "tommy"}, {"timothy", "tim", "timmy"},
    {"vincent", "vince", "vinny"}, {"william", "will", "bill", "billy", "liam"},
    {"zachary", "zach", "zack"}, {"alexander", "alex", "alexandra", "lexi"},
    {"abigail", "abby"}, {"amanda", "mandy"}, {"angela", "angie"},
    {"barbara", "barb"}, {"catherine", "katherine", "kate", "katie", "kathy", "cathy", "kat"},
    {"christina", "christine", "chris", "tina"}, {"cynthia", "cindy"},
    {"deborah", "deb", "debbie"}, {"elizabeth", "liz", "beth", "lizzie", "betty"},
    {"jennifer", "jen", "jenny"}, {"jessica", "jess"}, {"kimberly", "kim"},
    {"margaret", "maggie", "meg", "peggy", "marge"}, {"michelle", "shelly"},
    {"nicole", "nikki"}, {"pamela", "pam"}, {"patricia", "pat", "patty", "tricia"},
    {"rebecca", "becca", "becky", "reba"}, {"samantha", "sam", "sammy"},
    {"sandra", "sandy"}, {"stephanie", "steph"}, {"susan", "sue", "susie"},
    {"theresa", "teresa", "terry", "tess"}, {"victoria", "vicky", "tori"},
]
_NICKNAMES: dict[str, set[str]] = {}
for _grp in _NICKNAME_GROUPS:
    for _n in _grp:
        _NICKNAMES.setdefault(_n, set()).update(_grp - {_n})


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


def surname(name: str | None) -> str:
    """Last token of the normalized name (used to guard nickname variants)."""
    parts = norm(name).split()
    return parts[-1] if parts else ""


def is_co(location: str | None) -> bool:
    """True if a FargoRate `location` is Colorado. The field is inconsistent —
    sometimes just "CO", sometimes "Denver CO" or "Denver, CO" — so test the
    trailing state token, not an exact string match."""
    if not location:
        return False
    return location.upper().replace(",", " ").split()[-1] == "CO"


def variant_queries(full_name: str) -> list[str]:
    """First-name nickname variations of a full name, surname kept intact.
    'Andy Carroll' -> ['andrew carroll', 'drew carroll']. Empty if no mapping."""
    parts = clean_name(full_name).split()
    if len(parts) < 2:
        return []
    first = parts[0].lower()
    tail = parts[1:]
    return [" ".join([alt, *tail]) for alt in sorted(_NICKNAMES.get(first, ()))]


def first_name(name: str | None) -> str:
    """First token of the normalized name."""
    parts = norm(name).split()
    return parts[0] if parts else ""


def first_compatible(a: str, c: str) -> bool:
    """Are two first names plausibly the same person? True when one is a prefix of
    the other (≥3 chars, catches Shirish↔Shirishkumar, Dan↔Daniel) or they are a
    known nickname pair. Used to filter recovery candidates."""
    if not a or not c:
        return False
    if a == c:
        return True
    short, long = sorted([a, c], key=len)
    if len(short) >= 3 and long.startswith(short):
        return True
    return c in _NICKNAMES.get(a, set()) or a in _NICKNAMES.get(c, set())


def recover_query(name: str) -> str | None:
    """A surname search qualified by a short first-name prefix — avoids the broad
    'surname alone' queries that FargoRate 500s on, while still prefix-matching a
    truncated FargoRate first name. 'Shirishkumar Patel' -> 'Shir Patel'."""
    parts = clean_name(name).split()
    if len(parts) < 2:
        return None
    return f"{parts[0][:4]} {parts[-1]}"


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


def _attach_crosslink(roster: dict, pid: str, memberships: list[dict], today: str,
                      method: str = "name") -> int:
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
        existing.append({**m, "source": "apa", "match_method": method, "added_date": today})
        have.add(m["member_id"])
        added += 1
    if not existing:                      # nothing to keep — drop the empty key
        entry.pop("apa", None)
    return added


def pick_match(candidates: list) -> tuple[str, object]:
    """Choose a single FargoRate match from search candidates, CO-preferred.

    Returns ("resolved", record) when exactly one CO match exists (extra
    out-of-state namesakes are ignored), else exactly one out-of-state match;
    ("ambiguous", [records]) when a state bucket has >1; ("none", []) for no
    candidates. Candidates are deduped by player_id first."""
    seen: dict = {}
    for r in candidates:
        seen.setdefault(r.player_id, r)
    cands = list(seen.values())
    co = [r for r in cands if is_co(r.location)]
    noco = [r for r in cands if not is_co(r.location)]
    if len(co) == 1:
        return ("resolved", co[0])
    if len(co) > 1:
        return ("ambiguous", co)
    if len(noco) == 1:
        return ("resolved", noco[0])
    if len(noco) > 1:
        return ("ambiguous", noco)
    return ("none", [])


def _search_with_retry(fargo_api, name: str, session, attempts: int = 3) -> list:
    """Search, retrying transient failures (e.g. HTTP 500) with a short backoff."""
    last = None
    for k in range(attempts):
        try:
            return fargo_api.search(name, session=session)
        except Exception as exc:  # FargoApiError + any network hiccup
            last = exc
            if k < attempts - 1:
                time.sleep(1.5 * (k + 1))
    raise last


def _resolved_row(item: dict, rec, method: str) -> dict:
    return {"search_name": item["search_name"], "matched_via": method,
            "player_id": rec.player_id, "fargo_name": rec.name,
            "membership_id": rec.membership_id, "rating": rec.rating,
            "robustness": rec.robustness, "rating_quality": rec.rating_quality,
            "location": rec.location, "memberships": item["memberships"]}


def resolve(today: str) -> dict:
    """Search FargoRate for each queued name (CO-preferred, nickname fallback,
    retry on transient errors). Network; runs on a runner."""
    import fargo_api  # imported lazily — crossref/add must run without network

    if not TO_RESOLVE_PATH.exists():
        print(f"No resolve queue at {TO_RESOLVE_PATH}; run crossref first.", file=sys.stderr)
        raise SystemExit(1)
    queue = json.loads(TO_RESOLVE_PATH.read_text(encoding="utf-8"))
    session = fargo_api.new_session()

    resolved: list[dict] = []           # exact-name single matches — auto-added
    variant_candidates: list[dict] = []  # found only via a nickname variant — review
    ambiguous: list[dict] = []          # >1 distinct match — review, never picked
    unfound: list[dict] = []
    errors = 0

    for i, item in enumerate(queue, 1):
        name = item["search_name"]
        try:
            base = _search_with_retry(fargo_api, name, session)
        except Exception as exc:  # one bad name must not sink the batch
            errors += 1
            print(f"  [{i}/{len(queue)}] ERROR {name!r}: {exc}", file=sys.stderr)
            unfound.append({**item, "error": str(exc)})
            continue

        status, hit = pick_match(base)
        method = "name"
        tried: list[str] = []

        if status == "none":
            # Zero direct matches — retry with first-name nickname variants,
            # accepting only candidates whose surname matches.
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
        elif status == "resolved":  # variant single match — staged, NOT auto-added
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
    """Append matches to roster.json (no network). Idempotent: new ids create a
    slim entry; existing ids just gain the APA cross-link.

    By default only the `resolved` (exact-name single) bucket is added. With
    `include_variants=True` the reviewed `variant_candidates` bucket is added too
    (each link still records match_method "variant", so it stays auditable)."""
    if not RESOLUTION_PATH.exists():
        print(f"No resolution file at {RESOLUTION_PATH}; run resolve first.", file=sys.stderr)
        raise SystemExit(1)
    res = json.loads(RESOLUTION_PATH.read_text(encoding="utf-8"))
    roster = load_roster()
    players = roster.setdefault("players", {})
    summary = {"created": 0, "crosslinked_existing": 0, "already_present": 0}

    rows = list(res.get("resolved", []))
    if include_variants:
        rows += res.get("variant_candidates", [])

    for x in rows:
        key = str(x["player_id"])
        method = x.get("matched_via", "name")
        memberships = x.get("memberships", [])
        if key in players:
            n = _attach_crosslink(roster, key, memberships, today, method)
            if n:
                summary["crosslinked_existing"] += 1
            else:
                summary["already_present"] += 1
        else:
            players[key] = {
                "player_id": x["player_id"],
                "membership_id": x.get("membership_id"),
                "name": x.get("fargo_name"),
                "state": x.get("location"),
                "source": "apa",
                "added_date": today,
                "apa": [{**m, "source": "apa", "match_method": method, "added_date": today}
                        for m in memberships],
            }
            summary["created"] += 1

    save_roster(roster)
    summary["roster_total"] = len(players)
    return summary


def reclassify(today: str) -> dict:
    """Re-bucket the `ambiguous` entries through the current `pick_match` (no
    network). Each ambiguous entry stored its full candidate list, so re-running
    the selector recovers the matches that a stale CO check had wrongly flagged —
    e.g. a lone CO player ("Denver CO") among out-of-state namesakes now resolves
    to that CO player. Promoted entries move into `resolved` (so `add` will pick
    them up); the file is rewritten in place."""
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
    """Add hand-picked resolutions from docs/resolve/apa_manual.json (no network).

    For cases the automated resolve can't settle: an ambiguous multi-CO name the
    user disambiguated, or a player whose FargoRate name differs from APA (e.g.
    APA "Shirishkumar Patel" vs FargoRate "Shirish Patel") so the search missed
    them. Each pick is `{search_name, player_id, [fargo_name, membership_id,
    location, note]}`; the APA membership(s) are looked up from the resolve queue
    by name so the cross-link is attached. Links are tagged match_method
    "manual". Idempotent."""
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
                "source": "apa",
                "added_date": today,
                "apa": [{**m, "source": "apa", "match_method": "manual", "added_date": today}
                        for m in memberships],
            }
            summary["created"] += 1

    save_roster(roster)
    summary["roster_total"] = len(players)
    return summary


def recover(today: str) -> dict:
    """Second-chance pass over the `unfound` names (network; runner). Many were
    missed only because the APA name differs from FargoRate's (e.g.
    'Shirishkumar Patel' vs 'Shirish Patel') or the player moved. For each, search
    a short first-name prefix + surname and keep candidates with a matching
    surname and a compatible first name (`first_compatible`). Writes the review
    file docs/resolve/apa_recovery.json — **stages everything, adds nothing** (per
    user decision: recovery is lower-confidence than the exact pass)."""
    import fargo_api  # lazy — keeps the rest of the module network-free

    if not RESOLUTION_PATH.exists():
        print(f"No resolution file at {RESOLUTION_PATH}; run resolve first.", file=sys.stderr)
        raise SystemExit(1)
    res = json.loads(RESOLUTION_PATH.read_text(encoding="utf-8"))
    unfound = [x for x in res.get("unfound", []) if "error" not in x]
    session = fargo_api.new_session()

    cache: dict[str, list | None] = {}   # query -> results (dedupe shared surnames)
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


def fargo_quality(robustness) -> str:
    """rating_quality without importing fargo_api (keeps reclassify network-free)."""
    try:
        return "established" if int(robustness) >= 200 else "preliminary"
    except (TypeError, ValueError):
        return "preliminary"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cross-reference an APA master list to FargoRate.")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("crossref", help="bucket APA names vs roster; write resolve queue (no network)")
    c.add_argument("--path", type=Path, default=DEFAULT_PATH, help="path to the APA master json")
    c.add_argument("--dry-run", action="store_true", help="report only; write nothing")

    sub.add_parser("resolve", help="search FargoRate for queued names (network; runner)")
    ad = sub.add_parser("add", help="append resolved matches to roster.json (no network)")
    ad.add_argument("--variants", action="store_true",
                    help="also add the reviewed variant_candidates bucket")
    sub.add_parser("reclassify", help="re-bucket ambiguous via current pick_match (no network)")
    sub.add_parser("manual", help="add hand-picked resolutions from apa_manual.json (no network)")
    sub.add_parser("recover", help="second-chance fuzzy search over unfound names (network; runner)")
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
