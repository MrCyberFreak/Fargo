# Cross-league identity & roster sources -- design

Status: DESIGN (no code yet). Authored 2026-06-27. This is the contract for
adding **NAPA** and **BCA** as roster sources alongside the existing
**DigitalPool** and **APA**, with the overriding goal of **accurately
identifying the same human across the pool-data cluster projects**
(`Fargo`, `bca`, `NAPA`, `APA-Scraper`, consumed by `PoolPredict`).

Grounded by two domain experts (`entity-resolution-engineer`,
`pool-rating-systems-expert`) plus live probes of FargoRate. Decisions already
made by the user are marked **[DECIDED]**; remaining forks are in
"Open decisions" at the end.

---

## 1. Architecture -- Fargo is the identity hub  [DECIDED]

Each league importer in Fargo resolves a league's players to a stable FargoRate
`player_id` and writes an **additive, append-only cross-link** onto the matching
`roster.json` entry (exactly like today's `apa[]` list), admitting any new
player_ids to the tracked roster. Identity resolution is therefore **centralized
in Fargo**, which already owns the FargoRate spine (`roster.json` + `people.json`
+ `people_merges.json`).

The downstream consumer **PoolPredict** stops guessing identity by name+state and
instead consumes a slim, PII-free crosswalk Fargo publishes (section 6). This
directly fixes PoolPredict's current weakest link: it joins NAPA purely by
name+state, and NAPA's state field is empty, so that join is effectively
name-only today.

### Why this is the right shape
- Fargo is already documented as the "identity spine + anchor rating" in
  PoolPredict's own DESIGN.md.
- The `apa[]` cross-link + `people.py` union-find already implement the
  centralized-hub pattern for one source; NAPA/BCA are the same pattern repeated.
- Resolution against FargoRate (name search + corroboration) needs FargoRate API
  access; Fargo is the project built around that API.

### Data handoff -- `_ref/` clone in CI  [DECIDED]
Hard project boundary: Fargo must never read a sibling repo's local working copy.
The resolve/import workflows **git-clone the sibling GitHub repos into a
git-ignored `_ref/`** at run time and read their committed data there.

- `_ref/` is added to `.gitignore`; nothing under it is ever committed.
- A small helper (`scripts/sync_ref.ps1` or inline in the workflow) shallow-clones
  `MrCyberFreak/NAPA`, `MrCyberFreak/APA-Scraper`, and (later)
  `MrCyberFreak/bca` into `_ref/<name>/`.
- This also lets us **automate APA**: switch `import_apa.py` from the manual
  `basket/` drop to reading `_ref/APA-Scraper/data/.../players_master.json`.
  (Keep `basket/` working as a fallback.)
- Upstream data is read-only and untrusted-for-execution: **never run upstream
  code.** Parse committed data files only.

### Correction to a stale fact
The Fargo `CLAUDE.md` "build sandbox cannot reach fargorate.com" note is **no
longer true**: the sandbox now reaches both `lms.fargorate.com` and
`dashboard.fargorate.com/api` directly (verified 2026-06-27). Resolution can be
developed and tested locally; the scheduled daily pull still belongs on Actions
for automation. The CLAUDE.md note will be corrected when code lands.

---

## 2. What each source actually carries (VERIFIED 2026-06-27)

| Source | Identity key available | Corroboration signal | Strength |
|---|---|---|---|
| **DigitalPool** | FargoRate id directly (`fargo_data.readableId`) | n/a (strong key) | strongest |
| **BCA** (BCAPL on FargoRate LMS) | **name only** on the public LMS reports | **the player's real FargoRate rating** (LMS `RATING` column = FargoRate `effectiveRating`) | strong via rating fingerprint |
| **APA** | APA member id (no FargoRate id, no state) | none usable (SL is an ordinal bracket) | weak (name-based) |
| **NAPA** | NAPA 8-digit id (no FargoRate id) | CSR exists but is NOT Fargo-scale (+/-70 pt crosswalk); state field empty | weakest (name-only, single region) |

Verified specifics:
- **FargoRate API** `/api/indexsearch?q=<name>` returns candidates with
  `readableId` (= player_id), `membershipId`, `firstName/lastName`, `location`
  (inconsistent: "CO" / "Denver CO" / "Denver, CO"), `rating`, `robustness`,
  `effectiveRating`. `fargo_api.search()` normalizes `rec.rating =
  effectiveRating or rating`. `/api/players/{id}` returns `FargoRating` (the raw,
  NOT effective).
- **BCA LMS** `GeneratePlayerListReport/{divisionId}` returns NAME + RATING only.
  RATING is the FargoRate `effectiveRating` (verified: BCA "Chris Foster 518" ==
  the Centennial CO Chris Foster's API `effectiveRating` 518, vs the Little Rock
  AR namesake's 421). There is **no Fargo id and no membership number** anywhere
  in the public BCA reports. Some divisions are labeled "151/VB" (a division-name
  artifact, not a different rating scale -- see section 4.3).
- **NAPA** = NAPA of Northern Colorado (division 13077 today, ~85 players; more
  divisions planned). The committed export carries the NAPA 8-digit `player_id`,
  `name`, per-game CueSpeed Rating (`csr_8/9/10`), gender. `home_base`/state is
  **unpopulated**. The `napa.db` is NOT committed (regenerable); ingestion must
  parse NAPA's committed **raw** roster export, never rebuild the db (no upstream
  code execution).

---

## 3. Cross-link schema (additive, append-only, idempotent)

Mirror the existing `apa[]` pattern exactly. Verified current shapes:

```jsonc
// existing apa[] cross-link on a roster entry:
"apa": [ { "member_id": 2844341, "member_number": "80402048",
           "name": "Nathan Carroll", "first_session": 135, "last_session": 137,
           "sessions_count": 3, "source": "apa", "match_method": "name",
           "added_date": "2026-06-06" } ]

// existing slim importer entry (no record block):
{ "player_id": 625764, "membership_id": "0004670", "name": "Adam Sisneros",
  "state": "CO", "source": "digitalpool", "digitalpool_id": 602,
  "added_date": "2026-06-06" }
```

New cross-link lists (strictly additive; re-running an importer only appends new
memberships, never mutates or reorders existing fields):

```jsonc
// napa[] -- dedup key = napa_player_id (stable)
"napa": [ { "source": "napa", "napa_player_id": 81234567,
            "name": "<name as seen>", "division_id": "13077",
            "division_name": "NAPA of Northern Colorado",
            "csr": { "csr_8": 62, "csr_9": null, "csr_10": null },  // advisory only, never a gate
            "imputed_state": "CO",
            "match_method": "name|variant|manual", "confidence": "high|medium",
            "added_date": "YYYY-MM-DD" } ]

// bca[] -- dedup key = (bca_division_id, norm(name)); BCA has NO per-player id
"bca": [ { "source": "bca", "bca_division_id": "<lms divisionId>",
           "division_name": "...", "name": "<name as seen on LMS>",
           "lms_rating_seen": 518,            // the effectiveRating fingerprint at link time
           "matched_effective_rating": 518, "rating_delta": 0,
           "match_method": "name+rating|rating-disambig|name|from-bca-id|manual",
           "confidence": "high|medium", "added_date": "YYYY-MM-DD" } ]
```

Idempotency note: because BCA has no source-side player id, an LMS **name change**
would append a second `bca[]` entry rather than update one. Acceptable under the
additive invariant; caught by the audit (section 5). NAPA uses the stable
`napa_player_id` and has no such issue.

New slim roster entries for never-before-seen NAPA/BCA players follow the existing
importer shape (`player_id`, `membership_id`, `name`, `state`, `source`,
`added_date`, plus the cross-link list).

---

## 4. Per-source resolution design (precision-first; never auto-merge two humans)

### 4.0 Shared name-matching module (`src/namematch.py`)  [proposed refactor]
The functions `norm`, `surname`, `is_co`, `variant_queries`, `first_compatible`,
`recover_query`, `pick_match`, `_search_with_retry` currently live inside
`import_apa.py`. Extract them into `src/namematch.py` so APA/NAPA/BCA share ONE
normalization and cannot drift. APA behavior must stay byte-identical (existing
tests green). All three importers keep the proven `crossref -> resolve ->
add/reclassify/recover/manual` command shape.

Two hard quirks the shared module must honor (verified):
- **FargoRate search is fuzzy** -- a query for "Chris Foster" also returns
  "Christie Foster"/"Chris Fosterud". The `surname(candidate) == surname(query)`
  guard is mandatory before accepting any match.
- **Bare-surname searches 500** on FargoRate (and certain corrupted records do
  too). Always qualify a search with a first-name prefix; names hit by the bug
  must be added by id, not search. (Already learned in APA `recover`.)

### 4.1 BCA -- name + FargoRate-rating fingerprint  [DECIDED: deferred + modulate-by-robustness]

**BCA is on hold as a Fargo-side scraper.** The `bca` project is producing its
own roster; Fargo will **integrate that roster** when it is ready. Fargo does NOT
scrape `lms.fargorate.com`. Two integration scenarios -- the cross-link schema,
`people.py`, and crosswalk are identical either way:

- **Scenario A (preferred):** the `bca` project resolves its players to FargoRate
  ids on its side (it has the LMS name + rating and can run the exact
  rating-corroborated search below) and ships a committed, PII-free roster file
  carrying at least `{name, fargo_player_id, division_id, rating_seen,
  match_method, confidence, state}`. Fargo's `import_bca.py` becomes a thin
  **strong-key consumer** (like DigitalPool): admit the id, attach the `bca[]`
  link, re-validate against the live API. Lowest risk; no LMS knowledge
  duplicated in Fargo.
- **Scenario B (fallback):** the `bca` roster ships only `{name, rating_seen,
  division}`; Fargo runs the full rating-corroborated resolution below.

Either way, **the interface contract** (what fields the bca roster file must
carry, and where resolution happens) is the key coordination item with the bca
project -- see Open decisions.

**Rating-corroborated resolution (used by whichever side resolves):**
- Compare the LMS/seen rating against the candidate's **`effectiveRating`**
  (= `rec.rating` from `search()`), never `FargoRating`.
- **Modulate the tolerance by robustness** (grounded: most CO league players are
  preliminary -- the api.md fixture player is robustness 63 -- so a hard
  `rob>=200` floor would reject the majority):
  - `robustness >= 200` (established): confirm within **~15** pts.
  - `robustness ~50-200` (preliminary): confirm within **~30** pts AND require a
    second signal (CO/division prior or exact name) before auto-accept.
  - `robustness < ~20`: rating is a starter placeholder -> **do NOT
    rating-corroborate**; require name + region or stage for review.
  - Any candidate with `|delta| > ~50` is a rating CONFLICT -> stage (probably a
    different person), regardless of robustness.
- **Namesake separation:** two same-surname CO candidates are safely
  distinguishable only when their `effectiveRating`s differ by **>= ~50-60**. To
  auto-pick among namesakes, the chosen candidate must be inside the confirm band
  AND every other same-surname CO candidate must be clearly outside it. If two
  candidates both sit within ~30 of the seen rating -> **ambiguous, stage**.
- **Per-division scale check (critical safety):** the LMS `RATING` is Fargo by
  definition, but a handicap column could leak in for some division config. Before
  trusting any division's ratings: (1) range-gate the column (reject if values are
  small-integer / near-zero / negative / capped -- watch for a hard ceiling at
  151 on "151/VB" divisions), and (2) spot-check 3-5 known established players
  against the API. Cache the per-division verdict. Never decode the label; verify
  empirically.

BCA bucket routing -> `docs/resolve/bca_resolution.json`:
`resolved` (name+rating corroborate, auto-add) / `rating_corroborated` (rating
broke a namesake tie, auto-add) / `rating_conflict` (lone namesake, rating
disagrees -> review) / `name_only` (single CO match but candidate sub-floor,
rating non-informative -> review) / `ambiguous` (review) / `unfound`.
`add` auto-applies ONLY `resolved` + `rating_corroborated`.

### 4.2 NAPA -- name-only, CO imputed (the hard one; be honest)

NAPA has no FargoRate id, no state, and a single geographic region (N. Colorado).
Resolution does not need the source's state: search FargoRate by name and use
**FargoRate's own `location`** for CO-preference via `is_co()`. Mechanically this
is identical to APA's `resolve`.

- Auto-add ONLY exact-name single (CO-preferred) matches. Variants/ambiguous/
  recovery are **staged**, never auto-added.
- **CSR is NOT a corroboration gate.** It is not on the Fargo scale; the
  CSR->Fargo crosswalk residual is +/-70 pts (proven in PoolPredict M4) -- wide
  enough to avoid rejecting true matches but far too wide to discriminate two CO
  namesakes, and gating on a crosswalk fit from name+state joins would be
  circular. CSR is stored as advisory `csr` only, and may appear as a soft
  warning in the review queue.
- **Honest precision ceiling:** the *unique-name* NAPA slice can be made
  high-precision (one name -> one CO candidate). The *common-name* slice cannot be
  resolved name-only with no state -> **quarantine for human review, leave
  NAPA-only-unmatched rather than guess.** A wrong link on a never-pruned roster
  tracks the wrong human forever. A harmless unmatched duplicate is the correct
  failure mode.
- Geographic prior: prefer CO, but an out-of-state FargoRate `location` is NOT
  disqualifying (locals move; FargoRate location lags) -- same proven APA lesson.

NAPA buckets -> `docs/resolve/napa_resolution.json`: `resolved` /
`variant_candidates` / `ambiguous` / `unfound` (+ `napa_recovery.json`,
`napa_manual.json`), mirroring APA exactly. `add` auto-applies only `resolved`.

### 4.3 APA -- unchanged, plus `_ref/` automation
Keep the existing crossref/resolve/add/reclassify/recover/manual pipeline. Only
change: optionally read the APA master from `_ref/APA-Scraper/...` instead of the
manual `basket/` drop (basket/ stays as fallback). Behavior otherwise identical;
moves shared functions into `namematch.py`.

---

## 5. De-dup, false-merge safety, and audits

### Cross-linking is NOT merging (the core distinction)
- The same human in BCA + NAPA + APA = three cross-link lists hanging on the
  **one** Fargo `player_id`. `people.py` aggregates them into one profile. This is
  the normal, desired case and is fully **automatic and safe** -- it never touches
  `people_merges.json`.
- Two **different** Fargo `player_id`s that are the same human (duplicate Fargo
  registrations) is the only thing `people_merges.json` is for, and it stays
  **manual, curated, append-only**. Precision over recall binds hardest here: a
  wrong union fuses two humans and silently corrupts rack-level predictions
  downstream.

### Two distinct false-positive risks
1. **Wrong attachment** (a NAPA/BCA membership hung on a namesake's id):
   prevented by the precision gates above; detected by the audits below.
   **Correction ledger [proposed]:** add a `docs/resolve/<source>_unlink.json`
   list of `(source_member_key, wrong_fargo_id)` suppressed on rebuild, so
   corrections are replayable instead of silent hand-edits to `roster.json`.
   (Today the only fix is a manual roster edit -- e.g. commit `cafddb4`
   reassigned a wrong APA "Andrew Tran" link.)
2. **Cross-source merge candidate:** when BCA "John Smith" -> Fargo id A and NAPA
   "John Smith" (same `norm`, both CO) -> Fargo id B with A != B, that is
   *evidence* (not proof) A and B may be one human with two Fargo accounts. This
   must **never auto-write `people_merges.json`.** Emit to
   `docs/resolve/people_merge_candidates.json` for human review; a confirmed one
   becomes a hand-added merge row, then `people.py build`.

### `people.py` changes required
- `_sources_of`: add `"napa"` / `"bca"` when those keys are present.
- `_memberships_of`: iterate `entry.get("napa")` and `entry.get("bca")` in
  addition to `apa`.
- **BCA id-less dedup fix (verified bug):** `build()` dedups memberships on
  `(mem["source"], mem.get("member_id"))`. BCA has no `member_id`, so all of one
  person's BCA links would collapse to a single `(bca, None)`. `_memberships_of`
  must emit a synthetic stable key for BCA, e.g.
  `member_id = f"{bca_division_id}:{norm(name)}"`, so multiple BCA divisions
  survive. (NAPA uses `napa_player_id`, fine.)
- `_render_profile` reads `m.get("member_number")` -- BCA has none; render a
  sensible label (division + name) for BCA memberships.

### Audit procedures (read-only; each writes `docs/resolve/audit_*.json`)
1. **Inverted-index collision (hard error):** build `source_member_key ->
   {fargo_ids}`; any key mapping to >1 Fargo id = one source player linked to two
   humans. Must be empty.
2. **BCA rating re-check:** for every `bca[]` link, re-`search()` the linked name
   and confirm the linked id is still the closest CO candidate to
   `lms_rating_seen` and no other same-surname CO candidate is closer. (Use
   `search`, not `get_player` -- only `search` carries `effectiveRating`.)
3. **Provisional-seed audit:** flag any BCA auto-accept whose candidate had
   `robustness < ~20` (rating was a starter placeholder).
4. **Name-divergence audit:** flag any Fargo id whose attached source name
   normalizes differently beyond a known nickname pair (`first_compatible` false).
5. **Merge-sanity audit:** re-run union-find; flag any merged person whose
   constituent **established** ids are rated far apart or sit in different states,
   and list every id pulled in only by transitive closure (so a human can eyeball
   the chain).

---

## 6. PoolPredict consumption contract

Fargo publishes a slim, PII-free crosswalk (new `python src/people.py crosswalk`
-> `docs/crosswalk.json`) so PoolPredict consumes one stable file, not
`roster.json` internals:

```jsonc
{ "apa":  { "<member_id>":      { "fargo_player_id": 0, "person_id": 0, "confidence": "high|medium", "match_method": "..." } },
  "napa": { "<napa_player_id>": { "fargo_player_id": 0, "person_id": 0, "confidence": "...", "match_method": "..." } },
  "bca":  { "<division_id>:<norm_name>": { "fargo_player_id": 0, "person_id": 0, "confidence": "...", "match_method": "..." } } }
```

PoolPredict resolution order (the contract; PoolPredict code is edited in that
repo, not here):
1. If a source row's id is in the crosswalk, use that `fargo_player_id` and
   **skip name+state entirely**.
2. On a miss only, fall back to the current name+state heuristic AND tag the row
   `unverified` so it can be down-weighted and fed back to Fargo's review queue.
3. Carry `match_method`/`confidence`: PoolPredict may require `high` for
   rack-level prediction and treat `medium` as no better than its own guess.
4. Join on `person_id` to gather a human's accounts; predict per `fargo_player_id`
   (the rating grain). Provide both.

---

## 7. Build sequence (BCA deferred)

- **M0 Scaffolding:** `_ref/` clone helper + `.gitignore` entry; extract
  `src/namematch.py` from `import_apa.py` (APA tests stay green); correct the
  stale CLAUDE.md fargorate-reachability note.
- **M1 NAPA:** `src/import_napa.py` -- parse NAPA committed raw roster from
  `_ref/NAPA`, name-only CO-imputed resolution, staging buckets, `napa[]` links,
  honest common-name quarantine. Tests with stubbed `search` + fixtures.
- **M2 people.py + crosswalk:** extend `_sources_of` / `_memberships_of`, fix the
  BCA id-less dedup with a synthetic key, render BCA memberships, add the
  `crosswalk` subcommand -> `docs/crosswalk.json`. Tests.
- **M3 Audit + safety ledgers:** `audit_*.json` reports + `<source>_unlink.json`
  + `people_merge_candidates.json`. Tests.
- **M4 APA `_ref/` automation:** switch APA master read to `_ref/APA-Scraper`
  (basket/ fallback). Tests.
- **M5 CI:** `resolve-napa.yml` (clone -> resolve -> add -> people build ->
  commit). Update CLAUDE.md "Roster sources" + add a "Cross-league identity"
  contract section.
- **M6 BCA (deferred):** once the `bca` project ships its roster, build
  `src/import_bca.py` per the chosen interface scenario (4.1) + `resolve-bca.yml`.

Note: BCA is designed now (schema, people.py, crosswalk, audits all account for
it) but built last, pending the bca project's roster + the interface decision.

---

## Open decisions / coordination

1. **BCA interface contract with the `bca` project** (Scenario A vs B in 4.1):
   does the bca roster ship resolved `fargo_player_id`s (preferred -- Fargo is a
   thin consumer), or name+rating for Fargo to resolve? What exact PII-free fields
   will the committed bca roster file carry, and at what path?
2. **`namematch.py` refactor** before adding sources: confirm go-ahead
   (recommended -- keeps the three importers from drifting).
3. **`<source>_unlink.json` correction ledger** vs continuing to hand-edit
   `roster.json` for wrong links: adopt the ledger? (recommended).
4. **BCA tolerance constants** (`~15` established / `~30` preliminary / no
   corroboration < rob ~20 / conflict > ~50 / namesake separation >= ~50-60):
   accept as defaults or tune.
5. **Cross-source merge candidates stay manual** (`people_merge_candidates.json`
   -> hand-added `people_merges.json`, never auto-merged): confirm (this is the
   existing stated invariant; recommended yes).

## Provenance
- Experts: `entity-resolution-engineer` (linkage design, thresholds, audits,
  people.py touchpoints), `pool-rating-systems-expert` (BCA RATING ==
  effectiveRating + per-division scale-check, robustness modulation, NAPA CSR
  ceiling, FargoRate quirks). Both read the live Fargo project.
- Live probes (2026-06-27): FargoRate `indexsearch`/`players` reachable from
  sandbox; BCA LMS `GeneratePlayerListReport` returns NAME+RATING (= Fargo
  effectiveRating); the api.md fixture player (1310533) confirmed.
- Sibling repos inspected via GitHub (not local files, per the hard boundary):
  `PoolPredict/src/identity.py` + `docs/DESIGN.md`, `bca` README + models,
  `NAPA/DATA.md`, `APA-Scraper` tree.
