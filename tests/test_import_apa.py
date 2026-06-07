"""Tests for the APA cross-reference importer (src/import_apa.py).

No network: these cover the name-based bridge logic only (the `resolve` step's
real FargoRate calls are exercised on a runner; here `fargo_api.search` is
stubbed). They lock:

  * fee-prefix cleaning ("Owes $150 Anna Byrd" -> "Anna Byrd") and name norming
  * crossref bucketing into matched / ambiguous / new against a temp roster
  * the apa cross-link is strictly additive — existing fields untouched, re-run
    adds nothing, and one player can carry several APA memberships
  * nickname variant expansion, CO-preferred pick_match, resolve() bucket routing
    (resolved / variant_candidates / ambiguous / unfound), and add() upsert
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fargo_api  # noqa: E402
import resolve  # noqa: E402
import import_apa as imp  # noqa: E402


def rec(player_id, name, location, *, rating=400, robustness=50, membership="9900"):
    """A FargoRate search-result record."""
    return fargo_api.PlayerRecord(
        player_id=player_id, membership_id=membership, name=name, rating=rating,
        robustness=robustness, location=location,
        rating_quality=fargo_api.quality_for(robustness))


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


# --- resolve() helpers --------------------------------------------------------

def test_variant_queries_expands_first_name_keeps_surname():
    assert imp.variant_queries("Andy Carroll") == ["andrew Carroll", "drew Carroll"]
    assert imp.variant_queries("Owes $50 Mike Smith") == ["michael Smith", "mick Smith"]
    assert imp.variant_queries("Zzyzx Nomatch") == []   # no nickname mapping
    assert imp.variant_queries("Cher") == []            # single token


def test_is_co_handles_inconsistent_location_formats():
    assert imp.is_co("CO") and imp.is_co("co")
    assert imp.is_co("Denver CO") and imp.is_co("Denver, CO") and imp.is_co("Highlands Ranch CO")
    assert not imp.is_co("TX") and not imp.is_co("Fort Worth TX")
    assert not imp.is_co("") and not imp.is_co(None)


def test_first_compatible_prefix_and_nickname():
    assert imp.first_compatible("shirishkumar", "shirish")   # truncation
    assert imp.first_compatible("dan", "daniel")             # prefix
    assert imp.first_compatible("mike", "michael")           # nickname map
    assert not imp.first_compatible("shirishkumar", "shirley")   # shared prefix only, not full prefix
    assert imp.first_compatible("bob", "robert")            # nickname pair (both directions)
    assert imp.first_compatible("robert", "bob")
    assert not imp.first_compatible("jo", "joseph")          # too short (<3) to prefix-match
    assert not imp.first_compatible("alex", "alana")         # share "al" only -> not compatible


def test_recover_query_uses_first_name_prefix_plus_surname():
    assert imp.recover_query("Shirishkumar Patel") == "Shir Patel"
    assert imp.recover_query("Anna Byrd") == "Anna Byrd"     # 4-char first name unchanged
    assert imp.recover_query("Cher") is None                 # single token


def test_surname_and_recover_query_ignore_generational_suffix():
    assert imp.surname("Anthony Tacchia Jr") == "tacchia"    # not "jr"
    assert imp.surname("Bobby Brown II") == "brown"
    assert imp.surname("Fred Castellano Jr.") == "castellano"
    assert imp.recover_query("Anthony Tacchia Jr") == "Anth Tacchia"
    assert imp.recover_query("Daniel Gillespie JR") == "Dani Gillespie"


def test_recover_stages_compatible_matches(tmp_path, monkeypatch):
    resolve_dir = tmp_path / "resolve"; resolve_dir.mkdir()
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "RESOLUTION_PATH", resolve_dir / "apa_resolution.json")
    monkeypatch.setattr(imp, "RECOVERY_PATH", resolve_dir / "apa_recovery.json")
    res = {"generated_at": "x", "queue_size": 0, "resolved": [], "variant_candidates": [],
           "ambiguous": [], "errors": 1,
           "unfound": [
               {"search_name": "Shirishkumar Patel", "memberships": [{"member_id": 9}]},
               {"search_name": "Nobody Whatsoever", "memberships": [{"member_id": 8}]},
               {"search_name": "Anna Byrd", "memberships": [{"member_id": 7}], "error": "HTTP 500"},
           ]}
    (resolve_dir / "apa_resolution.json").write_text(json.dumps(res), encoding="utf-8")

    monkeypatch.setattr(fargo_api, "new_session", lambda: None)

    def fake_search(q, session=None):
        if q == "Shir Patel":
            return [rec(1199370, "Shirish Patel", "Baltimore MD"),
                    rec(999, "Shirley Patton", "TX")]      # wrong surname -> filtered
        return []
    monkeypatch.setattr(fargo_api, "search", fake_search)

    out = imp.recover(today="2026-06-07")
    # Anna Byrd skipped (error), Nobody -> no match, Shirishkumar -> 1 compatible match
    assert len(out["recovered"]) == 1
    r = out["recovered"][0]
    assert r["search_name"] == "Shirishkumar Patel" and r["query"] == "Shir Patel"
    assert [c["player_id"] for c in r["candidates"]] == [1199370]
    assert out["unfound_scanned"] == 2 and out["still_unfound"] == 1


def test_pick_match_treats_city_formatted_co_as_co():
    # "Denver CO" must beat an out-of-state namesake (regression: exact "CO" check)
    status, hit = imp.pick_match([rec(1, "A B", "Denver CO"), rec(2, "A B", "TX")])
    assert status == "resolved" and hit.player_id == 1
    # two CO (city-formatted) -> still ambiguous
    status, _ = imp.pick_match([rec(1, "A B", "Denver CO"), rec(2, "A B", "Boulder CO")])
    assert status == "ambiguous"


def test_pick_match_prefers_co_then_single_then_ambiguous():
    # one CO match wins even when out-of-state namesakes exist
    status, hit = imp.pick_match([rec(1, "A B", "CO"), rec(2, "A B", "TX"), rec(3, "A B", "NY")])
    assert status == "resolved" and hit.player_id == 1
    # no CO, exactly one out-of-state -> resolved (non-CO admitted)
    status, hit = imp.pick_match([rec(2, "A B", "TX")])
    assert status == "resolved" and hit.player_id == 2
    # >1 CO -> ambiguous
    status, hit = imp.pick_match([rec(1, "A B", "CO"), rec(2, "A B", "CO")])
    assert status == "ambiguous" and len(hit) == 2
    # no CO, >1 out-of-state -> ambiguous
    status, hit = imp.pick_match([rec(1, "A B", "TX"), rec(2, "A B", "NY")])
    assert status == "ambiguous"
    # dedup by player_id (same id returned twice is one match)
    status, hit = imp.pick_match([rec(5, "A B", "TX"), rec(5, "A B", "TX")])
    assert status == "resolved" and hit.player_id == 5
    # nothing
    assert imp.pick_match([])[0] == "none"


# --- resolve() bucket routing (fargo_api.search stubbed) ----------------------

def _run_resolve(tmp_path, monkeypatch, queue, search_map):
    resolve_dir = tmp_path / "resolve"
    resolve_dir.mkdir()
    (resolve_dir / "apa_to_resolve.json").write_text(json.dumps(queue), encoding="utf-8")
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "TO_RESOLVE_PATH", resolve_dir / "apa_to_resolve.json")
    monkeypatch.setattr(imp, "RESOLUTION_PATH", resolve_dir / "apa_resolution.json")

    monkeypatch.setattr(fargo_api, "new_session", lambda: None)

    def fake_search(name, session=None):
        out = search_map.get(name)
        if isinstance(out, Exception):
            raise out
        return out or []
    monkeypatch.setattr(fargo_api, "search", fake_search)
    return imp.resolve(today="2026-06-07")


def _q(name, mid=1):
    return {"search_name": name, "norm": imp.norm(name),
            "memberships": [{"member_id": mid, "member_number": f"804{mid:05d}",
                             "name": name}]}


def test_resolve_routes_into_four_buckets(tmp_path, monkeypatch):
    queue = [_q("Clean Match", 1), _q("Out Stater", 2), _q("Two Cos", 3),
             _q("Andy Variant", 4), _q("Ghost Nobody", 5)]
    search_map = {
        "Clean Match": [rec(11, "Clean Match", "CO")],
        "Out Stater": [rec(22, "Out Stater", "TX")],
        "Two Cos": [rec(31, "Two Cos", "CO"), rec(32, "Two Cos", "CO")],
        "Andy Variant": [],                       # no exact hit -> try variants
        "andrew Variant": [rec(44, "Andrew Variant", "CO")],
        "drew Variant": [],
        "Ghost Nobody": [],
    }
    out = _run_resolve(tmp_path, monkeypatch, queue, search_map)

    assert [r["player_id"] for r in out["resolved"]] == [11, 22]   # exact: CO + non-CO
    assert out["resolved"][0]["matched_via"] == "name"
    assert len(out["variant_candidates"]) == 1                     # Andy -> Andrew
    assert out["variant_candidates"][0]["player_id"] == 44
    assert out["variant_candidates"][0]["matched_via"] == "variant"
    assert [a["search_name"] for a in out["ambiguous"]] == ["Two Cos"]
    assert [u["search_name"] for u in out["unfound"]] == ["Ghost Nobody"]
    assert out["errors"] == 0


def test_resolve_variant_surname_guard_rejects_wrong_surname(tmp_path, monkeypatch):
    queue = [_q("Andy Carroll", 1)]
    search_map = {
        "Andy Carroll": [],
        "andrew Carroll": [rec(99, "Andrew Jones", "CO")],   # surname mismatch
        "drew Carroll": [],
    }
    out = _run_resolve(tmp_path, monkeypatch, queue, search_map)
    assert out["variant_candidates"] == [] and len(out["unfound"]) == 1


def test_resolve_retries_transient_error_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(imp.time, "sleep", lambda *_: None)   # no real backoff
    calls = {"n": 0}

    def flaky_search(name, session=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fargo_api.FargoApiError("HTTP 500 for search 'Anna Byrd'")
        return [rec(77, "Anna Byrd", "CO")]
    monkeypatch.setattr(fargo_api, "new_session", lambda: None)

    resolve_dir = tmp_path / "resolve"; resolve_dir.mkdir()
    (resolve_dir / "apa_to_resolve.json").write_text(json.dumps([_q("Anna Byrd")]), encoding="utf-8")
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "TO_RESOLVE_PATH", resolve_dir / "apa_to_resolve.json")
    monkeypatch.setattr(imp, "RESOLUTION_PATH", resolve_dir / "apa_resolution.json")
    monkeypatch.setattr(fargo_api, "search", flaky_search)

    out = imp.resolve(today="2026-06-07")
    assert out["errors"] == 0 and len(out["resolved"]) == 1
    assert out["resolved"][0]["player_id"] == 77


# --- add() upsert -------------------------------------------------------------

def _resolution(resolved, variant=None, tmp_path=None):
    return {"generated_at": "2026-06-07", "queue_size": len(resolved),
            "resolved": resolved, "variant_candidates": variant or [],
            "ambiguous": [], "unfound": [], "errors": 0}


def _resolved_row(player_id, name, location, member_id, *, via="name"):
    return {"search_name": name, "matched_via": via, "player_id": player_id,
            "fargo_name": name, "membership_id": "9900", "rating": 400,
            "robustness": 50, "rating_quality": "preliminary", "location": location,
            "memberships": [{"member_id": member_id, "member_number": f"804{member_id:05d}",
                             "name": name}]}


def _setup_add(tmp_path, monkeypatch, roster_players, resolution):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"players": roster_players}), encoding="utf-8")
    monkeypatch.setattr(resolve, "ROSTER_PATH", roster_path)
    resolve_dir = tmp_path / "resolve"; resolve_dir.mkdir()
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "RESOLUTION_PATH", resolve_dir / "apa_resolution.json")
    (resolve_dir / "apa_resolution.json").write_text(json.dumps(resolution), encoding="utf-8")
    return roster_path


def test_add_creates_new_and_crosslinks_existing(tmp_path, monkeypatch):
    roster_players = {"100": {"player_id": 100, "name": "Existing Player",
                              "membership_id": "111", "added_date": "2026-01-01"}}
    resolution = _resolution(
        resolved=[_resolved_row(100, "Existing Player", "CO", 1),    # existing -> crosslink
                  _resolved_row(200, "Brand New", "TX", 2)],         # new -> create entry
        variant=[_resolved_row(300, "Variant Only", "CO", 3, via="variant")])  # NOT added
    roster_path = _setup_add(tmp_path, monkeypatch, roster_players, resolution)

    s = imp.add(today="2026-06-07")
    assert s["created"] == 1 and s["crosslinked_existing"] == 1

    saved = json.loads(roster_path.read_text(encoding="utf-8"))["players"]
    # existing entry untouched except additive apa link
    assert saved["100"]["membership_id"] == "111"
    assert saved["100"]["added_date"] == "2026-01-01"
    assert saved["100"]["apa"][0]["member_number"] == "80400001"
    # new entry created with non-CO state + source apa
    assert saved["200"]["state"] == "TX" and saved["200"]["source"] == "apa"
    assert saved["200"]["apa"][0]["match_method"] == "name"
    # variant candidate NOT added
    assert "300" not in saved


def test_add_variants_flag_includes_variant_bucket(tmp_path, monkeypatch):
    resolution = _resolution(
        resolved=[_resolved_row(200, "Exact Match", "CO", 2)],
        variant=[_resolved_row(300, "Variant Match", "CO", 3, via="variant")])
    roster_path = _setup_add(tmp_path, monkeypatch, {}, resolution)

    # default: variant NOT added
    imp.add(today="2026-06-07")
    saved = json.loads(roster_path.read_text(encoding="utf-8"))["players"]
    assert "200" in saved and "300" not in saved

    # --variants: now the variant bucket is added too, tagged variant
    s = imp.add(today="2026-06-07", include_variants=True)
    saved = json.loads(roster_path.read_text(encoding="utf-8"))["players"]
    assert "300" in saved
    assert saved["300"]["apa"][0]["match_method"] == "variant"
    assert s["created"] == 1 and s["already_present"] == 1   # 200 already there


def test_reclassify_promotes_lone_co_among_namesakes(tmp_path, monkeypatch):
    resolve_dir = tmp_path / "resolve"; resolve_dir.mkdir()
    monkeypatch.setattr(imp, "RESOLUTION_PATH", resolve_dir / "apa_resolution.json")

    def amb(name, mid, cands):
        return {"search_name": name, "matched_via": "name",
                "memberships": [{"member_id": mid, "member_number": f"804{mid:05d}", "name": name}],
                "candidates": cands}

    def c(pid, loc):
        return {"player_id": pid, "name": "X", "rating": 400, "robustness": 50, "location": loc}

    res = {"generated_at": "x", "queue_size": 3, "resolved": [], "variant_candidates": [],
           "ambiguous": [
               amb("Lone Co", 1, [c(11, "Denver CO"), c(12, "TX"), c(13, "NY")]),   # -> promote 11
               amb("All Away", 2, [c(21, "TX"), c(22, "NY")]),                        # stays
               amb("Two Co", 3, [c(31, "Denver CO"), c(32, "Boulder CO")]),          # stays
           ], "unfound": [], "errors": 0}
    (resolve_dir / "apa_resolution.json").write_text(json.dumps(res), encoding="utf-8")

    s = imp.reclassify(today="2026-06-07")
    assert s["promoted"] == 1 and s["ambiguous_remaining"] == 2

    out = json.loads((resolve_dir / "apa_resolution.json").read_text(encoding="utf-8"))
    assert len(out["resolved"]) == 1
    assert out["resolved"][0]["player_id"] == 11 and out["resolved"][0]["location"] == "Denver CO"
    assert out["resolved"][0]["memberships"][0]["member_id"] == 1
    assert {a["search_name"] for a in out["ambiguous"]} == {"All Away", "Two Co"}


def test_manual_adds_picks_with_membership_from_queue(tmp_path, monkeypatch):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"players": {}}), encoding="utf-8")
    monkeypatch.setattr(resolve, "ROSTER_PATH", roster_path)
    resolve_dir = tmp_path / "resolve"; resolve_dir.mkdir()
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "MANUAL_PATH", resolve_dir / "apa_manual.json")
    monkeypatch.setattr(imp, "TO_RESOLVE_PATH", resolve_dir / "apa_to_resolve.json")

    # the resolve queue carries the APA memberships, keyed by name
    (resolve_dir / "apa_to_resolve.json").write_text(json.dumps([
        {"search_name": "Shirishkumar Patel", "norm": imp.norm("Shirishkumar Patel"),
         "memberships": [{"member_id": 9, "member_number": "80444097", "name": "Shirishkumar Patel"}]}]),
        encoding="utf-8")
    # a manual pick where the FargoRate name differs from the APA name
    (resolve_dir / "apa_manual.json").write_text(json.dumps([
        {"search_name": "Shirishkumar Patel", "player_id": 1199370,
         "fargo_name": "Shirish Patel", "membership_id": "9900006595597",
         "location": "Baltimore MD", "note": "moved to CO"}]), encoding="utf-8")

    s = imp.manual(today="2026-06-07")
    assert s["created"] == 1 and s["no_membership"] == 0
    saved = json.loads(roster_path.read_text(encoding="utf-8"))["players"]
    e = saved["1199370"]
    assert e["name"] == "Shirish Patel" and e["state"] == "Baltimore MD" and e["source"] == "apa"
    assert e["apa"][0]["member_number"] == "80444097"        # cross-link pulled from queue
    assert e["apa"][0]["match_method"] == "manual"

    # idempotent
    again = imp.manual(today="2026-06-08")
    assert again["created"] == 0 and again["already_present"] == 1


def test_add_is_idempotent(tmp_path, monkeypatch):
    resolution = _resolution(resolved=[_resolved_row(200, "Brand New", "TX", 2)])
    roster_path = _setup_add(tmp_path, monkeypatch, {}, resolution)

    first = imp.add(today="2026-06-07")
    second = imp.add(today="2026-06-08")
    assert first["created"] == 1
    assert second["created"] == 0 and second["already_present"] == 1
    saved = json.loads(roster_path.read_text(encoding="utf-8"))["players"]
    assert len(saved["200"]["apa"]) == 1                  # no duplicate membership
