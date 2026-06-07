# CLAUDE.md — FargoRate Rating Tracker

Contract for future Claude Code sessions. Keep it accurate; downstream behavior
depends on it.

## Goal
Track FargoRate (pool/billiards) ratings for a fixed roster of players over
time. On a schedule, fetch each player's current rating + robustness and record
a new dated entry **only when one of those values changed**. The committed
`data/history.csv` is the system of record; git history is the audit trail.

## Recording rules (the core behavior — do not change without instruction)
For each player on each run:
1. **First time a player is ever seen** → write a **baseline** entry (today's
   date, current rating + robustness, `entry_type = baseline`).
2. **Subsequent runs** → compare fetched `rating` and `robustness` to that
   player's **most recent recorded entry**:
   - If **either** differs → append a **change** entry.
   - If **both** are identical → write **nothing**.
3. **No threshold / no debounce.** Any difference, including a 1-point wobble,
   is recorded.

Only `rating` and `robustness` trigger a row. `rating_quality`
(`preliminary`/`established`, established at robustness ≥ 200) is recorded for
visibility but is **not** an independent trigger.

## Identity — key on `player_id`, never name
Resolution (Phase 1, `src/resolve.py`) maps names → ids once. The pull keys on
`player_id` forever and **never re-searches by name** (names collide).

Three identifiers exist and the API's names are confusing (see `docs/api.md`):
- `player_id` (e.g. `1310533`) — the join key and `/api/players/{id}` path key.
  The API calls this `readableId` (search) / `Id` (lookup).
- `membership_id` (e.g. `9900007849538`) — public number, stored as the
  `readable_id` column in history.csv for the future cross-DB join. API:
  `membershipId` / `BBMMembershipId`.
- a `row_id` GUID — preserved in the full record, never used as a key.

## Data files (append-only invariant)
- `data/history.csv` — append-only; new rows go at the end so each commit diff
  shows exactly what changed. Columns:
  `date_found, player_id, readable_id, name, rating, robustness, rating_quality, entry_type`
  (`entry_type` ∈ `baseline | change`). Never rewrite or reorder existing rows.
- `roster.json` — keyed on `player_id`; **append-only by player_id** (re-running
  any importer/resolver only adds new ids; existing entries are never rewritten).
  Entry shape varies by how the id was added, and that's fine — the pull only
  reads `player_id` (+ `name` as a log hint) and re-fetches everything live:
  - resolved via the API (`resolve.py`) → stores the full FargoRate `record`.
  - imported from a source list (`import_digitalpool.py`) → slim entry
    `{player_id, membership_id, name, state, source, <source>_id, added_date}`,
    no `record` block. Source ndjson is **never committed** (it carries PII —
    emails/phones); only the non-PII Fargo fields are extracted.

## Admission vs tracking (core invariant — do not break)
Location/league filters gate **admission only** — *which* player_ids get added
to the roster. Once a player is in the roster they are part of the tracked pool
**permanently and unconditionally**: the daily pull fetches every rostered id
regardless of location or league participation, and the roster is **never
pruned** (a player who moves out of CO, leaves a league, etc. keeps getting
tracked). Never add a location/league re-check to `pull.py`.

## Roster sources (how player_ids get into the roster)
The roster is built from external player lists, cross-referenced to a stable
FargoRate `player_id`. The filters below decide admission; see the invariant
above — they never cause an existing player to stop being tracked:
- **DigitalPool** (`import_digitalpool.py`) — built on FargoRate, so its export
  already carries the id at `properties.fargo_data.readableId` (the join key).
  Ignore the top-level `fargo_id` — it's the membership number with leading
  zeros stripped, NOT the id. Filtered to `fargo_data.state == "CO"` (local).
  No network needed; the daily pull validates each id on first fetch.
- **NAPA / APA** (planned) — own rating systems, no Fargo id; resolved by
  name+state via `/api/indexsearch`. Their role is the *team override* (include
  a local-league player regardless of FargoRate location) and gap-fill.
- **Scale note:** ~1,182 ids means ~1,182 fetches/run at ~1s each (~20 min on
  the Actions runner). Acceptable for a daily job; revisit if the roster grows.

## Partial-failure policy
If a player's fetch fails, log it, skip it, and continue. Successful players are
still recorded and committed. Exit **non-zero only when every player failed**
(systemic problem → red run). No-op runs write nothing → the workflow's
`git diff` guard makes no commit. Runs are idempotent.

## Date semantics (state this; it's a real caveat)
FargoRate exposes only the *current* value, not when it changed. `date_found`
is the date the change was **detected**, accurate to within one run interval
(~1 day on a daily schedule).

## Where things run
The build sandbox **cannot reach fargorate.com** (allowlist). Every FargoRate
call runs on a **GitHub Actions runner** (open internet): the scheduled pull
(`.github/workflows/pull.yml`) and name resolution
(`.github/workflows/resolve.yml`). Claude Code only needs `github.com`. No LLM
runs inside any scheduled job.

## Source of truth for the API
`docs/api.md` — verified endpoints, field mapping, and the known-answer fixture
(`player_id 1310533 → rating 438 / robustness 63 / CO / membership 9900007849538`).

## Out of scope (do not build without instruction)
Comparing the *ratings* of another system vs FargoRate (e.g. NAPA rating vs
Fargo) — note this is distinct from the in-scope *identity* cross-referencing
that builds the roster; change thresholds/debounce; off-platform alerts
(email/push) or Issue notifications. Top-world (non-local) players are
deferred — the importer keeps the hook but v1 is Colorado-only.

## Tests
`tests/test_pull.py` covers the recording rules; `tests/test_import_digitalpool.py`
covers the importer (id extraction, state filter, dedup, idempotent merge). Both
use temp files with no network. Run: `pip install -r requirements-dev.txt && pytest -q`.
