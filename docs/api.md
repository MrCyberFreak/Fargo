# FargoRate API contract (VERIFIED — Phase 0)

Source of truth for the API. Verified on **2026-06-05** by running probes on a
GitHub Actions runner (the build sandbox cannot reach `fargorate.com`; runners
have open internet). Raw captured responses are preserved under `docs/probe/`.

Base URL: `https://dashboard.fargorate.com/api`
Auth: **none** (no token, cookie, or header required). Build against this REST
API — **not** the FairMatch Meteor front-end (that search runs over DDP/websocket
server methods, not REST).

---

## Endpoints used

### 1. Reachability / no-auth check
```
GET /api/landingpagemetrics
```
→ `{"PlayerCount":522528,"GameCount":68614845}` (HTTP 200, `application/json`).
Used only to confirm the API is reachable and unauthenticated.

### 2. Search by name (Phase 1 — resolution)
```
GET /api/indexsearch?q=<name>
```
- The parameter is **`q`**. Other names (`query`, `searchterm`) return HTTP 500;
  `/api/playersearch` and `/api/search` return 404.
- Response shape: `{"value": [ <candidate>, ... ]}`.
- **Known upstream bug — some queries always HTTP 500.** Certain searches crash
  FargoRate's serializer and return 500 on *every* attempt (not transient):
  `q=Byrd` and `q=Anna Byrd` both 500 consistently (confirmed via two network
  paths). It appears a corrupted record matching that text breaks the response,
  so any query that would surface it fails. Broad single-token surname queries
  (e.g. `q=Patel`) also 500 — likely too-large / malformed result sets. **Avoid
  bare-surname searches**; qualify with a first-name prefix (`q=Shir Patel`,
  which prefix-matches and returns a small set — see `import_apa.recover_query`).
  Names hit by the bug (e.g. *Anna Byrd*) can't be resolved by search until
  FargoRate fixes the record; look the player up in the FargoRate app and add by
  id instead.

Example — `GET /api/indexsearch?q=Nathan%20Carroll`:
```json
{"value":[{
  "id":"8D6CADB3-A194-4CE6-A8B8-B2D800FB7182",
  "readableId":"1310533",
  "membershipId":"9900007849538",
  "firstName":"Nathan","lastName":"Carroll",
  "location":"CO",
  "rating":"437","effectiveRating":"438","robustness":"63",
  "provisionalRating":"0",
  "membershipNumber":null,"imageUrl":null,"lmsId":null,
  "shareMatches":null,"statsOverall":null,"statsByRating":null,"ratingHistory":null
}]}
```

### 3. Lookup by id (Phase 2 — the scheduled pull)
```
GET /api/players/<player_id>
```
- Path key is the integer id `1310533` (search's `readableId` / lookup's `Id`).
- `/api/player/<id>`, `/api/player?id=`, `/api/players?id=` etc. all 404 —
  the working path is the **plural** `players` with the id as a path segment.

Example — `GET /api/players/1310533`:
```json
{"RowId":"8d6cadb3-a194-4ce6-a8b8-b2d800fb7182","Id":1310533,
 "FirstName":"Nathan","LastName":"Carroll","Suffix":null,"Nickname":null,
 "City":"","State":"CO","Country":"USA",
 "FargoRating":"438","Robustness":"63","RecentRobustness":null,
 "Gender":"M","Staged":false,"ProvisionalRating":"0",
 "CSI":null,"BBMId":null,"BBMMembershipId":"9900007849538",
 "InLMS":false,"ActiveInLMS":null,"Ratings":null,
 "Links":[{"Rel":"self","Href":"/api/players/1310533",...},
          {"Rel":"ratings","Href":"/api/ratings/1310533",...}],
 "FullName":"Nathan Carroll"}
```

---

## The three identifiers (read this carefully)

The provider's naming **inverts** the build-plan terminology. There are three:

| Meaning | Example | `indexsearch` field | `players/{id}` field | Role |
|---|---|---|---|---|
| Integer id | `1310533` | `readableId` | `Id` | **join key**; `/api/players/{id}` path key |
| Membership # | `9900007849538` | `membershipId` | `BBMMembershipId` | public number; stored for the future cross-DB join |
| Internal GUID | `8d6cadb3-…` | `id` | `RowId` | true row id; preserved in the full record, **not** used as a key |

This codebase keys everything on the **integer id `1310533`** and calls it
`player_id`. The membership number is stored as `readable_id` in `history.csv`.
(Yes — the column named `readable_id` holds the value the API calls
`membershipId`, while the API's `readableId` is our `player_id`. Blame the API.)

## Field mapping → normalized record

`src/fargo_api.py` normalizes both endpoints to one schema so the rest of the
code never touches these field names:

| normalized | from `players/{id}` | from `indexsearch` |
|---|---|---|
| `player_id` (int) | `Id` | `readableId` |
| `membership_id` (str) | `BBMMembershipId` | `membershipId` |
| `row_id` (str) | `RowId` | `id` |
| `name` (str) | `FullName` | `firstName` + `lastName` |
| `rating` (int) — **RAW** | `FargoRating` | `effectiveRating` (fallback `rating`)² |
| `robustness` (int) | `Robustness` | `robustness` |
| `provisional_rating` (int) | `ProvisionalRating` | `provisionalRating` |
| `effective_rating` (int) — **DISPLAYED** | *computed*¹ | `effectiveRating` |
| `location` (str) | `State` | `location` |
| `rating_quality` | derived | derived |

Notes:
- **All numeric values come back as JSON strings** (`"438"`, `"63"`) — parse to int.
- **RAW vs DISPLAYED rating — these are two different numbers.** `FargoRating`
  (the pull's `rating`) is the raw whole-history performance rating. The
  fargorate.com **site displays** `effectiveRating`, a games-weighted blend of
  the raw rating and the `ProvisionalRating` "starter":

      effective = provisional + (min(robustness, 200) / 200) * (rating - provisional)

  Weight on the raw rating is `robustness/200`; at robustness ≥ 200 (established)
  the starter drops out and **effective == raw**. A provisional of `0` (no
  starter — established players and some new accounts) also gives effective ==
  raw. Verified live 2026-07-15: player 1310533 raw `458` / provisional `440` /
  robustness `67` → `446`, matching the site.
  ¹ `/api/players/{id}` does **not** return `effectiveRating` (and the id-keyed
  `/api/ratings/{id}` returns `[]`), so the pull **computes** it from the three
  fields the id lookup does return — `FargoRating`, `ProvisionalRating`,
  `Robustness` — via `fargo_api.effective_for()`. No name re-search needed, so
  the identity invariant holds. The exact half-integer rounding rule is
  UNVERIFIED (only whole-number cases observed); round-to-nearest is used.
  ² The `indexsearch` normalizer historically keeps `rating` = the *displayed*
  value (`effectiveRating`, fallback `rating`) for name resolution; the pull
  never uses search, so its `rating` is always the raw `FargoRating`.
- **This assumption used to be invisible.** At the Phase-0 fixture (2026-06-05)
  the player was raw 438 / provisional 0, so effective == raw == 438 and the two
  fields agreed — the divergence only appears once a preliminary player's raw
  rating pulls ahead of the blended one. Do **not** assume `FargoRating ==
  effectiveRating`.
- `rating_quality` = `established` if `robustness >= 200` else `preliminary`.
  The 200 threshold was confirmed in Phase 0 (FairMatch's progress bar read
  63/200 ≈ 32% for this player) and is the same line at which the starter drops.
- The pull records the raw rating in `data/history.csv` and the displayed
  (effective) rating in `data/effective_history.csv` (parallel append-only log,
  added 2026-07-15), each triggered independently by its own value moving.

## Known-answer fixture (regression anchor)

`player_id 1310533` → `rating 438`, `robustness 63`, `location CO`,
`membership_id 9900007849538`, `name "Nathan Carroll"`. Used in tests.
(Live ratings drift over time; if this player moves, the *shape* still holds —
only the rating/robustness numbers change.)

## Etiquette

Unauthenticated but be polite: descriptive `User-Agent`, ~1s between calls,
20s timeout, tolerate transient non-200s by skipping that player (see the
partial-failure policy in `src/pull.py`).
