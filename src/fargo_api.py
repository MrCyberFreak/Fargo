"""Thin client for the FargoRate public REST API (dashboard.fargorate.com).

Contract verified in Phase 0 — see docs/api.md. Two endpoints are used:

  * GET /api/indexsearch?q=<name>   -> search by name   (Phase 1 resolution)
  * GET /api/players/<player_id>    -> lookup by id      (Phase 2 scheduled pull)

Both are unauthenticated. The provider returns numeric values as strings and
uses inconsistent (camelCase vs PascalCase) field names across the two
endpoints, so everything is normalized to a single PlayerRecord schema here and
the rest of the codebase never touches a raw API field name.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import requests

BASE_URL = "https://dashboard.fargorate.com/api"
USER_AGENT = "fargo-tracker/1.0 (+https://github.com/MrCyberFreak/Fargo)"
TIMEOUT = 20          # seconds per request
REQUEST_DELAY = 1.0   # courtesy pause between calls (used by callers)

# Robustness at/above which FargoRate considers a rating "established".
# Verified in Phase 0 (the FairMatch progress bar read 63/200 for player 1310533).
ESTABLISHED_ROBUSTNESS = 200


class FargoApiError(RuntimeError):
    """API unreachable, non-200, or an unusable/empty response."""


@dataclass
class PlayerRecord:
    """Normalized player record. See the field mapping table in docs/api.md."""

    player_id: int             # join key + /api/players/<id> path key (Id / readableId)
    membership_id: str | None  # public membership number (BBMMembershipId / membershipId)
    name: str
    rating: int                # effectiveRating / FargoRating
    robustness: int
    location: str | None
    rating_quality: str        # "established" | "preliminary" (derived)
    row_id: str | None = None  # internal GUID (RowId / id) — preserved, never a key
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def quality_for(robustness: int) -> str:
    return "established" if robustness >= ESTABLISHED_ROBUSTNESS else "preliminary"


def _to_int(value: Any, fieldname: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise FargoApiError(f"non-numeric {fieldname!r}: {value!r}") from exc


def _robustness(value: Any) -> int:
    """FargoRate returns an empty Robustness for accounts with no games played
    yet; treat that (and a missing field) as 0 rather than a fetch failure. Any
    other non-numeric value is still a real error and is left to _to_int."""
    if value is None or str(value).strip() == "":
        return 0
    return _to_int(value, "Robustness")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def _get_json(session: requests.Session, url: str, *, params=None, what: str):
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise FargoApiError(f"request failed for {what}: {exc}") from exc
    if resp.status_code != 200:
        raise FargoApiError(f"HTTP {resp.status_code} for {what}")
    try:
        return resp.json()
    except ValueError as exc:
        raise FargoApiError(f"non-JSON response for {what}") from exc


def get_player(player_id: int | str, session: requests.Session | None = None) -> PlayerRecord:
    """Look up a single player by integer id via /api/players/<id>."""
    sess = session or new_session()
    data = _get_json(sess, f"{BASE_URL}/players/{player_id}", what=f"player {player_id}")
    if not isinstance(data, dict) or "Id" not in data:
        raise FargoApiError(f"empty/unexpected record for player {player_id}: {data!r}")

    robustness = _robustness(data.get("Robustness"))
    name = data.get("FullName") or f"{data.get('FirstName', '')} {data.get('LastName', '')}".strip()
    return PlayerRecord(
        player_id=_to_int(data.get("Id"), "Id"),
        membership_id=str(data["BBMMembershipId"]) if data.get("BBMMembershipId") else None,
        name=name,
        rating=_to_int(data.get("FargoRating"), "FargoRating"),
        robustness=robustness,
        location=data.get("State") or None,
        rating_quality=quality_for(robustness),
        row_id=data.get("RowId"),
        raw=data,
    )


def search(name: str, session: requests.Session | None = None) -> list[PlayerRecord]:
    """Search by name via /api/indexsearch?q=<name>. Returns candidate matches.

    Malformed candidate rows (missing/garbage required fields) are skipped so a
    single bad row does not sink the whole search.
    """
    sess = session or new_session()
    payload = _get_json(sess, f"{BASE_URL}/indexsearch", params={"q": name}, what=f"search {name!r}")
    candidates = (payload or {}).get("value") or []

    records: list[PlayerRecord] = []
    for item in candidates:
        try:
            robustness = _robustness(item.get("robustness"))
            rating_src = item.get("effectiveRating") or item.get("rating")
            records.append(
                PlayerRecord(
                    player_id=_to_int(item.get("readableId"), "readableId"),
                    membership_id=str(item["membershipId"]) if item.get("membershipId") else None,
                    name=f"{item.get('firstName', '')} {item.get('lastName', '')}".strip(),
                    rating=_to_int(rating_src, "rating"),
                    robustness=robustness,
                    location=item.get("location") or None,
                    rating_quality=quality_for(robustness),
                    row_id=item.get("id"),
                    raw=item,
                )
            )
        except FargoApiError:
            continue
    return records
