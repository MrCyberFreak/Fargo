"""Shared name-matching core for the roster-source importers (APA, NAPA, BCA).

These functions were originally defined inside `import_apa.py`; they are extracted
here so every league importer shares ONE normalization and matching policy and the
three cannot drift. APA behavior is unchanged (the importer re-imports these names,
so `import_apa.norm(...)` etc. still resolve and the APA tests stay byte-identical).

`clean_name`'s "owes $N" fee-prefix stripper is APA-originated (APA exports prepend
a league-fee annotation to some names). It is a harmless, idempotent no-op on a NAPA
or BCA name, so keeping it in the shared `clean_name` preserves a single normalization
for all sources without affecting non-APA data.

Nothing here touches the network: `_search_with_retry` takes the `fargo_api` module
as an argument so this module stays import-light and network-free.
"""

from __future__ import annotations

import re
import time

# League-fee annotations get prepended to some APA names, e.g. "Owes $150 Anna Byrd",
# "OWES$130 Jordan Freeman", "Owes $180Aaron Knobloch". Strip them off the front.
# (APA-originated; a no-op on names from other sources.)
_FEE_PREFIX = re.compile(r"(?i)^\s*owes?\s*\$?\s*\d+\s*")

# Common English given-name <-> nickname groups. Each line is one equivalence
# class; every member maps to all the others. Used only to retry names that
# returned zero FargoRate matches (a surname match is still required), so a wider
# net here costs a few extra searches, not false roster adds.
_NICKNAME_GROUPS = [
    {"andrew", "andy", "drew"}, {"anthony", "tony"}, {"benjamin", "ben"},
    {"bradley", "brad"}, {"charles", "charlie", "chuck"},
    {"christopher", "chris"}, {"daniel", "dan", "danny"},
    {"david", "dave", "davey"}, {"donald", "don", "donnie"},
    {"douglas", "doug"}, {"edward", "ed", "eddie", "ted"},
    {"frederick", "fred"}, {"gregory", "greg"}, {"jacob", "jake"},
    {"james", "jim", "jimmy", "jamie"}, {"jeffrey", "jeff"},
    {"jonathan", "jon"}, {"john", "johnny", "jack"}, {"joseph", "joe", "joey"},
    {"joshua", "josh"}, {"kenneth", "ken", "kenny"}, {"lawrence", "larry"},
    {"matthew", "matt"}, {"michael", "mike", "mick"}, {"nathan", "nate"},
    {"nathaniel", "nate", "nathan"}, {"nicholas", "nick", "nik"},
    {"patrick", "pat"}, {"peter", "pete"}, {"philip", "phillip", "phil"},
    {"raymond", "ray"}, {"richard", "rick", "rich", "dick", "richie"},
    {"robert", "rob", "bob", "bobby"}, {"ronald", "ron", "ronnie"},
    {"samuel", "sam", "sammy"}, {"stephen", "steven", "steve"},
    {"thomas", "tom", "tommy"}, {"timothy", "tim", "timmy"},
    {"vincent", "vince", "vinny"}, {"william", "will", "bill", "billy", "liam"},
    {"zachary", "zach", "zack"}, {"alexander", "alex", "alexandra", "lexi"},
    {"abigail", "abby"}, {"amanda", "mandy"}, {"angela", "angie"},
    {"barbara", "barb"}, {"catherine", "katherine", "kate", "katie", "kathy", "cathy", "kat"},
    {"christina", "christine", "chris", "tina"}, {"cynthia", "cindy"},
    {"deborah", "deb", "debbie"}, {"elizabeth", "liz", "beth", "lizzie", "betty"},
    {"jennifer", "jen", "jenny"}, {"jessica", "jess"}, {"kimberly", "kim"},
    {"margaret", "maggie", "meg", "peggy", "marge"}, {"michelle", "shelly"},
    {"nicole", "nikki"}, {"pamela", "pam"}, {"patricia", "pat", "patty", "tricia"},
    {"rebecca", "becca", "becky", "reba"}, {"samantha", "sam", "sammy"},
    {"sandra", "sandy"}, {"stephanie", "steph"}, {"susan", "sue", "susie"},
    {"theresa", "teresa", "terry", "tess"}, {"victoria", "vicky", "tori"},
]
_NICKNAMES: dict[str, set[str]] = {}
for _grp in _NICKNAME_GROUPS:
    for _n in _grp:
        _NICKNAMES.setdefault(_n, set()).update(_grp - {_n})


def clean_name(raw: str | None) -> str:
    """Strip fee prefixes and collapse whitespace; preserve the real name."""
    if not raw:
        return ""
    s = _FEE_PREFIX.sub("", raw)
    return re.sub(r"\s+", " ", s).strip()


def norm(name: str | None) -> str:
    """Normalize for name matching: lowercase, drop punctuation, collapse spaces."""
    if not name:
        return ""
    s = clean_name(name).lower()
    s = s.replace("'", "")            # O'Neill -> oneill (drop, don't split)
    s = re.sub(r"[.\-]", " ", s)      # hyphens/periods -> space
    return re.sub(r"\s+", " ", s).strip()


# Generational suffixes are not surnames — "Anthony Tacchia Jr" must key on
# "tacchia", not "jr" (otherwise a surname search matches every "...Jr").
_SUFFIXES = {"jr", "jnr", "sr", "snr", "ii", "iii", "iv", "v"}


def _strip_suffixes(tokens: list[str]) -> list[str]:
    out = list(tokens)
    while len(out) > 1 and out[-1].lower().strip(".") in _SUFFIXES:
        out = out[:-1]
    return out


def surname(name: str | None) -> str:
    """Surname for matching: last token, ignoring generational suffixes."""
    parts = _strip_suffixes(norm(name).split())
    return parts[-1] if parts else ""


def is_co(location: str | None) -> bool:
    """True if a FargoRate `location` is Colorado. The field is inconsistent —
    sometimes just "CO", sometimes "Denver CO" or "Denver, CO" — so test the
    trailing state token, not an exact string match."""
    if not location:
        return False
    return location.upper().replace(",", " ").split()[-1] == "CO"


def variant_queries(full_name: str) -> list[str]:
    """First-name nickname variations of a full name, surname kept intact.
    'Andy Carroll' -> ['andrew carroll', 'drew carroll']. Empty if no mapping."""
    parts = clean_name(full_name).split()
    if len(parts) < 2:
        return []
    first = parts[0].lower()
    tail = parts[1:]
    return [" ".join([alt, *tail]) for alt in sorted(_NICKNAMES.get(first, ()))]


def first_name(name: str | None) -> str:
    """First token of the normalized name."""
    parts = norm(name).split()
    return parts[0] if parts else ""


def first_compatible(a: str, c: str) -> bool:
    """Are two first names plausibly the same person? True when one is a prefix of
    the other (≥3 chars, catches Shirish↔Shirishkumar, Dan↔Daniel) or they are a
    known nickname pair. Used to filter recovery candidates."""
    if not a or not c:
        return False
    if a == c:
        return True
    short, long = sorted([a, c], key=len)
    if len(short) >= 3 and long.startswith(short):
        return True
    return c in _NICKNAMES.get(a, set()) or a in _NICKNAMES.get(c, set())


def recover_query(name: str) -> str | None:
    """A surname search qualified by a short first-name prefix — avoids the broad
    'surname alone' queries that FargoRate 500s on, while still prefix-matching a
    truncated FargoRate first name. 'Shirishkumar Patel' -> 'Shir Patel'.
    Generational suffixes are dropped so 'Anthony Tacchia Jr' -> 'Anth Tacchia'."""
    parts = _strip_suffixes(clean_name(name).split())
    if len(parts) < 2:
        return None
    return f"{parts[0][:4]} {parts[-1]}"


def pick_match(candidates: list) -> tuple[str, object]:
    """Choose a single FargoRate match from search candidates, CO-preferred.

    Returns ("resolved", record) when exactly one CO match exists (extra
    out-of-state namesakes are ignored), else exactly one out-of-state match;
    ("ambiguous", [records]) when a state bucket has >1; ("none", []) for no
    candidates. Candidates are deduped by player_id first."""
    seen: dict = {}
    for r in candidates:
        seen.setdefault(r.player_id, r)
    cands = list(seen.values())
    co = [r for r in cands if is_co(r.location)]
    noco = [r for r in cands if not is_co(r.location)]
    if len(co) == 1:
        return ("resolved", co[0])
    if len(co) > 1:
        return ("ambiguous", co)
    if len(noco) == 1:
        return ("resolved", noco[0])
    if len(noco) > 1:
        return ("ambiguous", noco)
    return ("none", [])


def _search_with_retry(fargo_api, name: str, session, attempts: int = 3) -> list:
    """Search, retrying transient failures (e.g. HTTP 500) with a short backoff."""
    last = None
    for k in range(attempts):
        try:
            return fargo_api.search(name, session=session)
        except Exception as exc:  # FargoApiError + any network hiccup
            last = exc
            if k < attempts - 1:
                time.sleep(1.5 * (k + 1))
    raise last


def fargo_quality(robustness) -> str:
    """rating_quality without importing fargo_api (keeps callers network-free)."""
    try:
        return "established" if int(robustness) >= 200 else "preliminary"
    except (TypeError, ValueError):
        return "preliminary"
