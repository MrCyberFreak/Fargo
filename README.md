# Fargo — FargoRate Rating Tracker

Tracks [FargoRate](https://www.fargorate.com/) (pool/billiards) ratings for a
fixed roster of players over time. A scheduled GitHub Actions job fetches each
player's current **rating** and **robustness** and appends a dated row to
`data/history.csv` **only when one of those values has changed**. It also tracks
the **displayed (effective) rating** — the blended number fargorate.com actually
shows a preliminary player — in a parallel `data/effective_history.csv`. The
repo *is* the database; git history is the audit trail. No external services, no
secrets.

See **[CLAUDE.md](CLAUDE.md)** for the full behavioral contract and
**[docs/api.md](docs/api.md)** for the verified API.

## How it works
- **`.github/workflows/pull.yml`** runs `src/pull.py` on a daily cron (UTC) and
  on manual dispatch. It records baseline/change rows and commits them back.
  No-change days produce no commit.
- **`roster.json`** is the fixed list of tracked players, keyed on the integer
  FargoRate `player_id` (never re-searched by name once resolved).
- **`data/history.csv`** is the append-only log of the **raw** rating:
  `date_found, player_id, readable_id, name, rating, robustness, rating_quality, entry_type`.
- **`data/effective_history.csv`** is the parallel append-only log of the
  **displayed (effective)** rating — the blended value the site shows, computed
  from the raw rating + provisional + robustness (see `docs/api.md`):
  `date_found, player_id, readable_id, name, effective_rating, provisional_rating, robustness, rating_quality, entry_type`.
  Each log is triggered independently, by its own value moving.

## Adding players (Phase 1 resolution)
Names collide, so each player is confirmed once, then tracked by id.

**With a local machine** (open internet):
```bash
pip install -r requirements.txt
python src/resolve.py search "Nathan Carroll"   # list candidates
python src/resolve.py add 1310533               # add the confirmed id
# or, interactively:
python src/resolve.py add-name "Nathan Carroll"
```

**Web / Actions-only** (no local internet to FargoRate): run the
**`fargo-resolve`** workflow (Actions → Run workflow):
1. Dispatch with **search** = the name → candidates are committed under
   `docs/resolve/candidates-*.json`.
2. Review, then dispatch again with **add_id** = the chosen `player_id` → the
   player is appended to `roster.json`.

## Schedule
`pull.yml` is set to `cron: '17 11 * * *'` (11:17 UTC daily) as a placeholder —
deliberately off the `:00`/`:30` marks, which are congested. Actions cron is
**best-effort** (5–30 min lag is normal). Change the cron to your preferred
local time converted to UTC.

## Running the pull manually
Actions → **fargo-pull** → *Run workflow*. The first run writes **baseline**
rows for every rostered player; later runs append a **change** row only when a
player's rating or robustness moved.

## Development / tests
The recording rules are covered by unit tests that use a fake API client and
temp files (no network), so they run anywhere:
```bash
pip install -r requirements-dev.txt
pytest -q
```
Test ↔ acceptance-criteria mapping lives at the top of `tests/test_pull.py`.
The end-to-end Actions + commit path (a real baseline run, and a no-op second
run that makes no commit) is validated by dispatching `fargo-pull` itself.

## Privacy
The repo holds rating data and is **private**. Private repos still get 2,000
free Actions minutes/month — far more than a daily two-minute pull needs.

## Layout
```
roster.json                 # tracked players: player_id -> full record + added_date
data/history.csv            # append-only RAW-rating log (created on first run)
data/effective_history.csv  # parallel DISPLAYED-rating log (created on first run)
src/fargo_api.py            # thin, normalized FargoRate API client
src/resolve.py              # Phase 1 name -> id resolution
src/pull.py                 # Phase 2 scheduled pull (recording rules)
tests/test_pull.py          # behavioral tests for the rules
docs/api.md                 # VERIFIED API contract (Phase 0)
docs/probe/                 # raw Phase 0 recon evidence
.github/workflows/pull.yml  # scheduled pull
.github/workflows/resolve.yml  # on-demand name resolution
```
