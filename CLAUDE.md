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
- `claude-expert` — Claude/Anthropic API & models: model ids, pricing, context windows, Messages API, tool use, prompt caching, batches, SDKs.
- `claude-design-expert` — Claude Design (claude.ai/design): canvas, prototypes, presentations, exports, `/design-sync`.
- `grok-expert` — xAI Grok models & API (docs.x.ai).
- `grok-build-expert` — Grok Build (xAI terminal coding CLI).
- `notion-expert` — Notion app & API (+ live workspace data via the Notion MCP).
- `mcp-expert` — Model Context Protocol itself: spec, building servers/clients, SDKs.
- `agile-expert` — UMBRELLA Agile: Manifesto/12 principles, mindset, Lean, XP, framework-selection; ROUTES framework-deep questions to the specialists below.
- `obsidian-expert` - Obsidian app, plugins, themes, vault, Plugin/Dev API (active; kept + documented).

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

Creator-monetization domain experts (TikTok; source-cited tracked libraries, promoted from the TikTokMonetize project):
- `tiktok-platform-monetization` — native TikTok money (Creator Rewards, Shop/Affiliate, Subscriptions, LIVE, Series): eligibility, payouts, RPM, faceless-fit.
- `faceless-content-strategy` — faceless formats, monetizable niche selection, audience-pivot mechanics, format→offer mapping.
- `brand-deals-sponsorship` — sponsorship rates, brand evaluation, deal sourcing/structures, FTC/ASA disclosure.
- `digital-products-passive-income` — build-once-sell-many offers (digital/software/affiliate/POD), unit economics, the TikTok→sale funnel.
- `audience-analytics-growth` — reading real analytics: audience liveness, pivot-transfer risk, engagement baselines, reactivation.
- `creator-legal-compliance` — TikTok policy, copyright/strikes/DMCA, FTC/ASA disclosure, refund/tax basics (not legal advice).

System & data critics (read-only - pressure-test your OWN AI/data systems):
- `agentic-systems-architect` - architecture critic for multi-agent / LLM-orchestration systems: topology, fan-out/fan-in, determinism, partial-failure/idempotency, cost/latency, observability, prompt-injection.
- `agent-eval-strategist` - evaluation & epistemics for LLM/agent pipelines with no ground truth: grounding/faithfulness, hallucinated-source detection, judge circularity, gold sets, calibration, drift.
- `opportunity-discovery-strategist` - whether an opportunity-discovery / idea-generation ENGINE creates real conviction vs manufacturing plausible volume.
- `predictive-model-critic` - read-only critic for TABULAR/STATISTICAL predictors (PoolPredict-style): data leakage, calibration (Brier/log-loss, Platt vs isotonic), train/test/backtest design, baseline-beating. The non-LLM sibling of `agent-eval-strategist`.

Domain experts (corpus-backed; read the live project first):
- `pool-rating-systems-expert` - cue-sports rating/handicap systems + cross-league pool data semantics for the PoolPredict cluster (FargoRate anchor/robustness, APA skill levels, NAPA CSR/rack grain, handicap->rack-level modeling, CSR/SL->Fargo crosswalks, per-source quirks). Grounds modeling/data choices, not coding.

Execution & roster:
- `roster-steward` - read-only capability-gap analyst for the whole agent/skill roster (gaps + redundant overlap vs your live projects; proposes a tiered shortlist, never builds).
- `windows-delivery-engineer` - package / schedule / headless-harden local apps + tools on Windows + PowerShell (Scheduled Tasks, encoding, unattended-run reliability).
- `sales-outreach-closer` - solo outbound sales for an already-chosen/priced offer (cold email/DM sequences, discovery scripts, proposals, follow-up cadence).

Data acquisition & identity (pool stack):
- `scrape-resilience-engineer` - keep scrapers running through bot-challenges / throttles / selector-drift (NAPA's HTTP-200 "one moment" interstitial, sticky-context + retry-the-first-goto); owns scrape RUNTIME robustness. Executor.
- `entity-resolution-engineer` - cross-source identity resolution / record linkage / de-dup (one person across Fargo/NAPA/APA/Digital Pool): blocking, precision-first auto-merge, union-find + merge-ledger, idempotent rebuild. Executor.
- `data-acquisition-legal-risk-expert` - legal RISK of scraping + warehousing real-player PII (ToS/CFAA, robots, copyright/database rights, data minimization/retention). Flags what needs a real lawyer; not legal advice.

Build-to-revenue (indie products):
- `indie-product-gtm-strategist` - pricing / packaging / positioning / distribution / launch for a self-built product or dev tool; the single global GTM owner.
- `product-monetization-validator` - pre-build demand validation of ONE concrete idea (cheap smoke / fake-door / pre-sale tests, kill-or-continue criteria) before you build it.

Code / project / built-in:
- `code-explainer` — map how a subsystem works / trace a flow across many files (read-only).
- `skill-scout` — spot where a new/existing skill could streamline a repeated process.
- `skill-builder` — build a skill from an APPROVED spec.
- `Explore` — broad read-only multi-file search. `Plan` — implementation planning.
- `general-purpose` — open-ended multi-step research/search. `claude-code-guide` — Q&A on Claude Code / Agent SDK / Claude API.
- `claude` — catch-all default. `statusline-setup` — configure the status line.

### Skills (invoke via the Skill tool / `/name`)
- **Session flow:** `handoff` (write end-of-session handoff), `handon` (resume from latest handoff), `oneprompt` (distill session into one prompt), `distill` (turn this session's corrections/mistakes into proposed durable rules — memories, CLAUDE.md rules, or checks).
- **Research / prior-art:** `deep-research` (multi-source cited report), `already-solved` (find existing libs/tools before building), `claude-api` (Claude API/SDK reference).
- **Thinking / planning:** `grill-me` (interrogate YOU one question at a time to pressure-test an idea/plan/decision), `council` (autonomous multi-persona panel + synthesized go/no-go verdict, for a second opinion before committing).
- **Code quality:** `code-review` (bugs + cleanups on the diff), `simplify` (quality cleanups only), `verify` (run the app to confirm a change), `run` (launch the app), `review` (review a PR), `security-review` (security pass on the branch), `init` (generate a CLAUDE.md).
- **Harness / config:** `update-config` (settings.json, hooks, permissions), `keybindings-help`, `fewer-permission-prompts`, `loop` (run a prompt on an interval), `schedule` (cron cloud agents), `scaffold` (lay the standard project template), `scaffold-expert` (stand up a new docs-backed/persona expert end-to-end — library + agent + optional /ww<x> skill + wire/validate), `sync-capabilities` (reconcile this list vs disk), `backup-config` (commit+push the global config).
- **Security:** `untrusted-repo-static-audit` (read-only audit of an untrusted clone).
- **Agile / delivery:** `user-stories`, `sprint-plan`, `retro`, `backlog-refine`, `kanban-flow` — methodology questions route through `agile-expert` to the specialists (`scrum-expert`, `sprint-expert`, `kanban-expert`, `agile-scaling-expert`, `agile-metrics-expert`, `agile-backlog-expert`).
- **Persona advisors:** `wwbd`, `wwkd`, `wwgd` (see the matching agents above).







