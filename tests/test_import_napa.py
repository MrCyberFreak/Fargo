"""Tests for the NAPA cross-reference importer (src/import_napa.py + src/napa_grid.py).

No network: the `resolve` step's real FargoRate calls run on a runner; here
`fargo_api.search` is stubbed. These lock:

  * the roster-grid HTML parser (8-digit id + name reliably; CSR advisory, positional
    to the per-team game-set header; double-space collapse; captain marker ignored)
  * build_master aggregation across divisions (one human -> one entry, csr merged,
    primary division = newest grid date)
  * crossref bucketing into matched / ambiguous / new, and the NAPA-internal
    name-collision QUARANTINE (>1 distinct napa id, same name -> never auto-resolved)
  * the napa cross-link is strictly additive (dedup on napa_player_id), idempotent,
    source/match_method/confidence tagged; CSR is carried but never a gate
  * resolve() carries the napa membership through, and add() upserts source "napa"
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fargo_api  # noqa: E402
import resolve  # noqa: E402
import napa_grid  # noqa: E402
import import_napa as imp  # noqa: E402


def rec(player_id, name, location, *, rating=400, robustness=50, membership="9900"):
    return fargo_api.PlayerRecord(
        player_id=player_id, membership_id=membership, name=name, rating=rating,
        robustness=robustness, location=location,
        rating_quality=fargo_api.quality_for(robustness))


def grid_html(division_id, league_name, teams):
    """teams: [(team_name, gameset_str, [(pid, name, csr_str, captain_bool), ...])]."""
    rows = []
    for team_name, gameset, players in teams:
        rows.append(
            f'<tr><td class="table-warning"><strong>{team_name}</strong><br>'
            f'<small>Venue<br><strong>Team #1</strong></small></td>'
            f'<td class="table-warning"><strong>CSR</strong><br>{gameset} </td>'
            f'<td class="table-warning"><strong>SM</strong></td></tr>')
        for i, (pid, name, csr, cap) in enumerate(players, 1):
            cmark = " (C)" if cap else ""
            rows.append(
                f'<tr align="center"><td>{i}</td>'
                f'<td><a href="https://poolshooters.com/stats.php?playerSelected=Y&amp;'
                f'playerID={pid}" target="_blank">{name}</a>{cmark}<br>{pid}</td>'
                f'<td>{csr} </td><td>8</td></tr>')
    return (f'<html><body><div align="center">'
            f'<h4>Roster Report: Division {division_id}</h4><h4>{league_name}</h4></div>'
            f'<table><tbody>{"".join(rows)}</tbody></table></body></html>')


def write_grid(ref, did, date, html):
    d = ref / "data" / "raw" / did / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "roster_grid.html").write_text(html, encoding="utf-8")


# --- napa_grid parser ---------------------------------------------------------

def test_parse_grid_extracts_id_name_and_positional_csr():
    html = grid_html("13077", "Thursday Test League", [
        ("Pocket Pals", "8 - 9 - 10", [(10000001, "Sam Trojan", "95 - 79 - 82", True),
                                       (10000002, "Kevin  Pikus", "50 - 53 - 56", False)]),
        ("Niners", "9 - 10", [(10000003, "Nine Tenner", "60 - 70", False)]),
    ])
    players = napa_grid.parse_grid(html, "13077")
    assert len(players) == 3
    p0 = players[0]
    assert p0.napa_player_id == 10000001 and p0.name == "Sam Trojan"   # (C) not in name
    assert p0.csr == {"csr_8": 95, "csr_9": 79, "csr_10": 82}
    assert p0.division_id == "13077" and p0.division_name == "Thursday Test League"
    assert players[1].name == "Kevin Pikus"                            # double space collapsed
    # the second team's distinct game set maps positionally (9/10 only)
    assert players[2].csr == {"csr_9": 60, "csr_10": 70}


def test_parse_grid_file_takes_division_from_path(tmp_path):
    ref = tmp_path / "_ref" / "NAPA"
    write_grid(ref, "14022", "2026-06-26",
               grid_html("14022", "Monday Four-Game", [
                   ("T", "8 - 9 - 10 - 10BP", [(10000099, "Bp Player", "30 - 31 - 32 - 33", False)])]))
    players = napa_grid.parse_grid_file(ref / "data" / "raw" / "14022" / "2026-06-26" / "roster_grid.html")
    assert players[0].division_id == "14022"
    assert players[0].csr == {"csr_8": 30, "csr_9": 31, "csr_10": 32, "csr_10bp": 33}


# --- build_master -------------------------------------------------------------

def test_build_master_aggregates_one_human_across_divisions(tmp_path):
    ref = tmp_path / "_ref" / "NAPA"
    write_grid(ref, "13077", "2026-06-01",
               grid_html("13077", "Div A", [("T", "8 - 9 - 10",
                         [(10000005, "Dual Div", "11 - 21 - 31", False)])]))
    write_grid(ref, "14000", "2026-06-10",
               grid_html("14000", "Div B", [("T", "8 - 9 - 10",
                         [(10000005, "Dual Div", "11 - 21 - 31", False),
                          (10000006, "Solo Guy", "40 - 50 - 60", False)])]))
    master = imp.build_master(ref)
    assert set(master) == {10000005, 10000006}
    dual = master[10000005]
    assert dual["divisions"] == ["13077", "14000"]
    assert dual["division_id"] == "14000"          # primary = newest grid date
    assert dual["division_name"] == "Div B"
    assert dual["csr"] == {"csr_8": 11, "csr_9": 21, "csr_10": 31}
    assert dual["imputed_state"] == "CO"


# --- crossref bucketing + quarantine ------------------------------------------

def npl(pid, name, did="13077", csr=None):
    return {"napa_player_id": pid, "name": name, "division_id": did, "division_name": "D",
            "divisions": [did], "csr": csr if csr is not None else {"csr_8": 50},
            "imputed_state": "CO"}


def _setup_crossref(tmp_path, monkeypatch, roster_players, master):
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"players": roster_players}), encoding="utf-8")
    monkeypatch.setattr(resolve, "ROSTER_PATH", roster_path)
    resolve_dir = tmp_path / "resolve"
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "TO_RESOLVE_PATH", resolve_dir / "napa_to_resolve.json")
    monkeypatch.setattr(imp, "REPORT_PATH", resolve_dir / "napa_crossref_report.json")
    monkeypatch.setattr(imp, "COLLISIONS_PATH", resolve_dir / "napa_name_collisions.json")
    monkeypatch.setattr(imp, "build_master", lambda ref: master)
    return roster_path, resolve_dir


def test_crossref_buckets_and_quarantines_collisions(tmp_path, monkeypatch):
    roster_players = {
        "100": {"player_id": 100, "name": "Anna Byrd"},        # single -> matched
        "200": {"player_id": 200, "name": "Tim Brown"},        # collide ->
        "201": {"player_id": 201, "name": "Tim Brown"},        #   ambiguous
    }
    master = {
        1: npl(10000001, "Anna Byrd"),                         # matched -> 100
        2: npl(10000002, "Tim Brown"),                         # ambiguous (2 roster ids)
        3: npl(10000003, "Brand New Person"),                  # new -> resolve queue
        4: npl(10000004, "Same Name"),                         # NAPA-internal collision
        5: npl(10000005, "Same Name"),                         #   -> quarantine
    }
    roster_path, resolve_dir = _setup_crossref(tmp_path, monkeypatch, roster_players, master)

    rep = imp.crossref(Path("ignored"), dry_run=False, today="2026-06-27")
    assert rep["matched_single"] == 1
    assert rep["ambiguous_existing"] == 1
    assert rep["new_to_resolve"] == 1
    assert rep["name_collisions_quarantined"] == 1

    saved = json.loads(roster_path.read_text(encoding="utf-8"))["players"]
    link = saved["100"]["napa"]
    assert len(link) == 1 and link[0]["napa_player_id"] == 10000001
    assert link[0]["source"] == "napa" and link[0]["match_method"] == "name"
    assert link[0]["confidence"] == "high" and "csr" in link[0]     # csr carried (advisory)

    queue = json.loads((resolve_dir / "napa_to_resolve.json").read_text(encoding="utf-8"))
    assert [q["search_name"] for q in queue] == ["Brand New Person"]
    assert queue[0]["memberships"][0]["napa_player_id"] == 10000003

    collisions = json.loads((resolve_dir / "napa_name_collisions.json").read_text(encoding="utf-8"))
    assert collisions[0]["napa_player_ids"] == [10000004, 10000005]


def test_napa_crosslink_additive_and_idempotent(tmp_path, monkeypatch):
    roster_players = {"100": {"player_id": 100, "name": "Anna Byrd",
                              "membership_id": "9900", "added_date": "2026-01-01"}}
    master = {1: npl(10000001, "Anna Byrd")}
    roster_path, _ = _setup_crossref(tmp_path, monkeypatch, roster_players, master)

    imp.crossref(Path("x"), dry_run=False, today="2026-06-27")
    imp.crossref(Path("x"), dry_run=False, today="2026-06-28")     # re-run

    e = json.loads(roster_path.read_text(encoding="utf-8"))["players"]["100"]
    assert e["membership_id"] == "9900" and e["added_date"] == "2026-01-01"  # untouched
    assert len(e["napa"]) == 1 and e["napa"][0]["added_date"] == "2026-06-27"


# --- resolve() routing (fargo_api.search stubbed) -----------------------------

def _run_resolve(tmp_path, monkeypatch, queue, search_map):
    resolve_dir = tmp_path / "resolve"
    resolve_dir.mkdir()
    (resolve_dir / "napa_to_resolve.json").write_text(json.dumps(queue), encoding="utf-8")
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "TO_RESOLVE_PATH", resolve_dir / "napa_to_resolve.json")
    monkeypatch.setattr(imp, "RESOLUTION_PATH", resolve_dir / "napa_resolution.json")
    monkeypatch.setattr(fargo_api, "new_session", lambda: None)

    def fake_search(name, session=None):
        out = search_map.get(name)
        if isinstance(out, Exception):
            raise out
        return out or []
    monkeypatch.setattr(fargo_api, "search", fake_search)
    return imp.resolve(today="2026-06-27")


def test_resolve_carries_napa_membership_and_routes(tmp_path, monkeypatch):
    queue = [
        {"search_name": "Clean Match", "norm": "clean match", "memberships": [npl(10000009, "Clean Match")]},
        {"search_name": "Two Cos", "norm": "two cos", "memberships": [npl(10000010, "Two Cos")]},
    ]
    search_map = {
        "Clean Match": [rec(11, "Clean Match", "CO")],
        "Two Cos": [rec(31, "Two Cos", "CO"), rec(32, "Two Cos", "CO")],
    }
    out = _run_resolve(tmp_path, monkeypatch, queue, search_map)
    assert [r["player_id"] for r in out["resolved"]] == [11]
    assert out["resolved"][0]["memberships"][0]["napa_player_id"] == 10000009
    assert [a["search_name"] for a in out["ambiguous"]] == ["Two Cos"]


# --- add() upsert -------------------------------------------------------------

def _napa_resolved_row(player_id, name, location, napa_id, *, via="name"):
    return {"search_name": name, "matched_via": via, "player_id": player_id,
            "fargo_name": name, "membership_id": "9900", "rating": 400, "robustness": 50,
            "rating_quality": "preliminary", "location": location,
            "memberships": [npl(napa_id, name)]}


def test_add_creates_new_and_crosslinks_existing(tmp_path, monkeypatch):
    roster_players = {"100": {"player_id": 100, "name": "Existing Player",
                              "membership_id": "111", "added_date": "2026-01-01"}}
    res = {"generated_at": "2026-06-27", "queue_size": 2,
           "resolved": [_napa_resolved_row(100, "Existing Player", "CO", 10000010),
                        _napa_resolved_row(200, "Brand New", "TX", 10000011)],
           "variant_candidates": [_napa_resolved_row(300, "Variant Only", "CO", 10000012, via="variant")],
           "ambiguous": [], "unfound": [], "errors": 0}

    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"players": roster_players}), encoding="utf-8")
    monkeypatch.setattr(resolve, "ROSTER_PATH", roster_path)
    resolve_dir = tmp_path / "resolve"; resolve_dir.mkdir()
    monkeypatch.setattr(imp, "RESOLVE_DIR", resolve_dir)
    monkeypatch.setattr(imp, "RESOLUTION_PATH", resolve_dir / "napa_resolution.json")
    (resolve_dir / "napa_resolution.json").write_text(json.dumps(res), encoding="utf-8")

    s = imp.add(today="2026-06-27")
    assert s["created"] == 1 and s["crosslinked_existing"] == 1

    saved = json.loads(roster_path.read_text(encoding="utf-8"))["players"]
    assert saved["100"]["membership_id"] == "111"                 # untouched
    assert saved["100"]["napa"][0]["napa_player_id"] == 10000010
    assert saved["200"]["state"] == "TX" and saved["200"]["source"] == "napa"
    assert saved["200"]["napa"][0]["confidence"] == "high"
    assert "300" not in saved                                     # variant NOT auto-added
