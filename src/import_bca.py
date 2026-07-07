"""Roster source -- fold the BCAPL / FargoRate-LMS player set into the tracker.

Unlike APA and NAPA (which run their own rating systems and carry NO FargoRate
number), the BCA/LMS export already carries each player's point-in-time
**FargoRate rating**. That rating is a strong identity CONFIRMER the other two
sources lack: after `pick_match` narrows to a candidate, we cross-check the
candidate's live FargoRate against the LMS snapshot, so a same-name namesake in
the wrong state (or an unrelated collision) is caught, and a genuine multi-CO
collision can often be broken by rating. Precision over recall stays the rule --
anything the rating cannot confirm is queued, never auto-added.

The BCA export is Colorado leagues, so state is CO-imputed exactly like APA/NAPA;
`pick_match` (from namematch) already CO-prefers.

Pipeline (mirrors import_apa / import_napa):

  1. `crossref` (no network) -- read the staged BCA extract, drop any name already
     on the roster (idempotent re-runs), and write the remaining "new" names to a
     resolve queue. The extract itself is a raw source list and lives under the
     gitignored basket/ (see .gitignore / CLAUDE.md); regenerate it from the bca
     repo's data/exports whenever you want to catch newly-seen players.
  2. `resolve` (network; runner) -- search FargoRate per queued name (CO-preferred,
     nickname-variant fallback, transient retry), then RATING-CONFIRM:
       resolved  : one confident match (single CO match, or a rating-disambiguated
                   one from a CO collision) whose live rating is compatible with
                   the LMS snapshot -> auto-add.
       ambiguous : >1 CO match the rating cannot uniquely break -> reported.
       unfound   : no candidate (after variants).
  3. `add` -- apply the `resolved` bucket to roster.json. New entries carry the
     full FargoRate `record`; every entry gets an additive, deduped `bca[]`
     cross-link (leagues + lms_rating + match_method + confidence), idempotent.

Only the `resolved` bucket is ever auto-added. See docs/cross-league-identity.md.

Usage:
  python src/import_bca.py crossref                  # no network; writes the queue
  python src/import_bca.py crossref --input <file> --dry-run
  python src/import_bca.py resolve                   # network; runner only
  python src/import_bca.py add                        # apply resolved -> roster
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time  # noqa: F401 -- tests patch time.sleep on the retry path
from pathlib import Path

from namematch import (  # noqa: F401 -- re-exported so tests can patch import_bca.<fn>
    is_co,
    norm,
    pick_match,
    variant_queries,
    _search_with_retry,
)
import fargo_api
from resolve import load_roster, save_roster

ROOT = Path(__file__).resolve().parent.parent
BASKET = ROOT / "basket"
RESOLVE_DIR = ROOT / "docs" / "resolve"

DEFAULT_INPUT = BASKET / "bca_players.json"
QUEUE_PATH = BASKET / "bca-queue.json"
RESOLVED_PATH = RESOLVE_DIR / "bca-resolved.json"
AMBIGUOUS_PATH = RESOLVE_DIR / "bca-ambiguous.json"
UNFOUND_PATH = RESOLVE_DIR / "bca-unfound.json"
ERRORED_PATH = RESOLVE_DIR / "bca-errored.json"

# A UNIQUE CO match is accepted as the person even name-only (APA/NAPA precedent);
# the LMS rating only DEMOTES it to review when wildly off (likely a stale roster
# collision), never the reverse. A ratings gap this large on a lone CO namesake is
# implausible for the same human across the ~weeks between the LMS snapshot and now.
CONFIRM_MAX_GAP = 125
# To BREAK a genuine multi-CO collision we need the rating to single one out and be
# close -- tighter, because here the rating is the only discriminator.
DISAMBIG_MAX_GAP = 25


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _search_name(name: str) -> str:
    """Clean an LMS display name into a search query: drop a trailing/embedded
    ``(nickname)`` and collapse whitespace. The raw LMS string (e.g.
    ``"Brian Noffsinger     (Bubba)"``) can 500 the search endpoint and never
    matches anyway."""
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", name or "")).strip()


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. crossref (no network)
# ---------------------------------------------------------------------------
def cmd_crossref(input_path: Path, dry_run: bool) -> int:
    players = _read_json(input_path, None)
    if players is None:
        print(f"No BCA extract at {input_path}. Stage it from the bca repo first.", file=sys.stderr)
        return 1

    roster = load_roster()["players"]
    roster_names = {norm(v.get("name")) for v in roster.values()}

    queue, matched = [], 0
    seen: set[str] = set()
    for p in players:
        key = norm(p.get("name"))
        if not key or key in seen:
            continue
        seen.add(key)
        if key in roster_names:
            matched += 1
            continue
        queue.append({"name": p.get("name"), "rating": p.get("rating"), "leagues": p.get("leagues") or []})

    print(f"BCA extract: {len(seen)} distinct  |  already on roster: {matched}  |  new (queued): {len(queue)}")
    if not dry_run:
        _write_json(QUEUE_PATH, queue)
        print(f"Wrote queue -> {_rel(QUEUE_PATH)}")
    return 0


# ---------------------------------------------------------------------------
# 2. resolve (network)
# ---------------------------------------------------------------------------
def _rating_gap(rec, lms_rating):
    """Absolute gap between a candidate's live FargoRate and the LMS snapshot, or
    None when either side is missing (rating cannot confirm/deny)."""
    if lms_rating is None or rec.rating is None:
        return None
    try:
        return abs(int(rec.rating) - int(lms_rating))
    except (TypeError, ValueError):
        return None


def _classify(cands, lms_rating):
    """-> (bucket, record, note). bucket in {resolved, ambiguous, unfound}."""
    status, payload = pick_match(cands)
    if status == "none":
        return "unfound", None, "no candidate"
    if status == "resolved":
        rec = payload
        gap = _rating_gap(rec, lms_rating)
        if gap is not None and gap > CONFIRM_MAX_GAP:
            return "ambiguous", None, f"lone match but rating off by {gap} (lms={lms_rating}, fargo={rec.rating})"
        note = "unique-co" if gap is None else f"unique-co, rating-confirmed (gap {gap})"
        return "resolved", rec, note
    # ambiguous: a state bucket held >1 -- break it with the rating ONLY when the
    # rating singles out exactly one candidate. Two both-plausible ratings stay
    # ambiguous (picking the marginally closer one would risk the wrong human).
    near = [(g, r) for r in payload if (g := _rating_gap(r, lms_rating)) is not None and g <= DISAMBIG_MAX_GAP]
    if len(near) == 1:
        g, rec = near[0]
        return "resolved", rec, f"co-collision broken by rating (unique within {DISAMBIG_MAX_GAP}, gap {g})"
    ids = ", ".join(str(r.player_id) for r in payload)
    return "ambiguous", None, f"{len(payload)} CO matches, rating did not single one out (ids: {ids})"


CHECKPOINT_EVERY = 50


def cmd_resolve(session) -> int:
    queue = _read_json(QUEUE_PATH, None)
    if queue is None:
        print(f"No queue at {QUEUE_PATH}. Run `crossref` first.", file=sys.stderr)
        return 1

    # Resume: reload prior buckets and skip queries already processed. Checkpoints
    # are written every CHECKPOINT_EVERY players, so a killed run (e.g. a container
    # restart) continues where it stopped instead of starting over.
    resolved = _read_json(RESOLVED_PATH, [])
    ambiguous = _read_json(AMBIGUOUS_PATH, [])
    unfound = _read_json(UNFOUND_PATH, [])
    errored = _read_json(ERRORED_PATH, [])
    done = {r["query"] for r in resolved} | {a["query"] for a in ambiguous} \
        | {u["query"] for u in unfound} | {e["query"] for e in errored}

    def checkpoint():
        _write_json(RESOLVED_PATH, resolved)
        _write_json(AMBIGUOUS_PATH, ambiguous)
        _write_json(UNFOUND_PATH, unfound)
        _write_json(ERRORED_PATH, errored)

    def progress(i):
        print(f"  ...{i}/{len(queue)}  resolved={len(resolved)} ambiguous={len(ambiguous)} "
              f"unfound={len(unfound)} errored={len(errored)}")

    if done:
        print(f"resume: {len(done)} already processed, continuing.")
    for i, item in enumerate(queue, 1):
        name, lms_rating = item["name"], item.get("rating")
        if name in done:
            continue
        query = _search_name(name)
        try:
            cands = _search_with_retry(fargo_api, query, session)
            if not cands:
                for vq in variant_queries(query):
                    cands = _search_with_retry(fargo_api, vq, session)
                    if cands:
                        break
        except Exception as exc:  # a persistent API failure must not abort the batch
            errored.append({"query": name, "lms_rating": lms_rating, "error": str(exc)})
            if i % CHECKPOINT_EVERY == 0:
                checkpoint(); progress(i)
            time.sleep(0.35)
            continue
        bucket, rec, note = _classify(cands, lms_rating)
        if bucket == "resolved":
            resolved.append({
                "query": name, "lms_rating": lms_rating, "leagues": item.get("leagues") or [],
                "player_id": rec.player_id, "name": rec.name, "note": note, "record": rec.to_dict(),
            })
        elif bucket == "ambiguous":
            ambiguous.append({"query": name, "lms_rating": lms_rating, "note": note,
                              "candidates": [c.to_dict() for c in cands]})
        else:
            unfound.append({"query": name, "lms_rating": lms_rating})
        if i % CHECKPOINT_EVERY == 0:
            checkpoint(); progress(i)
        time.sleep(0.35)

    checkpoint()
    print(f"resolve done: resolved={len(resolved)}  ambiguous={len(ambiguous)}  "
          f"unfound={len(unfound)}  errored={len(errored)}")
    print(f"  -> {_rel(RESOLVED_PATH)} / bca-ambiguous.json / bca-unfound.json / bca-errored.json")
    return 0


# ---------------------------------------------------------------------------
# 3. add (apply resolved -> roster)
# ---------------------------------------------------------------------------
def _bca_link(item) -> dict:
    return {
        "leagues": item.get("leagues") or [],
        "lms_rating": item.get("lms_rating"),
        "source": "bca",
        "match_method": "name+rating" if item.get("lms_rating") is not None else "name",
        "confidence": "high",
        "added_date": dt.date.today().isoformat(),
    }


def cmd_add() -> int:
    resolved = _read_json(RESOLVED_PATH, None)
    if resolved is None:
        print(f"No resolved bucket at {RESOLVED_PATH}. Run `resolve` first.", file=sys.stderr)
        return 1

    roster = load_roster()
    players = roster.setdefault("players", {})
    added = linked = 0
    for item in resolved:
        key = str(item["player_id"])
        entry = players.get(key)
        if entry is None:
            rec = item["record"]
            entry = players[key] = {
                "player_id": item["player_id"],
                "membership_id": rec.get("membership_id"),
                "name": item["name"],
                "added_date": dt.date.today().isoformat(),
                "record": rec.get("raw", rec),
            }
            added += 1
        # additive, deduped bca cross-link (idempotent on re-run)
        links = entry.setdefault("bca", [])
        sig = {tuple(sorted(l.get("leagues") or [])) for l in links}
        link = _bca_link(item)
        if tuple(sorted(link["leagues"])) not in sig:
            links.append(link)
            linked += 1

    save_roster(roster)
    print(f"add done: {added} new roster entries, {linked} bca cross-links. Roster now {len(players)}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fold the BCA/LMS player set into the FargoRate roster.")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("crossref", help="bucket the staged extract vs the roster (no network)")
    c.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    c.add_argument("--dry-run", action="store_true")
    sub.add_parser("resolve", help="search FargoRate per queued name + rating-confirm (network)")
    sub.add_parser("add", help="apply the resolved bucket to roster.json")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "crossref":
        return cmd_crossref(args.input, args.dry_run)
    if args.command == "resolve":
        return cmd_resolve(fargo_api.new_session())
    if args.command == "add":
        return cmd_add()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
