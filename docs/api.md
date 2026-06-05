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
| `rating` (int) | `FargoRating` | `effectiveRating` (fallback `rating`) |
| `robustness` (int) | `Robustness` | `robustness` |
| `location` (str) | `State` | `location` |
| `rating_quality` | derived | derived |

Notes:
- **All numeric values come back as JSON strings** (`"438"`, `"63"`) — parse to int.
- `rating` = **effectiveRating / FargoRating** (438), *not* the search field
  `rating` (437), which is a different (pre-effective) value.
- `rating_quality` = `established` if `robustness >= 200` else `preliminary`.
  The 200 threshold was confirmed in Phase 0 (FairMatch's progress bar read
  63/200 ≈ 32% for this player).

## Known-answer fixture (regression anchor)

`player_id 1310533` → `rating 438`, `robustness 63`, `location CO`,
`membership_id 9900007849538`, `name "Nathan Carroll"`. Used in tests.
(Live ratings drift over time; if this player moves, the *shape* still holds —
only the rating/robustness numbers change.)

## Etiquette

Unauthenticated but be polite: descriptive `User-Agent`, ~1s between calls,
20s timeout, tolerate transient non-200s by skipping that player (see the
partial-failure policy in `src/pull.py`).
