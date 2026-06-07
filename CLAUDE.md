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
  any importer/resolver only adds new ids; existing player_ids are never removed).
  Entry shape varies by how the id was added, and that's fine — the pull only
  reads `player_id` (+ `name` as a log hint) and re-fetches everything live:
  - resolved via the API (`resolve.py`) → stores the full FargoRate `record`.
  - imported from a source list (`import_digitalpool.py`) → slim entry
    `{player_id, membership_id, name, state, source, <source>_id, added_date}`,
    no `record` block. Source ndjson is **never committed** (it carries PII —
    emails/phones); only the non-PII Fargo fields are extracted.
  - **Additive cross-links are allowed** (the one exception to "don't touch
    existing entries"): an importer may *add a new field* to an existing entry to
    record a cross-system link — e.g. `import_apa.py` attaches an `apa` list of
    APA memberships to a matched player. The rule is strictly additive: existing
    fields are never modified or reordered, and the link itself is append-only
    (re-running adds new memberships, never rewrites). One human can hold several
    APA memberships ("skill levels"); all are kept on the one player, never
    collapsed.
- `people.json` — **generated master profile file** (one entry per *person*); see
  the person/profile layer below. Never hand-edit; regenerate with `people.py`.
- `people_merges.json` — curated, human-confirmed "these player_ids are the same
  human" list (the only hand-maintained input to `people.json`).

## Person/profile layer (`src/people.py`)
`roster.json` keys on `player_id` — but a single human can hold several FargoRate
accounts (duplicate registrations) and several league memberships. `people.json`
is the **master profile file**: one entry per person, owning all their
`fargo_player_ids` + source-tagged `memberships` + `sources` + notes.
- **Generated, not authored.** `python src/people.py build` regenerates
  `people.json` from `roster.json` + `people_merges.json` (union-find groups
  player_ids; the smallest id is the `person_id`). Idempotent; new roster
  additions flow in on the next build, and a person is never duplicated.
- **Additive only.** This layer never changes `roster.json`, the daily pull, or
  `history.csv` — every `player_id` stays in the roster and is still scraped
  daily. The profile just *aggregates* them.
- **Source-agnostic.** `memberships` are `{"source": "apa", ...}`, so NAPA/other
  leagues plug in with no schema change.
- **Profiles.** `python src/people.py profiles` renders `docs/profiles.md` (cards
  with each person's current rating per id from `history.csv` + source notes);
  default is merged / multi-source people, `--all` renders everyone.
- To merge ids confirmed as one human, add an entry to `people_merges.json` and
  re-run `build`. Merging is identity-only — it never prunes the roster.

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
- **APA** (`import_apa.py`) — APA runs its own rating system, so its master
  export carries **no FargoRate id and no state**, only APA ids + names. The
  bridge is therefore name-based and runs in two steps:
  - `crossref` (no network) — buckets each APA name against the roster:
    *matched* (one rostered id → attach an `apa` cross-link, see above),
    *ambiguous* (rostered name held by >1 id → reported, never auto-linked),
    *new* (not in roster → queued to `docs/resolve/apa_to_resolve.json`).
  - `resolve` (network, `.github/workflows/resolve-apa.yml`) — searches FargoRate
    per queued name and selects via `pick_match`: a single match is accepted
    **regardless of state** (CO is *preferred* when a CO and an out-of-state
    namesake both exist, so clean local matches are never lost); >1 match in a
    state bucket is **ambiguous**. CO is detected with `is_co()` (FargoRate's
    `location` is inconsistent — "CO" *or* "Denver CO" *or* "Denver, CO" — so it
    tests the trailing state token, not an exact string). Zero-hit names are
    retried with first-name nickname variants (Andy↔Andrew, …) guarded by a
    surname match; transient API errors are retried. Writes
    `docs/resolve/apa_resolution.json` with four buckets: `resolved` (exact-name
    single), `variant_candidates` (single via a nickname variant), `ambiguous`
    (>1), `unfound`.
  - `add` (no network; runs in the same workflow after `resolve`) — **auto-adds
    only the `resolved` (exact-name single) matches** to roster.json (slim entry
    + `apa` cross-link, or cross-link onto an existing id). `variant_candidates`
    and `ambiguous` are **staged for human review, never auto-added** — names
    collide and the roster is never pruned, so a wrong link would track the wrong
    person forever. Once reviewed, `add --variants` folds the variant bucket in.
  - `reclassify` (no network) — re-runs `pick_match` over the stored `ambiguous`
    candidate lists and promotes any that now resolve (e.g. a lone CO player
    among out-of-state namesakes). Lets a `pick_match` fix be applied to an
    existing resolution file without re-scraping.
  - `recover` (network, `.github/workflows/recover-apa.yml`) — second-chance pass
    over `unfound`: searches a short first-name prefix + surname (`recover_query`,
    e.g. "Shir Patel") to catch names that differ between APA and FargoRate
    (APA "Shirishkumar Patel" → FargoRate "Shirish Patel") or players who moved.
    A plain surname search 500s on FargoRate, so the prefix qualifier is required.
    Candidates are kept when surname matches and `first_compatible` (prefix or
    nickname). Writes `docs/resolve/apa_recovery.json` — **staged for review,
    never auto-added** (lower confidence than the exact pass).
  - `manual` (no network) — adds hand-picked resolutions from
    `docs/resolve/apa_manual.json` (`{search_name, player_id, …}`), tagging links
    `match_method "manual"`. For disambiguated multi-CO names and recovery/lookup
    hits the user confirmed. APA memberships are pulled from the resolve queue by
    name. **Out-of-state FargoRate location is NOT disqualifying** — many local
    APA players moved to CO while FargoRate still shows their old state, so a
    single out-of-state match is a real player. The raw APA file lives under
    git-ignored `basket/`.
- **NAPA** (planned) — same shape as APA (own rating system, name+state resolve);
  role is the *team override* (include a local-league player regardless of
  FargoRate location) and gap-fill.
- **Scale note:** ~1,182 ids means ~1,182 fetches/run at ~1s each (~20 min on
  the Actions runner). Acceptable for a daily job; revisit if the roster grows.
  The APA resolve is a one-off batch of ~2,000+ searches (~35–40 min), separate
  from the daily pull.

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
(`.github/workflows/pull.yml`), name resolution (`.github/workflows/resolve.yml`),
the APA batch resolve (`.github/workflows/resolve-apa.yml`), and the APA recovery
pass (`.github/workflows/recover-apa.yml`). Claude Code only needs `github.com`.
No LLM runs inside any scheduled job.

## Source of truth for the API
`docs/api.md` — verified endpoints, field mapping, and the known-answer fixture
(`player_id 1310533 → rating 438 / robustness 63 / CO / membership 9900007849538`).

## Out of scope (do not build without instruction)
Comparing the *ratings* of another system vs FargoRate (e.g. NAPA rating vs
Fargo) — note this is distinct from the in-scope *identity* cross-referencing
that builds the roster; change thresholds/debounce; off-platform alerts
(email/push) or Issue notifications.

**Location note (updated):** the DigitalPool import is still Colorado-only at
*admission*, but APA resolution now admits a player on a single unambiguous
FargoRate match **regardless of state** (an APA-CO-league player whose FargoRate
location is elsewhere is still tracked). Once admitted, the admission-vs-tracking
invariant applies unchanged: the pull fetches every rostered id forever.

## Tests
`tests/test_pull.py` covers the recording rules; `tests/test_import_digitalpool.py`
covers the DigitalPool importer (id extraction, state filter, dedup, idempotent
merge); `tests/test_import_apa.py` covers the APA cross-reference (fee-prefix
cleaning, name norming, crossref bucketing, strictly-additive/idempotent
cross-link, nickname `variant_queries`, `is_co` location parsing, CO-preferred
`pick_match` (incl. city-formatted CO), `resolve` bucket routing with
`fargo_api.search` stubbed — including the surname guard and transient-error
retry — `reclassify` promotion, `add` upsert incl. `--variants`, `first_compatible`
/`recover_query`/`recover` routing, and `manual` picks). All use temp files with
no network —
`resolve`'s real FargoRate calls are exercised on a runner, not in tests.
`tests/test_people.py` covers the person/profile layer (merge grouping, smallest-id
person_id, source union + membership dedup, chained merges, profile rendering with
history). Run: `pip install -r requirements-dev.txt && pytest -q`.
