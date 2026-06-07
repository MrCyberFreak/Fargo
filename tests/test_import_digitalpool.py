"""Tests for the DigitalPool roster importer (src/import_digitalpool.py).

These exercise the import logic against a tiny in-memory ndjson fixture and a
temp roster.json — no network, no real data files. They lock the three things
that matter:

  * the join key comes from fargo_data.readableId (NOT the misleading top-level
    fargo_id, which is the membership number with leading zeros stripped)
  * the state filter and the no-id skip behave
  * dedup by player_id, and an idempotent second merge adds nothing
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import resolve  # noqa: E402
import import_digitalpool as imp  # noqa: E402


def dp_record(dp_id, readable_id, *, state="CO", membership="0001234",
              first="Test", last="Player", top_fargo_id=999):
    """A minimal DigitalPool-shaped record. top_fargo_id is the TRAP field."""
    return {
        "id": dp_id,
        "name": f"{first} {last}",
        "fargo_id": top_fargo_id,  # deliberately wrong-looking; must be ignored
        "properties": {"fargo_data": {
            "readableId": readable_id,
            "membershipId": membership,
            "firstName": first, "lastName": last,
            "state": state, "effectiveRating": 500, "robustness": 250,
        }},
    }


def write_ndjson(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_keys_on_readable_id_not_top_fargo_id(tmp_path):
    src = tmp_path / "dp.ndjson"
    write_ndjson(src, [dp_record(467, "11320", membership="0053174", top_fargo_id=53174)])

    cands, stats = imp.read_candidates(src, state_filter="CO")

    assert list(cands) == [11320]               # readableId, not 53174
    assert cands[11320]["membership_id"] == "0053174"
    assert cands[11320]["source"] == "digitalpool"
    assert cands[11320]["digitalpool_id"] == 467
    assert stats["unique"] == 1


def test_state_filter_and_missing_id_are_skipped(tmp_path):
    src = tmp_path / "dp.ndjson"
    write_ndjson(src, [
        dp_record(1, "11320", state="CO"),
        dp_record(2, "22330", state="WY"),          # filtered out
        dp_record(3, None, state="CO"),             # no usable id
        {"id": 4, "properties": {}},                # no fargo_data at all
    ])

    cands, stats = imp.read_candidates(src, state_filter="CO")

    assert set(cands) == {11320}
    assert stats["filtered_state"] == 1
    assert stats["no_id"] == 2


def test_dedup_keeps_first_occurrence(tmp_path):
    src = tmp_path / "dp.ndjson"
    write_ndjson(src, [
        dp_record(10, "11320", first="First"),
        dp_record(11, "11320", first="Second"),     # same player_id, later row
    ])

    cands, stats = imp.read_candidates(src, state_filter="CO")

    assert stats["kept_rows"] == 2 and stats["unique"] == 1
    assert cands[11320]["digitalpool_id"] == 10     # first wins


def test_merge_is_idempotent(tmp_path, monkeypatch):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text('{"players": {}}')
    monkeypatch.setattr(resolve, "ROSTER_PATH", roster_path)

    cands = {11320: {"player_id": 11320, "membership_id": "0053174",
                     "name": "Test Player", "state": "CO",
                     "source": "digitalpool", "digitalpool_id": 467}}

    first = imp.merge_into_roster(cands, today="2026-06-06")
    assert first["added"] == 1 and first["roster_total"] == 1

    second = imp.merge_into_roster(cands, today="2026-06-07")
    assert second["added"] == 0 and second["already_present"] == 1

    saved = json.loads(roster_path.read_text())
    assert saved["players"]["11320"]["added_date"] == "2026-06-06"  # not overwritten
