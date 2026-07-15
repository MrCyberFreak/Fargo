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
    """Normalized player record. See the field mapping table in docs/api.md.

    `rating` is the RAW whole-history rating (`FargoRating` from /players/{id});
    `effective_rating` is the blended, publicly-DISPLAYED rating FargoRate shows
    (the two differ only while a player is preliminary — see `effective_for`).
    """

    player_id: int             # join key + /api/players/<id> path key (Id / readableId)
    membership_id: str | None  # public membership number (BBMMembershipId / membershipId)
    name: str
    rating: int                # RAW rating (FargoRating / search `rating`)
    robustness: int
    location: str | None
    rating_quality: str        # "established" | "preliminary" (derived)
    row_id: str | None = None  # internal GUID (RowId / id) — preserved, never a key
    provisional_rating: int = 0        # "starter" anchor blended out by robustness 200
    effective_rating: int | None = None  # displayed/blended rating; defaults to raw
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # No provisional / already established -> the displayed rating IS the raw
        # rating, so a record built without an explicit effective value (tests,
        # search fallbacks) still carries a sensible one.
        if self.effective_rating is None:
            self.effective_rating = self.rating

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def quality_for(robustness: int) -> str:
    return "established" if robustness >= ESTABLISHED_ROBUSTNESS else "preliminary"


def effective_for(rating: int, provisional: int, robustness: int) -> int:
    """FargoRate's displayed (blended) rating from the three id-keyed fields.

    For a *preliminary* player (robustness < 200) FargoRate does not display the
    raw rating; it shows a linear blend anchored on the provisional "starter"
    rating and weighted toward the raw rating by games played:

        effective = provisional + (robustness / 200) * (rating - provisional)

    Weight on the raw rating is robustness/200 and on the starter is the
    remainder, so at robustness 200 (the "established" line) the starter drops
    out entirely and effective == raw. A provisional of 0 means no starter
    applies (established players, and some new accounts), so effective == raw.

    Verified against live data: raw 458 / provisional 440 / robustness 67 ->
    440 + 0.335*(18) = 446.03 -> 446, matching the site. The exact half-integer
    rounding rule is UNVERIFIED (only whole-number cases observed); round-to-
    nearest is used and is correct for every observed case.
    """
    if provisional <= 0 or robustness >= ESTABLISHED_ROBUSTNESS:
        return rating
    weight = robustness / ESTABLISHED_ROBUSTNESS
    return round(provisional + weight * (rating - provisional))


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


def _provisional(value: Any) -> int:
    """ProvisionalRating is absent/empty/"0" for established players (no starter
    applies); treat empty or missing as 0. `effective_for` reads a 0 provisional
    as 'no blend', so this is the safe default."""
    if value is None or str(value).strip() == "":
        return 0
    return _to_int(value, "ProvisionalRating")


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
    rating = _to_int(data.get("FargoRating"), "FargoRating")
    provisional = _provisional(data.get("ProvisionalRating"))
    name = data.get("FullName") or f"{data.get('FirstName', '')} {data.get('LastName', '')}".strip()
    return PlayerRecord(
        player_id=_to_int(data.get("Id"), "Id"),
        membership_id=str(data["BBMMembershipId"]) if data.get("BBMMembershipId") else None,
        name=name,
        rating=rating,
        robustness=robustness,
        location=data.get("State") or None,
        rating_quality=quality_for(robustness),
        row_id=data.get("RowId"),
        provisional_rating=provisional,
        effective_rating=effective_for(rating, provisional, robustness),
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
            # Search has kept `rating` = the displayed value (effectiveRating,
            # fallback rating) for name resolution; left as-is. The search
            # endpoint also carries the blended rating + starter directly, so
            # populate the dedicated fields from them rather than recomputing.
            rating_src = item.get("effectiveRating") or item.get("rating")
            provisional = _provisional(item.get("provisionalRating"))
            eff_src = item.get("effectiveRating") or item.get("rating")
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
                    provisional_rating=provisional,
                    effective_rating=_to_int(eff_src, "effectiveRating"),
                    raw=item,
                )
            )
        except FargoApiError:
            continue
    return records
