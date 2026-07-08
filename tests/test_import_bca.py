"""Tests for the BCA/LMS roster importer (src/import_bca.py).

No network: `fargo_api.search` is stubbed. These lock the behavior that keeps the
never-pruned roster clean:

  * crossref drops names already on the roster (idempotent re-runs) and dedups the
    extract; only genuinely-new names are queued.
  * the RATING-CONFIRM gate -- the discriminator BCA has that APA/NAPA don't:
      - a lone CO match is accepted (name-only ok), UNLESS its live rating is wildly
        off the LMS snapshot (stale-collision guard) -> demoted to ambiguous;
      - a genuine multi-CO collision is auto-resolved ONLY when the rating singles
        out exactly one nearby candidate, else it stays ambiguous (never guessed);
      - no candidate (after variants) -> unfound.
  * add creates a full new entry for a new id, attaches an additive bca[] cross-link,
    and is idempotent (re-running adds neither a duplicate entry nor a duplicate link).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fargo_api  # noqa: E402
import resolve  # noqa: E402
import import_bca as imp  # noqa: E402


def rec(player_id, name, location, *, rating=400, robustness=50, membership="9900"):
    return fargo_api.PlayerRecord(
        player_id=player_id, membership_id=membership, name=name, rating=rating,
        robustness=robustness, location=location,
        rating_quality=fargo_api.quality_for(robustness),
        raw={"Id": player_id, "FullName": name, "State": location, "FargoRating": str(rating)})


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Point every read/write path at a tmp dir."""
    monkeypatch.setattr(resolve, "ROSTER_PATH", tmp_path / "roster.json")
    monkeypatch.setattr(imp, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(imp, "RESOLVED_PATH", tmp_path / "resolved.json")
    monkeypatch.setattr(imp, "AMBIGUOUS_PATH", tmp_path / "ambiguous.json")
    monkeypatch.setattr(imp, "UNFOUND_PATH", tmp_path / "unfound.json")
    monkeypatch.setattr(imp, "ERRORED_PATH", tmp_path / "errored.json")
    monkeypatch.setattr(imp.time, "sleep", lambda *_: None)
    return tmp_path


def write_roster(path, entries):
    path.write_text(json.dumps({"players": entries}, ensure_ascii=False), encoding="utf-8")


def stub_search(monkeypatch, mapping):
    """mapping: query-string -> [PlayerRecord]. Unknown queries return []."""
    monkeypatch.setattr(imp, "_search_with_retry",
                        lambda api, name, session, attempts=3: mapping.get(name, []))


# --- crossref -------------------------------------------------------------
def test_crossref_drops_rostered_and_dedups(paths, monkeypatch):
    write_roster(resolve.ROSTER_PATH, {"1": {"player_id": 1, "name": "Scott Hills"}})
    extract = paths / "extract.json"
    extract.write_text(json.dumps([
        {"name": "Scott Hills", "rating": 448},   # already rostered -> dropped
        {"name": "Jane Doe", "rating": 500},       # new
        {"name": "jane  doe", "rating": 500},      # dup of Jane Doe (normalized) -> dropped
    ]), encoding="utf-8")
    assert imp.cmd_crossref(extract, dry_run=False) == 0
    queue = json.loads(imp.QUEUE_PATH.read_text())
    assert [q["name"] for q in queue] == ["Jane Doe"]


# --- resolve: rating-confirm gate ----------------------------------------
def test_lone_co_match_confirmed_by_rating(paths, monkeypatch):
    imp.QUEUE_PATH.write_text(json.dumps([{"name": "Aaron Derr", "rating": 425, "leagues": ["x"]}]))
    stub_search(monkeypatch, {"Aaron Derr": [rec(10, "Aaron Derr", "Denver CO", rating=424)]})
    imp.cmd_resolve(session=None)
    resolved = json.loads(imp.RESOLVED_PATH.read_text())
    assert len(resolved) == 1 and resolved[0]["player_id"] == 10
    assert json.loads(imp.AMBIGUOUS_PATH.read_text()) == []


def test_lone_co_match_with_wild_rating_is_demoted(paths, monkeypatch):
    imp.QUEUE_PATH.write_text(json.dumps([{"name": "John Smith", "rating": 300, "leagues": []}]))
    # single CO namesake, but 250 points off the snapshot -> almost certainly not them
    stub_search(monkeypatch, {"John Smith": [rec(11, "John Smith", "Denver CO", rating=550)]})
    imp.cmd_resolve(session=None)
    assert json.loads(imp.RESOLVED_PATH.read_text()) == []
    assert len(json.loads(imp.AMBIGUOUS_PATH.read_text())) == 1


def test_co_collision_broken_by_rating(paths, monkeypatch):
    imp.QUEUE_PATH.write_text(json.dumps([{"name": "Adam Lee", "rating": 500, "leagues": []}]))
    stub_search(monkeypatch, {"Adam Lee": [
        rec(20, "Adam Lee", "Denver CO", rating=498),   # matches snapshot
        rec(21, "Adam Lee", "Boulder CO", rating=350),  # far off
    ]})
    imp.cmd_resolve(session=None)
    resolved = json.loads(imp.RESOLVED_PATH.read_text())
    assert len(resolved) == 1 and resolved[0]["player_id"] == 20


def test_co_collision_unbreakable_stays_ambiguous(paths, monkeypatch):
    imp.QUEUE_PATH.write_text(json.dumps([{"name": "Adam Lee", "rating": 500, "leagues": []}]))
    stub_search(monkeypatch, {"Adam Lee": [
        rec(20, "Adam Lee", "Denver CO", rating=498),   # both within tolerance,
        rec(21, "Adam Lee", "Boulder CO", rating=505),  # rating can't separate them
    ]})
    imp.cmd_resolve(session=None)
    assert json.loads(imp.RESOLVED_PATH.read_text()) == []
    assert len(json.loads(imp.AMBIGUOUS_PATH.read_text())) == 1


def test_no_candidate_is_unfound(paths, monkeypatch):
    imp.QUEUE_PATH.write_text(json.dumps([{"name": "Ghost Player", "rating": 400, "leagues": []}]))
    stub_search(monkeypatch, {})  # nothing, and no variant hits either
    imp.cmd_resolve(session=None)
    assert len(json.loads(imp.UNFOUND_PATH.read_text())) == 1


def test_api_error_is_bucketed_not_fatal(paths, monkeypatch):
    # A persistent HTTP 500 on ONE player must not abort the batch (the crash bug).
    imp.QUEUE_PATH.write_text(json.dumps([
        {"name": "Bad Player", "rating": 400, "leagues": []},
        {"name": "Aaron Derr", "rating": 425, "leagues": []},
    ]))

    def flaky(api, name, session, attempts=3):
        if name == "Bad Player":
            raise fargo_api.FargoApiError("HTTP 500 for search 'Bad Player'")
        return [rec(10, "Aaron Derr", "Denver CO", rating=424)]

    monkeypatch.setattr(imp, "_search_with_retry", flaky)
    assert imp.cmd_resolve(session=None) == 0                    # did not raise
    assert len(json.loads(imp.ERRORED_PATH.read_text())) == 1     # bad one bucketed
    assert len(json.loads(imp.RESOLVED_PATH.read_text())) == 1    # good one still resolved


def test_resolve_resumes_and_skips_done(paths, monkeypatch):
    # A prior (interrupted) run already resolved "Aaron Derr"; the resume must skip
    # it (not re-search) and only process the still-pending "Jane Doe".
    imp.QUEUE_PATH.write_text(json.dumps([
        {"name": "Aaron Derr", "rating": 425, "leagues": []},
        {"name": "Jane Doe", "rating": 500, "leagues": []},
    ]))
    imp.RESOLVED_PATH.write_text(json.dumps([
        {"query": "Aaron Derr", "player_id": 10, "name": "Aaron Derr", "lms_rating": 425,
         "leagues": [], "note": "prior", "record": {}},
    ]))
    searched = []

    def track(api, name, session, attempts=3):
        searched.append(name)
        return [rec(20, "Jane Doe", "Denver CO", rating=500)] if name == "Jane Doe" else []

    monkeypatch.setattr(imp, "_search_with_retry", track)
    imp.cmd_resolve(session=None)
    assert "Aaron Derr" not in searched            # skipped on resume
    resolved = json.loads(imp.RESOLVED_PATH.read_text())
    assert {r["player_id"] for r in resolved} == {10, 20}   # kept old + added new


def test_search_name_strips_nickname_and_spaces():
    assert imp._search_name("Brian Noffsinger     (Bubba)") == "Brian Noffsinger"
    assert imp._search_name("Jane Doe") == "Jane Doe"


# --- add: creation, cross-link, idempotency ------------------------------
def test_add_creates_entry_and_link_idempotently(paths, monkeypatch):
    write_roster(resolve.ROSTER_PATH, {})
    imp.RESOLVED_PATH.write_text(json.dumps([{
        "query": "Aaron Derr", "lms_rating": 425, "leagues": ["denver-goodtimes-bcapl"],
        "player_id": 10, "name": "Aaron Derr", "note": "unique-co",
        "record": rec(10, "Aaron Derr", "Denver CO", rating=424).to_dict(),
    }]))
    imp.cmd_add()
    roster = json.loads(resolve.ROSTER_PATH.read_text())["players"]
    assert "10" in roster
    assert roster["10"]["name"] == "Aaron Derr"
    assert len(roster["10"]["bca"]) == 1
    assert roster["10"]["bca"][0]["leagues"] == ["denver-goodtimes-bcapl"]

    # re-run: no duplicate entry, no duplicate link
    imp.cmd_add()
    roster2 = json.loads(resolve.ROSTER_PATH.read_text())["players"]
    assert len(roster2) == 1 and len(roster2["10"]["bca"]) == 1


def test_add_links_existing_id_without_clobbering(paths, monkeypatch):
    write_roster(resolve.ROSTER_PATH, {"10": {"player_id": 10, "name": "Aaron Derr", "record": {"Id": 10}}})
    imp.RESOLVED_PATH.write_text(json.dumps([{
        "query": "Aaron Derr", "lms_rating": 425, "leagues": ["lucky-shot-bca-pool-league"],
        "player_id": 10, "name": "Aaron Derr", "note": "unique-co",
        "record": rec(10, "Aaron Derr", "Denver CO", rating=424).to_dict(),
    }]))
    imp.cmd_add()
    roster = json.loads(resolve.ROSTER_PATH.read_text())["players"]
    assert len(roster) == 1                       # no new entry
    assert roster["10"]["bca"][0]["leagues"] == ["lucky-shot-bca-pool-league"]
