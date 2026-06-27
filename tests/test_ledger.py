"""Tests for the cross-link correction ledger (src/ledger.py) + its importer wiring."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ledger  # noqa: E402
import import_apa  # noqa: E402
import import_napa  # noqa: E402


def test_load_suppress_parses_all_entry_shapes(tmp_path):
    p = tmp_path / "apa_unlink.json"
    p.write_text(json.dumps([
        {"member_id": 7, "player_id": 100},
        {"napa_player_id": 81234567, "player_id": 200},
        {"member_key": "D1:john smith", "fargo_id": 300},
    ]), encoding="utf-8")
    s = ledger.load_suppress(p)
    assert s == {("7", 100), ("81234567", 200), ("D1:john smith", 300)}


def test_load_suppress_missing_file_is_empty(tmp_path):
    assert ledger.load_suppress(tmp_path / "nope.json") == set()


def test_strip_suppressed_removes_only_matching_pair(tmp_path):
    roster = {"players": {
        "100": {"player_id": 100, "apa": [{"member_id": 7}, {"member_id": 8}]},
        "200": {"player_id": 200, "apa": [{"member_id": 7}]},   # member 7 here is NOT suppressed
    }}
    removed = ledger.strip_suppressed(roster, "apa", lambda m: m.get("member_id"), {("7", 100)})
    assert removed == 1
    assert [m["member_id"] for m in roster["players"]["100"]["apa"]] == [8]
    assert [m["member_id"] for m in roster["players"]["200"]["apa"]] == [7]   # untouched


def test_strip_suppressed_drops_empty_list(tmp_path):
    roster = {"players": {"100": {"player_id": 100, "napa": [{"napa_player_id": 9}]}}}
    removed = ledger.strip_suppressed(roster, "napa", lambda m: m.get("napa_player_id"), {("9", 100)})
    assert removed == 1 and "napa" not in roster["players"]["100"]


def test_apa_attach_honors_suppress(tmp_path):
    roster = {"players": {"100": {"player_id": 100, "name": "A"}}}
    n = import_apa._attach_crosslink(roster, "100", [{"member_id": 7, "name": "A"}],
                                     "2026-06-27", suppress={("7", 100)})
    assert n == 0 and "apa" not in roster["players"]["100"]      # wrong link never re-added


def test_napa_attach_honors_suppress(tmp_path):
    roster = {"players": {"100": {"player_id": 100, "name": "A"}}}
    members = [{"napa_player_id": 81234567, "name": "A", "division_id": "13077"}]
    n = import_napa._attach_crosslink(roster, "100", members, "2026-06-27",
                                      suppress={("81234567", 100)})
    assert n == 0 and "napa" not in roster["players"]["100"]
