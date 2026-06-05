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
- `roster.json` — keyed on `player_id`; stores the full API record + `added_date`.
  Re-running resolution only appends new players.

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
Cross-database comparison vs another rating system; change thresholds/debounce;
off-platform alerts (email/push) or Issue notifications.

## Tests
`tests/test_pull.py` covers the recording rules against temp files with a fake
client (no network). Run: `pip install -r requirements-dev.txt && pytest -q`.
