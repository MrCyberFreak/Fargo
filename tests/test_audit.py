"""Tests for the read-only identity audits (src/audit.py). No network, no mutation."""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import audit  # noqa: E402
import people  # noqa: E402


def _setup(tmp_path, monkeypatch, roster, merges=None):
    rp = tmp_path / "roster.json"
    rp.write_text(json.dumps(roster), encoding="utf-8")
    mp = tmp_path / "people_merges.json"
    mp.write_text(json.dumps(merges or []), encoding="utf-8")
    monkeypatch.setattr(audit, "ROSTER_PATH", rp)
    monkeypatch.setattr(audit, "MERGES_PATH", mp)
    rd = tmp_path / "resolve"
    monkeypatch.setattr(audit, "RESOLVE_DIR", rd)
    for name in ("COLLISIONS_PATH", "COLLISION_ALLOWLIST_PATH", "NAME_DIVERGENCE_PATH",
                 "MERGE_SANITY_PATH", "MERGE_CANDIDATES_PATH"):
        monkeypatch.setattr(audit, name, rd / f"{name.lower()}.json")
    return rp


def test_collisions_flags_one_member_on_two_fargo_ids(tmp_path, monkeypatch):
    roster = {"players": {
        "100": {"player_id": 100, "name": "A", "apa": [{"source": "apa", "member_id": 7, "name": "A"}]},
        "200": {"player_id": 200, "name": "B", "apa": [{"source": "apa", "member_id": 7, "name": "B"}]},
        "300": {"player_id": 300, "name": "C", "napa": [{"source": "napa", "napa_player_id": 99, "name": "C"}]},
    }}
    _setup(tmp_path, monkeypatch, roster)
    out = audit.collisions("2026-06-27")
    assert out["collision_count"] == 1
    c = out["collisions"][0]
    assert c["source"] == "apa" and c["member_key"] == "7" and c["fargo_ids"] == [100, 200]


def test_collisions_ignores_merged_same_person_as_redundant(tmp_path, monkeypatch):
    # one APA member on two Fargo ids that ARE merged = same human, two accounts.
    roster = {"players": {
        "10": {"player_id": 10, "name": "Dup", "apa": [{"source": "apa", "member_id": 7, "name": "Dup"}]},
        "20": {"player_id": 20, "name": "Dup", "apa": [{"source": "apa", "member_id": 7, "name": "Dup"}]},
    }}
    merges = [{"person": "Dup", "fargo_player_ids": [10, 20]}]
    _setup(tmp_path, monkeypatch, roster, merges)
    out = audit.collisions("2026-06-27")
    assert out["collision_count"] == 0                       # not a true collision
    assert out["redundant_same_person_count"] == 1
    assert out["redundant_same_person"][0]["person_id"] == 10


def test_collisions_empty_when_clean(tmp_path, monkeypatch):
    roster = {"players": {
        "100": {"player_id": 100, "name": "A", "napa": [{"source": "napa", "napa_player_id": 1, "name": "A"}]},
        "200": {"player_id": 200, "name": "B",
                "bca": [{"source": "bca", "leagues": ["D1", "D2"]}]},  # 2 distinct league keys (name on parent)
    }}
    _setup(tmp_path, monkeypatch, roster)
    assert audit.collisions("2026-06-27")["collision_count"] == 0


def test_collisions_allowlist_accepts_reviewed_ambiguous(tmp_path, monkeypatch):
    # Two DISTINCT (unmerged) people sharing an id-less BCA league:name key.
    roster = {"players": {
        "100": {"player_id": 100, "name": "Pat Riley",
                "bca": [{"source": "bca", "leagues": ["lg1"], "confidence": "high"}]},
        "200": {"player_id": 200, "name": "Pat Riley",
                "bca": [{"source": "bca", "leagues": ["lg1"], "confidence": "high"}]},
    }}
    rp = _setup(tmp_path, monkeypatch, roster)
    allow = audit.COLLISION_ALLOWLIST_PATH
    allow.parent.mkdir(parents=True, exist_ok=True)

    # Unlisted -> a real hard collision.
    allow.write_text(json.dumps({"allow": []}), encoding="utf-8")
    out = audit.collisions("2026-07-14")
    assert out["collision_count"] == 1
    assert out["accepted_ambiguous_count"] == 0

    # Listed with the exact reviewed fids -> accepted, hard gate clears.
    allow.write_text(json.dumps({"allow": [
        {"source": "bca", "member_key": "lg1:pat riley", "fargo_ids": [100, 200]}]}),
        encoding="utf-8")
    out = audit.collisions("2026-07-14")
    assert out["collision_count"] == 0
    assert out["accepted_ambiguous_count"] == 1
    assert out["accepted_ambiguous"][0]["member_key"] == "lg1:pat riley"

    # A NEW unreviewed id on the same key re-flags it (fids not subset of reviewed set).
    roster["players"]["300"] = {"player_id": 300, "name": "Pat Riley",
                                "bca": [{"source": "bca", "leagues": ["lg1"], "confidence": "high"}]}
    rp.write_text(json.dumps(roster), encoding="utf-8")
    assert audit.collisions("2026-07-14")["collision_count"] == 1


def test_merge_candidates_cross_source_same_name_diff_ids(tmp_path, monkeypatch):
    roster = {"players": {
        "100": {"player_id": 100, "name": "John Smith",
                "apa": [{"source": "apa", "member_id": 1, "name": "John Smith"}]},
        "200": {"player_id": 200, "name": "John Smith",
                "napa": [{"source": "napa", "napa_player_id": 2, "name": "John Smith"}]},
        "300": {"player_id": 300, "name": "Solo One",
                "apa": [{"source": "apa", "member_id": 3, "name": "Solo One"}]},
    }}
    _setup(tmp_path, monkeypatch, roster)
    out = audit.merge_candidates("2026-06-27")
    assert out["candidate_count"] == 1
    cand = out["candidates"][0]
    assert cand["norm"] == "john smith"
    assert [f["fargo_player_id"] for f in cand["fargo_ids"]] == [100, 200]
    assert {s for f in cand["fargo_ids"] for s in f["sources"]} == {"apa", "napa"}


def test_name_divergence_flags_surname_mismatch_only(tmp_path, monkeypatch):
    roster = {"players": {
        # compatible (truncation): Shirish vs Shirishkumar -> NOT flagged
        "10": {"player_id": 10, "name": "Shirish Patel",
               "apa": [{"source": "apa", "member_id": 1, "name": "Shirishkumar Patel"}]},
        # surname mismatch -> flagged
        "20": {"player_id": 20, "name": "Bob Jones",
               "apa": [{"source": "apa", "member_id": 2, "name": "Bob Williams"}]},
    }}
    _setup(tmp_path, monkeypatch, roster)
    out = audit.name_divergence("2026-06-27")
    assert out["flag_count"] == 1
    assert out["flags"][0]["fargo_player_id"] == 20 and out["flags"][0]["link_name"] == "Bob Williams"


def test_merge_sanity_flags_far_apart_established(tmp_path, monkeypatch):
    roster = {"players": {
        "10": {"player_id": 10, "name": "Dup", "state": "CO"},
        "20": {"player_id": 20, "name": "Dup", "state": "CO"},
    }}
    merges = [{"person": "Dup", "fargo_player_ids": [10, 20]}]
    _setup(tmp_path, monkeypatch, roster, merges)

    hist = tmp_path / "history.csv"
    with hist.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date_found", "player_id", "readable_id", "name", "rating",
                    "robustness", "rating_quality", "entry_type"])
        w.writerow(["2026-06-01", "10", "", "Dup", "700", "250", "established", "baseline"])
        w.writerow(["2026-06-01", "20", "", "Dup", "400", "250", "established", "baseline"])
    monkeypatch.setattr(people, "HISTORY_PATH", hist)

    out = audit.merge_sanity("2026-06-27")
    assert out["flag_count"] == 1
    assert out["flags"][0]["rating_spread"] == 300 and out["flags"][0]["person_id"] == 10
