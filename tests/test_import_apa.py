"""Tests for the APA cross-reference importer (src/import_apa.py).

No network: these cover the name-based bridge logic only (the `resolve` step that
hits FargoRate is exercised on a runner, not here). They lock:

  * fee-prefix cleaning ("Owes $150 Anna Byrd" -> "Anna Byrd") and name norming
  * crossref bucketing into matched / ambiguous / new against a temp roster
  * the apa cross-link is strictly additive — existing fields untouched, re-run
    adds nothing, and one player can carry several APA memberships
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import resolve  # noqa: E402
import import_apa as imp  # noqa: E402


def apa_file(players: dict) -> dict:
    return {"minSession": 120, "sessionsIncluded": [], "players": players,
            "playerCount": len(players), "generatedAt": "2026-06-06T00:00:00+00:00"}


def apa_rec(member_id, number, name, sessions=(123, 124)):
    return {"memberId": member_id, "memberNumber": number, "displayName": name,
            "knownNames": [name], "firstSession": sessions[0],
            "lastSession": sessions[-1], "sessions": list(sessions)}


def test_clean_name_strips_fee_prefixes():
    assert imp.clean_name("Owes $150 Anna Byrd") == "Anna Byrd"
    assert imp.clean_name("OWES$130 Jordan Freeman") == "Jordan Freeman"
    assert imp.clean_name("OWES $60 Tommy Bonney") == "Tommy Bonney"
    assert imp.clean_name("Owes $180Aaron Knobloch") == "Aaron Knobloch"
    # "owe" inside a real surname must NOT be stripped
    assert imp.clean_name("Marvin Owens") == "Marvin Owens"
    assert imp.clean_name("  Ed   Powers ") == "Ed Powers"


def test_norm_matches_across_punctuation_and_case():
    assert imp.norm("James O'Neill") == imp.norm("james oneill")
    assert imp.norm("Mary-Jane Smith") == "mary jane smith"


def _setup(tmp_path, monkeypatch, roster_players, apa_players):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"players": roster_players}), encoding="utf-8")
    monkeypatch.setattr(resolve, "ROSTER_PATH", roster_path)

    resolve_dir = tmp_path / "resolve"
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "TO_RESOLVE_PATH", resolve_dir / "apa_to_resolve.json")
    monkeypatch.setattr(imp, "REPORT_PATH", resolve_dir / "apa_crossref_report.json")

    src = tmp_path / "players_master.json"
    src.write_text(json.dumps(apa_file(apa_players)), encoding="utf-8")
    return roster_path, src


def test_crossref_buckets_matched_ambiguous_new(tmp_path, monkeypatch):
    roster_players = {
        "100": {"player_id": 100, "name": "Anna Byrd"},          # single -> matched
        "200": {"player_id": 200, "name": "Tim Brown"},          # collides ->
        "201": {"player_id": 201, "name": "Tim Brown"},          #   ambiguous
    }
    apa_players = {
        "1": apa_rec(1, "80400001", "Owes $150 Anna Byrd"),      # matches id 100
        "2": apa_rec(2, "80400002", "Tim Brown"),                # ambiguous (2 ids)
        "3": apa_rec(3, "80400003", "Brand New Person"),         # new
    }
    roster_path, src = _setup(tmp_path, monkeypatch, roster_players, apa_players)

    rep = imp.crossref(src, dry_run=False, today="2026-06-06")

    assert rep["matched_single"] == 1
    assert rep["ambiguous_existing"] == 1
    assert rep["new_to_resolve"] == 1

    # matched -> cross-link attached to the existing entry, additively
    saved = json.loads(roster_path.read_text(encoding="utf-8"))
    link = saved["players"]["100"]["apa"]
    assert len(link) == 1
    assert link[0]["member_number"] == "80400001"
    assert link[0]["name"] == "Anna Byrd"          # fee prefix cleaned
    assert link[0]["match_method"] == "name"

    # new -> queued for resolution with its membership carried along
    queue = json.loads((imp.RESOLVE_DIR / "apa_to_resolve.json").read_text(encoding="utf-8"))
    assert [q["search_name"] for q in queue] == ["Brand New Person"]
    assert queue[0]["memberships"][0]["member_id"] == 3


def test_crosslink_is_additive_and_idempotent(tmp_path, monkeypatch):
    roster_players = {"100": {"player_id": 100, "name": "Anna Byrd",
                              "membership_id": "9900", "added_date": "2026-01-01"}}
    apa_players = {"1": apa_rec(1, "80400001", "Anna Byrd")}
    roster_path, src = _setup(tmp_path, monkeypatch, roster_players, apa_players)

    imp.crossref(src, dry_run=False, today="2026-06-06")
    imp.crossref(src, dry_run=False, today="2026-06-07")   # re-run

    saved = json.loads(roster_path.read_text(encoding="utf-8"))
    entry = saved["players"]["100"]
    assert entry["membership_id"] == "9900"               # existing field untouched
    assert entry["added_date"] == "2026-01-01"            # not rewritten
    assert len(entry["apa"]) == 1                         # no duplicate on re-run
    assert entry["apa"][0]["added_date"] == "2026-06-06"  # first link's date kept


def test_multiple_apa_memberships_on_one_player(tmp_path, monkeypatch):
    roster_players = {"100": {"player_id": 100, "name": "Jorge Aguilar"}}
    apa_players = {
        "1": apa_rec(1, "80402064", "Jorge Aguilar"),
        "2": apa_rec(2, "80401813", "Jorge Aguilar"),     # same human, 2nd membership
    }
    roster_path, src = _setup(tmp_path, monkeypatch, roster_players, apa_players)

    rep = imp.crossref(src, dry_run=False, today="2026-06-06")

    assert rep["matched_single"] == 1
    saved = json.loads(roster_path.read_text(encoding="utf-8"))
    numbers = {m["member_number"] for m in saved["players"]["100"]["apa"]}
    assert numbers == {"80402064", "80401813"}            # all memberships kept


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    roster_players = {"100": {"player_id": 100, "name": "Anna Byrd"}}
    apa_players = {"1": apa_rec(1, "80400001", "Anna Byrd")}
    roster_path, src = _setup(tmp_path, monkeypatch, roster_players, apa_players)

    before = roster_path.read_text(encoding="utf-8")
    imp.crossref(src, dry_run=True, today="2026-06-06")

    assert roster_path.read_text(encoding="utf-8") == before
    assert not (imp.RESOLVE_DIR / "apa_to_resolve.json").exists()
