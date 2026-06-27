"""Parse NAPA committed roster-grid HTML into a slim player master.

The sibling NAPA project commits raw HTML (`data/raw/<did>/<date>/roster_grid.html`);
its clean `players` table lives only in the gitignored, regenerable `napa.db`, which
Fargo must NEVER rebuild (no upstream-code execution -- see CLAUDE.md and
docs/cross-league-identity.md). Fargo needs only (napa_player_id, name, advisory CSR,
division) for identity resolution, so this is a FOCUSED, DEFENSIVE parser: the player
name and 8-digit NAPA id are extracted reliably; the CueSpeed Rating (CSR) is
best-effort advisory and parsing it NEVER raises (NAPA's own parser is strict; Fargo's
is not, because CSR is never a matching gate -- the CSR->Fargo residual is +/-70 pts).

Grid shape (verified against _ref/NAPA/data/raw/13077/.../roster_grid.html, 2026-06):
  - A team-header row carries a cell `CSR<br>8 - 9 - 10` declaring that team's game
    set. NAPA divisions vary: "8 - 9 - 10", "9 - 10", "8 - 9 - 10 - 10BP".
  - Each player row is `# | <a ...playerID=NNNNNNNN>Name</a> (C)<br>NNNNNNNN |
    95 - 79 - 82 | SM`; the CSR cell (the cell after the name) maps positionally to
    the current team's game set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

_PLAYER_ID_RE = re.compile(r"playerID=(\d{6,})")
_TOKEN_RE = re.compile(r"\d+BP|\d+", re.IGNORECASE)


@dataclass
class NapaPlayer:
    napa_player_id: int
    name: str
    csr: dict              # {"csr_8": 95, "csr_9": 79, ...}; advisory only, may be {}
    division_id: str
    division_name: str | None = None


def _gameset(csr_header_text: str) -> list[str]:
    """['csr_8','csr_9','csr_10'] from a 'CSR 8 - 9 - 10' header cell."""
    body = re.sub(r"(?i)\bCSR\b|\bSM\b", " ", csr_header_text or "")
    return [f"csr_{t.lower()}" for t in _TOKEN_RE.findall(body)]


def _is_header_cell(text: str) -> bool:
    return bool(re.match(r"(?i)\s*CSR\b", text or ""))


def _csr_values(text: str) -> list:
    """[95, 79, 82] from '95 - 79 - 82'; non-numeric tokens -> None (advisory)."""
    out: list = []
    for tok in re.split(r"\s*-\s*", (text or "").strip()):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            out.append(None)
    return out


def parse_grid(html: str, division_id: str) -> list[NapaPlayer]:
    """Extract every player row from one division's roster-grid HTML."""
    soup = BeautifulSoup(html, "html.parser")

    division_name = None
    for h in soup.find_all("h4"):
        t = h.get_text(" ", strip=True)
        if t and "Roster Report" not in t:
            division_name = t          # the league-name h4 (e.g. 'Thursday ... LC League')
            break

    players: list[NapaPlayer] = []
    gameset: list[str] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue

        # A team-header row refreshes the current game set; it has no player link.
        header = next((c for c in cells
                       if _is_header_cell(c.get_text(" ", strip=True))), None)
        if header is not None and tr.find("a", href=_PLAYER_ID_RE) is None:
            gs = _gameset(header.get_text(" ", strip=True))
            if gs:
                gameset = gs
            continue

        # A player row carries the 8-digit NAPA id in a profile link.
        name_idx = next((i for i, c in enumerate(cells)
                         if c.find("a", href=_PLAYER_ID_RE)), None)
        if name_idx is None:
            continue
        link = cells[name_idx].find("a", href=_PLAYER_ID_RE)
        m = _PLAYER_ID_RE.search(link.get("href", ""))
        if not m:
            continue
        pid = int(m.group(1))
        # collapse internal whitespace (grid cells carry stray newlines/double spaces)
        name = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()

        # CSR cell is the one immediately after the name cell; map positionally to the
        # team's game set. Best-effort: any parse trouble just leaves csr partial/empty.
        csr: dict = {}
        if name_idx + 1 < len(cells):
            vals = _csr_values(cells[name_idx + 1].get_text(" ", strip=True))
            for key, val in zip(gameset, vals):
                csr[key] = val

        players.append(NapaPlayer(napa_player_id=pid, name=name, csr=csr,
                                  division_id=str(division_id), division_name=division_name))
    return players


def parse_grid_file(path) -> list[NapaPlayer]:
    """Parse one roster_grid.html, taking the division id from its archive path
    (`.../data/raw/<did>/<date>/roster_grid.html`)."""
    p = Path(path)
    division_id = p.parents[1].name
    return parse_grid(p.read_text(encoding="utf-8"), division_id)
