"""Replayable cross-link correction ledger (`docs/resolve/<source>_unlink.json`).

A correction file lists links that were attached to the WRONG Fargo id. Each entry
SUPPRESSES re-adding that `(source member key -> fargo id)` pair, and `strip_suppressed`
removes it if already present -- so a fix is replayable on every rebuild instead of a
silent hand-edit to roster.json (cf. the manual APA "Andrew Tran" reassignment in commit
cafddb4). See docs/cross-league-identity.md section 5.

Entry shape (any of):
  {"member_key": "<key>", "fargo_id": N}
  {"member_id": N, "player_id": N}          # APA
  {"napa_player_id": N, "player_id": N}      # NAPA
The member key is whatever the source's `_attach_crosslink` keys on (APA member_id,
NAPA napa_player_id, BCA synthetic division:name) -- compared as a string.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_suppress(path: Path) -> set:
    """{(member_key:str, fargo_id:int)} suppressed pairs from a <source>_unlink.json."""
    if not path.exists():
        return set()
    out: set = set()
    for e in json.loads(path.read_text(encoding="utf-8")) or []:
        key = e.get("member_key")
        if key is None:
            key = e.get("member_id", e.get("napa_player_id"))
        fid = e.get("fargo_id", e.get("player_id"))
        if key is not None and fid is not None:
            out.add((str(key), int(fid)))
    return out


def strip_suppressed(roster: dict, source: str, key_of, suppress: set) -> int:
    """Remove any existing `source` cross-link whose (member key, fargo id) is suppressed.
    `key_of(membership) -> member key`. Returns the number of links removed."""
    if not suppress:
        return 0
    removed = 0
    for pid, entry in roster.get("players", {}).items():
        links = entry.get(source)
        if not links:
            continue
        fid = int(entry.get("player_id", pid))
        kept = [m for m in links if (str(key_of(m)), fid) not in suppress]
        removed += len(links) - len(kept)
        if kept:
            entry[source] = kept
        else:
            entry.pop(source, None)
    return removed
