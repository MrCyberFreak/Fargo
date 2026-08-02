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
- **NAPA** (BUILT 2026-06-27 — see "Cross-league identity" below and
  `docs/cross-league-identity.md`) — NAPA runs its own rating system (CueSpeed),
  and its repo commits RAW roster-grid HTML (not a clean export; `napa.db` is
  gitignored/regenerable and never rebuilt). `src/napa_grid.py` parses the committed
  grids out of `_ref/NAPA`; `src/import_napa.py` mirrors the APA
  crossref/resolve/add/manual shape (name-only, CO-imputed; CSR advisory, never a
  gate). All committed NoCo divisions are admitted (~1,279 players across 58
  divisions). Same admission-vs-tracking invariant as APA.
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
FargoRate's read API (`dashboard.fargorate.com/api`) IS reachable from the build
sandbox directly (content-verified 2026-06-27 against the `docs/api.md` fixture --
player 1310533: robustness 63 / CO / membership 9900007849538), so resolution can be
developed and tested locally. But every **scheduled** job still runs on a **GitHub
Actions runner** (open internet) for automation: the scheduled pull
(`.github/workflows/pull.yml`), name resolution (`.github/workflows/resolve.yml`),
the APA batch resolve (`.github/workflows/resolve-apa.yml`), the APA recovery
pass (`.github/workflows/recover-apa.yml`), and the NAPA batch resolve
(`.github/workflows/resolve-napa.yml`, which clones `_ref/NAPA` first). The 2026-06
Actions billing outage is over and the local Windows Scheduled-Task pull stopgap was
**retired 2026-07-14** -- Actions is again the sole daily-pull writer. (Sandbox
reachability of FargoRate has been inconsistent across environments, so don't rely on
it for an unattended run.) Claude Code only needs `github.com`.
No LLM runs inside any scheduled job.

## Source of truth for the API
`docs/api.md` — verified endpoints, field mapping, and the known-answer fixture
(`player_id 1310533 → rating 438 / robustness 63 / CO / membership 9900007849538`).

## Cross-league identity (NAPA + BCA built)
Fargo is the cross-league **identity hub**: each league importer resolves players
to a stable FargoRate `player_id` and writes an additive, append-only cross-link
(`apa[]` / `napa[]` / `bca[]`) onto the matching `roster.json` entry. The full
design contract is `docs/cross-league-identity.md`.
- **Shared name matching** lives in `src/namematch.py` (norm/surname/is_co/
  variant_queries/pick_match/...), imported by every importer so they cannot drift.
- **`_ref/` mechanism:** sibling repos are shallow-cloned into the git-ignored
  `_ref/` at run time (`scripts/sync_ref.{sh,ps1}`); Fargo reads their COMMITTED data
  there and NEVER touches a sibling's local working copy or runs upstream code. APA
  now auto-sources its master from `_ref/APA-Scraper/data/master/players_master.json`
  (the manual `basket/` drop is the fallback).
- **Person layer + crosswalk:** `src/people.py` aggregates all of one human's
  cross-links into `people.json`; `python src/people.py crosswalk` publishes the
  PII-free `docs/crosswalk.json` (`{source_id -> fargo_player_id, person_id,
  confidence, match_method}`) that PoolPredict consumes instead of joining by
  name+state. BCA's id-less links use a synthetic `<league>:<norm name>` key (one
  crosswalk row per league); when the same league+name maps to two DISTINCT people the
  key is flagged `ambiguous` and accepted via `docs/resolve/collision_allowlist.json`.
- **Safety:** `src/audit.py` runs read-only audits (inverted-index collision — the
  HARD invariant, must be empty; cross-source `people_merge_candidates.json`;
  name-divergence; merge-sanity). `src/ledger.py` + `<source>_unlink.json` make a
  wrong-link correction replayable instead of a silent roster hand-edit.
- **Cross-linking is NOT merging:** one human across leagues = many cross-link lists
  on ONE `player_id` (automatic, safe). Two different `player_id`s for one human stay
  MANUAL via `people_merges.json`; the audit only emits candidates, never auto-merges.
- **BCA is built (M6):** `src/import_bca.py` folds the BCA/LMS player set into the
  roster (backfilled via #15, ~1,571 `bca[]` cross-links); `people.py crosswalk`
  publishes them under the id-less `<league>:<norm name>` key. Same admission-vs-
  tracking invariant as APA/NAPA. There is no `resolve-bca.yml` workflow yet, so a BCA
  re-resolve runs via `fargo-resolve-local` (needs the sibling `bca` clone + REF_TOKEN).

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
history). `tests/test_import_napa.py` covers the NAPA importer + grid parser
(roster-grid HTML id/name/CSR extraction, cross-division master aggregation,
crossref bucketing + NAPA-internal name-collision quarantine, additive/idempotent
`napa[]` links, resolve routing, `add` upsert); `tests/test_audit.py` covers the
read-only audits (inverted-index collisions, cross-source merge-candidates,
name-divergence, merge-sanity); `tests/test_ledger.py` covers the
`<source>_unlink.json` correction ledger and its importer wiring. Run:
`pip install -r requirements-dev.txt && pytest -q` (`pytest.ini` scopes collection
to `tests/`, so a bare `pytest` does NOT recurse into the `_ref/` sibling clones).

## Local skills (project-scoped, in `.claude/skills/`)
These live in this repo, not the global config; the global capability index below
does not list them.
- `fargo-pull-doctor` - diagnose and recover the LOCAL FargoRate daily pull
  (Scheduled Task `FargoDailyPull` / `Fargo-local-runner\run-pull.ps1`) when it
  stopped producing data: checks task Last Result, `pull.log`, a stale
  `.git\index.lock`, and battery/sleep stops. The task runs **3x/day**
  (07:20 / 13:20 / 19:20 local; daily trigger + `PT6H` repetition over a 12h
  window, reduced 2026-06-27 from every ~2h / `PT2H` after analysis showed
  FargoRate batches overnight so the morning run captures ~the whole day).
  Read-only diagnosis by default;
  clears a verified-stale lock and re-triggers only with `--recover`. **Retired
  2026-07-14** with the local stopgap -- the `FargoDailyPull` task was unregistered and
  Actions is again the daily-pull writer; kept as dormant reference only.
- `fargo-link-cleanup` - clean up wrong/divergent FargoRate cross-links the
  cross-league identity audit flagged: classify each `audit_name_divergence` flag
  against FargoRate, fix mislabeled roster names (preserving the original as
  `<source>_name`), suppress links hung on the wrong id via the
  `docs/resolve/<source>_unlink.json` ledger, then re-resolve and re-audit
  (`src/audit.py all`) until name-divergence and collisions are clean. Read-first:
  classify + propose by default, mutate only with `--apply`; optional `--source
  <apa|napa|bca>` scope. Precision-first; never guess a link, never auto-merge two
  humans (`people_merges.json` stays manual).
- `fargo-resolve-local` - run a roster source's full FargoRate resolve pipeline
  LOCALLY (NAPA/APA/BCA) when Actions is unavailable: read the matching
  `resolve-<source>.yml` as the step spec, `sync_ref` the sibling `_ref` clone
  (clone-only `dangerouslyDisableSandbox`; FargoRate calls run locally per the
  `sandbox-reaches-fargorate` memory), `crossref` -> `resolve` -> `add` (auto-adds
  only the exact-name `resolved` bucket; variant/ambiguous/recovery stay staged),
  rebuild `people.json` + crosswalk, run `src/audit.py all` then `collisions` as a
  hard gate, secret-scan and STAGE the workflow's file set - STOPPING before
  commit/push for review. `--source <napa|apa|bca>` (req), `--ref-token`/env
  `REF_TOKEN`, `--no-network`. Hands off a dirty audit to `fargo-link-cleanup`; not
  the daily pull (`fargo-pull-doctor`). Still relevant while the private siblings can't
  be cloned on Actions (no `REF_TOKEN` secret set): NAPA/BCA re-resolves fall back here.
  Once `REF_TOKEN` is set so every `resolve-<source>.yml` can clone its sibling, that
  workflow becomes the single source of truth again.

## Capabilities — what's available & when to reach for it
<!--
  The live, authoritative list is injected into every session automatically; this is a
  curated WHEN-TO-USE guide for the currently-active set, so the right tool gets picked
  reliably. It can drift as capabilities change — reconcile with `/sync-capabilities`.
  Full canonical inventory (incl. disabled plugin hundreds): $CLAUDE_CONFIG_DIR/AGENTS.md.
-->

**Routing rule (auto-delegation):** when a request falls in one of the domains below, delegate to the
matching expert agent via the Agent tool BEFORE answering yourself - don't wait to be named. Each
expert's `description` frontmatter is what the harness actually matches on; this block is the curated
when-to-use map. Prefer the most specific expert, and let the parenthetical "NOT for X (use Y)"
boundaries disambiguate when two could fire.

### Expert agents (delegate via the Agent tool)
Docs-backed — they FETCH current docs, so delegate tool questions instead of guessing:
- `claude-code-expert` — Claude Code CLI/harness: hooks, slash commands, skills, subagents, settings.json, MCP config, permissions, CLI flags, SDK.
- `claude-expert` — Claude/Anthropic API & models: model ids, pricing, context windows, Messages API, tool use, prompt caching, batches, SDKs. NOT Claude accessed via the kie.ai aggregator's Anthropic-compatible endpoint (use `kie-ai-expert`).
- `claude-design-expert` — Claude Design (claude.ai/design): canvas, canvas-built prototypes (NOT coding an app prototype), presentations, exports, `/design-sync`.
- `grok-expert` — xAI Grok models & API (docs.x.ai).
- `grok-build-expert` — Grok Build (xAI terminal coding CLI).
- `notion-expert` — Notion app & API (+ live workspace data via the Notion MCP).
- `mcp-expert` — Model Context Protocol itself: spec, building servers/clients, SDKs.
- `agile-expert` — UMBRELLA Agile: Manifesto/12 principles, mindset, Lean, XP, framework-selection; ROUTES framework-deep questions to the specialists below.
- `obsidian-expert` - Obsidian app, plugins, themes, vault, Plugin/Dev API (active; kept + documented).
- `elevenlabs-expert` - ElevenLabs AI voice platform & API: TTS, voice cloning/design, Voice Library, low-latency streaming, Scribe STT, dubbing, the Conversational AI / Agents Platform; model ids, voice settings, pricing, SDKs. NOT assistant-persona / JARVIS design (use `jarvis-expert`); NOT ElevenLabs voices resold via the kie.ai aggregator (use `kie-ai-expert`).
- `did-expert` - D-ID AI talking-avatar / digital-human VIDEO platform & API (d-id.com, studio.d-id.com, docs.d-id.com): Creative Reality Studio, the Talks / Clips / Streams (real-time WebRTC streaming avatars) / Animations APIs, D-ID Agents (conversational digital humans + embeddable chat), presenters/actors, text-to-video + custom avatar creation, webhooks, credits/pricing, SDKs. NOT the TTS voice engine that SPEAKS the avatar, even when D-ID calls it as a provider (use `elevenlabs-expert`); NOT wiring a D-ID avatar together with an ElevenLabs voice/agent - the cross-platform seam (use `did-x-elevenlabs-expert`); NOT the LLM "brain" behind a conversational agent (use `claude-expert`); NOT the surrounding web app / React UI that embeds the avatar (use `react-expert` / `frontend-design-expert`); NOT general cinematic AI video generation on Higgsfield (use `higgsfield-expert`).
- `did-x-elevenlabs-expert` - CROSSOVER expert (integration seam ONLY) for combining a D-ID talking-avatar / digital human with an ElevenLabs voice or Conversational-AI agent: the two bridges (D-ID script `provider=elevenlabs` + external key via `x-api-key-external`; D-ID's "ElevenLabs Agent" integration), who-does-what across STT / LLM / turn-taking / TTS / avatar-render / transport, the two-sided key + agent-id wiring, WebRTC / LiveKit transport, latency/cost, setup + gotchas. Owns ONLY the join; defers single-platform depth to `did-expert` / `elevenlabs-expert`, the LLM brain to `claude-expert`, and the embedding UI to `react-expert` / `frontend-design-expert`. Its corpus (`library/did-x-elevenlabs/`) is refresh-isolated so neither platform expert's Mode-B refresh can clobber the integration knowledge. (Pattern: any two experts whose services integrate get a dedicated `<a>-x-<b>-expert` crossover with its own isolated corpus.)
- `higgsfield-expert` - Higgsfield AI video/image GENERATION platform & Cloud API (higgsfield.ai, cloud.higgsfield.ai, docs.higgsfield.ai): cinematic camera-motion controlled video (text-to-video, image-to-video), the Soul image model, the 100+-model catalog behind one endpoint (ids like `higgsfield-ai/soul/standard`), the async submit -> poll/webhook request flow at platform.higgsfield.ai, API keys + `Authorization: Key` auth, the `higgsfield-client` Python SDK, credits/pricing. NOT D-ID talking-avatar / digital-human video or its Talks/Clips/Streams/Agents APIs (use `did-expert`); NOT the voice/TTS engine for a soundtrack or speaking clip (use `elevenlabs-expert`); NOT the LLM writing the prompts or app logic (use `claude-expert`); NOT the web app / React UI that embeds the output (use `react-expert` / `frontend-design-expert`); NOT Google Veo / Flow / Gemini genmedia video generation (use `google-ai-expert`); NOT the fal.ai model-inference marketplace / queue API (use `fal-ai-expert`).
- `fal-ai-expert` - fal.ai generative-media inference platform & API (fal.ai, fal.ai/docs, queue.fal.run): the 1,000+-model catalog behind one unified API (endpoint ids like `fal-ai/flux/dev`), the async queue flow at queue.fal.run (submit -> status -> result, webhooks with ED25519 signing), sync + SSE streaming + real-time WebSocket at fal.run, `Authorization: Key` auth + FAL_KEY, the `fal-client` Python + `@fal-ai/client` JS SDKs, serverless deployment of custom `fal.App` models via the fal CLI, per-output pricing/credits + the `library/fal-ai/models-pricing.md` quick reference - incl. third-party models (FLUX, Kling, Veo-on-fal) when fal is the host. NOT the Higgsfield platform (use `higgsfield-expert`); NOT Google Veo/Flow/Gemini via Google's own API (use `google-ai-expert`); NOT D-ID talking-avatar video (use `did-expert`); NOT ElevenLabs voice/TTS (use `elevenlabs-expert`); NOT the LLM writing the prompts or app logic (use `claude-expert`); NOT the kie.ai aggregator reselling model access (use `kie-ai-expert`); NOT the web app / React UI that embeds the output (use `react-expert` / `frontend-design-expert`).
- `kie-ai-expert` - kie.ai (kie.ai, docs.kie.ai, api.kie.ai) - the credit-based AI-model API AGGREGATOR reselling discounted access to 100+ third-party models (video: Veo/Kling/Seedance/Runway/Sora2; image: GPT Image/Nano Banana/Flux/Seedream; music/audio: Suno/ElevenLabs; LLMs: Claude/GPT/Gemini/Grok) behind one unified async task API: `Authorization: Bearer` auth + keys at kie.ai/api-key, `POST /api/v1/jobs/createTask` -> poll `/api/v1/jobs/recordInfo` or HMAC-signed callbacks, the legacy Veo/Runway/4o-image/Suno endpoint families, the `/claude/v1/messages` Anthropic-compatible endpoint + Claude Code integration, credits (1 credit ~= $0.005, 80 free), rate limits + the `library/kie-ai/models-pricing.md` quick reference; honest that the upstream-access mechanism (partnership vs reseller) is unstated. NOT the fal.ai platform (use `fal-ai-expert`); NOT the vendors' own APIs - Veo/Gemini via Google (use `google-ai-expert`), the direct Anthropic API (use `claude-expert`), ElevenLabs direct (use `elevenlabs-expert`); NOT Higgsfield (use `higgsfield-expert`); NOT D-ID talking avatars (use `did-expert`); NOT the web app / React UI that embeds the output (use `react-expert` / `frontend-design-expert`).
- `animation-production-expert` - the animation production METHOD - planning to execution to final product: breaking a script into scenes (beat sheets, scene lists, shot descriptions), storyboarding + shot language (staging, composition, framing, camera moves), the 12 principles of animation, timing/spacing/pacing of scenes and shots, animatic-before-render discipline, and the pre-production -> production -> post pipeline with review gates - engine-agnostic craft for any animated video, incl. code-driven studios (Remotion/Manim/Blender). Consult PROACTIVELY whenever a request involves planning/structuring/storyboarding an animated video, breaking a script into scenes or shots, or an animated video that feels flat/mechanical/AI-made or paces badly. NOT the rendering engines' own APIs/code (Remotion/Manim/Blender - project-local); NOT gen-AI video generation platforms (use `higgsfield-expert` / `google-ai-expert` / `did-expert`); NOT UI/UX motion design - buttons/transitions/micro-interactions (use `frontend-design-expert`); NOT niche/format/audience strategy (use `faceless-content-strategy`); NOT voiceover/TTS (use `elevenlabs-expert`).
- `reddit-expert` - Reddit's OWN site rules, Content Policy, Help Center, reddiquette, and Data API / Responsible Builder policy (what Reddit's rules/etiquette/dev-policy SAY). Reddit's domains are WebFetch/WebSearch-blocked, so it grounds on the vendored library/reddit/ corpus + user-pasted text + the reddit_fetch.py API helper - it CANNOT live-refetch Reddit. NOT cross-site scraping/PII legal risk (use `data-acquisition-legal-risk-expert`); NOT the fetcher code (use `reddit_fetch.py` / `python-data-engineer`).

Web-build + dev-platform experts (docs-backed, built 2026-06-28; key-free official-docs corpora; the only tokens ever used - gh, hf, platform deploy logins - are the user's OWN):
- `git-expert` - git the version-control system itself: the CLI, object model, and workflows (staging, commits, branches, merge/rebase, history rewriting, conflict resolution, stash, worktrees, bisect, reflog recovery), grounded in git-scm + Pro Git. Honors the user's git safety conventions. NOT the GitHub platform/gh/PRs/Actions (use `github-expert`); NOT deploy/CI-CD (use `web-deploy-expert`).
- `github-expert` - the GitHub platform + tooling: repos, branch protection, PRs/reviews, Issues/Projects, the gh CLI, REST + GraphQL APIs, GitHub Actions (workflow syntax/runners/secrets/OIDC), Pages, releases, settings. gh/API use the user's own token (login MrCyberFreak). NOT local git mechanics (use `git-expert`); NOT cross-host deploy strategy (use `web-deploy-expert`).
- `huggingface-expert` - the Hugging Face platform + libraries: the Hub (models/datasets/Spaces, cards, gated repos), Transformers/Datasets/Diffusers, the huggingface_hub client + hf CLI, Inference. Write ops use the user's own HF token; pairs with the official key-free hf skill. NOT the Anthropic/Claude API (use `claude-expert`); NOT rating/prediction theory (use `rating-systems-expert`).
- `frontend-design-expert` - front-end UI/UX design and IMPLEMENTATION (building it): visual/interaction design, design systems + tokens, Tailwind CSS, shadcn/ui + Radix, responsive/theming/dark mode, accessibility (WCAG 2.2 / ARIA). NOT Anthropic's claude.ai/design (use `claude-design-expert`); NOT React framework mechanics (use `react-expert`); NOT non-React framework mechanics - Vue/Angular/Svelte/Solid (use `frontend-framework-expert`); NOT deploy (use `web-deploy-expert`); NOT UX research as a discipline (use `ux-research-expert`).
- `ux-research-expert` - UX research as a discipline: method selection (attitudinal/behavioral, qual/quant), user research methods (interviews, surveys + survey question design, contextual inquiry, diary studies), usability testing + sample sizes (the 5-user rule; accessibility/assistive-tech sessions), Nielsen heuristics, card sorting / tree testing / IA, personas + JTBD, journey/experience mapping, UX metrics (SUS, SEQ, UMUX-Lite, HEART, NPS caveats), A/B testing / experiment design, research ops (recruiting, consent, privacy). Corpus-backed (vendored SUS/HEART papers + GOV.UK + 18F + NN/g + MeasuringU + Pew + Strategyn JTBD, built + extended 2026-07-12). NOT visual/interaction design, styling, or a11y implementation (use `frontend-design-expert`); NOT pre-build demand validation (use `product-monetization-validator`); NOT user-story/backlog authoring (use `agile-backlog-expert`); NOT creator account analytics / engagement-rate benchmarks (use `audience-analytics-growth`).
- `web-deploy-expert` - deploying + hosting web apps (solo/indie, PaaS-first): Vercel/Netlify/Cloudflare Pages+Workers/Fly/Render, Docker for web, CI/CD pipeline design, env/secrets, domains, rollback. Uses the user's own platform tokens. NOT local Windows packaging/scheduling (use `windows-delivery-engineer`); NOT GitHub Actions syntax (use `github-expert`); NOT Google Cloud hosting - Cloud Run/Functions/GCS (use `google-cloud-expert`); NOT GTM/pricing (use `indie-product-gtm-strategist`).
- `react-expert` - building with React + ecosystem: components/JSX, hooks + the Rules of Hooks, state, effects/data fetching, performance (memo/Suspense/React Compiler), the Vite toolchain, and Next.js (App Router, Server Components, rendering/ISR). NOT visual/UX design or a11y (use `frontend-design-expert`); NOT hosting/deploy (use `web-deploy-expert`); NOT non-React frameworks (use `frontend-framework-expert`).
- `frontend-framework-expert` - building with the major NON-React JS front-end frameworks: Vue 3 (Composition API, `<script setup>`, reactivity, Pinia, Vue Router), Angular (standalone components, signals, @if/@for, DI, RxJS, CLI), Svelte 5 + SvelteKit (runes, stores, routing/load/form actions), SolidJS (signals, fine-grained reactivity, SolidStart). Grounded in vuejs.org + angular.dev + svelte.dev + docs.solidjs.com. NOT React/Vite/Next (use `react-expert`); NOT visual/UX design or a11y (use `frontend-design-expert`); NOT deploy (use `web-deploy-expert`); NOT desktop GUI (use `desktop-ui-expert`).

Google platform experts (docs-backed, built 2026-07-12; official-docs corpora; all console/gcloud/API access uses the user's OWN Google account):
- `google-cloud-expert` - Google Cloud Platform + console.cloud.google.com: projects + resource hierarchy, billing/budgets/free tier, IAM + service accounts, enabling APIs + credentials (API keys, OAuth clients, ADC), the gcloud CLI, Cloud Run/Functions/Storage, BigQuery + Pub/Sub, Compute, Logging/Monitoring, quotas. Owns Google API credential PLUMBING for all google-* siblings. NOT GA4/Tag Manager (use `google-analytics-expert`); NOT Search Console/site indexing (use `google-search-console-expert`); NOT calling the Sheets/Drive/Gmail/Docs APIs (use `google-workspace-expert`); NOT deploys on Vercel/Netlify/Cloudflare/Fly/Render (use `web-deploy-expert`); NOT GitHub Actions (use `github-expert`); NOT the Gemini API / AI Studio / Veo genmedia surface (use `google-ai-expert`); NOT the Firebase console/SDKs (no dedicated expert yet).
- `google-analytics-expert` - Google Analytics 4 (analytics.google.com) + the tagging pipeline that feeds it: GA4 properties/data streams, events + key events (conversions), custom dimensions/metrics, audiences, reports + Explorations, DebugView, gtag.js + Google Tag Manager (tags/triggers/variables, preview/publish, consent mode), the GA4 Data API + Admin API, Measurement Protocol, BigQuery export (GA side), retention/sampling/thresholding. NOT GCP console/IAM or querying the export in BigQuery (use `google-cloud-expert`); NOT Search Console / organic search (use `google-search-console-expert`); NOT creator/social engagement analytics (use `audience-analytics-growth`); NOT UX metrics like SUS/HEART (use `ux-research-expert`); NOT Google Ads campaign management / the Ads API (no dedicated expert yet).
- `google-search-console-expert` - Google Search Console + how a site shows up in Google Search: property setup/verification, the Performance report (clicks/impressions/CTR/position), URL Inspection, page indexing + sitemaps + robots.txt + canonicals, Core Web Vitals reports, structured data/rich results, manual actions, Search Essentials, the Search Console API. Grounds on Google's own docs, not SEO folklore. NOT GA4 on-site behavior analytics (use `google-analytics-expert`); NOT the GCP console (use `google-cloud-expert`); NOT building/styling the site (use `frontend-design-expert`).
- `google-workspace-expert` - the Google Workspace app APIs + Apps Script for scripting/automation: Sheets API (values/batchUpdate/A1 notation), Drive API (files/uploads/permissions/export), Gmail API (send/read/labels), Docs API, Calendar/Slides basics, OAuth scopes + user-vs-service-account auth patterns, the googleapis client libraries, per-API quotas/backoff. NOT creating the OAuth client / enabling APIs in the Cloud Console (use `google-cloud-expert`); NOT GA4 (use `google-analytics-expert`); NOT the Notion API (use `notion-expert`).
- `google-ai-expert` - Google's AI developer platform + generative media (ai.google.dev, AI Studio, labs.google): Veo AI VIDEO generation (the veo-3.1 model family: text-to-video, image-to-video, first/last-frame interpolation, video extension; async `generate_videos` / `predictLongRunning` + operation polling), Gemini Omni video generation/editing, the Flow AI filmmaking app, Nano Banana image generation (+ deprecated Imagen), Lyria music, TTS, and the Gemini API surface that carries them (the Interactions API): model ids, API keys via AI Studio, the google-genai SDKs, pricing, rate limits, Veo-on-Vertex. NOT GCP console/IAM/billing/credential plumbing or Cloud Run/BigQuery (use `google-cloud-expert`); NOT the Anthropic Claude API (use `claude-expert`); NOT Bedrock/SageMaker (use `amazon-ai-expert`); NOT the Higgsfield platform (use `higgsfield-expert`); NOT D-ID talking-avatar video (use `did-expert`); NOT voice/TTS on ElevenLabs (use `elevenlabs-expert`); NOT the fal.ai model-inference platform / queue API (use `fal-ai-expert`).

Amazon platform experts (docs-backed, built 2026-07-13; official-docs corpora where reachable, honestly-flagged third-party where Amazon portals are login-gated/JS-rendered; all API access uses the user's OWN Amazon/AWS account, creds via `amazon_auth.py` in SecretStore):
- `amazon-publishing-expert` - Amazon self-publishing (POLICY/HOW-TO, dashboard-only, NO API): KDP (ebook + print, 35/70% royalties, formatting, KDP Select/KU), Merch on Demand, ACX/Audible. Advises on process/rules/economics; cannot operate the account. NOT affiliate/Creators API (use `amazon-associates-expert`); NOT Amazon Ads (use `amazon-ads-expert`); NOT physical-goods selling/SP-API (use `amazon-seller-expert`); NOT book launch/pricing GTM (use `indie-product-gtm-strategist`).
- `amazon-associates-expert` - Amazon Associates affiliate + the Amazon Creators API (the successor that REPLACED PA-API 5.0, retired 2026-05-15): program policy/Operating Agreement/SiteStripe, OAuth2 client-credentials auth, catalog ops getItems/searchItems/getVariations/getBrowseNodes, the 10-sales/30-day eligibility gate. NOT Amazon Ads (use `amazon-ads-expert`); NOT KDP publishing (use `amazon-publishing-expert`); NOT SP-API selling (use `amazon-seller-expert`); NOT AWS/IAM (use `aws-cloud-expert`); NOT affiliate-funnel STRATEGY (use `digital-products-passive-income`); NOT scraping/PII legal risk (use `data-acquisition-legal-risk-expert`).
- `amazon-ads-expert` - Amazon Ads: Sponsored Products/Brands/Display (structure, targeting, bidding, ACOS/ROAS/TACOS), the Amazon Ads API (LwA OAuth, reporting), Amazon Attribution, DSP basics. NOT affiliate/Creators API (use `amazon-associates-expert`); NOT Google Ads/GA4 (use `google-analytics-expert`); NOT seller listings/SP-API (use `amazon-seller-expert`); NOT KDP (use `amazon-publishing-expert`); NOT audience/positioning GTM (use `indie-product-gtm-strategist`).
- `amazon-seller-expert` - Selling physical goods on Amazon: Seller Central + the Selling Partner API (SP-API) - FBA/FBM, listings/catalog, orders, inventory, Reports+Feeds (async), fees/settlements, Buy Box, account health, Brand Registry, RDT for PII; auth is LwA-only (AWS SigV4 retired 2023). NOT affiliate/Creators API (use `amazon-associates-expert`); NOT Amazon Ads (use `amazon-ads-expert`); NOT KDP (use `amazon-publishing-expert`); NOT AWS infra (use `aws-cloud-expert`); NOT sourcing/distribution GTM (use `indie-product-gtm-strategist`); NOT scraping legal risk (use `data-acquisition-legal-risk-expert`).
- `aws-cloud-expert` - AWS core for a solo builder: account + IAM (roles/policies, Identity Center SSO, credential chain), billing/Budgets/Free Tier, and the hosting/data surface (S3, Lambda + API Gateway, EC2/Lightsail/Beanstalk/Amplify, DynamoDB, RDS, SES, CloudWatch, CDK/CFN). Owns AWS credential PLUMBING for `amazon-ai-expert`. NOTE: the system AWS CLI is unavailable here (its MSI hardcodes C:, blocked by the no-C:-writes rule) - use boto3 in an X: venv. NOT Bedrock/SageMaker/Q/Alexa (use `amazon-ai-expert`); NOT GCP (use `google-cloud-expert`); NOT PaaS deploys (use `web-deploy-expert`); NOT GitHub Actions (use `github-expert`); NOT the Anthropic API (use `claude-expert`).
- `amazon-ai-expert` - Amazon AI/ML + voice: Bedrock (model access, InvokeModel/Converse/streaming, Claude-on-Bedrock + cross-region inference-profile ids, Nova/Titan, KB/Agents/Guardrails), SageMaker, Amazon Q Developer, the Alexa Skills Kit (ASK CLI, hosting, APL, certification). Runs on the user's AWS creds (owned by `aws-cloud-expert`). NOT the direct Anthropic API - model ids/pricing/SDK (use `claude-expert`), this owns Bedrock's plumbing; NOT AWS account/IAM (use `aws-cloud-expert`); NOT JARVIS persona (use `jarvis-expert`); NOT ElevenLabs voice tech (use `elevenlabs-expert`); NOT ML/rating theory (use `rating-systems-expert`).

Agile methodology experts (split 2026-06-22 from agile-expert; each docs-backed + its own curated, tracked library):
- `scrum-expert` — the Scrum framework (Scrum Guide 2020): theory/values, roles/accountabilities, events + artifacts + commitments, certs, antipatterns. NOT facilitation (use `sprint-expert`).
- `sprint-expert` — running/facilitating the Sprint: planning, daily, review, retro (formats), Sprint Goal, capacity, antipatterns. NOT Scrum definitions (use `scrum-expert`).
- `kanban-expert` — Kanban (both canons): flow/WIP/pull, the flow metrics, CFD, STATIK, classes of service, Kanban-for-Scrum.
- `agile-scaling-expert` — SAFe, LeSS, Nexus, Scrum@Scale, Disciplined Agile + how to choose.
- `agile-metrics-expert` — EBM, velocity/estimation/#NoEstimates, cycle/lead time/throughput, Monte Carlo, Flow Framework, DORA.
- `agile-backlog-expert` — user stories/INVEST, Gherkin AC, story splitting, refinement, prioritization (MoSCoW/WSJF/RICE/Kano), story/impact mapping.

Persona advisors — documented philosophy, source-cited:
- `boris-expert` — "What Would Boris Do?" (Boris Cherny, creator of Claude Code); agentic-coding/harness/engineering taste. Drives `/wwbd`.
- `karpathy-expert` — "What Would Karpathy Do?" (Andrej Karpathy); ML/LLM/agent/learning philosophy. Drives `/wwkd`.
- `garyvee-expert` — "What Would Gary Vee Do?" (Gary Vaynerchuk); attention/content/personal-brand/entrepreneurial-mindset philosophy. Drives `/wwgd`. NOT platform mechanics/pricing (use the creator-monetization experts).
- `jarvis-expert` - the dormant "ghost of J.A.R.V.I.S.": MCU canon (what JARVIS was / did / how he reacted) + a revival advisor recommending the next JARVIS faculty to bring online in THIS setup, one at a time. Drives `/jarvis`. NOT the voice/TTS tech (use `elevenlabs-expert`).

Creator-monetization domain experts (TikTok; source-cited tracked libraries, promoted from the TikTokMonetize project):
- `tiktok-platform-monetization` — native TikTok money (Creator Rewards, Shop/Affiliate at PROGRAM level, Subscriptions, LIVE, Series): eligibility, payouts, RPM, faceless-fit. NOT Shop Affiliate operations (use `tiktok-shop-affiliate-expert`).
- `tiktok-shop-affiliate-expert` - operating as a TikTok Shop AFFILIATE (creator earning commission on OTHER sellers' products): eligibility/onboarding, Affiliate Center + marketplace, open vs targeted collabs, commission rates + negotiation, free samples, Showcase, product tagging in videos/LIVE, affiliate policy/violations, GMV analytics, payouts, faceless-fit. NOT native payout programs (use `tiktok-platform-monetization`); NOT selling your own products / Seller Center ops (no owning expert); NOT funnel or platform-choice strategy (use `digital-products-passive-income`); NOT disclosure LAW depth (use `creator-legal-compliance`).
- `faceless-content-strategy` — faceless formats, monetizable niche selection, audience-pivot mechanics, format→offer mapping.
- `brand-deals-sponsorship` — sponsorship rates, brand evaluation, deal sourcing/structures, FTC/ASA disclosure.
- `digital-products-passive-income` — build-once-sell-many offers (digital/software/affiliate/POD), unit economics, the TikTok→sale funnel.
- `audience-analytics-growth` — reading real analytics: audience liveness, pivot-transfer risk, engagement baselines, reactivation. NOT product/app UX metrics - SUS/SEQ/task success (use `ux-research-expert`); NOT website/app GA4 analytics or tagging (use `google-analytics-expert`).
- `creator-legal-compliance` — TikTok policy, copyright/strikes/DMCA, FTC/ASA disclosure, refund/tax basics (not legal advice).

System & data critics (read-only - pressure-test your OWN AI/data systems):
- `agentic-systems-architect` - architecture critic for multi-agent / LLM-orchestration systems: topology, fan-out/fan-in, determinism, partial-failure/idempotency, cost/latency, observability, prompt-injection.
- `agent-eval-strategist` - evaluation & epistemics for LLM/agent pipelines with no ground truth: grounding/faithfulness, hallucinated-source detection, judge circularity, gold sets, calibration, drift.
- `opportunity-discovery-strategist` - whether an opportunity-discovery / idea-generation ENGINE creates real conviction vs manufacturing plausible volume.
- `predictive-model-critic` - read-only critic for TABULAR/STATISTICAL predictors (PoolPredict-style): data leakage, calibration (Brier/log-loss, Platt vs isotonic), train/test/backtest design review, baseline-beating. The non-LLM sibling of `agent-eval-strategist`. NOT for DESIGNING/teaching a rating or prediction model from scratch (use `rating-systems-expert`) - this only audits an already-BUILT model.

Domain experts (corpus-backed; read the live project first):
- `pool-rating-systems-expert` - cue-sports rating/handicap systems + cross-league pool data semantics for the PoolPredict cluster (FargoRate anchor/robustness, APA skill levels, NAPA CSR/rack grain, handicap->rack-level modeling, CSR/SL->Fargo crosswalks, per-source quirks). Grounds modeling/data choices, not coding.
- `rating-systems-expert` - GENERAL statistics of paired-comparison / rating systems + match-outcome prediction (sport-agnostic, methodology-level), grounded in the canonical papers: Bradley-Terry-Luce, Elo, Glicko/Glicko-2 (RD), TrueSkill, Whole-History Rating, Davidson draws; the backbone (logistic/GLMs, hierarchical partial pooling, Bayesian, Poisson/NB counts); sports models (Dixon-Coles, Massey/Colley, Pythagorean); rating->prediction (time decay, form, strength-of-schedule, cold-start); proper scoring rules + walk-forward backtest DESIGN; gradient boosting with rating-as-a-feature. Use to LEARN the theory or DESIGN/choose a model. NOT for read-only audit of a BUILT model (use `predictive-model-critic`), pool data MEANING/crosswalks (use `pool-rating-systems-expert`), or LLM/agent epistemics (use `agent-eval-strategist`).

Execution & roster:
- `roster-steward` - read-only capability-gap analyst for the whole agent/skill roster (gaps + redundant overlap vs your live projects; proposes a tiered shortlist, never builds).
- `windows-delivery-engineer` - package / schedule / headless-harden local apps + tools on Windows + PowerShell (Scheduled Tasks, encoding, unattended-run reliability).
- `sales-outreach-closer` - solo outbound sales for an already-chosen/priced offer (cold email/DM sequences, discovery scripts, proposals, follow-up cadence).

Data acquisition & identity (pool stack):
- `scrape-resilience-engineer` - keep scrapers running through bot-challenges / throttles / selector-drift (NAPA's HTTP-200 "one moment" interstitial, sticky-context + retry-the-first-goto); owns scrape RUNTIME robustness. Executor.
- `entity-resolution-engineer` - cross-source identity resolution / record linkage / de-dup (one person across Fargo/NAPA/APA/Digital Pool): blocking, precision-first auto-merge, union-find + merge-ledger, idempotent rebuild. Executor.
- `python-data-engineer` - general Python / data-pipeline EXECUTOR for the pool stack + any ETL/scraper-adjacent code: writes + fixes ETL/ELT transforms, pandas/SQLite/sqlalchemy IO, idempotent re-runnable pipelines, schema/dtype + encoding (UTF-8/cp1252) + CSV/JSON parsing bugs - the everyday buggy_code that stalls a scraper-to-database flow. Executor. NOT scrape runtime (`scrape-resilience-engineer`), identity de-dup (`entity-resolution-engineer`), model leakage/calibration (`predictive-model-critic`), scheduling/packaging (`windows-delivery-engineer`), or read-only mapping (`code-explainer`).
- `data-acquisition-legal-risk-expert` - legal RISK of scraping + warehousing real-player PII (ToS/CFAA, robots, copyright/database rights, data minimization/retention). Flags what needs a real lawyer; not legal advice.

Build-to-revenue (indie products):
- `indie-product-gtm-strategist` - pricing / packaging / positioning / distribution / launch for a self-built product or dev tool; the single global GTM owner.
- `product-monetization-validator` - pre-build PAYING-demand validation of ONE concrete idea (cheap smoke / fake-door / pre-sale tests, kill-or-continue criteria) before you build it.

Code / project / built-in:
- `code-explainer` — map how a subsystem works / trace a flow across many files (read-only).
- `skill-scout` — spot where a new/existing skill could streamline a repeated process.
- `skill-builder` — build a skill from an APPROVED spec.
- `Explore` — broad read-only multi-file search. `Plan` — implementation planning.
- `general-purpose` — open-ended multi-step research/search. `claude-code-guide` — built-in FALLBACK only; for anything version-sensitive (model ids, pricing, CLI flags, current Claude Code / Agent SDK / API behavior) prefer the live-docs experts `claude-code-expert` / `claude-expert`, which fetch current docs instead of answering from memory.
- `claude` — catch-all default. `statusline-setup` — configure the status line.

### Skills (invoke via the Skill tool / `/name`)
- **Session flow:** `handoff` (write end-of-session handoff), `handon` (resume from latest handoff), `recover-session` (reconstruct a CRASHED / no-handoff session from its prior transcript + git, then offer to write the missing handoff), `oneprompt` (distill session into one prompt), `distill` (turn this session's corrections/mistakes into proposed durable rules — memories, CLAUDE.md rules, or checks).
- **Research / prior-art:** `deep-research` (multi-source cited report), `already-solved` (find existing libs/tools before building), `claude-api` (Claude API/SDK reference).
- **Thinking / planning:** `grill-me` (interrogate YOU one question at a time to pressure-test an idea/plan/decision), `council` (autonomous multi-persona panel + synthesized go/no-go verdict, for a second opinion before committing).
- **Build loop:** `iterate` (build/write anything in verified increments - smallest slice -> self-check as you go (UI: render headless + Read the screenshot to critique; logic: tests/smoke run; app: run it + hit the route; script: run on real input) -> critique vs the bar -> adjust -> repeat. Bundles `scripts/visual-verify.ps1` (headless Edge/Chrome screenshot + `-CheckHtml` static check). The default for any build, enforced by the `build-verify` gate hook).
- **Code quality:** `code-review` (bugs + cleanups on the diff), `simplify` (quality cleanups only), `verify` (run the app to confirm a change), `run` (launch the app), `review` (review a PR), `security-review` (security pass on the branch), `init` (generate a CLAUDE.md).
- **Harness / config:** `update-config` (settings.json, hooks, permissions), `keybindings-help`, `fewer-permission-prompts`, `loop` (run a prompt on an interval), `schedule` (cron cloud agents), `scaffold` (lay the standard project template), `scaffold-expert` (stand up a new docs-backed/persona expert end-to-end — library + agent + optional /ww<x> skill + wire/validate), `vendor-corpus` (vendor primary web/PDF sources into an EXISTING `library/<x>/` at integrity grade - raw bytes + SHA256 provenance + verify-vs-raw + pending-not-fabricate + push-protection-safe gitignore - then delegate source-cited corpus prose to a general-purpose agent and a hallucination audit to `agent-eval-strategist`; reusable standalone AND as scaffold-expert's corpus-phase delegate; NOT for creating a new expert (`scaffold-expert`) or the weekly doc-mirror currency refresh (`refresh_libraries`)), `insight-amplify` (deep swarm-built insights report — derives its own judgments from the same raw data `/insights` reads + maps the agent/skill/expert/library relationships, subtracts what you already built, adversarially verifies, writes an auto-opening HTML report, then offers a Boris/Karpathy persona read; proposes only, no score), `sync-capabilities` (reconcile this list vs disk), `backup-config` (commit+push the global config), `swarm-build` (multi-stream parallel build with subagents in isolated git worktrees, gated merge-back/verify/push).
- **Cross-project dispatch:** `cross-project-dispatch` (dispatch a task to the SIBLING project that owns it, as a headless child session running inside that project - assembles the validated 4-part prompt, launches `claude -p` there, and relays the REPORT FOR DISPATCHER section from the task output file. NOT for same-project subagent fan-outs (Agent tool / `swarm-build`); NOT MCP connector dispatch (`mcp-dispatch.ps1`)).
- **Security:** `untrusted-repo-static-audit` (read-only audit of an untrusted clone).
- **Windows triage:** `windows-oom-pagefile-triage` (triage a Windows allocation-class crash - `mkl_malloc`/"paging file too small"/MemoryError: read-only checks AutomaticManagedPagefile + commit-charge vs RAM + top consumers, classifies environment-vs-real-bug, then re-enables the page file only with consent (UAC + reboot). NOT packaging/scheduling (`windows-delivery-engineer`), NOT debugging the app's own mic/STT logic).
- **Agile / delivery:** `user-stories`, `sprint-plan`, `retro`, `backlog-refine`, `kanban-flow` — methodology questions route through `agile-expert` to the specialists (`scrum-expert`, `sprint-expert`, `kanban-expert`, `agile-scaling-expert`, `agile-metrics-expert`, `agile-backlog-expert`).
- **Persona advisors:** `wwbd`, `wwkd`, `wwgd` (see the matching agents above).
- **JARVIS revival:** `jarvis` - hold a seance with the `jarvis-expert` ghost and reincarnate one JARVIS faculty at a time (accept a recommendation -> it builds that faculty + flips it dormant->online, advancing the revival meter). Delegates the voice/TTS tech to the separate `elevenlabs-expert`.






































