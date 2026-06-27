"""Tests for the person/profile layer (src/people.py). No network.

Lock the things that matter:
  * merges group player_ids into one person (smallest id is the person_id)
  * a person aggregates source-tagged, deduped memberships + a source union
  * unmerged player_ids each stand alone
  * profiles render merged people and read current ratings from history.csv
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import people as ppl  # noqa: E402


def _setup(tmp_path, monkeypatch, roster_players, merges):
    rp = tmp_path / "roster.json"
    rp.write_text(json.dumps({"players": roster_players}), encoding="utf-8")
    mp = tmp_path / "people_merges.json"
    mp.write_text(json.dumps(merges), encoding="utf-8")
    pp = tmp_path / "people.json"
    monkeypatch.setattr(ppl, "ROSTER_PATH", rp)
    monkeypatch.setattr(ppl, "MERGES_PATH", mp)
    monkeypatch.setattr(ppl, "PEOPLE_PATH", pp)
    return rp, pp


def test_build_merges_into_one_person(tmp_path, monkeypatch):
    roster_players = {
        "1058794": {"player_id": 1058794, "name": "Greg Brown", "source": "apa",
                    "added_date": "2026-06-07",
                    "apa": [{"source": "apa", "member_id": 1, "member_number": "80401514",
                             "match_method": "manual"}]},
        "1015017": {"player_id": 1015017, "name": "Greg Brown", "source": "apa",
                    "added_date": "2026-06-06",
                    "apa": [{"source": "apa", "member_id": 1, "member_number": "80401514",
                             "match_method": "manual"}]},
        "999": {"player_id": 999, "name": "Someone Else", "source": "digitalpool",
                "added_date": "2026-06-05"},
    }
    merges = [{"person": "Greg Brown", "fargo_player_ids": [1015017, 1058794], "reason": "same"}]
    _, pp = _setup(tmp_path, monkeypatch, roster_players, merges)

    out = ppl.build(today="2026-06-07")
    assert out["person_count"] == 2 and out["merged_count"] == 1

    people = json.loads(pp.read_text(encoding="utf-8"))["people"]
    greg = people["1015017"]                       # person_id is the SMALLEST id
    assert greg["fargo_player_ids"] == [1015017, 1058794]
    assert len(greg["memberships"]) == 1           # deduped across the two entries
    assert greg["memberships"][0]["source"] == "apa"
    assert greg["sources"] == ["apa"]
    assert greg["added_date"] == "2026-06-06"      # earliest of the two
    assert greg["notes"] == "same"
    assert people["999"]["fargo_player_ids"] == [999]   # unmerged stands alone


def test_sources_union_and_membership_dedup(tmp_path, monkeypatch):
    roster_players = {
        "100": {"player_id": 100, "name": "Dual Source", "source": "digitalpool",
                "apa": [{"source": "apa", "member_id": 7, "member_number": "80400007"}]},
    }
    _, pp = _setup(tmp_path, monkeypatch, roster_players, [])
    ppl.build(today="2026-06-07")
    p = json.loads(pp.read_text(encoding="utf-8"))["people"]["100"]
    assert p["sources"] == ["apa", "digitalpool"]   # union, sorted
    assert len(p["memberships"]) == 1


def test_chained_merges_collapse_to_one_person(tmp_path, monkeypatch):
    roster_players = {str(i): {"player_id": i, "name": "Chain", "source": "apa"} for i in (10, 20, 30)}
    merges = [{"person": "Chain", "fargo_player_ids": [10, 20]},
              {"person": "Chain", "fargo_player_ids": [20, 30]}]
    _, pp = _setup(tmp_path, monkeypatch, roster_players, merges)
    out = ppl.build(today="2026-06-07")
    assert out["person_count"] == 1
    assert json.loads(pp.read_text(encoding="utf-8"))["people"]["10"]["fargo_player_ids"] == [10, 20, 30]


def test_profiles_render_merged_with_history(tmp_path, monkeypatch):
    roster_players = {
        "1015017": {"player_id": 1015017, "name": "Greg Brown", "source": "apa",
                    "apa": [{"source": "apa", "member_number": "80401514", "match_method": "manual"}]},
        "1058794": {"player_id": 1058794, "name": "Greg Brown", "source": "apa"},
        "999": {"player_id": 999, "name": "Solo Player", "source": "digitalpool"},
    }
    merges = [{"person": "Greg Brown", "fargo_player_ids": [1015017, 1058794], "reason": "same"}]
    _setup(tmp_path, monkeypatch, roster_players, merges)
    ppl.build(today="2026-06-07")

    hist = tmp_path / "history.csv"
    with hist.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date_found", "player_id", "readable_id", "name", "rating",
                    "robustness", "rating_quality", "entry_type"])
        w.writerow(["2026-06-07", "1015017", "", "Greg Brown", "447", "234", "established", "baseline"])
    monkeypatch.setattr(ppl, "HISTORY_PATH", hist)
    prof = tmp_path / "profiles.md"
    monkeypatch.setattr(ppl, "PROFILES_PATH", prof)

    n = ppl.profiles(today="2026-06-07", all_people=False)
    assert n == 1                                   # only the merged person by default
    text = prof.read_text(encoding="utf-8")
    assert "Greg Brown" in text and "merged profile — 2 FargoRate accounts" in text
    assert "rating 447, robustness 234" in text     # from history
    assert "pending first pull" in text             # 1058794 has no history row yet
    assert "Solo Player" not in text                # single-source singleton excluded by default

    assert ppl.profiles(today="2026-06-07", all_people=True) == 2   # --all: merged Greg + Solo


def test_napa_and_bca_memberships_dedup_without_collapse(tmp_path, monkeypatch):
    roster_players = {
        "100": {"player_id": 100, "name": "Multi League", "source": "digitalpool",
                "apa": [{"source": "apa", "member_id": 7, "member_number": "80400007",
                         "name": "Multi League"}],
                "napa": [{"source": "napa", "napa_player_id": 10000001, "name": "Multi League",
                          "division_id": "13077", "confidence": "high"}],
                "bca": [{"source": "bca", "bca_division_id": "D1", "name": "Multi League",
                         "confidence": "high"},
                        {"source": "bca", "bca_division_id": "D2", "name": "Multi League",
                         "confidence": "high"}]},
    }
    _, pp = _setup(tmp_path, monkeypatch, roster_players, [])
    ppl.build(today="2026-06-27")
    p = json.loads(pp.read_text(encoding="utf-8"))["people"]["100"]
    assert p["sources"] == ["apa", "bca", "digitalpool", "napa"]       # union, sorted
    by_source: dict = {}
    for m in p["memberships"]:
        by_source.setdefault(m["source"], []).append(m)
    # BCA's two divisions survive (synthetic division:name key), NOT collapsed to (bca, None)
    assert len(by_source["bca"]) == 2
    assert len(by_source["napa"]) == 1 and len(by_source["apa"]) == 1


def test_crosswalk_maps_source_ids_to_fargo_and_person(tmp_path, monkeypatch):
    roster_players = {
        "100": {"player_id": 100, "name": "Greg Brown", "source": "apa",
                "apa": [{"source": "apa", "member_id": 7, "member_number": "80400007",
                         "name": "Greg Brown", "match_method": "name"}]},
        "1058794": {"player_id": 1058794, "name": "Greg Brown", "source": "napa",
                    "napa": [{"source": "napa", "napa_player_id": 10000005, "name": "Greg Brown",
                              "division_id": "13077", "match_method": "name", "confidence": "high"}]},
    }
    merges = [{"person": "Greg Brown", "fargo_player_ids": [100, 1058794], "reason": "same"}]
    _, pp = _setup(tmp_path, monkeypatch, roster_players, merges)
    cw = tmp_path / "crosswalk.json"
    monkeypatch.setattr(ppl, "CROSSWALK_PATH", cw)

    ppl.build(today="2026-06-27")
    out = ppl.crosswalk(today="2026-06-27")

    # both leagues' ids resolve to their fargo account AND the shared person_id (smallest id)
    assert out["apa"]["7"] == {"fargo_player_id": 100, "person_id": 100,
                               "confidence": "high", "match_method": "name"}
    assert out["napa"]["10000005"]["fargo_player_id"] == 1058794
    assert out["napa"]["10000005"]["person_id"] == 100
    assert out["counts"] == {"apa": 1, "napa": 1, "bca": 0}
    assert json.loads(cw.read_text(encoding="utf-8"))["napa"]["10000005"]["confidence"] == "high"
